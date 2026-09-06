"""
RHC Marketing Assistant - Main Application
"""
import json
import hmac
import hashlib
import base64
import re
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
import os
import sys
import threading
import uvicorn
from datetime import datetime, timezone, timedelta

# Optional imports
try:
    from app.feishu_client import feishu
except ImportError:
    feishu = None

try:
    from app.models import (
        CopyRequest, CopyResponse,
        ComposeRequest, ComposeResponse,
        ProductUpsertRequest
    )
except ImportError:
    class CopyRequest(BaseModel):
        product_id: str = ""
        target_language: str = "en"
        tone: str = "professional"
        product_model: str = ""
        platform: str = ""
        language: str = ""
        extra_keywords: str = ""

    class CopyResponse(BaseModel):
        title: str = ""
        body: str = ""
        hashtags: List[str] = []

    class ComposeRequest(BaseModel):
        animal: str = ""
        product_id: str = ""
        style: str = "professional"
        text: str = ""
        mode: str = ""
        prompt: str = ""
        ai_background: bool = False
        ai_prompt: str = ""
        ai_style: str = ""

    class ComposeResponse(BaseModel):
        composed_image_url: str = ""
        copy: Dict[str, Any] = {}
        animal_image_url: str = ""

    class ProductUpsertRequest(BaseModel):
        product_model: str = ""
        product_name: str = ""
        category: str = ""
        main_selling_point: str = ""
        product_image_url: str = ""
        price_tier: str = ""
        status: str = "active"

try:
    from app.llm import generate_copy
except ImportError:
    def generate_copy(product_id: str, target_language: str = "en", tone: str = "professional"):
        return {"title": "Coming Soon", "body": "LLM module not deployed", "hashtags": []}

try:
    from app.config import settings
except ImportError:
    class Settings:
        coze_pat: str = os.getenv("COZE_PAT", "")
        coze_workflow_id: str = os.getenv("COZE_WORKFLOW_ID", "")
        openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
        openai_text_model: str = os.getenv("OPENAI_TEXT_MODEL", "deepseek-chat")
    settings = Settings()

app = FastAPI(title="RHC Marketing Assistant", version="1.0.0")

# ============================================================
# 认证系统 (Auth)
# ============================================================
SECRET_KEY = os.getenv("RHC_SECRET_KEY", "rhc-marketing-secret-2026")
TOKEN_EXPIRY = 24 * 60 * 60  # 24 hours

# 兜底账号：飞书多维表格不可用（网络/凭证/限流）时使用，保证系统不会被锁死。
# 正常账号数据源为飞书「系统账号」表（见下方 _load_users 相关逻辑）。
USERS = {
    "ella": {"password": "rhc2026", "role": "admin", "name": "Ella"},
}

def _create_token(username: str) -> str:
    """Create a simple signed token: base64(json(payload)).signature"""
    payload = {
        "user": username,
        "exp": int(time.time()) + TOKEN_EXPIRY,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"

def _verify_token(token: str) -> Optional[dict]:
    """Verify token and return user info, or None if invalid/expired."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        expected_sig = hmac.new(SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        username = payload.get("user")
        if not username:
            return None
        # 角色/姓名从当前用户源（飞书表优先，失败回退 USERS）取，
        # 以便在飞书表中修改角色后，已签发的 token 也能拿到最新角色。
        users = _get_users()
        u = users.get(username)
        if u:
            return {"username": username, "role": u["role"], "name": u["name"]}
        return None
    except Exception:
        return None

def _get_token_from_request(request: Request) -> Optional[str]:
    """Extract token from Authorization header or cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    # Check cookie
    cookie_token = request.cookies.get("rhc_auth_token")
    if cookie_token:
        return cookie_token
    return None

class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""

@app.post("/api/auth/login")
async def api_auth_login(req: LoginRequest):
    username = req.username.strip()
    password = req.password.strip()
    if not username or not password:
        return JSONResponse({"ok": False, "message": "请输入用户名和密码"})
    # 账号来自飞书「系统账号」表（60秒内存缓存）；飞书故障时回退兜底 USERS
    users = _get_users()
    user = users.get(username)
    if (not user or user["password"] != password):
        # 登录失败时强制刷新一次账号缓存再判（覆盖「刚在飞书表里新增账号/改密码」
        # 但缓存尚未过期的场景）；仍失败则返回错误
        users = _get_users(force_refresh=True)
        user = users.get(username)
    if not user or user["password"] != password:
        return JSONResponse({"ok": False, "message": "用户名或密码错误"})
    if not user.get("enabled", True):
        return JSONResponse({"ok": False, "message": "该账号已停用，请联系管理员"})
    token = _create_token(username)
    resp = JSONResponse({
        "ok": True,
        "token": token,
        "user": {"username": username, "role": user["role"], "name": user["name"]},
    })
    # Also set cookie for convenience
    resp.set_cookie(
        key="rhc_auth_token",
        value=token,
        max_age=TOKEN_EXPIRY,
        httponly=False,
        samesite="lax",
    )
    return resp

@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    token = _get_token_from_request(request)
    if not token:
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
    user_info = _verify_token(token)
    if not user_info:
        return JSONResponse({"ok": False, "message": "登录已过期"}, status_code=401)
    return {"ok": True, "user": user_info}

@app.post("/api/auth/logout")
async def api_auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("rhc_auth_token")
    return resp


@app.middleware("http")
async def no_cache_middleware(request, call_next):
    # 框架快速迭代期：HTML页面与数据快照禁用浏览器缓存，避免用户看到旧版
    resp = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path == "/" or path.endswith("snapshot.json"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/health")
async def api_health():
    return {"status": "ok"}

@app.post("/api/copy/generate", response_model=CopyResponse)
async def api_copy_generate(req: CopyRequest):
    try:
        result = generate_copy(
            product_id=req.product_id,
            target_language=req.target_language,
            tone=req.tone,
            product_model=req.product_model,
            platform=req.platform,
            language=req.language,
            extra_keywords=req.extra_keywords
        )
        return CopyResponse(
            title=result.get("title", ""),
            body=result.get("body", ""),
            hashtags=result.get("hashtags", [])
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/compose")
async def api_compose(req: ComposeRequest):
    try:
        # Handle mode-based routing
        mode = getattr(req, 'mode', '') or ''
        if mode == 'animal_cutout':
            from app.composer import generate_animal_cutout
            prompt = getattr(req, 'prompt', '') or ''
            result = generate_animal_cutout(prompt=prompt)
            if result.get("status") == "failed" or not result.get("image_url"):
                raise HTTPException(status_code=502, detail=result.get("error", "") or "动物素材生成失败，请稍后重试")
            return {"image_url": result.get("image_url", ""), "status": result.get("status", "success")}
        elif mode == 'background' or getattr(req, 'ai_background', False):
            from app.composer import generate_ai_background
            ai_prompt = getattr(req, 'ai_prompt', '') or getattr(req, 'text', '') or ''
            ai_style = getattr(req, 'ai_style', '') or getattr(req, 'style', 'professional') or 'professional'
            result = generate_ai_background(prompt=ai_prompt, style=ai_style)
            return result
        else:
            from app.composer import compose_image
            result = compose_image(
                animal=req.animal,
                text=req.text,
                style=req.style
            )
            return ComposeResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ai-background")
async def api_ai_background(req: ComposeRequest):
    try:
        from app.composer import generate_ai_background
        result = generate_ai_background(
            prompt=req.text,
            style=req.style
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/animal-image")
async def api_animal_image(animal: str):
    try:
        from app.composer import search_animal_image
        url = search_animal_image(animal)
        return {"animal": animal, "image_url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/animals")
async def api_animals_list():
    return {"items": ["cat", "dog", "rabbit", "horse", "cow", "sheep", "goat", "pig"]}

FEISHU_AID = os.getenv("FEISHU_APP_ID", "")
FEISHU_ASE = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_ATK = os.getenv("FEISHU_APP_TOKEN", "")
FEISHU_TID = os.getenv("FEISHU_TABLE_ID", "")

def _feishu_token():
    import urllib.request as _ur
    tr = _ur.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": FEISHU_AID, "app_secret": FEISHU_ASE}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _ur.urlopen(tr, timeout=10) as r:
        return json.loads(r.read()).get("tenant_access_token")

def _feishu_headers():
    return {"Authorization": f"Bearer {_feishu_token()}", "Content-Type": "application/json"}

def _tv(v):
    if v is None: return ""
    if isinstance(v, list): return ", ".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
    if isinstance(v, dict): return v.get("text", str(v))
    return str(v)

# ============================================================
# 系统账号表（飞书多维表格数据源，替代硬编码 USERS）
# 用户可直接在飞书表「系统账号」中增删账号；表结构不存在时自动建表+种子数据。
# TODO: 演示阶段密码明文存储，正式版需改为哈希存储（如 bcrypt）。
# ============================================================
ACCOUNT_TABLE_NAME = "系统账号"
_ACCOUNT_CACHE_TTL = 60  # 账号列表内存缓存秒数，避免每次登录都调飞书 API

_account_table_id = None
_users_cache = {"data": None, "ts": 0.0}

def _feishu_api(method, path, payload=None, timeout=15):
    """统一的飞书 API 请求（沿用项目现有 urllib 风格，不引入新依赖）。"""
    import urllib.request as _ur
    import urllib.error as _ue
    url = f"https://open.feishu.cn/open-apis{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    rq = _ur.Request(url, data=data, headers=_feishu_headers(), method=method)
    try:
        with _ur.urlopen(rq, timeout=timeout) as r:
            return json.loads(r.read())
    except _ue.HTTPError as e:
        # 读取错误响应体，便于日志定位（凭证失效/限流/参数错误等）
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        raise RuntimeError(f"feishu api {method} {path} -> HTTP {e.code}: {detail}")

def _ensure_account_table():
    """确保多维表中存在「系统账号」表，返回 table_id；不存在则自动创建。"""
    global _account_table_id
    if _account_table_id:
        return _account_table_id
    if not FEISHU_ATK:
        raise RuntimeError("FEISHU_APP_TOKEN 未配置")
    # 1) 列出多维表下所有数据表，按名字查找
    resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables?page_size=100")
    for t in resp.get("data", {}).get("items", []):
        if t.get("name") == ACCOUNT_TABLE_NAME:
            _account_table_id = t.get("table_id")
            return _account_table_id
    # 2) 不存在则创建（主字段为第一个 field：用户名，文本类型）
    fields = [
        {"field_name": "用户名", "type": 1},  # 文本（主字段）
        {"field_name": "密码", "type": 1},    # 文本；TODO: 正式版改为加密存储
        {"field_name": "姓名", "type": 1},    # 文本
        {"field_name": "角色", "type": 3,     # 单选
         "property": {"options": [
             {"name": "admin"}, {"name": "sales"}, {"name": "viewer"}]}},
        {"field_name": "启用", "type": 3,     # 单选：是/否
         "property": {"options": [{"name": "是"}, {"name": "否"}]}},
    ]
    resp = _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables",
                       {"table": {"name": ACCOUNT_TABLE_NAME,
                                  "default_view_name": "账号列表",
                                  "fields": fields}})
    _account_table_id = resp.get("data", {}).get("table_id")
    if not _account_table_id:
        raise RuntimeError(f"创建「{ACCOUNT_TABLE_NAME}」表失败: {resp}")
    print(f"[auth] 已创建飞书账号表「{ACCOUNT_TABLE_NAME}」: {_account_table_id}")
    return _account_table_id

def _seed_accounts_if_empty(tid):
    """表为空时写入种子账号（与兜底 USERS 一致：ella / rhc2026 / Ella / admin / 启用）。"""
    resp = _feishu_api(
        "GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records?page_size=1")
    if resp.get("data", {}).get("total", 0) > 0 or resp.get("data", {}).get("items"):
        return
    fields = {"用户名": "ella", "密码": "rhc2026", "姓名": "Ella",
              "角色": "admin", "启用": "是"}
    _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records",
                {"fields": fields})
    print("[auth] 账号表为空，已写入种子账号 ella/admin")

def _fetch_users_from_feishu():
    """从飞书「系统账号」表读取全部账号，返回 {username: {password,role,name,enabled}}。"""
    tid = _ensure_account_table()
    _seed_accounts_if_empty(tid)
    users = {}
    page_token = None
    while True:
        path = f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records?page_size=100"
        if page_token:
            path += f"&page_token={page_token}"
        resp = _feishu_api("GET", path)
        data = resp.get("data", {})
        for it in data.get("items", []):
            fl = it.get("fields", {})
            username = _tv(fl.get("用户名")).strip()
            if not username:
                continue
            # 单选字段读取值可能是 {"text": "admin"} 结构，统一用 _tv 归一
            role = _tv(fl.get("角色")).strip() or "admin"
            if role not in ("admin", "sales", "viewer"):
                role = "admin"
            enabled = _tv(fl.get("启用")).strip()
            users[username] = {
                "password": _tv(fl.get("密码")),
                "role": role,
                "name": _tv(fl.get("姓名")) or username,
                # 单选「启用」未填写时默认视为启用；仅明确为「否」才停用
                "enabled": enabled != "否",
                # 飞书记录 ID，账号管理改/删使用；内部字段，不参与登录比对
                "_record_id": it.get("record_id", ""),
            }
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return users

def _get_users(force_refresh=False):
    """获取账号字典：优先飞书表（60秒缓存），飞书失败时回退硬编码 USERS。
    force_refresh=True 时跳过缓存强制拉取（登录失败重试场景使用）。"""
    now = time.time()
    if not force_refresh and _users_cache["data"] is not None \
            and now - _users_cache["ts"] < _ACCOUNT_CACHE_TTL:
        return _users_cache["data"]
    try:
        users = _fetch_users_from_feishu()
        _users_cache["data"] = users
        _users_cache["ts"] = now
        return users
    except Exception as e:
        # 兜底：飞书故障（网络/凭证/限流）时回退硬编码账号，保证系统不被锁死
        print(f"[auth] 警告: 读取飞书账号表失败，回退到内置兜底账号: {e}")
        if _users_cache["data"] is not None:
            # 有旧缓存则沿用旧数据（可能略有延迟，但不影响登录可用性）
            return _users_cache["data"]
        return USERS

def _warmup_account_table():
    """启动后台预热：尽早建表/写种子，失败不影响服务启动（登录时仍会自动重试/回退）。"""
    try:
        _get_users(force_refresh=True)
        if _users_cache["data"] is not None:
            print("[auth] 飞书账号表初始化完成")
        else:
            print("[auth] 飞书账号表暂不可用，当前使用内置兜底账号（登录时会自动重试）")
    except Exception as e:
        print(f"[auth] 飞书账号表初始化失败（登录时将自动重试/回退兜底账号）: {e}")

def _invalidate_users_cache():
    """写操作成功后调用：立即失效账号缓存并强制刷新，保证改完马上生效。
    刷新失败则置空缓存（下次读取会重新拉取；拉取失败仍回退兜底 USERS）。"""
    _users_cache["data"] = None
    _users_cache["ts"] = 0.0
    try:
        _get_users(force_refresh=True)
    except Exception as e:
        print(f"[admin] 账号缓存刷新失败，下次读取将重试: {e}")

def _find_user_by_record_id(record_id):
    """按飞书 record_id 找到对应账号：返回 (username, user_dict) 或 (None, None)。
    管理写操作专用：强制拉取最新数据，避免 60 秒缓存内拿到旧记录。"""
    users = _get_users(force_refresh=True)
    for uname, u in users.items():
        if u.get("_record_id") == record_id:
            return uname, u
    return None, None

def _count_active_admins(users):
    """统计启用中的 admin 数量（用于「最后一个管理员」防呆）。"""
    return sum(1 for u in users.values()
               if u.get("role") == "admin" and u.get("enabled", True))

# ============================================================
# 账号管理 API（配置中心「账号管理」页）
# 所有接口需登录；写操作（增/改/删）仅限 admin 角色。
# 数据源：飞书多维表「系统账号」表；写操作成功后立即失效账号缓存。
# ============================================================
class AdminAccountCreate(BaseModel):
    username: str = ""
    password: str = ""
    name: str = ""
    role: str = "viewer"

class AdminAccountUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = None
    enabled: Optional[bool] = None

@app.get("/api/admin/accounts")
async def api_admin_accounts_list(request: Request):
    token = _get_token_from_request(request)
    if not token or not _verify_token(token):
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        users = _get_users(force_refresh=True)
    except Exception as e:
        print(f"[admin] 读取账号列表失败: {e}")
        return JSONResponse({"ok": False, "message": f"读取账号列表失败：{e}"}, status_code=502)
    items = []
    for uname, u in users.items():
        items.append({
            "username": uname,
            "name": u.get("name", uname),
            "role": u.get("role", "viewer"),
            "enabled": u.get("enabled", True),
            "record_id": u.get("_record_id", ""),
        })
    return {"ok": True, "items": items, "total": len(items)}

@app.post("/api/admin/accounts")
async def api_admin_account_create(req: AdminAccountCreate, request: Request):
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    if user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "仅管理员可新增账号"}, status_code=403)

    username = (req.username or "").strip()
    password = req.password or ""
    name = (req.name or "").strip() or username
    role = (req.role or "").strip()
    if not username:
        return JSONResponse({"ok": False, "message": "用户名不能为空"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"ok": False, "message": "密码至少 6 位"}, status_code=400)
    if role not in ("admin", "sales", "viewer"):
        return JSONResponse({"ok": False, "message": "角色仅支持 admin / sales / viewer"}, status_code=400)

    try:
        users = _get_users(force_refresh=True)
        if username in users:
            return JSONResponse({"ok": False, "message": f"用户名「{username}」已存在，请更换"}, status_code=400)
        tid = _ensure_account_table()
        fields = {"用户名": username, "密码": password, "姓名": name,
                  "角色": role, "启用": "是"}
        resp = _feishu_api(
            "POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records",
            {"fields": fields})
        rec = resp.get("data", {}).get("record", {})
        _invalidate_users_cache()
        return {"ok": True, "message": "账号已创建",
                "record_id": rec.get("record_id", ""),
                "account": {"username": username, "name": name,
                            "role": role, "enabled": True,
                            "record_id": rec.get("record_id", "")}}
    except Exception as e:
        print(f"[admin] 新增账号失败（{username}）: {e}")
        return JSONResponse({"ok": False, "message": f"新增账号失败：{e}"}, status_code=502)

@app.put("/api/admin/accounts/{record_id}")
async def api_admin_account_update(record_id: str, req: AdminAccountUpdate, request: Request):
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    if user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "仅管理员可修改账号"}, status_code=403)

    try:
        target_uname, target_user = _find_user_by_record_id(record_id)
        if not target_user:
            return JSONResponse({"ok": False, "message": "账号不存在或已被删除"}, status_code=404)

        fields = {}
        if req.name is not None:
            name = req.name.strip() or target_uname
            fields["姓名"] = name
        if req.role is not None:
            role = req.role.strip()
            if role not in ("admin", "sales", "viewer"):
                return JSONResponse({"ok": False, "message": "角色仅支持 admin / sales / viewer"}, status_code=400)
            fields["角色"] = role
        if req.password is not None and req.password != "":
            # 空字符串/不传 = 不修改密码
            if len(req.password) < 6:
                return JSONResponse({"ok": False, "message": "密码至少 6 位"}, status_code=400)
            fields["密码"] = req.password
        if req.enabled is not None:
            fields["启用"] = "是" if req.enabled else "否"

        # ---- 防呆规则 ----
        is_self = (target_uname == user_info.get("username"))
        if is_self and fields.get("启用") == "否":
            return JSONResponse({"ok": False, "message": "不能停用当前登录账号"}, status_code=400)
        if is_self and "角色" in fields:
            return JSONResponse({"ok": False, "message": "不能修改当前登录账号的角色（如需变更请由其他管理员操作）"}, status_code=400)
        # 预演变更后的状态：不能让系统失去最后一个启用中的 admin
        new_role = fields.get("角色", target_user.get("role"))
        new_enabled = fields.get("启用")
        new_enabled = True if new_enabled == "是" else (False if new_enabled == "否" else target_user.get("enabled", True))
        if new_role != "admin" or not new_enabled:
            # 取最新全量账号模拟变更后统计
            users_now = _get_users(force_refresh=True)
            remain = 0
            for uname, u in users_now.items():
                r = new_role if uname == target_uname else u.get("role")
                en = new_enabled if uname == target_uname else u.get("enabled", True)
                if r == "admin" and en:
                    remain += 1
            if remain < 1:
                return JSONResponse({"ok": False, "message": "系统至少需保留一个启用中的管理员账号"}, status_code=400)

        if not fields:
            return {"ok": True, "message": "无需要修改的内容"}
        _feishu_api(
            "PUT",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{_account_table_id}/records/{record_id}",
            {"fields": fields})
        _invalidate_users_cache()
        return {"ok": True, "message": "账号已更新"}
    except Exception as e:
        print(f"[admin] 修改账号失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"修改账号失败：{e}"}, status_code=502)

@app.delete("/api/admin/accounts/{record_id}")
async def api_admin_account_delete(record_id: str, request: Request):
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    if user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "仅管理员可删除账号"}, status_code=403)

    try:
        target_uname, target_user = _find_user_by_record_id(record_id)
        if not target_user:
            return JSONResponse({"ok": False, "message": "账号不存在或已被删除"}, status_code=404)
        if target_uname == user_info.get("username"):
            return JSONResponse({"ok": False, "message": "不能删除当前登录账号"}, status_code=400)
        # 不能删除最后一个启用中的 admin
        if target_user.get("role") == "admin" and target_user.get("enabled", True) \
                and _count_active_admins(_get_users(force_refresh=True)) <= 1:
            return JSONResponse({"ok": False, "message": "系统至少需保留一个启用中的管理员账号"}, status_code=400)
        _feishu_api(
            "DELETE",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{_account_table_id}/records/{record_id}")
        _invalidate_users_cache()
        return {"ok": True, "message": f"账号「{target_uname}」已删除"}
    except Exception as e:
        print(f"[admin] 删除账号失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"删除账号失败：{e}"}, status_code=502)

# ============================================================
# 商机线索表（飞书多维表格「商机线索」）
# 销售从商机信号认领的线索落库锁定归属，可补录公司/邮箱/备注、
# 转客户或释放；邮件助手直接引用跟进中线索作为收件客户。
# 表结构不存在时自动建表；启动时后台预热（同系统账号表模式）。
# ============================================================
LEADS_TABLE_NAME = "商机线索"
LEADS_FIELD_MAP = {
    "标题": "线索标题",
    "商机类型": "商机类型",
    "公司机构": "公司/机构",
    "摘要": "摘要",
    "来源": "来源",
    "原文链接": "原文链接",
    "地区": "地区",
    "发布日期": "发布日期",
    "认领人": "认领人",
    "认领时间": "认领时间",
    "状态": "状态",
    "联系邮箱": "联系邮箱",
    "跟进备注": "跟进备注",
    "邮箱来源": "邮箱来源",
}
LEAD_ACTIVE_STATUS = ("跟进中", "已转客户")
LEAD_STATUS_OPTIONS = ("跟进中", "已转客户", "已释放")
LEAD_OPP_OPTIONS = ("诊所扩张", "招标采购", "展会机会", "渠道动态", "采购动态")

_leads_table_id = None
_leads_cache = {"data": None, "ts": 0.0}
_LEADS_CACHE_TTL = 30  # 线索列表内存缓存秒数（信号接口 enrichment 使用）


def _ensure_leads_table():
    """确保多维表中存在「商机线索」表，返回 table_id；不存在则自动创建。"""
    global _leads_table_id
    if _leads_table_id:
        return _leads_table_id
    if not FEISHU_ATK:
        raise RuntimeError("FEISHU_APP_TOKEN 未配置")
    resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables?page_size=100")
    for t in resp.get("data", {}).get("items", []):
        if t.get("name") == LEADS_TABLE_NAME:
            _leads_table_id = t.get("table_id")
            # 旧表幂等补字段（如「邮箱来源」），内部吞异常不阻断
            _ensure_leads_fields(_leads_table_id)
            return _leads_table_id
    fields = [
        {"field_name": "线索标题", "type": 1},   # 文本（主字段）
        {"field_name": "商机类型", "type": 3,    # 单选
         "property": {"options": [{"name": n} for n in LEAD_OPP_OPTIONS]}},
        {"field_name": "公司/机构", "type": 1},  # 文本
        {"field_name": "摘要", "type": 1},       # 文本
        {"field_name": "来源", "type": 1},       # 文本
        {"field_name": "原文链接", "type": 1},   # 文本
        {"field_name": "地区", "type": 1},       # 文本
        {"field_name": "发布日期", "type": 1},   # 文本
        {"field_name": "认领人", "type": 1},     # 文本
        {"field_name": "认领时间", "type": 1},   # 文本（ISO 时间）
        {"field_name": "状态", "type": 3,        # 单选
         "property": {"options": [{"name": n} for n in LEAD_STATUS_OPTIONS]}},
        {"field_name": "联系邮箱", "type": 1},   # 文本
        {"field_name": "跟进备注", "type": 1},   # 文本
        {"field_name": "邮箱来源", "type": 1},   # 文本（智能查找采用邮箱时记录来源 URL）
    ]
    resp = _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables",
                       {"table": {"name": LEADS_TABLE_NAME,
                                  "default_view_name": "线索列表",
                                  "fields": fields}})
    _leads_table_id = resp.get("data", {}).get("table_id")
    if not _leads_table_id:
        raise RuntimeError(f"创建「{LEADS_TABLE_NAME}」表失败: {resp}")
    print(f"[leads] 已创建飞书线索表「{LEADS_TABLE_NAME}」: {_leads_table_id}")
    return _leads_table_id


# 线索表全量字段定义（表名 -> 类型/选项）。用于对线上旧表做幂等补字段：
# 线上表由旧版代码建好时可能缺少后加的字段（如「邮箱来源」），预热/写操作时自动补齐。
LEADS_FIELDS_SCHEMA = [
    {"field_name": "线索标题", "type": 1},
    {"field_name": "商机类型", "type": 3,
     "property": {"options": [{"name": n} for n in LEAD_OPP_OPTIONS]}},
    {"field_name": "公司/机构", "type": 1},
    {"field_name": "摘要", "type": 1},
    {"field_name": "来源", "type": 1},
    {"field_name": "原文链接", "type": 1},
    {"field_name": "地区", "type": 1},
    {"field_name": "发布日期", "type": 1},
    {"field_name": "认领人", "type": 1},
    {"field_name": "认领时间", "type": 1},
    {"field_name": "状态", "type": 3,
     "property": {"options": [{"name": n} for n in LEAD_STATUS_OPTIONS]}},
    {"field_name": "联系邮箱", "type": 1},
    {"field_name": "跟进备注", "type": 1},
    {"field_name": "邮箱来源", "type": 1},
]


def _ensure_leads_fields(tid):
    """幂等补齐线索表缺失字段（含单选选项）。线上旧表没有「邮箱来源」等新字段时自动补上。"""
    try:
        resp = _feishu_api(
            "GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/fields?page_size=100")
        existing = {}
        for f in resp.get("data", {}).get("items", []):
            existing[f.get("field_name", "")] = f
        for fdef in LEADS_FIELDS_SCHEMA:
            name = fdef["field_name"]
            cur = existing.get(name)
            if not cur:
                # 主字段（线索标题）是建表时自动生成的，正常不会缺失；缺失时尝试创建
                _feishu_api(
                    "POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/fields",
                    fdef)
                print(f"[leads] 线索表补字段「{name}」")
                continue
            # 单选字段：选项不全则补齐（PUT 更新字段 property）
            if fdef.get("type") == 3 and fdef.get("property", {}).get("options"):
                want = {o["name"] for o in fdef["property"]["options"]}
                have = {o.get("name", "") for o in
                        (cur.get("property") or {}).get("options", [])}
                missing = want - have
                if missing:
                    merged = list((cur.get("property") or {}).get("options", [])) + \
                             [{"name": n} for n in sorted(missing)]
                    _feishu_api(
                        "PUT",
                        f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/fields/{cur.get('field_id')}",
                        {"field_name": name, "type": 3,
                         "property": {"options": merged}})
                    print(f"[leads] 线索表单选字段「{name}」补选项：{sorted(missing)}")
    except Exception as e:
        # 字段补齐失败不阻断主流程（读接口通常不依赖新字段；写新字段时若仍缺失会另行报错）
        print(f"[leads] 线索表字段补齐检查失败（忽略）: {e}")


def _warmup_leads_table():
    """启动后台预热：尽早建表并读取一次，失败不影响服务启动（接口调用时会重试）。"""
    try:
        _fetch_leads(force_refresh=True)
        print(f"[leads] 飞书线索表初始化完成（{len(_leads_cache['data'] or [])} 条）")
    except Exception as e:
        print(f"[leads] 飞书线索表初始化失败（接口调用时将自动重试）: {e}")


def _norm_lead_record(rec: dict) -> dict:
    """飞书记录 -> 归一化字段（英文短 key 供前端使用，另附 record_id）。"""
    fl = rec.get("fields", {})
    out = {"record_id": rec.get("record_id", "")}
    for short, full in LEADS_FIELD_MAP.items():
        out[short] = _tv(fl.get(full))
    return out


def _fetch_leads(force_refresh=False) -> list:
    """读取线索表全部记录（按认领时间倒序），30 秒内存缓存。
    飞书失败时抛出异常（由调用方决定降级或 502）。"""
    now = time.time()
    if not force_refresh and _leads_cache["data"] is not None \
            and now - _leads_cache["ts"] < _LEADS_CACHE_TTL:
        return list(_leads_cache["data"])
    tid = _ensure_leads_table()
    items = []
    page_token = None
    while True:
        path = f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records?page_size=100"
        if page_token:
            path += f"&page_token={page_token}"
        resp = _feishu_api("GET", path)
        data = resp.get("data", {})
        for it in data.get("items", []):
            items.append(_norm_lead_record(it))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    items.sort(key=lambda x: x.get("认领时间", ""), reverse=True)
    _leads_cache["data"] = items
    _leads_cache["ts"] = now
    return list(items)


def _invalidate_leads_cache():
    _leads_cache["data"] = None
    _leads_cache["ts"] = 0.0


def _find_active_lead_by_url(leads: list, url: str):
    """按原文链接查找有效线索（状态=跟进中/已转客户），返回记录或 None。"""
    u = (url or "").strip()
    if not u:
        return None
    for ld in leads:
        if ld.get("原文链接", "").strip() == u and ld.get("状态") in LEAD_ACTIVE_STATUS:
            return ld
    return None


class LeadClaimRequest(BaseModel):
    title: str = ""
    opp_type: str = ""
    summary: str = ""
    source: str = ""
    url: str = ""
    regions: List[str] = []
    date: str = ""
    company: Optional[str] = None
    org: Optional[str] = None


class LeadUpdateRequest(BaseModel):
    company: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    note: Optional[str] = None
    email_source: Optional[str] = None


class LeadFindEmailRequest(BaseModel):
    record_id: str = ""
    company: str = ""  # 前端输入框当前值（未保存时也可直接搜索）


@app.post("/api/leads/claim")
async def api_leads_claim(req: LeadClaimRequest, request: Request):
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    title = (req.title or "").strip()
    if not title:
        return JSONResponse({"ok": False, "message": "线索标题不能为空"}, status_code=400)
    try:
        leads = _fetch_leads(force_refresh=True)
        # 按原文链接查重：已有有效线索（跟进中/已转客户）则拒绝重复认领
        dup = _find_active_lead_by_url(leads, req.url)
        if dup:
            return JSONResponse({
                "detail": "该信号已被认领",
                "claimed_by": dup.get("认领人", ""),
                "status": dup.get("状态", ""),
            }, status_code=409)

        opp_label = (req.opp_type or "").strip()
        if opp_label not in LEAD_OPP_OPTIONS:
            # 兼容传入英文 opp_type key（clinic_expansion 等）
            from app.insights_llm import OPP_LABELS
            opp_label = OPP_LABELS.get(opp_label, "采购动态")
        # 公司/机构：优先用信号自带 AI 提取机构名（org），兼容 company 字段
        company = ((req.org or "").strip() or (req.company or "").strip())
        regions = req.regions if isinstance(req.regions, list) else []
        now_iso = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        fields = {
            "线索标题": title,
            "商机类型": opp_label,
            "公司/机构": company,
            "摘要": (req.summary or "")[:2000],
            "来源": req.source or "",
            "原文链接": req.url or "",
            "地区": ", ".join(str(r) for r in regions if r),
            "发布日期": req.date or "",
            "认领人": user_info.get("name") or user_info.get("username", ""),
            "认领时间": now_iso,
            "状态": "跟进中",
            "联系邮箱": "",
            "跟进备注": "",
            "邮箱来源": "",
        }
        tid = _ensure_leads_table()
        resp = _feishu_api(
            "POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records",
            {"fields": fields})
        rec = resp.get("data", {}).get("record", {})
        lead = _norm_lead_record(rec) if rec else dict(fields, record_id="")
        _invalidate_leads_cache()
        return {"ok": True, "lead": lead}
    except Exception as e:
        print(f"[leads] 认领失败（{title[:30]}）: {e}")
        return JSONResponse({"ok": False, "message": f"认领失败：飞书线索服务暂时不可用（{e}）"},
                            status_code=502)


@app.get("/api/leads")
async def api_leads_list(request: Request):
    token = _get_token_from_request(request)
    if not token or not _verify_token(token):
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        leads = _fetch_leads(force_refresh=True)
    except Exception as e:
        print(f"[leads] 读取线索列表失败: {e}")
        return JSONResponse({"ok": False, "message": f"读取线索列表失败：飞书线索服务暂时不可用（{e}）"},
                            status_code=502)
    return {"ok": True, "items": leads, "total": len(leads)}


@app.put("/api/leads/{record_id}")
async def api_leads_update(record_id: str, req: LeadUpdateRequest, request: Request):
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        tid = _ensure_leads_table()
        fields = {}
        if req.company is not None:
            fields["公司/机构"] = req.company.strip()
        if req.email is not None:
            fields["联系邮箱"] = req.email.strip()
        if req.note is not None:
            fields["跟进备注"] = req.note.strip()
        if req.email_source is not None:
            fields["邮箱来源"] = req.email_source.strip()
        if req.status is not None:
            status = req.status.strip()
            if status not in LEAD_STATUS_OPTIONS:
                return JSONResponse({"ok": False, "message": f"状态仅支持：{'/'.join(LEAD_STATUS_OPTIONS)}"},
                                    status_code=400)
            fields["状态"] = status
        if not fields:
            return {"ok": True, "message": "无需要更新的内容"}
        _feishu_api(
            "PUT",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}",
            {"fields": fields})
        _invalidate_leads_cache()

        # 状态转为「已转客户」时，自动在客户表建档（同邮箱不重复创建）。
        # 建档失败不阻断线索状态更新（仅日志记录），客户可在客户分级页补建。
        customer_created = False
        if fields.get("状态") == "已转客户":
            try:
                from app import business
                lead = None
                for ld in _fetch_leads(force_refresh=True):
                    if ld.get("record_id") == record_id:
                        lead = ld
                        break
                if lead:
                    _, created = business.ensure_customer_from_lead(lead)
                    customer_created = created
            except Exception as ce:
                print(f"[leads] 转客户自动建档失败（不阻断状态更新）{record_id}: {ce}")
        return {"ok": True, "customer_created": customer_created}
    except Exception as e:
        print(f"[leads] 更新线索失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"更新线索失败：飞书线索服务暂时不可用（{e}）"},
                            status_code=502)


@app.delete("/api/leads/{record_id}")
async def api_leads_delete(record_id: str, request: Request):
    """删除线索记录。admin 可删任意；普通销售仅可删本人认领的记录。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
    try:
        leads = _fetch_leads(force_refresh=True)
        target = None
        for ld in leads:
            if ld.get("record_id") == record_id:
                target = ld
                break
        if not target:
            return JSONResponse({"detail": "线索记录不存在或已被删除"}, status_code=404)
        is_admin = user_info.get("role") == "admin"
        claimer = (target.get("认领人") or "").strip()
        my_name = (user_info.get("name") or user_info.get("username") or "").strip()
        if not is_admin and claimer != my_name:
            return JSONResponse(
                {"detail": "仅可删除本人认领的线索；他人线索请联系管理员"}, status_code=403)
        tid = _ensure_leads_table()
        try:
            _feishu_api(
                "DELETE",
                f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}")
        except RuntimeError as e:
            # 飞书记录已不存在视为删除成功（幂等），其余错误抛出
            if "HTTP 404" not in str(e):
                raise
        _invalidate_leads_cache()
        return {"ok": True}
    except Exception as e:
        print(f"[leads] 删除线索失败（{record_id}）: {e}")
        return JSONResponse({"detail": f"删除失败：飞书线索服务暂时不可用（{e}）"},
                            status_code=502)


# ============================================================
# 线索邮箱智能查找：搜索引擎找官网 -> 抓官网/联系页/新闻原文 -> 正则提取邮箱
# 仅用标准库 urllib/re/html/json，不引入新依赖。结果供人工审核采用，不自动落库。
# ============================================================
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_FETCH_TIMEOUT = 8          # 单个 HTTP 请求超时（秒）
_FIND_EMAIL_BUDGET = 35.0         # 整体时间预算（秒），到点即返回已收集结果
_FIND_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 社媒/招聘/百科/平台站：不作为候选官网
_EMAIL_SKIP_HOST_KEYWORDS = (
    "facebook.com", "linkedin.com", "instagram.com", "youtube.com", "x.com",
    "twitter.com", "wikipedia.org", "indeed.com", "glassdoor.com",
    "duckduckgo.com", "bing.com", "microsoft.com", "google.com",
    "yelp.com", "yellowpages.com", "bloomberg.com", "crunchbase.com",
    "amazon.", "reddit.com", "pinterest.com", "tiktok.com",
)
_EMAIL_BAD_DOMAINS = (
    "example.com", "example.org", "sentry.io", "wordpress.org", "w3.org",
    "schema.org", "sentry.wtf",
)
_EMAIL_BAD_DOMAIN_KEYWORDS = ("schema", "wordpress", "w3.org", "sentry")
# 图片/样式误匹配的邮箱式字符串域名后缀
_EMAIL_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico")


# 带 CookieJar 的全局 opener：跟随 302/301 并保存 cookie，
# 应对 Bing 等搜索引擎在数据中心 IP 上的「302 挑战 + Set-Cookie」反爬
import urllib.request as _ur_mod
import http.cookiejar as _cookiejar_mod
_web_cookiejar = _cookiejar_mod.CookieJar()
_web_opener = _ur_mod.build_opener(_ur_mod.HTTPCookieProcessor(_web_cookiejar))
_web_opener.addheaders = [
    ("User-Agent", _FIND_UA),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.9,zh-CN;q=0.8"),
]


def _http_fetch(url: str, timeout: int = _EMAIL_FETCH_TIMEOUT):
    """带浏览器 UA + cookie 的 GET，跟随重定向。
    返回 (final_url, html_text)；非 HTML/异常时返回 (url, "") 或抛出由调用方处理。"""
    with _web_opener.open(url, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        final_url = r.geturl()
        if "text/html" not in ctype and "application/xhtml" not in ctype and \
           "text/plain" not in ctype and not final_url.endswith((".html", ".htm", "/")):
            return final_url, ""
        raw = r.read(2_000_000)
    enc = "utf-8"
    try:
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            enc = m.group(1)
    except Exception:
        pass
    return final_url, raw.decode(enc, "ignore")


def _http_get(url: str, timeout: int = _EMAIL_FETCH_TIMEOUT) -> str:
    """带浏览器 UA 的 GET，返回解码后的 HTML 文本；任何异常抛出由调用方吞掉。"""
    return _http_fetch(url, timeout)[1]


def _company_slug(company: str) -> str:
    """公司名 -> 域名 slug：去法律后缀（Inc/Ltd/LLC...）、去空格标点、小写。"""
    c = (company or "").lower()
    # 法律/公司后缀（先长后短，避免 Inc 误伤）
    for suf in ("limited", "company", "co.,ltd", "co. ltd", "corporation",
                "incorporated", "holdings", "group", "technologies",
                "technology", "solutions", "medical", "healthcare",
                "vet", "veterinary", "animal health", "pharmaceuticals",
                "pharma", "l.l.c", "llc", "ltd", "inc", "corp", "co.", "co",
                "gmbh", "pvt", "pte", "s.a.", "s.a", "s.r.l"):
        c = re.sub(r"[\s\.\-,]?" + re.escape(suf) + r"\.?$", "", c.strip())
    c = re.sub(r"[^a-z0-9]", "", c)
    return c


def _company_keywords(company: str) -> list:
    """公司名关键词（用于首页内容匹配）：去掉常见通用词与过短词。"""
    stop = {"the", "and", "of", "inc", "ltd", "llc", "corp", "co", "company",
            "group", "limited", "gmbh", "medical", "health", "healthcare",
            "vet", "veterinary", "animal", "pharmaceuticals", "pharma",
            "international", "global", "new", "usa", "us"}
    words = re.findall(r"[a-zA-Z0-9]+", (company or "").lower())
    return [w for w in words if len(w) >= 4 and w not in stop]


def _homepage_matches_company(html_text: str, company: str, slug: str) -> bool:
    """首页 title/正文包含公司关键词或 slug 即认定为该公司官网。"""
    if not html_text:
        return False
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    head = ((m.group(1) if m else "") + " " + html_text[:40000]).lower()
    slug = (slug or "").lower()
    if len(slug) >= 4 and slug in head:
        return True
    for kw in _company_keywords(company):
        if kw in head:
            return True
    return False


def _validate_candidate_urls(urls: list, company: str, logs: list,
                             tag: str, deadline: float, limit: int = 2) -> list:
    """对候选 URL 取首页，验证是否为目标公司官网；返回命中的 host 列表（按注册域去重）。"""
    hosts = []
    seen_reg = set()
    for u in urls:
        if time.time() >= deadline or len(hosts) >= limit:
            break
        try:
            u = u.strip()
            if not u or not u.lower().startswith("http"):
                continue
            if u.lower().endswith(".pdf"):
                continue
            netloc = _host_of(u)
            if not netloc or _is_skippable_host(netloc):
                continue
            final_url, html_text = _http_fetch(u)
            final_host = _host_of(final_url) or netloc
            reg = _reg_host(final_host)
            if reg in seen_reg:
                continue
            if _homepage_matches_company(html_text, company, _company_slug(company)):
                seen_reg.add(reg)
                hosts.append(final_host)
        except Exception as e:
            logs.append(f"{tag} 校验 {_host_of(u) or u[:40]} 失败:{type(e).__name__}")
            continue
    return hosts


def _ddg_real_url(href: str) -> str:
    """DuckDuckGo 跳转链接 //duckduckgo.com/l/?uddg=<编码URL>&... -> 真实 URL。"""
    try:
        from urllib.parse import urlparse, parse_qs, unquote
        p = urlparse("https:" + href if href.startswith("//") else href)
        qs = parse_qs(p.query)
        u = qs.get("uddg", [""])[0]
        if u:
            return unquote(u)
    except Exception:
        pass
    return href


def _search_ddg(company: str) -> list:
    """DuckDuckGo HTML 版搜索，返回结果真实 URL 列表。"""
    import urllib.parse as _up
    import urllib.request as _ur
    q = _up.urlencode({"q": company + " official website contact"})
    url = "https://html.duckduckgo.com/html/?" + q
    req = _ur.Request(url, headers={"User-Agent": _FIND_UA,
                                    "Accept-Language": "en-US,en;q=0.9"})
    with _ur.urlopen(req, timeout=_EMAIL_FETCH_TIMEOUT) as r:
        html_text = r.read(1_500_000).decode("utf-8", "ignore")
    out = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html_text):
        u = _ddg_real_url(m.group(1))
        if u and u not in out:
            out.append(u)
    return out


def _search_bing(company: str) -> list:
    """Bing 搜索（末级降级）：用 CookieJar opener 跟随 302 挑战/重定向后解析外链。"""
    import urllib.parse as _up
    from urllib.parse import urlparse
    q = _up.urlencode({"q": company + " contact email"})
    url = "https://www.bing.com/search?" + q
    with _web_opener.open(url, timeout=_EMAIL_FETCH_TIMEOUT) as r:
        html_text = r.read(1_500_000).decode("utf-8", "ignore")
    out = []
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"', html_text):
        u = m.group(1)
        try:
            host = urlparse(u).netloc.lower()
        except Exception:
            continue
        if not host or "bing.com" in host or "microsoft.com" in host or \
           "baidu.com" in host or "zhihu.com" in host or "sogou.com" in host:
            continue
        if u not in out:
            out.append(u)
    return out


def _discover_by_guess(company: str, logs: list, deadline: float) -> list:
    """第 1 级：直猜域名（slug.com/.org + http 兜底），首页内容匹配即认定官网。"""
    tag = "直猜域名"
    slug = _company_slug(company)
    if len(slug) < 4:
        logs.append(f"{tag}:公司名过短跳过")
        return []
    candidates = [
        f"https://www.{slug}.com/",
        f"https://{slug}.com/",
        f"https://www.{slug}.org/",
        f"https://{slug}.org/",
        f"http://www.{slug}.com/",
        f"http://{slug}.com/",
    ]
    return _validate_candidate_urls(candidates, company, logs, tag, deadline, limit=1)


def _search_ddg_ia(company: str) -> list:
    """第 2 级：DuckDuckGo Instant Answer API（机房友好），收集 Abstract/Results/Related 中的 URL。"""
    import urllib.parse as _up
    import urllib.request as _ur
    q = _up.urlencode({"q": company, "format": "json",
                       "no_html": "1", "no_redirect": "1"})
    url = "https://api.duckduckgo.com/?" + q
    req = _ur.Request(url, headers={"User-Agent": _FIND_UA})
    with _ur.urlopen(req, timeout=_EMAIL_FETCH_TIMEOUT) as r:
        data = json.loads(r.read(500_000).decode("utf-8", "ignore"))
    urls = []

    def collect(node):
        if isinstance(node, dict):
            for k in ("AbstractURL", "FirstURL", "OfficialSiteURL"):
                v = node.get(k)
                if isinstance(v, str) and v.startswith("http") and v not in urls:
                    urls.append(v)
            # Results / RelatedTopics 可能嵌套
            for v in node.values():
                if isinstance(v, (list, dict)):
                    collect(v)
        elif isinstance(node, list):
            for it in node:
                collect(it)
    collect(data)
    return urls


def _discover_by_ddg_ia(company: str, logs: list, deadline: float) -> list:
    try:
        urls = _search_ddg_ia(company)
        if not urls:
            logs.append("DDG-IA:无结果")
            return []
        return _validate_candidate_urls(urls, company, logs, "DDG-IA", deadline)
    except Exception as e:
        logs.append(f"DDG-IA:{type(e).__name__}")
        return []


def _search_wikipedia(company: str) -> list:
    """第 3 级：Wikipedia API（机房友好）。搜索词条 -> parse externlinks 取官网外链。"""
    import urllib.parse as _up
    import urllib.request as _ur
    base = "https://en.wikipedia.org/w/api.php?"
    # 1) 搜索词条
    q = _up.urlencode({"action": "query", "list": "search",
                       "srsearch": company, "format": "json", "srlimit": "1"})
    req = _ur.Request(base + q, headers={"User-Agent": _FIND_UA})
    with _ur.urlopen(req, timeout=_EMAIL_FETCH_TIMEOUT) as r:
        data = json.loads(r.read(300_000).decode("utf-8", "ignore"))
    hits = data.get("query", {}).get("search", [])
    if not hits:
        return []
    title = hits[0].get("title", "")
    # 2) 取该页外链
    q2 = _up.urlencode({"action": "parse", "page": title,
                        "prop": "externlinks", "format": "json",
                        "limit": "30"})
    req2 = _ur.Request(base + q2, headers={"User-Agent": _FIND_UA})
    with _ur.urlopen(req2, timeout=_EMAIL_FETCH_TIMEOUT) as r2:
        d2 = json.loads(r2.read(500_000).decode("utf-8", "ignore"))
    urls = []
    for link in d2.get("parse", {}).get("externlinks", []):
        for v in link.values():
            if isinstance(v, str) and v.startswith("http") and v not in urls:
                urls.append(v)
    return urls


def _discover_by_wikipedia(company: str, logs: list, deadline: float) -> list:
    try:
        urls = _search_wikipedia(company)
        if not urls:
            logs.append("Wikipedia:无词条或外链")
            return []
        return _validate_candidate_urls(urls, company, logs, "Wikipedia", deadline)
    except Exception as e:
        logs.append(f"Wikipedia:{type(e).__name__}")
        return []


def _discover_by_search_engines(company: str, logs: list, deadline: float) -> list:
    """第 4 级（末级）：DDG HTML + Bing（CookieJar 跟随 302），结果链接取首页验证。"""
    urls = []
    try:
        for u in _search_ddg(company):
            if u not in urls:
                urls.append(u)
    except Exception as e:
        logs.append(f"DDG-HTML:{type(e).__name__}")
    try:
        for u in _search_bing(company):
            if u not in urls:
                urls.append(u)
    except Exception as e:
        logs.append(f"Bing:{type(e).__name__}")
    if not urls:
        return []
    return _validate_candidate_urls(urls, company, logs, "搜索引擎", deadline)


def _discover_official_hosts(company: str, deadline: float):
    """多级降级发现官网域名。返回 (hosts, logs)。
    顺序：直猜域名 -> DDG Instant Answer -> Wikipedia -> DDG HTML/Bing。
    任一级命中即返回；各级失败原因记入 logs 供 502 排查。"""
    logs = []
    hosts = _discover_by_guess(company, logs, deadline)
    if hosts or time.time() >= deadline:
        if not hosts:
            logs.append("直猜域名无命中")
        return hosts, logs
    hosts = _discover_by_ddg_ia(company, logs, deadline)
    if hosts or time.time() >= deadline:
        return hosts, logs
    hosts = _discover_by_wikipedia(company, logs, deadline)
    if hosts or time.time() >= deadline:
        return hosts, logs
    hosts = _discover_by_search_engines(company, logs, deadline)
    return hosts, logs


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return ""


def _reg_host(host: str) -> str:
    """取注册域名近似值（去 www. 等前缀与端口；多级域名取末两段，country-level 例外不细分）。"""
    h = (host or "").lower().split(":")[0]  # 去端口
    if h.startswith("www."):
        h = h[4:]
    parts = h.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return h


def _is_skippable_host(host: str) -> bool:
    h = (host or "").lower()
    return any(k in h for k in _EMAIL_SKIP_HOST_KEYWORDS)


def _candidate_official_domains(urls: list) -> list:
    """从搜索结果 URL 中挑前 4 个候选官网域名（去社媒/招聘/百科/PDF，按注册域去重）。"""
    from urllib.parse import urlparse
    seen = set()
    domains = []
    for u in urls:
        try:
            if not u or not u.lower().startswith("http"):
                continue
            low = u.lower()
            if low.endswith(".pdf") or ".pdf?" in low or "/pdf/" in low:
                continue
            netloc = (urlparse(u).netloc or "").lower()  # 含端口（本地/非常规环境兼容）
            host = netloc.split(":")[0]
            if not host or _is_skippable_host(host):
                continue
            reg = _reg_host(host)
            if reg in seen:
                continue
            seen.add(reg)
            domains.append(netloc or host)
        except Exception:
            continue
        if len(domains) >= 4:
            break
    return domains


def _abs_url(base: str, link: str) -> str:
    from urllib.parse import urljoin
    try:
        return urljoin(base, link)
    except Exception:
        return ""


def _crawl_official_site(host: str, deadline: float) -> list:
    """抓官网首页 + 首页中 contact/about 链接（每域名最多 3 页），返回 [{url, html}]。"""
    pages = []
    if time.time() > deadline:
        return pages
    home_html = ""
    home = "https://" + host + "/"
    try:
        home_html = _http_get(home)
    except Exception:
        home_html = ""
    if not home_html:
        # https 失败/为空兜底试一次 http
        home = "http://" + host + "/"
        try:
            home_html = _http_get(home)
        except Exception:
            return pages
    if not home_html:
        return pages
    pages.append({"url": home, "html": home_html})
    if time.time() > deadline:
        return pages
    # 从首页提取 contact / about 链接
    sub_links = []
    seen = {pages[0]["url"]}
    for m in re.finditer(r'href=["\']([^"\']+)["\']', pages[0]["html"], re.I):
        link = m.group(1).strip()
        low = link.lower()
        if not ("/contact" in low or "contact-" in low or "/about" in low
                or "about-" in low or "contactus" in low):
            continue
        if low.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = _abs_url(pages[0]["url"], link)
        if not absu or not absu.startswith("http"):
            continue
        if _reg_host(_host_of(absu)) != _reg_host(host):
            continue  # 只抓同域页面
        if absu.rstrip("/") in seen or absu in seen:
            continue
        seen.add(absu.rstrip("/"))
        sub_links.append(absu)
        if len(sub_links) >= 2:  # 首页 + 2 个子页 = 每域名最多 3 页
            break
    for su in sub_links:
        if time.time() > deadline:
            break
        try:
            txt = _http_get(su)
            if txt:
                pages.append({"url": su, "html": txt})
        except Exception:
            continue
    return pages


def _is_valid_email(em: str) -> bool:
    """过滤示例域名、图片误匹配、schema/wordpress 等噪声、超长邮箱。"""
    low = em.lower()
    if len(em) > 40:
        return False
    dom = low.split("@", 1)[1] if "@" in low else ""
    if not dom:
        return False
    if any(dom == d or dom.endswith("." + d) for d in _EMAIL_BAD_DOMAINS):
        return False
    if any(k in dom for k in _EMAIL_BAD_DOMAIN_KEYWORDS):
        return False
    if any(dom.endswith(suf) for suf in _EMAIL_IMAGE_SUFFIXES):
        return False
    # 域名末段必须是纯字母（正则已保证 {2,}），再排掉数字 TLD 误匹配
    tld = dom.rsplit(".", 1)[-1]
    if not tld.isalpha():
        return False
    return True


def _scan_emails(html_text: str, source_url: str, out: list, seen: set):
    """从单页 HTML 提取邮箱并登记来源（out 追加，seen 去重）。"""
    try:
        for m in _EMAIL_RE.finditer(html_text or ""):
            em = m.group(0)
            if em in seen or not _is_valid_email(em):
                continue
            seen.add(em)
            out.append({
                "email": em,
                "source_url": source_url,
                "host": _host_of(source_url),
            })
    except Exception:
        pass


def find_lead_email_candidates(company: str, signal_url: str = "") -> dict:
    """核心查找流程（供接口与本地测试直接调用）。
    返回 {ok, candidates:[{email,source_url,host,kind}], message?}。
    搜索引擎全部失败抛 RuntimeError（接口层转 502）。"""
    import html as _html
    deadline = time.time() + _FIND_EMAIL_BUDGET

    # 1) 多级降级找官网域名：直猜域名 -> DDG-IA -> Wikipedia -> DDG HTML/Bing
    #    数据中心 IP 常被搜索引擎拦截，直猜与 API 类入口机房友好，故优先。
    official_hosts, discover_logs = _discover_official_hosts(company, deadline)
    official_regs = {_reg_host(h) for h in official_hosts}
    print(f"[find-email] 官网发现（{company}）: {official_hosts or '无'} | {' ; '.join(discover_logs)}")

    found = []
    seen = set()

    # 3) 新闻原文页（新闻稿常含媒体联系邮箱），失败忽略
    su = (signal_url or "").strip()
    if su and time.time() < deadline:
        try:
            txt = _http_get(su)
            if txt:
                _scan_emails(txt, su, found, seen)
        except Exception as e:
            print(f"[find-email] 原文页抓取失败（{su[:80]}）: {e}")

    # 4) 逐个官网：首页 + 联系/关于页，每域最多 3 页
    for host in official_hosts:
        if time.time() >= deadline or len(found) >= 8:
            break
        try:
            pages = _crawl_official_site(host, deadline)
        except Exception as e:
            print(f"[find-email] 官网抓取失败（{host}）: {e}")
            continue
        reg = _reg_host(host)
        for pg in pages:
            # 扫描前先反转义 HTML 实体（&amp; 等），保证邮箱完整
            _scan_emails(_html.unescape(pg["html"]), pg["url"], found, seen)
            # kind 标记：来源域名属于搜索结果官网域 -> 官网，否则（原文页）-> 新闻原文
        # 标记本轮官网抓到的邮箱
        for item in found:
            if "kind" in item:
                continue
            if _reg_host(item.get("host", "")) == reg:
                item["kind"] = "官网"

    # 未标 kind 的（来自新闻原文页或非官网域）统一为「新闻原文」
    for item in found:
        item.setdefault("kind", "新闻原文")
        # 兜底：若来源域恰好命中某官网注册域，归为官网
        if item["kind"] == "新闻原文" and \
                _reg_host(item.get("host", "")) in official_regs:
            item["kind"] = "官网"

    # 官网候选排前面，同类按发现顺序
    found.sort(key=lambda x: 0 if x.get("kind") == "官网" else 1)
    candidates = found[:5]
    if not candidates:
        if official_hosts:
            # 找到了官网但页面未提取到邮箱：属正常空结果
            return {"ok": True, "candidates": [],
                    "message": "未在公开网页自动找到邮箱，可手动搜索或查看原文联系页"}
        # 官网发现链路全失败（数据中心被拦/超时等）：抛错附各级原因，供接口返回 502 排查
        reason = "；".join(discover_logs) if discover_logs else "全部入口无响应"
        if len(reason) > 200:
            reason = reason[:200]
        raise RuntimeError(f"官网发现失败：{reason}")
    return {"ok": True, "candidates": candidates}


@app.post("/api/leads/find-email")
async def api_leads_find_email(req: LeadFindEmailRequest, request: Request):
    """智能查找线索联系邮箱：自动搜官网/联系页/新闻原文，返回候选供人工审核采用。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
    record_id = (req.record_id or "").strip()
    if not record_id:
        return JSONResponse({"detail": "缺少 record_id"}, status_code=400)
    try:
        tid = _ensure_leads_table()
        resp = _feishu_api(
            "GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}")
        rec = resp.get("data", {}).get("record") or {}
        fields = rec.get("fields", {})
        company = _tv(fields.get("公司/机构")).strip()
        signal_url = _tv(fields.get("原文链接")).strip()
    except Exception as e:
        print(f"[find-email] 读取线索记录失败（{record_id}）: {e}")
        return JSONResponse({"detail": f"读取线索失败：飞书线索服务暂时不可用（{e}）"},
                            status_code=502)
    # 前端输入框里未保存的公司名优先；为空再退回飞书记录里的值
    company = (getattr(req, "company", "") or "").strip() or company
    if not company:
        return JSONResponse({"detail": "请先填写公司/机构名再查找邮箱"}, status_code=400)
    try:
        result = find_lead_email_candidates(company, signal_url)
    except RuntimeError as e:
        # 官网发现链路全失败：detail 附各级原因（截断 200 字符），便于线上排查
        print(f"[find-email] 搜索服务失败（{company}）: {e}")
        reason = str(e)
        if len(reason) > 200:
            reason = reason[:200]
        return JSONResponse(
            {"detail": f"搜索服务暂不可用，请稍后重试或手动搜索（{reason}）"},
            status_code=502)
    except Exception as e:
        print(f"[find-email] 查找异常（{company}）: {e}")
        return JSONResponse({"detail": "搜索服务暂不可用，请稍后重试或手动搜索"},
                            status_code=502)
    return result


@app.post("/api/products")
async def api_product_create(req: ProductUpsertRequest):
    import urllib.request as _ur
    try:
        fields = {"product_model": req.product_model, "product_name_cn": req.product_name,
                  "category": req.category, "main_selling_point": req.main_selling_point,
                  "product_image_url": req.product_image_url, "status": req.status or "active"}
        rq = _ur.Request(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_ATK}/tables/{FEISHU_TID}/records",
            data=json.dumps({"fields": fields}).encode(), headers=_feishu_headers(), method="POST")
        with _ur.urlopen(rq, timeout=15) as r:
            resp = json.loads(r.read())
        rec = resp.get("data", {}).get("record", {})
        fl = rec.get("fields", {})
        return {"record_id": rec.get("record_id", ""),
                "product_model": _tv(fl.get("product_model", req.product_model)),
                "product_name": _tv(fl.get("product_name_cn", fl.get("product_name", req.product_name))),
                "category": _tv(fl.get("category", req.category)),
                "main_selling_point": _tv(fl.get("main_selling_point", req.main_selling_point)),
                "product_image_url": _tv(fl.get("product_image_url", req.product_image_url)),
                "status": _tv(fl.get("status", req.status))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/products/{record_id}")
async def api_product_update(record_id: str, req: ProductUpsertRequest):
    import urllib.request as _ur
    try:
        fields = {}
        if req.product_model: fields["product_model"] = req.product_model
        if req.product_name: fields["product_name_cn"] = req.product_name
        if req.category: fields["category"] = req.category
        if req.main_selling_point: fields["main_selling_point"] = req.main_selling_point
        if req.product_image_url: fields["product_image_url"] = req.product_image_url
        rq = _ur.Request(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_ATK}/tables/{FEISHU_TID}/records/{record_id}",
            data=json.dumps({"fields": fields}).encode(), headers=_feishu_headers(), method="PUT")
        with _ur.urlopen(rq, timeout=15) as r:
            resp = json.loads(r.read())
        rec = resp.get("data", {}).get("record", {})
        fl = rec.get("fields", {})
        return {"record_id": record_id,
                "product_model": _tv(fl.get("product_model", req.product_model)),
                "product_name": _tv(fl.get("product_name_cn", fl.get("product_name", req.product_name))),
                "category": _tv(fl.get("category", req.category)),
                "main_selling_point": _tv(fl.get("main_selling_point", req.main_selling_point)),
                "product_image_url": _tv(fl.get("product_image_url", req.product_image_url)),
                "status": _tv(fl.get("status", req.status))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/products/{record_id}")
async def api_product_delete(record_id: str):
    import urllib.request as _ur
    try:
        rq = _ur.Request(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_ATK}/tables/{FEISHU_TID}/records/{record_id}",
            headers=_feishu_headers(), method="DELETE")
        with _ur.urlopen(rq, timeout=15) as r:
            json.loads(r.read())
        return {"status": "ok", "record_id": record_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cutout")
async def api_cutout(request: Request):
    """Accept image upload, remove background via rembg, return transparent PNG URL."""
    try:
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            raise HTTPException(status_code=400, detail="No file provided")
        file_data = await file.read()
        if not file_data:
            raise HTTPException(status_code=400, detail="Empty file")

        # Remove background
        from rembg import remove
        from PIL import Image
        import io as _io
        input_img = Image.open(_io.BytesIO(file_data))
        if input_img.mode not in ("RGB", "RGBA"):
            input_img = input_img.convert("RGBA" if "A" in input_img.getbands() else "RGB")
        # Downscale to avoid OOM on 512MB Railway instance (rembg is memory-heavy)
        max_dim = 1400
        w, h = input_img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            input_img = input_img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
        output_img = remove(input_img)
        buf = _io.BytesIO()
        output_img.save(buf, format="PNG", optimize=True)
        cutout_bytes = buf.getvalue()

        # Upload to freeimage
        import urllib.request as _ur
        import uuid
        boundary = uuid.uuid4().hex
        filename = f"cutout_{uuid.uuid4().hex[:8]}.png"
        body = _build_multipart(boundary, cutout_bytes, filename)
        rq = _ur.Request(
            "https://freeimage.host/api/1/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with _ur.urlopen(rq, timeout=60) as r:
            resp = json.loads(r.read())
        if resp and resp.get("image") and resp["image"].get("url"):
            return {"url": resp["image"]["url"], "status": "ok"}
        raise HTTPException(status_code=502, detail="Failed to upload cutout result")
    except HTTPException:
        raise
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"rembg not available: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/proxy-image")
async def api_proxy_image(url: str):
    """Proxy image to avoid CORS taint on canvas. Returns image bytes with CORS headers."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        from fastapi.responses import Response
        content_type = resp.headers.get("content-type", "image/png")
        return Response(
            content=resp.content,
            media_type=content_type,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",
            },
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proxy failed: {e}")


@app.post("/api/upload-image")
async def api_upload_image(request: Request):
    import urllib.request as _ur
    import uuid
    try:
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            raise HTTPException(status_code=400, detail="No file provided")
        file_data = await file.read()
        filename = file.filename or "upload.png"
        boundary = uuid.uuid4().hex
        body = _build_multipart(boundary, file_data, filename)
        rq = _ur.Request("https://freeimage.host/api/1/upload", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with _ur.urlopen(rq, timeout=30) as r:
            resp = json.loads(r.read())
        if resp and resp.get("image") and resp["image"].get("url"):
            return {"url": resp["image"]["url"], "status": "ok"}
        err = resp.get("error", {}).get("message", "Upload failed") if resp else "Upload failed"
        raise HTTPException(status_code=502, detail=err)
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _build_multipart(boundary, file_data, filename):
    api_key = os.getenv("FREEIMAGE_API_KEY", "6d207e02198a847aa98d0a2a901485a5").encode()
    CRLF = b"\r\n"
    parts = [b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="key"', b"", api_key,
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="action"', b"", b"upload",
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="type"', b"", b"file",
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="source"; filename="' + filename.encode() + b'"',
        b"Content-Type: application/octet-stream", b"", file_data,
        b"--" + boundary.encode() + b"--"]
    return CRLF.join(parts)

@app.get("/api/products")
async def api_products_list():
    import urllib.request as _ur
    try:
        _tk=_feishu_token()
        _all=[]
        _pt=None
        while True:
            _u=f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_ATK}/tables/{FEISHU_TID}/records?page_size=100"
            if _pt:
                _u+=f"&page_token={_pt}"
            _rq=_ur.Request(_u, headers={"Authorization":f"Bearer {_tk}"})
            with _ur.urlopen(_rq,timeout=15) as r:
                _d=json.loads(r.read())
            _all.extend(_d.get("data",{}).get("items",[]))
            if not _d.get("data",{}).get("has_more"):
                break
            _pt=_d.get("data",{}).get("page_token")
        def _tv(v):
            if v is None:
                return ""
            if isinstance(v,list):
                return ", ".join(str(x.get("text",x) if isinstance(x,dict) else x) for x in v)
            if isinstance(v,dict):
                return v.get("text",str(v))
            return str(v)
        ps=[]
        for it in _all:
            fl=it.get("fields",{})
            ps.append({
                "record_id":it.get("record_id",""),
                "product_model":_tv(fl.get("product_model","")),
                "product_name":_tv(fl.get("product_name_cn",fl.get("product_name",""))),
                "category":_tv(fl.get("category","")),
                "main_selling_point":_tv(fl.get("main_selling_point","")),
                "product_image_url":_tv(fl.get("product_image_url","")),
                "price_tier":_tv(fl.get("price_tier","")),
                "status":_tv(fl.get("status",""))
            })
        return {"items":ps,"total":len(ps)}
    except Exception as e:
        return {"items":[],"error":str(e)}

# ============================================================
# 市场洞察 / 新闻中心 API
# ============================================================
@app.get("/api/insights")
async def api_insights_list():
    from app import insights_store
    all_items = insights_store.get_items()
    # 线上只对外提供 RSS 真新闻；source='seed' 的 12 条手工快照仅用于
    # 前端离线兜底（文件里自带），不混入线上数据。RSS 为空时才退回种子。
    rss_items = [it for it in all_items if it.get("source") != "seed"]
    items = rss_items if rss_items else all_items
    return {
        "items": items,
        "total": len(items),
        "last_refresh": insights_store.status().get("last_refresh"),
    }

@app.post("/api/insights/refresh")
async def api_insights_refresh():
    """手动刷新：后台线程执行抓取+翻译（20s~2min），立即返回受理状态，
    前端轮询 /api/insights 等待数据更新；避免长请求阻塞事件循环。"""
    from app import insights_store

    if insights_store.status().get("refreshing"):
        return {"accepted": False, "reason": "refreshing"}
    # 限频检查（在发起线程前快速判定）
    import time as _time
    if _time.time() - insights_store._state.get("last_refresh_ts", 0) < insights_store.REFRESH_MIN_INTERVAL:
        remain = int(insights_store.REFRESH_MIN_INTERVAL -
                     (_time.time() - insights_store._state.get("last_refresh_ts", 0)))
        return {"accepted": False, "reason": "rate_limited", "remain_seconds": remain}

    def _run():
        try:
            insights_store.refresh(force=True)
        except Exception as e:
            print(f"[insights] 后台刷新失败: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"accepted": True}

@app.get("/api/insights/status")
async def api_insights_status():
    from app import insights_store
    return insights_store.status()

@app.get("/api/signals")
async def api_signals_list(limit: str = "20", opp_type: Optional[str] = None):
    """销售商机信号：从洞察存储中筛出 is_opportunity=true 的条目，
    按日期倒序返回。数据随 /api/insights 刷新管线自动更新，不另建存储。
    query: limit（默认20，上限100）、opp_type（可选，按商机类型过滤）。"""
    from app import insights_store
    from app.insights_llm import OPP_LABELS, OPP_COLORS, OPP_TYPES

    # 与 /api/insights 一致：线上只对外提供 RSS 真新闻；种子快照不混入
    all_items = insights_store.get_items()
    rss_items = [it for it in all_items if it.get("source") != "seed"]
    base_items = rss_items if rss_items else all_items

    sig = [it for it in base_items
           if it.get("is_opportunity") and it.get("opp_type") in OPP_TYPES]
    if opp_type:
        opp_type = opp_type.strip()
        if opp_type in OPP_TYPES:
            sig = [it for it in sig if it.get("opp_type") == opp_type]
    sig.sort(key=lambda x: x.get("date", ""), reverse=True)
    total = len(sig)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    sig = sig[:limit]

    items = [{
        "id": it.get("id", ""),
        "title": it.get("title", ""),
        "summary": it.get("summary", ""),
        "opp_type": it.get("opp_type"),
        "opp_label": OPP_LABELS.get(it.get("opp_type"), "采购动态"),
        "opp_color": OPP_COLORS.get(it.get("opp_type"), "#5B21B6"),
        "org": it.get("opp_org", "") or "",
        "date": it.get("date", ""),
        "source": it.get("source", ""),
        "url": it.get("url", ""),
        "regions": it.get("regions") or ["global"],
        "category": it.get("category", ""),
        "categoryLabel": it.get("categoryLabel", ""),
        "lang": it.get("lang", ""),
    } for it in sig]

    # 附加线索认领状态（按原文链接匹配有效线索）。
    # 飞书线索表查询失败时降级为 claimed:false，不影响信号主接口。
    lead_by_url = {}
    try:
        for ld in _fetch_leads():
            if ld.get("状态") in LEAD_ACTIVE_STATUS and ld.get("原文链接"):
                lead_by_url[ld["原文链接"].strip()] = ld
    except Exception as e:
        print(f"[signals] 线索认领状态 enrichment 失败（降级为未认领）: {e}")
        lead_by_url = {}
    for it in items:
        ld = lead_by_url.get((it.get("url") or "").strip())
        if ld:
            it["claimed"] = True
            it["claimed_by"] = ld.get("认领人", "")
            it["lead_status"] = ld.get("状态", "")
            it["lead_id"] = ld.get("record_id", "")
        else:
            it["claimed"] = False
            it["claimed_by"] = ""
            it["lead_status"] = ""
            it["lead_id"] = ""

    return {
        "ok": True,
        "items": items,
        "total": total,
        "generated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "opp_types": [{"key": k, "label": OPP_LABELS[k], "color": OPP_COLORS[k]}
                      for k in OPP_TYPES],
    }

# ============================================================
# 经营仪表盘 API（真实数据聚合 + AI 经营速览）
# ============================================================
@app.get("/api/dashboard/stats")
async def api_dashboard_stats(request: Request):
    """经营仪表盘聚合统计：信号（总数/今日新增/待认领）、线索
    （跟进中/已转客户/已释放/超期未跟进）、销售漏斗 6 层、层间转化率、
    商机地区分布。需登录（与 /api/leads 一致，Bearer token 或 cookie）。
    飞书线索表不可用时信号侧统计照常返回，线索侧字段为 null。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        from app.dashboard import collect_dashboard_stats, save_daily_snapshot
        stats = collect_dashboard_stats(user_info=user_info)
        # 每次调用落当日快照（覆盖写），供环比与 AI 速览趋势分析
        save_daily_snapshot(stats)
        return stats
    except Exception as e:
        print(f"[dashboard] 统计聚合失败: {e}")
        return JSONResponse({"ok": False, "message": f"经营数据聚合失败：{e}"}, status_code=502)


@app.get("/api/dashboard/ai-brief")
async def api_dashboard_ai_brief(request: Request, refresh: str = "0"):
    """AI 经营速览（SCQA 中文诊断，只基于真实统计数字）。
    结果文件缓存 1 小时；?refresh=1 强制重新生成（刷新按钮）。
    需登录。生成失败返回 502，由前端显示降级文案，不阻塞页面。"""
    token = _get_token_from_request(request)
    if not token or not _verify_token(token):
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        from app.dashboard import get_ai_brief
        return get_ai_brief(force_refresh=(refresh == "1"))
    except Exception as e:
        print(f"[dashboard] AI 速览接口失败: {e}")
        return JSONResponse({"ok": False, "message": "诊断生成中，请稍后刷新"}, status_code=502)


# ============================================================
# 业务表底座 API：客户档案 / 邮件收发 / PI 订单 / 系统配置
# 数据源：飞书多维表格（客户表、订单表复用 + 邮件记录表、系统配置表幂等新建）
# ============================================================

class CustomerUpsertRequest(BaseModel):
    name: Optional[str] = None
    region: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None
    channel: Optional[str] = None
    products: Optional[str] = None
    grade: Optional[str] = None        # A / B / C / 未分级
    cust_status: Optional[str] = None  # 活跃 / 沉睡
    follow_status: Optional[str] = None  # 初步接触/需求沟通/报价中/已成交/复购中/沉睡
    note: Optional[str] = None
    source_lead: Optional[str] = None


class MailSendRequest(BaseModel):
    to: str = ""
    subject: str = ""
    body: str = ""
    lead_id: str = ""
    customer_id: str = ""


class PiUpsertRequest(BaseModel):
    pi_no: Optional[str] = None
    customer_name: Optional[str] = None
    region: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    status: Optional[str] = None       # 草稿/已发送/已确认/已成交/已取消
    products: Optional[str] = None
    sales: Optional[str] = None
    customer_id: Optional[str] = None
    note: Optional[str] = None
    created_at: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    items: Dict[str, str] = {}


_CUSTOMER_GRADES = ("A", "B", "C", "未分级")
_CUSTOMER_STATUS = ("活跃", "沉睡")
_CUSTOMER_FOLLOW = ("初步接触", "需求沟通", "报价中", "已成交", "复购中", "沉睡")
_CUSTOMER_CHANNELS = ("展会", "社媒", "官网询盘", "老客户介绍", "其他")


def _business_auth(request: Request):
    """鉴权并返回 (user_info, JSONResponse)。失败时后者为 401 响应。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return None, JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    return user_info, None


@app.get("/api/customers")
async def api_customers_list(request: Request):
    """客户档案列表（复用飞书「客户表」，幂等补字段）。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    try:
        from app import business
        items = business.fetch_customers(force_refresh=True)
        return {"ok": True, "items": items, "total": len(items)}
    except Exception as e:
        print(f"[customers] 列表读取失败: {e}")
        return JSONResponse({"ok": False, "message": f"客户档案读取失败：{e}"}, status_code=502)


@app.post("/api/customers")
async def api_customers_create(req: CustomerUpsertRequest, request: Request):
    """手动新建客户档案。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    name = (req.name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "客户名称不能为空"}, status_code=400)
    try:
        from app import business
        if req.grade and req.grade not in _CUSTOMER_GRADES:
            return JSONResponse({"ok": False, "message": f"分级仅支持：{'/'.join(_CUSTOMER_GRADES)}"},
                                status_code=400)
        if req.cust_status and req.cust_status not in _CUSTOMER_STATUS:
            return JSONResponse({"ok": False, "message": f"客户状态仅支持：{'/'.join(_CUSTOMER_STATUS)}"},
                                status_code=400)
        if req.follow_status and req.follow_status not in _CUSTOMER_FOLLOW:
            return JSONResponse({"ok": False, "message": "跟进状态仅支持：%s" % "/".join(_CUSTOMER_FOLLOW)},
                                status_code=400)
        if req.channel and req.channel not in _CUSTOMER_CHANNELS:
            return JSONResponse({"ok": False, "message": "来源渠道仅支持：%s" % "/".join(_CUSTOMER_CHANNELS)},
                                status_code=400)
        now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        fields = {
            "客户名称": name,
            "国家/地区": (req.region or "").strip(),
            "联系人": (req.contact or "").strip(),
            "邮箱": (req.email or "").strip(),
            "主营产品": (req.products or "").strip(),
            "备注": (req.note or "").strip(),
            "分级": (req.grade or "未分级"),
            "客户状态": (req.cust_status or "活跃"),
            "创建时间": now_str,
        }
        if req.follow_status:
            fields["跟进状态"] = req.follow_status
        if req.channel:
            fields["来源渠道"] = req.channel
        if req.source_lead:
            fields["来源线索"] = req.source_lead.strip()
        rec = business.create_customer(fields)
        return {"ok": True, "customer": rec}
    except Exception as e:
        print(f"[customers] 新建失败: {e}")
        return JSONResponse({"ok": False, "message": f"客户档案创建失败：{e}"}, status_code=502)


@app.put("/api/customers/{record_id}")
async def api_customers_update(record_id: str, req: CustomerUpsertRequest, request: Request):
    """更新客户档案（分级/状态/跟进状态/备注等白名单字段）。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    try:
        from app import business
        fields = {}
        if req.name is not None:
            v = req.name.strip()
            if v:
                fields["客户名称"] = v
        if req.region is not None:
            fields["国家/地区"] = req.region.strip()
        if req.contact is not None:
            fields["联系人"] = req.contact.strip()
        if req.email is not None:
            fields["邮箱"] = req.email.strip()
        if req.products is not None:
            fields["主营产品"] = req.products.strip()
        if req.note is not None:
            fields["备注"] = req.note.strip()
        if req.grade is not None:
            if req.grade not in _CUSTOMER_GRADES:
                return JSONResponse({"ok": False, "message": f"分级仅支持：{'/'.join(_CUSTOMER_GRADES)}"},
                                    status_code=400)
            fields["分级"] = req.grade
        if req.cust_status is not None:
            if req.cust_status not in _CUSTOMER_STATUS:
                return JSONResponse({"ok": False, "message": f"客户状态仅支持：{'/'.join(_CUSTOMER_STATUS)}"},
                                    status_code=400)
            fields["客户状态"] = req.cust_status
        if req.follow_status is not None:
            if req.follow_status not in _CUSTOMER_FOLLOW:
                return JSONResponse({"ok": False, "message": "跟进状态仅支持：%s" % "/".join(_CUSTOMER_FOLLOW)},
                                    status_code=400)
            fields["跟进状态"] = req.follow_status
        if req.channel is not None:
            if req.channel and req.channel not in _CUSTOMER_CHANNELS:
                return JSONResponse({"ok": False, "message": "来源渠道仅支持：%s" % "/".join(_CUSTOMER_CHANNELS)},
                                    status_code=400)
            fields["来源渠道"] = req.channel
        if not fields:
            return {"ok": True, "message": "无需要更新的内容"}
        business.update_customer(record_id, fields)
        return {"ok": True}
    except Exception as e:
        print(f"[customers] 更新失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"客户档案更新失败：{e}"}, status_code=502)


@app.get("/api/mails")
async def api_mails_list(request: Request, lead_id: str = ""):
    """邮件记录列表（复用飞书「邮件记录」表）。可按 ?lead_id= 筛选。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    try:
        from app import business
        items = business.fetch_mails(force_refresh=True, lead_id=lead_id or None)
        items.sort(key=lambda m: m.get("time") or "", reverse=True)
        return {"ok": True, "items": items, "total": len(items)}
    except Exception as e:
        print(f"[mails] 列表读取失败: {e}")
        return JSONResponse({"ok": False, "message": f"邮件记录读取失败：{e}"}, status_code=502)


@app.post("/api/mails/send")
async def api_mails_send(req: MailSendRequest, request: Request):
    """SMTP SSL 真实发信并落「邮件记录」。
    邮箱未配置（地址/授权码缺失）返回 503，前端提示去配置中心。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    to_addr = (req.to or "").strip()
    subject = (req.subject or "").strip()
    body = (req.body or "").strip()
    if not to_addr or not subject or not body:
        return JSONResponse({"ok": False, "message": "收件人、主题、正文均不能为空"}, status_code=400)
    try:
        from app import business
        rec = business.send_email(to_addr, subject, body,
                                  lead_id=(req.lead_id or "").strip(),
                                  customer_id=(req.customer_id or "").strip())
        return {"ok": True, "mail": rec}
    except business.EmailNotConfigured as e:
        return JSONResponse(
            {"ok": False, "message": str(e), "code": "EMAIL_NOT_CONFIGURED"},
            status_code=503)
    except Exception as e:
        print(f"[mails] 发信失败 -> {to_addr}: {e}")
        return JSONResponse({"ok": False, "message": f"邮件发送失败：{e}"}, status_code=502)


@app.post("/api/mails/sync")
async def api_mails_sync(request: Request):
    """IMAP 拉取最近 30 天收件箱邮件落库（按邮箱关联线索/客户，消息ID 去重）。
    邮箱未配置返回 503。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    try:
        from app import business
        result = business.sync_inbox()
        return {"ok": True, **result}
    except business.EmailNotConfigured as e:
        return JSONResponse(
            {"ok": False, "message": str(e), "code": "EMAIL_NOT_CONFIGURED"},
            status_code=503)
    except Exception as e:
        print(f"[mails] 收件同步失败: {e}")
        return JSONResponse({"ok": False, "message": f"收件同步失败：{e}"}, status_code=502)


@app.get("/api/pi")
async def api_pi_list(request: Request):
    """PI 订单列表（复用飞书「订单表」，幂等补 PI 字段）。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    try:
        from app import business
        items = business.fetch_pi(force_refresh=True)
        items.sort(key=lambda p: p.get("created_at") or "", reverse=True)
        return {"ok": True, "items": items, "total": len(items),
                "status_options": list(business.PI_STATUS_OPTIONS)}
    except Exception as e:
        print(f"[pi] 列表读取失败: {e}")
        return JSONResponse({"ok": False, "message": f"PI 列表读取失败：{e}"}, status_code=502)


@app.post("/api/pi")
async def api_pi_create(req: PiUpsertRequest, request: Request):
    """新建 PI（落订单表，PI状态=草稿/已发送等）。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    customer_name = (req.customer_name or "").strip()
    if not customer_name:
        return JSONResponse({"ok": False, "message": "客户名称不能为空"}, status_code=400)
    try:
        from app import business
        if req.status and req.status not in business.PI_STATUS_OPTIONS:
            return JSONResponse({"ok": False,
                                 "message": f"PI状态仅支持：{'/'.join(business.PI_STATUS_OPTIONS)}"},
                                status_code=400)
        now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        fields = {
            "订单号": (req.pi_no or "").strip() or f"PI-{now_str.replace('-', '')}",
            "客户名称": customer_name,
            "国家/地区": (req.region or "").strip(),
            "订单金额（原币）": (req.amount or "").strip(),
            "币种": (req.currency or "USD").strip(),
            "PI状态": (req.status or "草稿"),
            "产品明细": (req.products or "").strip(),
            "负责销售": (req.sales or user_info.get("name") or user_info.get("username", "")).strip(),
            "备注": (req.note or "").strip(),
            "下单日期": (req.created_at or now_str),
        }
        if req.customer_id:
            fields["关联客户"] = req.customer_id.strip()
        rec = business.create_pi(fields)
        return {"ok": True, "pi": rec}
    except Exception as e:
        print(f"[pi] 新建失败: {e}")
        return JSONResponse({"ok": False, "message": f"PI 创建失败：{e}"}, status_code=502)


@app.put("/api/pi/{record_id}")
async def api_pi_update(record_id: str, req: PiUpsertRequest, request: Request):
    """更新 PI（状态/金额/币种/备注等白名单）。
    PI状态=已成交/已取消时同步写履约「当前状态」，兼容订单分布统计。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    try:
        from app import business
        fields = {}
        if req.pi_no is not None and req.pi_no.strip():
            fields["订单号"] = req.pi_no.strip()
        if req.customer_name is not None and req.customer_name.strip():
            fields["客户名称"] = req.customer_name.strip()
        if req.region is not None:
            fields["国家/地区"] = req.region.strip()
        if req.amount is not None:
            fields["订单金额（原币）"] = req.amount.strip()
        if req.currency is not None:
            fields["币种"] = req.currency.strip()
        if req.products is not None:
            fields["产品明细"] = req.products.strip()
        if req.note is not None:
            fields["备注"] = req.note.strip()
        if req.sales is not None:
            fields["负责销售"] = req.sales.strip()
        if req.status is not None:
            status = req.status.strip()
            if status not in business.PI_STATUS_OPTIONS:
                return JSONResponse({"ok": False,
                                     "message": f"PI状态仅支持：{'/'.join(business.PI_STATUS_OPTIONS)}"},
                                    status_code=400)
            fields["PI状态"] = status
            if status in ("已成交", "已取消"):
                fields["当前状态"] = status
        if not fields:
            return {"ok": True, "message": "无需要更新的内容"}
        business.update_pi(record_id, fields)
        return {"ok": True}
    except Exception as e:
        print(f"[pi] 更新失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"PI 更新失败：{e}"}, status_code=502)


@app.get("/api/config")
async def api_config_get(request: Request):
    """系统配置读取。敏感项（邮箱授权码）仅 admin 返回，
    非 admin 以 ******** 掩码回显（用于判断是否已配置）。需登录。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    try:
        from app import business
        is_admin = user_info.get("role") == "admin"
        cfg = business.get_config(include_sensitive=is_admin)
        cfg["is_admin"] = is_admin
        return {"ok": True, "config": cfg}
    except Exception as e:
        print(f"[config] 读取失败: {e}")
        return JSONResponse({"ok": False, "message": f"配置读取失败：{e}"}, status_code=502)


@app.put("/api/config")
async def api_config_update(req: ConfigUpdateRequest, request: Request):
    """系统配置写入（仅 admin）。授权码传空串表示不修改（保留原值）。"""
    user_info, err = _business_auth(request)
    if err:
        return err
    if user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "仅管理员可修改系统配置"}, status_code=403)
    try:
        from app import business
        items = req.items or {}
        # 读取现有明文配置：授权码为掩码/空时保留原值，避免被 ******** 覆盖
        existing = business.get_config(include_sensitive=True)
        saved = []
        for k, v in items.items():
            k = (k or "").strip()
            if not k:
                continue
            v = v if isinstance(v, str) else str(v)
            if k in business.SENSITIVE_CONFIG_KEYS and (not v or v == "********"):
                if existing.get(k):
                    continue  # 保留已存授权码
            business.set_config_kv(k, v.strip())
            saved.append(k)
        return {"ok": True, "saved": saved}
    except Exception as e:
        print(f"[config] 写入失败: {e}")
        return JSONResponse({"ok": False, "message": f"配置保存失败：{e}"}, status_code=502)


try:
    from app import insights_store
    insights_store.start_scheduler()
except Exception as _e:
    print(f"[insights] 模块初始化失败: {_e}")

# 启动时后台预热飞书「系统账号」表（建表+种子数据），不阻塞服务启动
threading.Thread(target=_warmup_account_table, daemon=True).start()
# 启动时后台预热飞书「商机线索」表（自动建表），不阻塞服务启动
threading.Thread(target=_warmup_leads_table, daemon=True).start()

# Serve frontend - try multiple possible locations
_candidate_dirs = [
    os.path.join(os.path.dirname(__file__), "frontend"),                              # Railway root=backend/: /app/frontend
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend"),              # Railway root=repo: /backend/frontend
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend"),  # extra fallback
]
static_dir = None
for _d in _candidate_dirs:
    if os.path.isdir(_d):
        static_dir = _d
        break
# Serve generated uploads (transparent PNGs etc.) as static files
_uploads_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(_uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")

if static_dir:
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
