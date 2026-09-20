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
import asyncio

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
        coze_lead_score_workflow_id: str = os.getenv("COZE_LEAD_SCORE_WORKFLOW_ID", "7685777171490930688")
        coze_email_workflow_id: str = os.getenv("COZE_EMAIL_WORKFLOW_ID", "7685799159194239002")
        openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
        openai_text_model: str = os.getenv("OPENAI_TEXT_MODEL", "deepseek-chat")
        smtp_host: str = os.getenv("SMTP_HOST", "smtp.exmail.qq.com")
        smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
        smtp_user: str = os.getenv("SMTP_USER", "ellachen@rhcmed.com")
        smtp_password: str = os.getenv("SMTP_PASSWORD", "h4zJZ47A688cW6t9")
        smtp_from_name: str = os.getenv("SMTP_FROM_NAME", "RHC Veterinary Medical")
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
                raise HTTPException(status_code=502, detail=(result.get("error", "") or "动物素材生成失败，请稍后重试").replace("502: ", ""))
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
    "认领状态": "认领状态",
    "综合评分": "综合评分",
    "评级": "评级",
    "官网": "官网",
    "行业": "行业",
    "邮箱格式": "邮箱格式",
    "决策人": "决策人",
    "LinkedIn": "LinkedIn",
    "电话": "电话",
    "进口记录": "进口记录",
    "补搜状态": "补搜状态",
    "跟进状态": "跟进状态",
    "发件邮箱": "发件邮箱",
    "最近发信时间": "最近发信时间",
    "发信次数": "发信次数",
    "入池时间": "入池时间",
    "页面类型": "页面类型",
    "买家类型": "买家类型",
    "系统排除": "系统排除",
    "市场优先级": "市场优先级",
    "观察标签": "观察标签",
}
LEAD_ACTIVE_STATUS = ("跟进中", "已转客户")
LEAD_STATUS_OPTIONS = ("跟进中", "已转客户", "已释放")
LEAD_OPP_OPTIONS = ("诊所扩张", "招标采购", "展会机会", "渠道动态", "采购动态")
# 线索开发信跟进状态（单选）；新线索前端按「待开发」兜底展示
LEAD_FOLLOW_STATUS_OPTIONS = ("待开发", "开发信已发", "客户已回", "洽谈中", "已成交", "已搁置")

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
        {"field_name": "认领状态", "type": 3,    # 单选：未认领/已认领
         "property": {"options": [{"name": "未认领"}, {"name": "已认领"}]}},
        {"field_name": "综合评分", "type": 2},   # 数字（0-100）
        {"field_name": "评级", "type": 1},       # 文本：A/B/C，严格按 _grade_from_score(综合评分) 同源回写
        {"field_name": "官网", "type": 1},       # 文本
        {"field_name": "行业", "type": 1},       # 文本
        {"field_name": "邮箱格式", "type": 1},   # 文本
        {"field_name": "决策人", "type": 1},     # 文本
        {"field_name": "LinkedIn", "type": 1},   # 文本
        {"field_name": "电话", "type": 1},         # 文本：官网联系页提取的电话（含可拨 WhatsApp 的号码），不做格式校验
        {"field_name": "进口记录", "type": 1},   # 文本
        {"field_name": "补搜状态", "type": 3,    # 单选
         "property": {"options": [
             {"name": "未补搜"}, {"name": "轻补搜中"},
             {"name": "已轻补"}, {"name": "深度补搜中"},
             {"name": "已深度补全"}, {"name": "补搜失败"},
             {"name": "已检索·未找到联系方式"}, {"name": "需人工补全·官网反爬"}
         ]}},
        {"field_name": "跟进状态", "type": 3,    # 单选（开发信跟进）
         "property": {"options": [{"name": n} for n in LEAD_FOLLOW_STATUS_OPTIONS]}},
        {"field_name": "发件邮箱", "type": 1},   # 文本（实际发件销售邮箱）
        {"field_name": "最近发信时间", "type": 1},  # 文本
        {"field_name": "发信次数", "type": 2},   # 数字
        {"field_name": "入池时间", "type": 1},   # 文本（ISO 时间，线索进入公海池的时间戳）
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
    {"field_name": "认领状态", "type": 3,
     "property": {"options": [{"name": "未认领"}, {"name": "已认领"}]}},
    {"field_name": "综合评分", "type": 2},
    {"field_name": "评级", "type": 1},
    {"field_name": "官网", "type": 1},
    {"field_name": "行业", "type": 1},
    {"field_name": "邮箱格式", "type": 1},
    {"field_name": "决策人", "type": 1},
    {"field_name": "LinkedIn", "type": 1},
    {"field_name": "电话", "type": 1},
    {"field_name": "进口记录", "type": 1},
    {"field_name": "补搜状态", "type": 3,
     "property": {"options": [
         {"name": "未补搜"}, {"name": "轻补搜中"},
         {"name": "已轻补"}, {"name": "深度补搜中"},
         {"name": "已深度补全"}, {"name": "补搜失败"},
         {"name": "已检索·未找到联系方式"}, {"name": "需人工补全·官网反爬"}
     ]}},
    {"field_name": "跟进状态", "type": 3,
     "property": {"options": [{"name": n} for n in LEAD_FOLLOW_STATUS_OPTIONS]}},
    {"field_name": "发件邮箱", "type": 1},
    {"field_name": "最近发信时间", "type": 1},
    {"field_name": "发信次数", "type": 2},
    {"field_name": "入池时间", "type": 1},
    {"field_name": "页面类型", "type": 1},   # 文本：company/directory/b2b_platform/news_report/navigation/competitor/unknown
    {"field_name": "买家类型", "type": 1},   # 文本：importer/distributor/wholesaler/dealer/hospital/clinic/manufacturer/unknown（用文本避免单选枚举置空）
    {"field_name": "系统排除", "type": 1},   # 文本：AI/规则判定非买家时写排除原因（如 ai:not_company），公海池过滤；留空=正常。只标记不删除，可回滚
    {"field_name": "质量标记", "type": 1},   # 文本：一档规则直接定性跳过Coze时写 rule_finalized，便于误杀申诉回溯；空=经Coze或未落规则判定
    {"field_name": "市场优先级", "type": 3,
     "property": {"options": [{"name": n} for n in ("P0", "P1", "P2", "待识别")]}},
    {"field_name": "观察标签", "type": 1},   # 文本：human_medical_mixed 等，标注需观察但不立即排除的线索
]


# 非真实买家的 AI 分类结果：命中即系统排除（移出公海池，仅标记不删除）
_EXCLUDE_PAGE_TYPES = ("directory", "b2b_platform", "news_report", "navigation", "competitor")


def _ai_exclude_reason(page_type: str, buyer_type: str) -> str:
    """根据 AI 分类返回系统排除原因；属于真实买家返回空串。"""
    pt = (page_type or "").strip().lower()
    bt = (buyer_type or "").strip().lower()
    if pt in _EXCLUDE_PAGE_TYPES:
        return f"ai:{pt}"
    # 制造商/工厂是同行卖家而非采购方（company 页但 buyer_type=manufacturer 也排除）
    if bt == "manufacturer":
        return "ai:manufacturer"
    return ""


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


_legacy_score_migrated = False


async def _migrate_legacy_scores():
    """一次性静默迁移：把"可达性闸门"上线前遗留的虚高老分数校正。
    只处理 无任何联系方式（邮箱/官网/决策人/LinkedIn）且当前评分≥50 的线索，
    用最新逻辑重算并回写；校正后这些线索分数已<50，下次重启不再命中，故天然只跑一次、幂等。"""
    global _legacy_score_migrated
    if _legacy_score_migrated:
        return
    _legacy_score_migrated = True
    await asyncio.sleep(20)  # 等飞书表预热完成，避免与启动建表争抢
    try:
        leads = _fetch_leads(force_refresh=True)
    except Exception as e:
        print(f"[score-migrate] 读取线索失败，跳过本次迁移: {e}")
        return
    fixed = 0
    for ld in leads:
        rid = ld.get("record_id", "")
        if not rid or _lead_is_reachable(ld):
            continue
        try:
            old = int(float(ld.get("综合评分") or 0))
        except (TypeError, ValueError):
            continue
        if old < 50:
            continue  # 已是低分，无需修正
        try:
            new_score, page_type, buyer_type = await call_coze_scoring_workflow(ld)
            upd = {"综合评分": new_score, "评级": _grade_from_score(new_score)}
            if page_type:
                upd["页面类型"] = page_type
            if buyer_type:
                upd["买家类型"] = buyer_type
            if new_score != old or page_type or buyer_type:
                _update_leads_record(rid, upd)
                if new_score != old:
                    fixed += 1
                print(f"[score-migrate] 校正虚高分：{ld.get('公司/机构','')} {old} -> {new_score}（{page_type}/{buyer_type}）")
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"[score-migrate] 单条校正失败（{rid}）: {e}")
    if fixed:
        _invalidate_leads_cache()
    print(f"[score-migrate] 历史评分迁移完成，共校正 {fixed} 条不可触达线索")


async def _reset_stuck_enrichment():
    """启动时复位因部署/重启被杀掉的在途补搜任务。
    线上线程在 轻补搜中/深度补搜中 被中断后，状态会永久滞留导致前端转圈。
    重启即代表没有任何在途任务，故把滞留的"补搜中"统一复位为"未补搜"，允许重试。幂等。"""
    await asyncio.sleep(15)  # 等飞书表预热完成
    try:
        leads = _fetch_leads(force_refresh=True)
    except Exception as e:
        print(f"[enrich-reset] 读取线索失败，跳过复位: {e}")
        return
    stuck_states = {"轻补搜中", "深度补搜中"}
    reset = 0
    for ld in leads:
        rid = ld.get("record_id", "")
        status = (str(ld.get("补搜状态") or "")).strip()
        if not rid or status not in stuck_states:
            continue
        try:
            _update_leads_record(rid, {"补搜状态": "未补搜"})
            reset += 1
            print(f"[enrich-reset] 复位卡死补全：{ld.get('公司/机构','')}（{status} -> 未补搜）")
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"[enrich-reset] 单条复位失败（{rid}）: {e}")
    if reset:
        _invalidate_leads_cache()
    print(f"[enrich-reset] 补全状态复位完成，共复位 {reset} 条卡死线索")


def _norm_lead_record(rec: dict) -> dict:
    """飞书记录 -> 归一化字段（英文短 key 供前端使用，另附 record_id）。"""
    fl = rec.get("fields", {})
    out = {"record_id": rec.get("record_id", "")}
    for short, full in LEADS_FIELD_MAP.items():
        out[short] = _tv(fl.get(full))
    # 市场优先级：优先读 bitable 字段值，否则从地区字段实时计算
    mp = out.get("市场优先级", "")
    if not mp or mp not in ("P0", "P1", "P2", "待识别"):
        out["市场优先级"] = _get_market_priority(out.get("地区", ""))
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
    exclusion: Optional[str] = None
    region: Optional[str] = None
    observation: Optional[str] = None


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
        if req.exclusion is not None:
            fields["系统排除"] = req.exclusion.strip()
        if req.region is not None:
            fields["地区"] = req.region.strip()
        if req.observation is not None:
            fields["观察标签"] = req.observation.strip()
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
_BAD_SITE_MARKS = ("duckduckgo.com", "duck.com", "google.com/search", "bing.com/search",
                   "facebook.com/search", "amazon.com/s", "youtube.com/results")
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
    """抓官网首页 + 首页中联系/关于链接（每域名最多 5 页），返回 [{url, html}]。
    支持多语言联系页路径（/kontakt, /contatti, /contacto 等）及导航中发现的更多联系页。"""
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
    # 从首页提取联系/关于链接（支持多语言）
    sub_links = []
    seen = {pages[0]["url"]}
    for m in re.finditer(r'href=["\']([^"\']+)["\']', pages[0]["html"], re.I):
        link = m.group(1).strip()
        low = link.lower()
        if not re.search(r'(/contact|contact-|/about|about-|contactus|/kontakt|/contatti|/contacto|/contactez-nous|get-in-touch|reach-us|/imprint|/impressum)', low):
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
        if len(sub_links) >= 4:  # 首页 + 4 个子页 = 每域名最多 5 页
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
    """从单页 HTML 提取邮箱并登记来源（out 追加，seen 去重）。
    增强：Cloudflare cf-email 解码 + [at][dot] 混淆还原 + HTML 实体反转义。"""
    try:
        import html as _html_mod
        txt = _html_mod.unescape(html_text or "")
        raw_emails = set(_EMAIL_RE.findall(txt))
        # Cloudflare cf-email 解码
        for mm in _CF_EMAIL_RE.finditer(html_text or ""):
            d = _decode_cf_email(mm.group(1))
            if "@" in d:
                raw_emails.add(d)
        # [at][dot] 混淆还原
        for mm in _AT_DOT_RE.finditer(txt):
            cand = f"{mm.group(1)}@{mm.group(2)}.{mm.group(3)}"
            raw_emails.add(cand)
        for em in raw_emails:
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
    import os as _os
    if url.startswith("/") and not url.startswith("//"):
        from fastapi.responses import FileResponse
        _base = _os.path.dirname(_os.path.dirname(__file__))
        _safe = _os.path.normpath(_os.path.join(_base, url.lstrip("/")))
        _uploads = _os.path.join(_base, "uploads")
        if _safe.startswith(_uploads + _os.sep) and _os.path.isfile(_safe):
            return FileResponse(_safe)
        raise HTTPException(status_code=404, detail="local file not found")
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

# ============================================================
# 主动获客搜索引擎（POST /api/leads/search）
# 用 DuckDuckGo 搜索潜在客户（进口商/经销商/动物医院），
# 去重已有线索，自动质量评级（A/B/C），搜索完自动推送消息通知。
# ============================================================
_SEARCH_PRODUCTS = [
    "veterinary anesthesia machine",
    "vet ventilator",
    "veterinary injection pump",
    "veterinary patient monitor",
    "veterinary surgical equipment",
    "animal hospital equipment",
]
_SEARCH_COUNTRIES = {
    # 南美
    "Brazil": "南美", "Argentina": "南美", "Colombia": "南美", "Chile": "南美",
    # 非洲
    "Nigeria": "非洲", "Kenya": "非洲", "South Africa": "非洲", "Egypt": "非洲",
    "Ghana": "非洲", "Tanzania": "非洲",
    # 欧洲
    "Germany": "欧洲", "France": "欧洲", "UK": "欧洲", "Spain": "欧洲",
    "Italy": "欧洲", "Netherlands": "欧洲",
    # 东欧（2026-09 分布校准轮换补充）
    "Poland": "东欧", "Romania": "东欧", "Czech Republic": "东欧",
    "Hungary": "东欧", "Ukraine": "东欧",
    # 东南亚
    "Thailand": "东南亚", "Vietnam": "东南亚", "Philippines": "东南亚",
    "Indonesia": "东南亚", "Malaysia": "东南亚", "Myanmar": "东南亚",
}

# ============================================================
# 市场优先级映射（IT 2026-09-20：评分维度解耦）
# P0：核心目标市场（4 大区 23 国）
# P1：成熟市场（澳新加美日韩 + 中东富裕国）
# P2：其他市场
# ============================================================
_MARKET_PRIORITY_P0 = {
    # 非洲（10）
    "South Africa", "Kenya", "Tanzania", "Ghana", "Nigeria",
    "Egypt", "Ethiopia", "Uganda", "Morocco", "Senegal",
    # 东南亚（6）
    "Thailand", "Vietnam", "Indonesia", "Philippines", "Malaysia", "Myanmar",
    # 南亚（4）
    "India", "Pakistan", "Bangladesh", "Sri Lanka",
    # 中东（3）
    "UAE", "Saudi Arabia", "Turkey",
    # 欧洲（7）
    "Germany", "France", "United Kingdom", "Spain", "Italy", "Poland", "Netherlands",
}
_MARKET_PRIORITY_P1 = {
    # 大洋洲
    "Australia", "New Zealand",
    # 北美
    "Canada", "United States",
    # 东亚
    "Japan", "South Korea",
    # 中东富裕国
    "Qatar", "Kuwait", "Oman",
}


def _get_market_priority(country: str) -> str:
    """根据国家名称返回市场优先级 P0 / P1 / P2 / 待识别。
    country 可以是英文国家名（如 'South Africa'）或中文区域-国家格式（如 '非洲 - South Africa'）。"""
    if not country:
        return "待识别"
    # 处理 "区域 - 国家" 格式
    c = country.strip()
    if " - " in c:
        c = c.split(" - ", 1)[1].strip()
    # 未知/空值归为待识别
    if not c or c in ("未知", "unknown", ""):
        return "待识别"
    # 标准化别名
    aliases = {"UK": "United Kingdom", "USA": "United States", "US": "United States"}
    c = aliases.get(c, c)
    if c in _MARKET_PRIORITY_P0:
        return "P0"
    if c in _MARKET_PRIORITY_P1:
        return "P1"
    return "P2"


_RHC_PRODUCTS = [
    "RHC-V500 兽用麻醉机", "RHC-V300 兽用呼吸机",
    "RHC-IP600 兽用注射泵", "RHC-PM800 兽用监护仪",
    "RHC-SE200 兽用手术设备", "RHC-AH 动物医院整体方案",
]

# ===== 精准搜索：行业关键词组合（site: 语法对行业网站无效，改回通用搜索+精准词） =====
# 策略：用高意图搜索词（含 importer/distributor/hospital/clinic）+ 目标市场
# site: 仅保留 B2B 平台（MedicalExpo/Kompass 的搜索结果本身就是公司列表）

_TARGET_SITES = []  # B2B平台 site: 搜索结果多为产品/目录页而非真实公司，已弃用

_search_results_cache = {"data": None, "ts": 0.0}
_SEARCH_CACHE_TTL = 30  # 搜索结果30秒缓存
# P2：每类丢弃原因落「搜索丢弃日志」时最多保留的样本条数（控制写库量）
_DROP_SAMPLE_PER_REASON = 5
_DROP_LOG_TABLE_NAME = "搜索丢弃日志"
# 丢弃原因英文键 → 中文标签（落「搜索丢弃日志」用人读标签）
_DROP_LABELS = {
    "gov": "政府/教育/官方贸易页", "seller": "卖家货架/经销栏目页",
    "platform": "B2B/电商撮合平台", "secondhand": "二手/翻新设备",
    "manufacturer": "工厂/制造商卖家", "non_company": "非公司主体页",
    "dirty": "脏公司名/搜索词短语",
}
# 丢弃原因 → 精确规则名（落「搜索丢弃日志」rule_name，便于误杀时定位具体规则）
_DROP_RULE_NAMES = {
    "gov": "rule_gov_edu_url",
    "seller": "rule_seller_or_section_url",
    "platform": "rule_platform_title",
    "secondhand": "rule_secondhand_result",
    "manufacturer": "rule_manufacturer_title",
    "non_company": "rule_non_company_title",
    "dirty": "rule_dirty_company_name",
}
_drop_log_table_id = None


def _ensure_drop_log_table():
    """确保飞书存在「搜索丢弃日志」表（P2：丢弃明细可追溯），返回 table_id。懒加载单例。"""
    global _drop_log_table_id
    if _drop_log_table_id:
        return _drop_log_table_id
    if not FEISHU_ATK:
        raise RuntimeError("FEISHU_APP_TOKEN 未配置")
    resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables?page_size=100")
    for t in resp.get("data", {}).get("items", []):
        if t.get("name") == _DROP_LOG_TABLE_NAME:
            _drop_log_table_id = t.get("table_id")
            return _drop_log_table_id
    fields = [
        {"field_name": "标题", "type": 1},    # 文本（主字段）
        {"field_name": "丢弃原因", "type": 1},
        {"field_name": "规则名", "type": 1},   # 精确规则名，如 rule_secondhand_title
        {"field_name": "本轮挡掉总数", "type": 1},  # 该轮该原因挡掉的总数（不止样本5条）
        {"field_name": "链接", "type": 1},
        {"field_name": "命中搜索词", "type": 1},
        {"field_name": "轮次", "type": 1},
        {"field_name": "记录时间", "type": 1},
    ]
    resp = _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables",
                       {"table": {"name": _DROP_LOG_TABLE_NAME,
                                  "default_view_name": "丢弃明细",
                                  "fields": fields}})
    _drop_log_table_id = resp.get("data", {}).get("table_id")
    if not _drop_log_table_id:
        raise RuntimeError(f"创建「{_DROP_LOG_TABLE_NAME}」表失败: {resp}")
    print(f"[leads] 已创建飞书表「{_DROP_LOG_TABLE_NAME}」: {_drop_log_table_id}")
    return _drop_log_table_id


def _persist_drop_logs(search_diag: dict, round_id: int):
    """P2：把本轮丢弃样本批量写入飞书「搜索丢弃日志」。best-effort，任何异常只记录不影响主流程。"""
    try:
        samples = (search_diag or {}).get("drop_samples") or {}
        if not samples:
            return 0
        tid = _ensure_drop_log_table()
        now_iso = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        # 各原因本轮挡掉总数（与 stats 同口径），写进每条样本行，便于直接汇总
        _count_keys = {
            "gov": "gov_dropped", "seller": "seller_dropped", "platform": "platform_dropped",
            "secondhand": "secondhand_dropped", "manufacturer": "manufacturer_dropped",
            "non_company": "non_company_dropped", "dirty": "dirty_dropped",
        }
        records = []
        for reason_key, items in samples.items():
            label = _DROP_LABELS.get(reason_key, reason_key)
            rule_name = _DROP_RULE_NAMES.get(reason_key, reason_key)
            total_n = search_diag.get(_count_keys.get(reason_key, ""), len(items))
            for it in items[:_DROP_SAMPLE_PER_REASON]:
                records.append({"fields": {
                    "标题": (it.get("title") or "(无标题)")[:200],
                    "丢弃原因": label,
                    "规则名": rule_name,
                    "本轮挡掉总数": str(total_n),
                    "链接": (it.get("url") or "")[:500],
                    "命中搜索词": (it.get("query") or "")[:200],
                    "轮次": str(round_id),
                    "记录时间": now_iso,
                }})
        if not records:
            return 0
        # 飞书批量建记录上限 500/次，这里每类最多5条×7类≤35，单次足够
        _feishu_api("POST",
                    f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/batch_create",
                    {"records": records})
        print(f"[leads] 第{round_id}轮丢弃明细落盘 {len(records)} 条 → 「{_DROP_LOG_TABLE_NAME}」")
        return len(records)
    except Exception as de:
        print(f"[leads] 丢弃明细落盘失败（不影响搜索）: {de}")
        return 0


def _build_search_queries(custom_queries: Optional[list] = None):
    """构建搜索词列表：通用搜索用高意图关键词（importer/distributor/hospital + 目标市场）
    每次调用时随机打乱顺序，保证多轮搜索能发现不同线索。
    custom_queries 非空时直接用管理员给的原始词（轮换国家/产品线/意图），只统一追加负词。"""
    import random
    # 统一追加：头部竞品 -site: 排除 + 二手/翻新精确短语排除
    _neg_suffix = f"{_COMPETITOR_SITE_SUFFIX} {_SECONDHAND_SUFFIX}"
    if custom_queries:
        return [f"{q} {_neg_suffix}" for q in custom_queries if str(q).strip()]
    queries = []

    # ===== 高意图通用搜索 =====
    # 核心产品 + importer + 目标市场（最直接的客户线索）
    for product in _SEARCH_PRODUCTS[:4]:
        for country in list(_SEARCH_COUNTRIES.keys())[:6]:
            queries.append(f'"{product}" importer {country}')
    # 经销商搜索
    for product in _SEARCH_PRODUCTS[:3]:
        for country in list(_SEARCH_COUNTRIES.keys())[6:12]:
            queries.append(f'"{product}" distributor {country}')
    # 动物医院/诊所搜索（买家主体）
    for country in list(_SEARCH_COUNTRIES.keys())[:10]:
        queries.append(f'veterinary hospital equipment supplier {country}')
        queries.append(f'veterinary clinic supply {country}')

    # ===== 补充多样化查询（扩展覆盖面） =====
    # 制造商/供应商搜索
    for product in _SEARCH_PRODUCTS[:3]:
        for country in random.sample(list(_SEARCH_COUNTRIES.keys()), min(6, len(_SEARCH_COUNTRIES))):
            queries.append(f'"{product}" supplier OR manufacturer {country}')
    # 采购/经销相关
    for country in list(_SEARCH_COUNTRIES.keys())[4:14]:
        queries.append(f'veterinary medical equipment dealer {country}')
        queries.append(f'animal health products distributor {country}')

    # 统一追加：头部竞品 -site: 排除 + 二手/翻新精确短语排除
    _neg_suffix = f"{_COMPETITOR_SITE_SUFFIX} {_SECONDHAND_SUFFIX}"
    queries = [f"{q} {_neg_suffix}" for q in queries]

    # 随机打乱顺序，每次搜索覆盖不同组合
    random.shuffle(queries)
    return queries


# 主动获客抓取层诊断（最近一次搜索各引擎真实状态），供API返显到界面
_lead_search_diag = {"engine_status": {}, "last_ok_engine": "", "ts": 0.0}
_search_in_progress = False  # 搜索并发锁：防止两次搜索互相干扰
# 轮次代号：每次启动搜索自增。旧轮次的后台线程（评分/补搜）若仍在跑，
# 只能更新飞书，不得再覆盖全局 _lead_search_job / 锁 / _last_search_record_ids，
# 防止"新轮已开始、旧线程把 stats/结果串台写回"。
_search_round = 0
_last_search_record_ids: list[str] = []  # 上一次搜索结果中的 record_id 列表，用于下次搜索时推入公海池

# 异步搜索任务状态：POST 立即启动后台线程并返回，前端轮询 /status 直到 finished/error。
# 这样全网搜索（Brave 1QPS，常 60s+）不会被平台网关按长请求掐断。
_lead_search_job = {
    "running": False,       # 后台线程是否仍在执行（含评分+补搜）
    "searching": False,     # 是否仍在"全网搜索"阶段（评分/补搜阶段为 False）
    "status": "idle",       # idle / running / finished / error
    "phase": "",            # 人读进度文案
    "done_queries": 0,
    "total_queries": 0,
    "result": None,         # 完成后的响应载荷（items/stats/empty_reason/diag）
    "error": "",
    "ts": 0.0,
}


def _http_fetch_with_headers(url, headers, timeout=6, data=None):
    """统一HTTP抓取，返回 (http_code, body_text)；HTTPError也读body，其它异常上抛。"""
    import urllib.request as _ur
    import urllib.error as _ue
    payload = data.encode("utf-8") if isinstance(data, str) else data
    req = _ur.Request(url, headers=headers, data=payload,
                      method="POST" if payload is not None else "GET")
    try:
        with _ur.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), r.read(1_500_000).decode("utf-8", "ignore")
    except _ue.HTTPError as e:
        try:
            body = e.read(300_000).decode("utf-8", "ignore")
        except Exception:
            body = ""
        return e.code, body


def _parse_ddg_html_results(html_text: str) -> list:
    """解析 DuckDuckGo HTML 结果页，按 result__body 块配对标题与摘要（兼容属性顺序变化）。"""
    results = []
    parts = re.split(r'<div[^>]+class="[^"]*result__body', html_text)
    for block in parts[1:]:
        block = block[:4000]
        mt = re.search(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.I | re.S)
        if not mt:
            continue
        href = _ddg_real_url(mt.group(1))
        title = re.sub(r'<[^>]+>', '', mt.group(2)).strip()
        if not href or not title:
            continue
        snippet = ""
        ms = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|span)>',
                       block, re.I | re.S)
        if ms:
            snippet = re.sub(r'<[^>]+>', '', ms.group(1)).strip()[:300]
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= 8:
            break
    return results


def _parse_bing_html_results(html_text: str) -> list:
    """解析 Bing 结果页 b_algo 块，取真实外链标题与摘要，过滤搜索引擎/社媒/门户。"""
    from urllib.parse import urlparse
    results = []
    skip_hosts = ("bing.com", "microsoft.com", "msn.com", "go.microsoft", "duckduckgo.com",
                  "google.com", "facebook.com", "youtube.com", "instagram.com",
                  "twitter.com", "x.com", "tiktok.com", "linkedin.com")
    for block in re.split(r'<li[^>]+class="[^"]*b_algo', html_text)[1:]:
        block = block[:5000]
        mt = re.search(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                       block, re.I | re.S)
        if not mt:
            continue
        href = mt.group(1)
        title = re.sub(r'<[^>]+>', '', mt.group(2)).strip()
        try:
            host = urlparse(href).netloc.lower()
        except Exception:
            host = ""
        if not title or not host or any(b in host for b in skip_hosts):
            continue
        snippet = ""
        ms = re.search(r'<p[^>]*>(.*?)</p>', block, re.I | re.S)
        if ms:
            snippet = re.sub(r'<[^>]+>', '', ms.group(1)).strip()[:300]
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= 8:
            break
    return results


# ===== Brave 熔断（防止额度到顶后在免费引擎上干等、防止超支）=====
class BraveBillingError(Exception):
    """401/402：欠费/未授权，直接终止本轮、熔断，不重试。"""


class BraveRateLimitError(Exception):
    """429：限流，单条退避重试1次；持续则熔断本轮。"""


class BraveCircuitOpenError(Exception):
    """熔断已打开，本次直接跳过 Brave（区别于本次请求刚触发的欠费/限流）。"""


# 内存熔断态（重启清空；充值后调 reset 端点即可恢复）
_brave_breaker = {
    "open": False,          # 是否熔断中
    "reason": "",           # billing / rate_limited
    "tripped_at": "",       # 最近一次熔断时间
    "last_status": None,    # 最近一次 HTTP 状态码
    "remaining": None,      # Brave 返回的剩余额度（若响应头带）
    "trip_count": 0,        # 本进程累计熔断次数
}
# 免费引擎回退的硬上限（熔断/限流触发时）：最多若干 query、总耗时封顶
_FREE_FALLBACK_MAX_QUERIES = 3
_FREE_FALLBACK_BUDGET_SEC = 30.0
_BRAVE_TRIP_LOG_TABLE_NAME = "Brave熔断日志"


def _trip_brave_breaker(reason: str, status=None, remaining=None, persist=True):
    """熔断置位 + 打印告警 + 落盘一条熔断日志。reason: billing/rate_limited。"""
    now_iso = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    _brave_breaker.update({
        "open": True, "reason": reason, "tripped_at": now_iso,
        "last_status": status, "remaining": remaining,
        "trip_count": _brave_breaker["trip_count"] + 1,
    })
    print(f"[brave-circuit] ⚠️ 熔断 reason={reason} http={status} "
          f"剩余额度={remaining} 时间={now_iso} 第{_brave_breaker['trip_count']}次")
    if persist:
        try:
            _persist_brave_trip(reason, status, remaining, now_iso)
        except Exception as e:
            print(f"[brave-circuit] 熔断日志落盘失败: {e}")


def reset_brave_breaker():
    _brave_breaker.update({"open": False, "reason": "", "last_status": None, "remaining": None})
    print("[brave-circuit] 熔断已人工复位")


def _ensure_brave_trip_table():
    """飞书「Brave熔断日志」表（幂等）：时间/原因/HTTP状态/剩余额度/进程内第几次/备注。"""
    app_token = FEISHU_ATK
    def _list_tables():
        resp = _feishu_api("GET", f"/bitable/v1/apps/{app_token}/tables?page_size=200")
        return ((resp.get("data") or {}).get("items") or []) if isinstance(resp, dict) else []
    tid = None
    try:
        for t in _list_tables():
            if t.get("name") == _BRAVE_TRIP_LOG_TABLE_NAME:
                tid = t.get("table_id"); break
    except Exception:
        tid = None
    if not tid:
        resp = _feishu_api("POST", f"/bitable/v1/apps/{app_token}/tables",
                           {"table": {"name": _BRAVE_TRIP_LOG_TABLE_NAME,
                                      "default_view_name": "全部",
                                      "fields": [{"field_name": "时间", "type": 1},
                                                 {"field_name": "原因", "type": 1},
                                                 {"field_name": "HTTP状态", "type": 1},
                                                 {"field_name": "Brave剩余额度", "type": 1},
                                                 {"field_name": "进程内第几次", "type": 1},
                                                 {"field_name": "备注", "type": 1}]}})
        tid = (((resp or {}).get("data") or {}).get("table_id")) if isinstance(resp, dict) else None
    return tid


def _persist_brave_trip(reason, status, remaining, now_iso):
    tid = _ensure_brave_trip_table()
    if not tid:
        return False
    label = {"billing": "欠费/未授权(401/402)，已终止本轮",
             "rate_limited": "限流(429)，本轮熔断"}.get(reason, reason)
    _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records",
                {"fields": {"时间": now_iso, "原因": label,
                            "HTTP状态": str(status if status is not None else ""),
                            "Brave剩余额度": str(remaining) if remaining is not None else "(未返回)",
                            "进程内第几次": str(_brave_breaker["trip_count"]),
                            "备注": "搜索已快速结束，未在免费引擎上长时间重试"}})
    return True


def _brave_api_search(query: str, timeout: int = 8) -> list:
    """Brave Search API（独立索引、机房IP不被封）。需环境变量 BRAVE_API_KEY。
    返回 [{title, url, snippet}]；未配置 key 抛 RuntimeError 由上层回退。
    401/402 抛 BraveBillingError；429 退避重试1次后抛 BraveRateLimitError。"""
    import urllib.parse as _up
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("BRAVE_API_KEY 未配置")
    qs = _up.urlencode({
        "q": query, "count": 20, "country": "US",
        "safesearch": "off", "result_filter": "web",
    })
    url = "https://api.search.brave.com/res/v1/web/search?" + qs
    # 熔断打开期间不再打 Brave（充值后由 reset/强制跑 端点复位）
    if _brave_breaker.get("open"):
        raise BraveCircuitOpenError("Brave 熔断中，跳过")
    import urllib.request as _bur
    import urllib.error as _berr
    import time as _time
    _headers = {"Accept": "application/json",
                "X-Subscription-Token": key, "User-Agent": _FIND_UA}

    def _do_request():
        req = _bur.Request(url, headers=_headers, method="GET")
        try:
            with _bur.urlopen(req, timeout=timeout) as r:
                hd = {k.lower(): v for k, v in r.headers.items()}
                return getattr(r, "status", 200), r.read(1_500_000).decode("utf-8", "ignore"), hd
        except _berr.HTTPError as e:
            hd = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            try:
                bd = e.read(300_000).decode("utf-8", "ignore")
            except Exception:
                bd = ""
            return e.code, bd, hd

    code, body, resp_headers = _do_request()
    remaining = resp_headers.get("x-ratelimit-remaining")
    if code in (401, 402):
        # 欠费/未授权：不重试，直接上抛由整轮熔断
        raise BraveBillingError(f"Brave http{code}")
    if code == 429:
        # 限流：退避 2 秒，仅重试 1 次
        _time.sleep(2)
        code, body, resp_headers = _do_request()
        remaining = resp_headers.get("x-ratelimit-remaining")
        if code in (401, 402):
            raise BraveBillingError(f"Brave http{code}")
        if code == 429:
            raise BraveRateLimitError("Brave 429 退避重试后仍限流")
    if code != 200 or not body:
        raise RuntimeError(f"Brave http{code}")
    if remaining is not None:
        _brave_breaker["remaining"] = remaining
    data = json.loads(body)

    # ① 优先取 web.results（标准路径）
    web_results = (data.get("web") or {}).get("results") or []

    # ② 兜底：新版 mixed 引用结构 — mixed.main 条目通过 type+index 指向顶级数组
    if not web_results:
        mixed = data.get("mixed") or {}
        main_items = mixed.get("main") or []
        for item in main_items:
            rtype = item.get("type", "")
            idx = item.get("index")
            section = (data.get(rtype) or {}).get("results") or []
            if idx is not None and 0 <= idx < len(section):
                web_results.append(section[idx])
            else:
                r = item.get("result") or {}
                if r.get("url") and r.get("title"):
                    web_results.append(r)

    # ③ 诊断：结果为空时打印完整响应结构（截断）
    if not web_results:
        body_preview = body[:800] if body else "(empty)"
        print(f"[brave-diag] query={query[:80]} code={code} "
              f"response_keys={list(data.keys())} "
              f"web_keys={list((data.get('web') or {}).keys()) if data.get('web') else 'None'} "
              f"mixed_keys={list((data.get('mixed') or {}).keys())} "
              f"body_preview={body_preview}")

    out = []
    for it in web_results:
        u = (it.get("url") or "").strip()
        t = (it.get("title") or "").strip()
        if u and t:
            out.append({"title": t, "url": u,
                        "snippet": (it.get("description") or "")[:300]})
    return out


# 主动获客结果域名黑名单：B2B贸易目录/聚合站、社媒、电商、新闻、招聘等非企业官网。
# 这些站点既不是可开发的买家，也产生 "Global ... trade data" 这类脏公司名。
# 竞品/同行域名：命中即视为同行而非潜在买家，直接从线索中排除（可审计，见丢弃日志）。
_COMPETITOR_HOST_MARKS = (
    "mindrayanimal.com", "mindray.com", "dremed.com", "dreveterinary.com",
    "vetlandmedical.com", "vetland.com", "lifesupport.in", "eickemeyer.com",
    "kruuse.com", "dispomed.com", "vetamac.com", "infiniumvet.com",
    "gradymedical.com", "rwdstco.com", "rwdlife.com", "midmark.com",
    # 全球动物保健/兽医设备制造商（主体身份即厂家，非采购方；host-only 匹配，
    # 经销商页面"提到"这些品牌不受影响）。与 Coze 分类 prompt 的 competitor 名单保持一致
    "zoetis.com", "zoetis.", "msd-animal-health.com", "msd-animal-health.",
    "merck-animal-health.com", "merck-animal-health.", "elanco.com", "elanco.",
    "ceva.com", "ceva.", "virbac.com", "virbac.", "boehringer-ingelheim.com",
    "boehringer-ingelheim.", "dechra.com", "dechra.", "vetoquinol.com",
    "vetoquinol.", "hipra.com", "hipra.",
)

# 搜索 query 只挂头部竞品的 -site: 排除（Brave 对 query 长度有限制），
# 其余竞品统一交给上面的后置域名过滤兜底。
_COMPETITOR_SITE_EXCLUDE = (
    "mindrayanimal.com", "kruuse.com", "dremed.com",
    "vetlandmedical.com", "eickemeyer.com",
)
# 拼装一次复用：-site:mindrayanimal.com -site:kruuse.com ...
_COMPETITOR_SITE_SUFFIX = " ".join(f"-site:{d}" for d in _COMPETITOR_SITE_EXCLUDE)

# 二手/翻新精确短语排除（不用裸 -used，避免误伤 "used by veterinarians" 等正常公司页）
_SECONDHAND_SUFFIX = '-"used equipment" -"second hand" -"second-hand" -"pre-owned" -refurbished'

_JUNK_HOST_MARKS = (
    # B2B / 贸易数据 / 黄页目录
    "volza.com", "turkishexporter.net", "exportersindia.com", "tradeindia.com",
    "indiamart.com", "go4worldbusiness.com", "alibaba.com", "made-in-china.com",
    "globalsources.com", "tradekey.com", "importgenius.com", "panjiva.com",
    "zauba.com", "52wmb.com", "seair.co.in", "exportgenius", "ec21.com",
    "kompass.com", "europages.com", "thomasnet.com", "yellowpages", "yelp.com",
    "dnb.com", "tridge.com", "coimex", "ambalaj", "customs.info", "exim.com",
    "bizearch", "companylist", "hotfrog", "cylex", "brownbook", "findyello",
    "businesslist", "listcompany", "companieslist",
    # 电商 / 零售平台
    "amazon.", "ebay.", "etsy.com", "aliexpress.com", "walmart.com",
    # 二手设备交易平台 / 分类信息广告
    "equipnet.com", "3diequipment.com", "intriquip.com", "usedvetequipment.com",
    "machineseeker.", "vetecom.co.uk", "dotmed.com", "labx.com", "kitmondo.com",
    "gumtree.", "quoka.", "craigslist.org", "facebook.com/marketplace",
    # 通用兽医供应商目录/黄页（非单一公司主体）
    "vetsuppliersdirectory", "suppliersdirectory",
    "veterinarydirectory", "vetdirectory",
    # 社媒 / 内容 / 论坛
    "linkedin.com", "facebook.com", "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "pinterest.com", "reddit.com",
    "quora.com", "medium.com", "wordpress.com", "blogspot.com", "tumblr.com",
    "wikipedia.org", "wikidata.org", "glassdoor", "indeed.com", "naukri.com",
    "jobstreet", "glassdoor", "trustpilot.com", "crunchbase.com", "bloomberg.com",
    "reuters.com", "prnewswire.com", "businesswire.com", "wsj.com", "ft.com",
    "forbes.com", "marketresearch", "grandviewresearch", "fortunebusinessinsights",
    # 搜索引擎自身
    "duckduckgo.com", "bing.com", "google.com", "google.", "yahoo.com", "mojeek.com",
)


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _platform_mark_hit(host: str, mark: str) -> bool:
    """平台类黑名单按"域名主体"匹配（IT 要求），一次覆盖所有国别后缀：
    取 mark 的第一个标签作为平台主体（europages.com / europages. 都是 europages；
    alibaba.com 是 alibaba；machineseeker. 是 machineseeker），
    只要 host 中存在"完全相等"的域名标签即命中（host 各标签逐段比较）。
    逐段相等可挡 europages.co.uk/.de/.fr、www.europages.com，同时不误伤
    europages-vet-clinic.com（标签是 europages-vet-clinic，不等于 europages）。
    注意：竞品名单不走这里，仍用精确子串，避免误挡同主域名的企业官网。"""
    h = _strip_www(host)
    key = mark.split(".", 1)[0].strip()
    if not key:  # 形如 ".com" 等异常 mark，忽略
        return False
    return key in h.split(".")


def _is_junk_result_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
    except Exception:
        return True
    if not host:
        return True
    # 竞品域名：保持精确子串（防止误挡同主域名的企业官网）
    if any(mark in host for mark in _COMPETITOR_HOST_MARKS):
        return True
    # 平台/目录/二手等：按域名主体匹配，覆盖全部国别后缀
    if any(_platform_mark_hit(host, m) for m in _JUNK_HOST_MARKS):
        return True
    # 域名本身即目录/黄页站（如 vetsuppliersdirectory.com.au、xxx-directory.net），非单一公司主体
    if "directory" in host:
        return True
    return False


def _host_forced_page_type(url: str) -> str:
    """规则层域名强制分类（不依赖 Coze）：
    - 命中竞品域名 → competitor（固定 5 分，系统排除）；
    - 命中平台/黄页/二手域名主体（含国别后缀，如 businesslist.co.ke、europages.co.uk）→ directory（10 分）。
    无可判定返回空串。用于 Coze 误判/未跑时的硬兜底，防止竞品与黄页混入公司页高分。"""
    try:
        from urllib.parse import urlparse
        host = urlparse((url or "").lower()).netloc.lower()
    except Exception:
        return ""
    if not host:
        return ""
    if any(mark in host for mark in _COMPETITOR_HOST_MARKS):
        return "competitor"
    if any(_platform_mark_hit(host, m) for m in _JUNK_HOST_MARKS):
        return "directory"
    if "directory" in host:
        return "directory"
    return ""


def _apply_host_page_override(page_type: str, buyer_type: str, url: str) -> tuple:
    """域名强制分类覆盖：命中竞品/平台黄页 host 时，以规则判定为准（压过 Coze 误判）。
    返回 (page_type, buyer_type)。竞品→competitor；平台黄页→directory。"""
    forced = _host_forced_page_type(url)
    if forced:
        return forced, buyer_type
    return page_type, buyer_type


def _filter_junk_results(results: list):
    """剔除聚合站/社媒结果，返回 (干净结果, 被过滤数)。"""
    clean = [r for r in results if not _is_junk_result_url(r.get("url", ""))]
    return clean, len(results) - len(clean)


def _multi_engine_search(query: str, timeout: int = 6, skip_brave: bool = False,
                         deadline: float = None) -> list:
    """主动获客单查询：有 BRAVE_API_KEY 优先 Brave；否则/失败再回退
    DDG GET → DDG POST → Bing。每个引擎结果先过滤聚合站，干净结果命中即返回；
    每个引擎真实状态写入 _lead_search_diag，避免静默吞错导致"假无线索"。
    skip_brave=True：熔断/免费回退模式，只打免费引擎。
    deadline=time.monotonic() 截止点：超过后免费引擎直接放弃（30秒短上限）。
    Brave 欠费/限流/熔断异常直接上抛，由搜索主循环决定是否熔断整轮。"""
    import urllib.parse as _up
    import time as _time
    base_headers = {
        "User-Agent": _FIND_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    q = _up.urlencode({"q": query})
    engines = []
    if os.environ.get("BRAVE_API_KEY", "").strip() and not skip_brave:
        engines.append(("brave", None, None, None, _brave_api_search, True))
    engines += [
        ("ddg_get", "https://html.duckduckgo.com/html/?" + q,
         dict(base_headers), None, _parse_ddg_html_results, False),
        ("ddg_post", "https://html.duckduckgo.com/html/",
         {**base_headers, "Content-Type": "application/x-www-form-urlencoded",
          "Referer": "https://html.duckduckgo.com/"}, q, _parse_ddg_html_results, False),
        ("bing", "https://www.bing.com/search?" + _up.urlencode({"q": query, "count": "20"}),
         dict(base_headers), None, _parse_bing_html_results, False),
    ]
    for name, url, headers, data, parser, is_brave in engines:
        # 免费回退模式：非 Brave 引擎受 30 秒总预算约束，超时即放弃，不再干等
        if not is_brave and deadline is not None and _time.monotonic() > deadline:
            _lead_search_diag["engine_status"][name] = "免费回退总预算超时，跳过"
            continue
        try:
            if is_brave:
                raw = _brave_api_search(query, timeout)
                code_note = "api"
            else:
                code, body = _http_fetch_with_headers(url, headers, timeout, data)
                code_note = f"http{code}"
                if not body:
                    _lead_search_diag["engine_status"][name] = f"空响应({code_note})"
                    continue
                low_head = body[:4000].lower()
                if "anomaly" in low_head or ("challenge" in low_head and name.startswith("ddg")):
                    _lead_search_diag["engine_status"][name] = f"被限流/验证页({code_note})"
                    continue
                raw = parser(body)
            clean, junk_n = _filter_junk_results(raw)
            _lead_search_diag["engine_status"][name] = (
                f"{code_note}/原始{len(raw)}/过滤聚合{junk_n}/有效{len(clean)}")
            if clean:
                _lead_search_diag["last_ok_engine"] = name
                return clean
        except (BraveBillingError, BraveRateLimitError, BraveCircuitOpenError):
            raise  # 熔断类异常上抛，主循环据此快速结束
        except Exception as e:
            _lead_search_diag["engine_status"][name] = type(e).__name__
            continue
    return []


def _search_ddg_leads(query: str, timeout: int = 8, skip_brave: bool = False,
                      deadline: float = None) -> list:
    """主动获客搜索（多引擎兜底），返回 [{title, url, snippet}]。"""
    return _multi_engine_search(query, timeout=min(timeout, 6),
                                skip_brave=skip_brave, deadline=deadline)



# 纯产品/通用名词（小写）：标题剥离这些词后若没有任何"品牌实词"，说明不是公司名
_PRODUCT_GENERIC_WORDS = {
    "veterinary", "vet", "animal", "pet", "hospital", "clinic", "equipment",
    "device", "devices", "machine", "machines", "anesthesia", "anaesthesia",
    "ventilator", "ventilators", "monitor", "monitors", "monitoring", "pump",
    "pumps", "surgical", "medical", "medicine", "health", "healthcare",
    "care", "product", "products", "category", "system", "systems", "supply",
    "supplies", "new", "best", "top", "for", "sale", "price", "prices", "buy",
    "shop", "store", "online", "wholesale", "supplier", "suppliers",
    "manufacturer", "manufacturers", "distributor", "dealer", "importer",
    "exporter", "trade", "trading", "the", "and", "of", "in", "with", "co",
    "inc", "ltd", "llc", "gmbh", "corp", "group",
    # 西语/葡语产品通名（南美/西班牙/葡萄牙市场标题常用）
    "maquina", "maquinas", "máquina", "máquinas", "de", "del", "la", "el", "y",
    "anestesia", "veterinaria", "veterinário", "veterinaria", "equipos",
    "equipamento", "equipamentos", "mascotas", "mascote", "animalia",
    "para", "com", "por", "venta", "precio", "compra", "produto", "produtos",
    "producto", "productos", "categoria", "categoría", "tienda",
}


# 多级公共后缀（国家二级域 + 通用后缀），整体从域名尾部剥掉，避免 .co.za 残个 "za"
_MULTI_PUBLIC_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "edu.au",
    "co.nz", "co.za", "org.za", "co.ke", "or.ke", "ac.ke", "go.ke",
    "com.br", "com.mx", "com.ar", "co.in", "co.id", "co.th", "co.kr", "co.jp",
    "com.cn", "com.tr", "com.eg", "com.ng", "com.sg", "com.my", "com.ph",
    "com.vn", "com.co", "com.pe", "com.cl", "com.py", "com.uy", "com.ve",
    "co.ve", "com.ec", "com.gt", "com.cr", "com.pa", "com.do", "co.tz",
    "or.tz", "ac.tz", "go.tz", "co.ug", "ac.ug", "co.gh", "com.gh",
    "co.cm", "co.ao", "co.mz", "co.zm", "co.zw", "co.bw", "co.na",
}
# 单标签后缀：通用 gTLD + 国家/地区 ccTLD（两字母一律视为后缀，品牌名里基本不会只有国家码）
_SINGLE_PUBLIC_SUFFIXES = {
    "com", "org", "net", "io", "co", "info", "biz", "gmbh", "ltd", "inc", "llc",
    "shop", "store", "online", "site", "health", "vet", "care", "group", "za",
    "uk", "au", "nz", "ke", "tz", "ug", "gh", "ng", "eg", "cm", "ao", "mz",
    "zm", "zw", "bw", "na", "br", "mx", "ar", "cl", "co", "pe", "py", "uy",
    "ve", "ec", "gt", "cr", "pa", "do", "in", "id", "th", "kr", "jp", "cn",
    "tr", "sg", "my", "ph", "vn", "de", "fr", "es", "it", "pt", "nl", "pl",
    "us", "ca", "eu", "me",
}
# 通用主机/二级标签（非品牌）
_GENERIC_HOST_LABELS = {
    "www", "shop", "store", "online", "get", "buy", "us", "uk", "eu",
    "en", "www2", "m", "mail", "info", "contact", "order", "orders",
}


def _brand_from_domain(url: str) -> str:
    """从企业域名推断品牌名：apexx-equipment.com -> Apexx Equipment。
    先剥多级公共后缀(.co.za/.com.au)，再剥单标签 gTLD/ccTLD 和通用主机标签，
    连字符/点拆词后首字母大写。无法判断返回空串。"""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().split(":")[0]
        labels = [p for p in host.split(".") if p]
        if len(labels) >= 2:
            tail2 = ".".join(labels[-2:])
            if tail2 in _MULTI_PUBLIC_SUFFIXES:
                labels = labels[:-2]
        if labels and labels[-1] in _SINGLE_PUBLIC_SUFFIXES:
            labels = labels[:-1]
        words = []
        for lab in labels:
            if lab in _GENERIC_HOST_LABELS or lab.isdigit() or len(lab) <= 1:
                continue
            words.extend(w for w in lab.split("-") if w)
        words = [w for w in words if w not in _GENERIC_HOST_LABELS and not w.isdigit() and len(w) > 1]
        if not words:
            return ""
        brand = " ".join(words).replace("_", " ")
        # 全是产品/类目通名词则不算品牌
        toks = re.findall(r"[a-z]+", brand.lower())
        if toks and all(t in _PRODUCT_GENERIC_WORDS for t in toks):
            return ""
        return " ".join(w.capitalize() for w in brand.split())
    except Exception:
        return ""


def _is_product_phrase(name: str) -> bool:
    """标题/公司名是否只是纯产品短语（没有任何品牌专名）。"""
    if not name:
        return True
    toks = [t for t in re.findall(r"[a-z0-9]+", name.lower())]
    if not toks:
        return True
    brand_words = [t for t in toks if t not in _PRODUCT_GENERIC_WORDS]
    return len(brand_words) == 0


# 搜索结果标题里常见的"页面样板"噪音前缀（导航/电商/门户口吻），不是公司名的一部分
_COMPANY_NOISE_PREFIX_RE = re.compile(
    r"^\s*(?:welcome\s+to(?:\s+the)?|the\s+official\s+(?:site|website|homepage)\s+of|"
    r"official\s+(?:site|website|homepage)\s+of|home\s*page|homepage|"
    r"company\s+home(?:\s*page)?|about(?:\s+us)?|shop\s+for|shop\s+online(?:\s+for)?|"
    r"sa(?:'s|’s)?\s+number\s+one|"
    r"africa(?:'s|’s)?\s+number\s+one|number\s+one|"
    r"leading|premier|top\s*rated|best)\s+",
    re.I,
)
# 仅当整串就是单个通用导航/电商词时才剥离（避免第二轮把品牌里的 animal/home 误吃）
_COMPANY_NOISE_BARE_RE = re.compile(
    r"^\s*(?:home|shop|store|buy|order|get|find|visit|find\s+your|visit\s+our)\s+",
    re.I,
)
# 尾部样板噪音（分页/站点口吻），剥离后仍是公司名主体
_COMPANY_NOISE_SUFFIX_RE = re.compile(
    r"\s*(?:[-–|,·•]\s*)?(?:home\s*page|homepage|official\s+website|official\s+site|"
    r"website|web\s*site|home|welcome|about\s+us|contact\s+us)\s*$",
    re.I,
)
# 剥离后若剩下的全是这些"非品牌"词，说明标题没有公司主体 → 回退用域名品牌
_COMPANY_NONBRAND_WORDS = _PRODUCT_GENERIC_WORDS | {
    "home", "page", "homepage", "website", "site", "official", "welcome",
    "about", "us", "contact", "portal", "company", "number", "one", "south",
    "africa", "africas", "agriculture", "agricultural", "agricultureand",
    "livestock", "farming", "farm", "farms", "poultry", "agrovet", "agro",
    "marketplace",
}


def _strip_company_name_noise(name: str, url: str) -> str:
    """剥掉搜索标题里的页面样板噪音（Company home page / Shop for … / SA's number one …），
    还原公司名；只对最终保留的公司型线索使用。剥完若已无品牌主体，则用域名品牌兜底。"""
    c = (name or "").strip()
    if not c:
        return _brand_from_domain(url)
    # 第一轮：多词样板前缀；仅当整串是单个导航词时才剥裸词；最多两轮防止叠加前缀
    for i in range(2):
        new = _COMPANY_NOISE_PREFIX_RE.sub("", c, count=1).strip(" -–|,·•")
        if new == c:
            new = _COMPANY_NOISE_BARE_RE.sub("", c, count=1).strip(" -–|,·•")
        if new == c:
            break
        c = new
    c = _COMPANY_NOISE_SUFFIX_RE.sub("", c).strip(" -–|,·•")
    if not c:
        return _brand_from_domain(url)
    toks = re.findall(r"[a-z0-9]+", c.lower())
    if not toks or all(t in _COMPANY_NONBRAND_WORDS for t in toks):
        return _brand_from_domain(url)
    # 剥离后若整体仍是全小写（品牌大写信息在原标题里已丢失），做首字母规范化
    if c == c.lower():
        c = " ".join(w.capitalize() for w in c.split())
    return c[:80]


# 电商货架/购物路径特征：在线商店的商品详情/分类/购物车页，是同行卖家货架而非买家主体
_SELLER_PATH_MARKS = (
    "/product/", "/products/",  # 单数商品详情页与复数商品列表页（zzolive.com/products/...）
    "/product-category/", "/product-categories/", "/collections/",
    "/shop/", "/store/", "/cart", "/checkout", "/wishlist",
    "/item/", "/goods/", "/categoria/", "/categorias/", "/produto/", "/produtos/",
    # 通用分类/栏目页（equipnet.com/category/... 这类二手/零售平台的商品聚合页）
    "/category/", "/categories/",
    # 厂商/网站的"找经销商/分销商"栏目页（是厂家招商页，不是买家主体）
    "/distributors/", "/dealers/", "/resellers/",
    # 二手设备站点的二手专区路径
    "/used/", "/used-equipment", "/second-hand/", "/secondhand/", "/pre-owned/", "/preowned/",
    # 拍卖/竞拍栏目（vetecom.co.uk/auction-practice、站点 /auctions/ 列表；只按路径段，不放 query 层）
    "/auction", "/auctions",
)

# 子域名中的电商/产品站特征（如 products.covetrus.com, shop.example.com）
_SELLER_SUBDOMAIN_MARKS = (
    "products.", "shop.", "store.", "catalog.", "catalogue.", "ecommerce.", "ecom.",
)


def _is_seller_or_section_url(url: str) -> bool:
    # 去掉查询串后统一补一个结尾斜杠：栏目根 URL（如 /distributors/、/used/）经 rstrip 会丢斜杠
    # 而漏判，故改成"保证有结尾斜杠"，使 /xxx/ 段标记既能匹配栏目根也能匹配其子路径
    u = (url or "").lower().split("?")[0]
    if not u.endswith("/"):
        u += "/"
    # 检查路径
    if any(mark in u for mark in _SELLER_PATH_MARKS):
        return True
    # 检查子域名（如 products.covetrus.com）
    try:
        from urllib.parse import urlparse
        parsed = urlparse(u)
        domain = parsed.netloc.lower()
        if any(domain.startswith(mark) for mark in _SELLER_SUBDOMAIN_MARKS):
            return True
    except Exception:
        pass
    return False


# 政府/教育等非买家主体的公共部门二级域（仅对两位字母国家后缀做组合判断，避免误伤 go.com 这类商业站）
_GOV_EDU_SLD = {"gov", "go", "gob", "gouv", "govt", "mil", "edu", "ac"}
# 官方贸易/出口指南路径特征（如 trade.gov/country-commercial-guides/...-distribution-sales-channels）
_TRADE_GUIDE_MARKS = (
    "country-commercial-guides", "country-commercial-guide",
    "distribution-sales-channels", "trade.gov/",
)


def _is_gov_edu_result(url: str) -> bool:
    """政府/军队/教育/官方贸易指南页：不是买家公司主体，命中即丢弃。
    判定只看域名后缀（.gov/.gov.xx/.edu/.edu.xx/.ac.xx/.go.xx/.gob.xx/.gouv.xx/.mil）
    与官方指南路径，不看正文，避免误判提到政府的正常公司。"""
    u = (url or "").lower().split("?")[0]
    try:
        from urllib.parse import urlparse
        host = urlparse(u).netloc.lower().split(":")[0]
        labels = [p for p in host.split(".") if p]
        if len(labels) >= 2:
            tld = labels[-1]
            sld = labels[-2]
            if tld in ("gov", "mil", "edu"):           # .gov / .mil / .edu 直接命中
                return True
            # 两位国家后缀 + gov/go/gob/gouv/govt/mil/edu/ac 二级域
            if len(tld) == 2 and tld.isalpha() and sld in _GOV_EDU_SLD:
                return True
    except Exception:
        pass
    # 官方出口/贸易指南页（trade.gov 的 country-commercial-guides）
    path = u.split("://", 1)[-1]
    if any(m in path for m in _TRADE_GUIDE_MARKS):
        return True
    return False


# B2B 撮合/电商平台标题自述（是平台而非单一买家公司）。只看标题，不看正文（正文常出现 platform 一词）。
_PLATFORM_TITLE_RE = re.compile(
    r"(?:b2b\s+(?:marketplace|platform|portal)|business[\s-]to[\s-]business\s+(?:platform|marketplace)|"
    r"(?:medical|health(?:care)?|veterinary|vet|animal|pharma(?:ceutical)?|online)\s+marketplace\b|"
    r"\bmarketplace\s+for\b|\bnumber\s+one\s+\w+\s+marketplace\b|"
    r"online\s+trading\s+platform|procurement\s+portal|tender\s+portal)",
    re.I,
)


def _is_platform_title(title: str) -> bool:
    """标题自述为 B2B/电商撮合平台或采购门户（非单一买家）→ True。"""
    return bool(title and _PLATFORM_TITLE_RE.search(title))


# 二手/翻新设备：标题或URL命中即判为二手交易页（只看标题与URL，不看摘要，避免 "systems used by vets" 误杀）
_SECONDHAND_TITLE_RE = re.compile(
    r"(?:\bused\b\s+(?:vet(?:erinary)?|medical|animal|an[aes]+the[sz]ia|surgical|pharma\w*|equipment|machine|device|system"
    r"|ventilators?|ventilation|breathing)"
    r"|\bsecond[\s\-]?hand\b|\bpre[\s\-]?owned\b|\brefurbished\b|used\s+vet\s+equipment|"
    r"vehicles\s+and\s+vet\s+boxes"
    # 兽医/医疗设备拍卖（equipment/machine/ventilator/breathing 等与 auction 同标题共现，前后 40 字内）
    r"|\b(?:vet(?:erinary)?|medical|animal|surgical|pharma\w*|equipment|devices?|machines?|ventilators?|ventilation|breathing)\b"
    r"[^\n.]{0,40}\bauctions?\b"
    r"|\bauctions?\b[^\n.]{0,40}\b(?:vet(?:erinary)?|medical|animal|surgical|pharma\w*|equipment|devices?|machines?|ventilators?|ventilation|breathing)\b)",
    re.I,
)


def _is_secondhand_result(url: str, title: str) -> bool:
    t = (title or "").lower()
    if _SECONDHAND_TITLE_RE.search(title or ""):
        return True
    u = (url or "").lower().split("?")[0]
    # URL 中二手语义段：usedvetequipment.com、/used/、/second-hand/、?used-medical 等
    if re.search(r"(^|[/.\-])used([/.\-]|vet|medical|equipment|machine)", u):
        return True
    if re.search(r"second[\-]?hand|pre[\-]?owned|refurbished", u):
        return True
    return False


# 工厂/制造商卖家：标题以工厂身份自述（同行卖家，非采购方）。保守匹配，避免误伤正文提及工厂的买家。
_MANUFACTURER_TITLE_RE = re.compile(
    r"(?:manufacturer\s*[/|·-]?\s*(?:company|factory|supplier)"
    r"|supplies?\s+manufacturer|factory\s*[/|]\s*(?:company|manufacturer)"
    # SEO 着陆页典型形态：以 Manufacturer(s) 结尾，或 Manufacturer 连续/重复堆叠
    r"|manufacturers?\s+manufacturers?"
    r"|manufacturers?\s*$)",
    re.I,
)


def _is_manufacturer_title(title: str) -> bool:
    return bool(_MANUFACTURER_TITLE_RE.search(title or ""))


# 国家/地区域名后缀 → 国家英文名（仅在标题/摘要识别不到国家时兜底补全，不用于硬过滤）
_TLD_COUNTRY_MAP = {
    ".co.za": "South Africa", ".com.au": "Australia", ".co.ke": "Kenya",
    ".com.br": "Brazil", ".com.mx": "Mexico", ".com.ar": "Argentina",
    ".co.uk": "United Kingdom", ".ac.uk": "United Kingdom",
    ".de": "Germany", ".fr": "France", ".es": "Spain", ".it": "Italy",
    ".nl": "Netherlands", ".pl": "Poland", ".pt": "Portugal",
    ".vn": "Vietnam", ".th": "Thailand", ".co.id": "Indonesia",
    ".com.ph": "Philippines", ".com.sg": "Singapore", ".com.my": "Malaysia",
    ".co.nz": "New Zealand", ".ca": "Canada", ".com.tr": "Turkey",
    ".ae": "United Arab Emirates", ".sa": "Saudi Arabia", ".eg": "Egypt",
    ".com.ng": "Nigeria", ".co.in": "India", ".jp": "Japan", ".kr": "South Korea",
    ".cl": "Chile", ".com.co": "Colombia", ".com.pe": "Peru", ".com.ec": "Ecuador",
    ".com.uy": "Uruguay", ".com.py": "Paraguay", ".com.bo": "Bolivia",
}


def _country_from_tld(url: str) -> str:
    """从域名后缀兜底识别国家；识别不到返回空串。只补全、不过滤。"""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""
    if not host:
        return ""
    # 先匹配多级后缀（.co.za 等），再回退单级 ccTLD
    for suffix, country in _TLD_COUNTRY_MAP.items():
        if host.endswith(suffix):
            return country
    m = re.search(r"\.([a-z]{2})$", host)
    if m:
        two = "." + m.group(1)
        return _TLD_COUNTRY_MAP.get(two, "")
    return ""



# ===== 假线索识别规则（优化2） =====
_FAKE_LEAD_PATTERNS = [
    # 市场报告类
    re.compile(r"market\s*size", re.I),
    re.compile(r"market\s+growth", re.I),
    re.compile(r"growth\s*\[?\s*20\d{2}", re.I),
    # 新闻稿/发货类
    re.compile(r"delivered\s+to", re.I),
    re.compile(r"shipped\s+to", re.I),
    # 二手设备类
    re.compile(r"used\s+equipment", re.I),
    re.compile(r"second[\s\-]hand", re.I),
    # 预测报告类（未来年份 + Growth/Forecast）
    re.compile(r"(203[0-9]|202[6-9]).{0,15}(growth|forecast|projection)", re.I),
    re.compile(r"(growth|forecast|projection).{0,15}(203[0-9]|202[6-9])", re.I),
    # 目录/列表/合集类
    re.compile(r"^list\s+of", re.I),
    re.compile(r"^top\s+\d+\s+", re.I),
    re.compile(r"^\d+\s+best\s+", re.I),
    re.compile(r"^\d+\s+top\s+", re.I),
    # 通用导航页
    re.compile(r"^our\s+distributors\b", re.I),
    re.compile(r"^our\s+partners\b", re.I),
    re.compile(r"^our\s+clients\b", re.I),
    re.compile(r"^our\s+suppliers\b", re.I),
    # 纯国家名标题（前后无其他有意义内容）
    re.compile(r"^(chile|brazil|india|china|mexico|colombia|peru|ecuador)$", re.I),
    # Wikipedia / 百科类
    re.compile(r"wikipedia", re.I),
    re.compile(r"^home\s*[-|]", re.I),
]

# 标题长度检测关键词（优化3）
_TITLE_PRODUCT_KEYWORDS = re.compile(
    r"\b(anesthe[sz]ia|ventilat|monitor|surgical|equipment|device|instrument|apparatus)\b",
    re.I
)

def _is_fake_lead_title(title: str) -> bool:
    """检查标题是否匹配假线索模式（市场报告/新闻稿/二手设备/预测报告）。
    返回 True 表示是假线索，应丢弃或标低分。"""
    if not title:
        return False
    return any(p.search(title) for p in _FAKE_LEAD_PATTERNS)


def _is_long_title_product_page(title: str) -> bool:
    """标题长度 > 50 字符且包含产品关键词 → 大概率是新闻稿/产品页，标低分。"""
    if not title or len(title) <= 50:
        return False
    return bool(_TITLE_PRODUCT_KEYWORDS.search(title))


def _extract_company_info(title: str, snippet: str, url: str) -> dict:
    """从搜索结果中提取公司信息"""
    text = f"{title} {snippet}".lower()
    # 提取公司名（title中第一个有意义的词组）
    company = title.split("|")[0].split("-")[0].split(",")[0].split("–")[0].strip()
    # 去掉通用词
    for word in ["veterinary", "animal", "hospital", "equipment", "supplier",
                  "importer", "distributor", "wholesale", "official",
                  "manufacturer", "distributors", "suppliers", "manufacturers",
                  "company", "companies", "corporation", "services", "solutions",
                  "products", "international", "global", "worldwide"]:
        company = company.replace(word, " ").replace("  ", " ").strip()
    company = re.sub(r'\s+', ' ', company).strip()
    
    # 剥离后若只是纯产品短语（无品牌）或空串，尝试用域名品牌
    if _is_product_phrase(company) or len(company) < 2:
        domain_brand = _brand_from_domain(url)
        if domain_brand:
            company = domain_brand
        else:
            company = ""
    
    # 如果公司名只有一个词且是常见通用词（如国家名），也尝试用域名
    if company and company.lower() in _COUNTRY_NAMES_ONLY:
        domain_brand = _brand_from_domain(url)
        company = domain_brand if domain_brand else ""
    
    # 如果公司名以 "List of" / "Our " / "About " 等开头，尝试用域名
    low_company = company.lower()
    bad_company_starts = ("list of", "our ", "about ", "the ", "welcome",
                          "home", "contact", "find ", "top ", "best ",
                          "manufacturer of", "distributor of")
    if any(low_company.startswith(p) for p in bad_company_starts):
        domain_brand = _brand_from_domain(url)
        if domain_brand:
            company = domain_brand
        else:
            company = ""

    # 剥离标题里的页面样板噪音（Company home page / Shop for … / SA's number one …），
    # 剥完无品牌主体则用域名品牌兜底；只影响最终保留的公司名
    if company:
        _stripped = _strip_company_name_noise(company, url)
        if _stripped:
            company = _stripped

    # 识别国家
    country = ""
    region = ""
    for c, r in _SEARCH_COUNTRIES.items():
        if c.lower() in text:
            country = c
            region = r
            break

    # 识别产品需求
    demands = []
    product_keywords = {
        "anesthesia machine": "麻醉机", "ventilator": "呼吸机",
        "injection pump": "注射泵", "patient monitor": "监护仪",
        "surgical equipment": "手术设备", "hospital equipment": "医院设备",
    }
    for kw, cn in product_keywords.items():
        if kw in text:
            demands.append(cn)

    # 判断是否明确采购意向
    is_importer = any(w in text for w in ["import", "distributor", "dealer",
                      "wholesale", "supplier", "procurement", "purchase", "buy"])
    is_hospital = any(w in text for w in ["hospital", "clinic", "veterinary clinic",
                    "animal care", "vet center"])

    return {
        "company_name": company[:80],
        "country": country,
        "region": region,
        "product_demand": "、".join(demands) if demands else "兽用医疗设备",
        "is_importer": is_importer,
        "is_hospital": is_hospital,
    }


# 非公司主体页面的标题特征（展会联系页、活动页、目录页等）
_NON_COMPANY_TITLE_PREFIXES = (
    "contact ", "contact our", "about ", "find a", "find your", "join ",
    "register ", "sign up", "login", "log in", "exhibitor list",
    "exhibitor directory", "sponsor ", "become a", "apply ", "submission",
    "schedule", "agenda", "program", "welcome to", "home -", "homepage",
    # 列表/目录/合集页
    "list of", "list: ", "directory of", "catalog of", "catalogue of",
    "top ", "best ", "leading ", "major ", "key ",
    # 通用导航/站点页
    "home", "our distributors", "our partners", "our clients",
    "our suppliers", "our team", "our services", "our products",
    "meet the", "meet our",
)
# 非公司主体页面的标题关键词（出现在标题任意位置即过滤）
_NON_COMPANY_TITLE_KEYWORDS = (
    "exhibitor information", "exhibitor service", "exhibitor resources",
    "exhibitor success", "exhibitor warning", "exhibitor rules",
    "partner pr opportunities", "expo hall", "events archives",
    "industry partners supporting", "support veterinary education",
    "continuing education", "skillshop", "overview vmx",
    "new trends in veterinary", "trust your veterinary",
    "north american veterinary community",
    # 展会官网泛页面（不是参展商公司）
    "exhibitors", "exhibitor list", "exhibitor directory",
    "press release", "career advice", "newsroom",
    # 列表/目录/合集类
    "list of", "directory of", "directory", "top 10", "top 20", "top 50", "top 100",
    "best companies", "leading companies", "major companies",
    # 新闻/博客/文章类
    "news", "blog", "article", "journal", "magazine",
    "press release", "announces", "announced",
    # 招聘/维基/百科类
    "wikipedia", "wiki", "linkedin company",
    "hiring", "careers", "jobs", "job openings",
    # 社交/视频平台
    "facebook", "instagram", "youtube", "twitter", "tiktok",
    # 市场报告/行业分析
    "market report", "market analysis", "industry report", "industry analysis",
    "market size", "market share", "market forecast",
)


def _is_non_company_title(title: str) -> bool:
    """判断标题是否不是公司主体页面（展会联系页、活动页、目录页等）。"""
    low = title.lower().strip()
    if any(low.startswith(p) for p in _NON_COMPANY_TITLE_PREFIXES):
        return True
    return any(kw in low for kw in _NON_COMPANY_TITLE_KEYWORDS)


def _rate_lead_quality(info: dict) -> str:
    """质量评级：A=明确进口/采购+目标市场匹配，B=动物医院+目标市场，C=其他"""
    if info.get("is_importer") and info.get("country"):
        return "A"
    if info.get("is_hospital") and info.get("country"):
        return "B"
    if info.get("country"):
        return "B"
    return "C"


def _lead_val(lead_info: dict, *keys) -> str:
    """从线索字典里按多个候选 key（英文/中文）取第一个非空值。"""
    for k in keys:
        v = lead_info.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _real_website(url: str) -> bool:
    """判定官网是否为真实企业站点（过滤搜索引擎/聚合页假网址）。"""
    if not url:
        return False
    u = url.strip().lower()
    if not (u.startswith("http://") or u.startswith("https://") or u.startswith("www.")):
        return False
    return not any(m in u for m in _BAD_SITE_MARKS)


def _real_email(val: str) -> bool:
    """判定是否存在真实可用的邮箱地址（含@与域名，且不是示例/占位）。"""
    if not val:
        return False
    m = _EMAIL_RE.search(val)
    if not m:
        return False
    addr = m.group(0).lower()
    local, _, domain = addr.partition("@")
    if not local or domain in ("example.com", "domain.com", "email.com", "xxx.com"):
        return False
    if domain.endswith((".png", ".jpg", ".jpeg", ".gif")):
        return False
    return True


def _pick_real_website(lead_info: dict) -> str:
    """从候选字段中挑出第一个真实企业官网；来源网址/搜索引擎页一律排除。
    补搜成功后内存 lead 的英文 website 装的是真官网，初始搜索时它装的是来源网址，
    故统一用 _real_website 校验过滤。"""
    for k in ("official_website", "官网", "website"):
        u = _lead_val(lead_info, k)
        if _real_website(u):
            return u
    return ""


def _lead_is_reachable(lead_info: dict) -> bool:
    """可达性硬门槛：有真实邮箱 / 官网 / 决策人 / LinkedIn 任一即视为可触达。
    注意：只认真实企业官网；信号「原文链接」是来源网页（新闻/B2B目录/搜索引擎），
    不能当作企业官网，否则无联系方式的线索会被误判为可触达。"""
    if _real_email(_lead_val(lead_info, "email_pattern", "邮箱格式", "联系邮箱", "email")):
        return True
    if _pick_real_website(lead_info):
        return True
    if _lead_val(lead_info, "decision_maker", "决策人"):
        return True
    if _lead_val(lead_info, "linkedin", "LinkedIn"):
        return True
    return False


def _grade_from_score(score) -> str:
    """评级唯一出口：严格跟分数走。>=80=A，>=60=B，其余=C。
    修复历史问题：Coze 重评分只回写分数、评级仍停在落库初评，导致 8 分却给 A 级话术。"""
    try:
        s = int(float(score))
    except (TypeError, ValueError):
        return "C"
    if s >= 80:
        return "A"
    if s >= 60:
        return "B"
    return "C"


# 非公司页（目录/平台/新闻/导航/竞品等被排除主体）的固定分：与 Coze 提示词口径一致
_EXCLUDED_PAGE_SCORES = {
    "competitor": 5,
    "directory": 10,
    "b2b_platform": 10,
    "news_report": 10,
    "navigation": 10,
}


def _is_excluded_page_type(page_type: str) -> bool:
    return (page_type or "").strip().lower() in _EXCLUDED_PAGE_SCORES


def _excluded_page_score(page_type: str) -> int:
    """非公司页固定分：竞品 5，其余非公司页 10；未知页型返回 -1 表示"不适用"。"""
    return _EXCLUDED_PAGE_SCORES.get((page_type or "").strip().lower(), -1)


def _finalize_lead_score(score, lead_info: dict, page_type: str, buyer_type: str) -> int:
    """分数唯一收口：非公司页固定分（竞品5/其余10）；company+manufacturer 同行压5；
    其余真实公司走可达性闸门。保证被排除主体不占用公海高分排序位置。"""
    pt = (page_type or "").strip().lower()
    bt = (buyer_type or "").strip().lower()
    if _is_excluded_page_type(pt):
        return _excluded_page_score(pt)
    if pt == "company" and bt == "manufacturer":
        return 5
    return _apply_score_gate(score, lead_info, pt, bt)


def _rule_finalized_classification(page_type: str, buyer_type: str, lead: dict) -> bool:
    """三档分流的第一档（IT 2026-09-20）：规则层是否已能"完全定性"。
    命中 → 跳过 Coze、落库即固定分+系统排除、不触发补搜。一档只包含两类高置信主体：
      1) host/规则明确的非公司页（竞品 competitor / 黄页 directory / 平台 / 新闻 / 导航）；
      2) 假标题、长标题产品页（_score_penalty，规则已给固定低分）。
    注意（IT 2026-09-20 补充）：规则粗判 company+manufacturer **不进一档**——标题/粗判
    容易误伤 "Authorized Manufacturer Distributor" 这类经销商，统一降二档送 Coze 复核。
    host 强制的 manufacturer 若未来出现，可凭 host 标记单独判，当前 host 只强制 competitor/directory。
    字段全空（unknown）→ 第三档送 Coze 定夺。判定保守，宁送 Coze 也不漏掉真买家。"""
    pt = (page_type or "").strip().lower()
    if _is_excluded_page_type(pt):          # directory/b2b_platform/news_report/navigation/competitor
        return True
    if lead.get("_score_penalty"):          # 假标题/长标题产品页，规则已给固定低分
        return True
    return False


def _rule_finalized_category(page_type: str, buyer_type: str, lead: dict) -> str:
    """一档跳过 Coze 的线索，返回写入「系统排除」的**具体类别**（IT 2026-09-20）：
    competitor / directory / b2b_platform / news_report / navigation /
    invalid_title（假标题）/ product_shelf（长标题货架/产品页）。非一档返回空串。"""
    pt = (page_type or "").strip().lower()
    if _is_excluded_page_type(pt):
        return pt                           # competitor/directory/b2b_platform/news_report/navigation
    if lead.get("_long_title_reason") == "long_title_product_page":
        return "product_shelf"
    if lead.get("_score_penalty"):
        return "invalid_title"
    return ""


# 采购/经销侧买家类型：身份已识别、只差联系方式的真实潜客
_RESELLER_BUYER_TYPES = {"importer", "distributor", "wholesaler", "dealer"}


def _has_direct_contact(lead_info: dict) -> bool:
    """强联系信号：真实邮箱 / 决策人 / LinkedIn 任一存在即可直接触达（不含官网）。
    官网只能证明主体真实，不能直接联系人，故单独拆出，用于闸门分档。"""
    if _real_email(_lead_val(lead_info, "email_pattern", "邮箱格式", "联系邮箱", "email")):
        return True
    if _lead_val(lead_info, "decision_maker", "决策人"):
        return True
    if _lead_val(lead_info, "linkedin", "LinkedIn"):
        return True
    return False


def _apply_score_gate(score, lead_info: dict, page_type: str = "", buyer_type: str = "") -> int:
    """评分出口闸门，按可触达强度分三档（IT 2026-09-20 定稿）：
    1) 有强联系信号（真实邮箱/决策人/LinkedIn）→ 不封顶，按真实分；
    2) 无强联系，但有真实官网 + page_type=company + buyer_type∈
       importer/distributor/wholesaler/dealer → 封顶 59（C 里排名最高，补全后自然升 B）；
    3) 其余（四要素皆空且非合格公司）→ 仍封顶 45。
    59 而非 60：保留"B(≥60)=至少能联系上"的业务语义。"""
    try:
        s = int(float(score))
    except (TypeError, ValueError):
        s = 30
    s = max(0, min(100, s))
    pt = (page_type or "").strip().lower()
    bt = (buyer_type or "").strip().lower()
    if _has_direct_contact(lead_info):
        return s
    if pt == "company" and bt in _RESELLER_BUYER_TYPES and _pick_real_website(lead_info):
        return min(s, 59)
    return min(s, 45)


def _rule_fallback_score(lead_info: dict, page_type: str = "", buyer_type: str = "") -> int:
    """规则兜底打分：Coze工作流不可用时使用。A=90/B=60/C=30 + 补搜信息加分；
    出口同样经过可达性三档闸门（强联系不封顶/真经销商官网封顶59/其余封顶45）。"""
    grade = _lead_val(lead_info, "confidence", "评级") or "C"
    base_score = {"A": 90, "B": 60, "C": 30}.get(grade, 30)
    bonus = 0
    if _real_website(_lead_val(lead_info, "website", "官网", "原文链接")):
        bonus += 5
    if _lead_val(lead_info, "industry", "行业"):
        bonus += 3
    if _real_email(_lead_val(lead_info, "email_pattern", "邮箱格式", "联系邮箱", "email")):
        bonus += 5
    return _apply_score_gate(min(100, base_score + bonus), lead_info, page_type, buyer_type)


def _get_sales_coze_pat() -> str:
    """销售系统（线索打分/开发信）专用PAT：优先读独立变量 COZE_SALES_PAT，
    避免与背景图功能共用的 COZE_PAT 互相影响。未配置则返回空串由调用方回退。"""
    return (os.getenv("COZE_SALES_PAT", "")
            or getattr(settings, "COZE_SALES_PAT", "")
            or getattr(settings, "coze_sales_pat", "") or "")


def _coze_workflow_run(workflow_id: str, parameters: dict, timeout: int = 60) -> dict:
    """同步调用 Coze workflow/run，返回解析后的输出 dict。失败抛异常。
    输出约定：工作流结束节点返回的字段会被组装进 data（JSON字符串）。"""
    import urllib.request as _ur
    pat = (_get_sales_coze_pat()
           or getattr(settings, "COZE_PAT", "") or getattr(settings, "coze_pat", "") or "")
    if not pat or not workflow_id:
        raise RuntimeError("COZE_PAT 或 workflow_id 未配置")
    payload = {"workflow_id": workflow_id, "parameters": parameters}
    req = _ur.Request(
        "https://api.coze.cn/v1/workflow/run",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with _ur.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"Coze workflow code={result.get('code')} msg={result.get('msg','')}")
    data = result.get("data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            raise RuntimeError(f"Coze data 非JSON: {data[:200]}")
    return data if isinstance(data, dict) else {}


def _extract_scoring_payload(data: dict) -> dict:
    """从工作流返回中提取 total_score/breakdown/recommendation。
    兼容两种接线：(a)结束节点各字段已是独立值 (b)每个字段都装了整份JSON字符串。"""
    def _maybe_load(v):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("{"):
                try:
                    return json.loads(s)
                except Exception:
                    return None
        return None

    total = data.get("total_score")
    # 情况b：total_score 是一整份 JSON 字符串
    embedded = _maybe_load(total)
    if embedded and ("total_score" in embedded):
        return {
            "total_score": embedded.get("total_score"),
            "breakdown": embedded.get("breakdown", ""),
            "recommendation": embedded.get("recommendation", ""),
            "page_type": embedded.get("page_type", ""),
            "buyer_type": embedded.get("buyer_type", ""),
        }
    # 情况a：字段已各归各位
    return {
        "total_score": total,
        "breakdown": data.get("breakdown", ""),
        "recommendation": data.get("recommendation", ""),
        "page_type": data.get("page_type", ""),
        "buyer_type": data.get("buyer_type", ""),
    }


# 页面类型 / 买家类型枚举（与 Coze Lead_Score 提示词保持一致）
_PAGE_TYPES = ("company", "directory", "b2b_platform", "news_report", "navigation", "competitor")
_BUYER_TYPES = ("importer", "distributor", "wholesaler", "dealer",
                "hospital", "clinic", "manufacturer")

# 页面类型规则粗判：URL 路径 + 标题（Coze 返回前先写初值，返回后由 AI 覆盖）
_RULE_PAGE_TYPE_HINTS = (
    ("directory", ("/directory", "/directories", "/categories", "/category",
                   "/list-of", "list-of-", "/sellers", "/companies")),
    ("b2b_platform", ("alibaba.com", "made-in-china", "tradeindia", "indiamart",
                      "globalsources", "europages", "kompass", "thomasnet")),
    ("news_report", ("/news", "/press", "/article", "/blog", "prnewswire",
                     "businesswire", "reuters", "/media")),
    ("navigation", ("/sitemap", "/tag/", "/tags/", "/search?", "/index/")),
)
_RULE_PAGE_TITLE_HINTS = (
    ("directory", ("list of", "top 10", "top 20", "best companies", "directory",
                   "companies in", "suppliers in", "our distributors", "where to buy")),
    ("news_report", ("news", "report", "press release", "announces", "market research")),
)
# 买家类型规则粗判（仅在判定为公司页时有意义）
_RULE_BUYER_HINTS = (
    ("importer", ("importer", "import", "importacion", "importadora")),
    ("distributor", ("distributor", "distribuidor", "distribution", "distribute")),
    ("wholesaler", ("wholesale", "wholesaler", "mayorista")),
    ("dealer", ("dealer", "dealership", "reseller", "agent")),
    ("hospital", ("animal hospital", "veterinary hospital", "pet hospital", "hospital veterin")),
    ("clinic", ("clinic", "clinica", "veterinary center", "vet center")),
    ("manufacturer", ("manufacturer", "manufacturing", "factory", "producer", "fabricante")),
)


def _rule_guess_page_type(url: str = "", title: str = "", snippet: str = "") -> str:
    """规则法粗判页面类型，返回枚举值；无法判断时返回 unknown。Coze 结果返回后覆盖。"""
    u = (url or "").lower()
    t = f"{title or ''} {snippet or ''}".lower()
    for ptype, marks in _RULE_PAGE_TYPE_HINTS:
        if any(m in u for m in marks):
            return ptype
    for ptype, marks in _RULE_PAGE_TITLE_HINTS:
        if any(m in t for m in marks):
            return ptype
    return "company" if (url or title) else "unknown"


def _rule_guess_buyer_type(url: str = "", title: str = "", snippet: str = "") -> str:
    """规则法粗判买家类型，返回枚举值；无法判断时返回 unknown。Coze 结果返回后覆盖。"""
    blob = f"{title or ''} {snippet or ''} {url or ''}".lower()
    for btype, marks in _RULE_BUYER_HINTS:
        if any(m in blob for m in marks):
            return btype
    return "unknown"


def _normalize_page_type(v: str) -> str:
    v = (v or "").strip().lower()
    return v if v in _PAGE_TYPES else ""


def _normalize_buyer_type(v: str) -> str:
    v = (v or "").strip().lower()
    return v if v in _BUYER_TYPES or v == "unknown" else ""


async def call_coze_scoring_workflow(lead_info: dict) -> tuple:
    """调用Coze Lead_Score工作流AI打分，返回 (score:int, page_type:str, buyer_type:str)。
    - 入参兼容英文/中文字段名；
    - 打分前清洗脏公司名（搜索词短语），且脏名不向 product 白嫖关键词；
    - 同时把 source_url/page_title/page_snippet 传给工作流做页面真实性闸门分类；
    - 出口经过可达性闸门：无邮箱/官网/决策人/LinkedIn 的线索封顶45；
    工作流不可用/解析失败/超时 → 分数规则兜底、分类规则粗判，保证主流程不中断。"""
    wf_id = (getattr(settings, "COZE_LEAD_SCORE_WORKFLOW_ID", None)
             or getattr(settings, "coze_lead_score_workflow_id", "") or "")
    pat = (_get_sales_coze_pat()
           or getattr(settings, "COZE_PAT", "") or getattr(settings, "coze_pat", "") or "")

    raw_company = _lead_val(lead_info, "company_name", "公司/机构", "公司名")
    clean_company = _clean_company_name(raw_company)
    country = _lead_val(lead_info, "country", "国家", "地区")
    raw_product = _lead_val(lead_info, "product", "需求产品", "推荐产品")
    industry = _lead_val(lead_info, "industry", "行业")
    # 只认真实企业官网（补搜得到的站点）；来源网址(website在初始搜索时/原文链接)不是官网，不参与打分
    website = _pick_real_website(lead_info)
    email_pattern = _lead_val(lead_info, "email_pattern", "邮箱格式", "联系邮箱", "email")
    grade = _lead_val(lead_info, "confidence", "评级") or "C"

    # 页面真实性闸门三要素：搜索命中的原始 URL / 标题 / 摘要
    source_url = (_lead_val(lead_info, "source_url", "原文链接", "url")
                  or lead_info.get("website", "") or "")
    page_title = _lead_val(lead_info, "page_title", "线索标题", "title")
    page_snippet = _lead_val(lead_info, "page_snippet", "摘要", "snippet")

    # 脏公司名：若产品词直接来自该垃圾短语，不可让它白嫖"产品契合度"
    product = raw_product
    if not clean_company and raw_company:
        if not product or product.lower() in raw_company.lower():
            product = ""

    norm = {
        "company_name": clean_company, "country": country, "product": product,
        "industry": industry, "website": website, "email_pattern": email_pattern,
        "confidence": grade,
        "decision_maker": _lead_val(lead_info, "decision_maker", "决策人"),
        "linkedin": _lead_val(lead_info, "linkedin", "LinkedIn"),
    }

    # 任意兜底路径：分数用规则，分类用 URL+标题+摘要规则粗判（AI 不可用时仍有初值）
    def _fallback():
        pt = _rule_guess_page_type(source_url, page_title, page_snippet)
        bt = _rule_guess_buyer_type(source_url, page_title, page_snippet)
        # 域名强制分类兜底：竞品/平台黄页 host 压过规则粗判（不依赖 Coze）
        pt, bt = _apply_host_page_override(pt, bt, source_url)
        # 非公司页/同行：规则兜底也直接给固定分，不挂 45 占位分，避免与真实未补全公司混排
        if _is_excluded_page_type(pt) or (pt == "company" and bt == "manufacturer"):
            return _finalize_lead_score(0, norm, pt, bt), pt, bt
        return _rule_fallback_score(norm, pt, bt), pt, bt

    if not pat or not wf_id:
        return _fallback()
    parameters = {
        "company_name": clean_company,
        "country": country,
        "product": product,
        "industry": industry,
        "website": website,
        "email_pattern": email_pattern,
        "grade": grade,
        "source_url": source_url,
        "page_title": page_title,
        "page_snippet": page_snippet,
    }
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_coze_workflow_run, wf_id, parameters, 60),
            timeout=70)
        parsed = _extract_scoring_payload(data)
        score = parsed.get("total_score")
        score = int(float(score))
        if 0 <= score <= 100:
            # 分类以 AI 为准；AI 没给或给了非法值时回退规则粗判
            page_type = (_normalize_page_type(parsed.get("page_type", ""))
                         or _rule_guess_page_type(source_url, page_title, page_snippet))
            buyer_type = (_normalize_buyer_type(parsed.get("buyer_type", ""))
                          or _rule_guess_buyer_type(source_url, page_title, page_snippet))
            # 域名强制分类兜底：命中竞品/平台黄页 host 时压过 Coze 误判（如 MSD 被判 company）
            page_type, buyer_type = _apply_host_page_override(page_type, buyer_type, source_url)
            # 非公司页固定分（竞品5/其余10）；company+manufacturer 同行5；其余走可达性闸门
            final_score = _finalize_lead_score(score, norm, page_type, buyer_type)
            return final_score, page_type, buyer_type
        return _fallback()
    except asyncio.TimeoutError:
        print(f"[score] Coze打分超时，规则兜底: {clean_company or raw_company}")
        return _fallback()
    except Exception as e:
        print(f"[score] Coze打分失败({e})，规则兜底: {clean_company or raw_company}")
        return _fallback()


# ============================================================
# 异步补搜引擎（轻补搜 → 重评分 → Top5深度补搜）
# 基础搜索立即返回 → 后台线程跑补搜 → 前端轮询看到渐进更新
# ============================================================
_ENRICH_CONCURRENCY = 3      # 最大并发补搜数，避免IP被封
_ENRICH_DELAY = 0.5          # 每条补搜间隔（秒）
_ENRICH_TIMEOUT = 10         # 单条补搜超时（秒）


def _update_leads_record(record_id: str, fields: dict):
    """更新飞书线索表单条记录的指定字段"""
    if not record_id:
        return
    tid = _ensure_leads_table()
    try:
        _feishu_api(
            "PUT",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}",
            {"fields": fields})
    except Exception as e:
        print(f"[enrich] 更新线索记录失败 ({record_id}): {e}")


def backfill_lead_grades(apply: bool = False) -> dict:
    """一次性：把全部历史线索的「评级」按 _grade_from_score(综合评分) 重刷，
    修复存量"45分 A级 / 10分 A级"撕裂数据。
    apply=False 仅干跑返回差异清单不写库；apply=True 才逐条 PUT。
    只更新「评级」字段，不动评分/系统排除等其他列。"""
    leads = _fetch_leads(force_refresh=True)
    changes, skipped, errors = [], 0, 0
    for ld in leads:
        rid = ld.get("record_id") or ld.get("_record_id") or ""
        if not rid:
            continue
        raw_score = ld.get("综合评分")
        try:
            score = int(float(raw_score))
        except (TypeError, ValueError):
            score = None
        cur = (ld.get("评级") or "").strip().upper()
        if score is None:
            skipped += 1
            continue
        want = _grade_from_score(score)
        if cur == want:
            skipped += 1
            continue
        # _fetch_leads 返回归一化短键（见 LEADS_FIELD_MAP）：公司机构/认领人/认领状态
        _claimer = (ld.get("认领人") or "").strip()
        _claim_st = (ld.get("认领状态") or "").strip()
        changes.append({"record_id": rid,
                        "company": (ld.get("公司机构") or ld.get("标题") or "")[:40],
                        "score": score, "old_grade": cur or "(空)", "new_grade": want,
                        "claimed": bool(_claimer) or "已认领" in _claim_st,
                        "claimer": _claimer,
                        "url": (ld.get("原文链接") or "")[:80]})
        if apply:
            try:
                _update_leads_record(rid, {"评级": want})
            except Exception as e:
                errors += 1
                print(f"[backfill] 评级回填失败 {rid}: {e}")
    return {"total": len(leads), "to_change": len(changes),
            "unchanged": skipped, "errors": errors,
            "applied": bool(apply), "changes": changes}


def _search_ddg_single(query: str, timeout: int = 6) -> list:
    """单条DuckDuckGo搜索，返回 [{title, url, snippet}]（复用现有模式）"""
    import urllib.parse as _up
    import urllib.request as _ur
    import urllib.error as _ue
    q = _up.urlencode({"q": query})
    url = "https://html.duckduckgo.com/html/?" + q
    req = _ur.Request(url, headers={
        "User-Agent": _FIND_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    results = []
    try:
        with _ur.urlopen(req, timeout=timeout) as r:
            html_text = r.read(1_000_000).decode("utf-8", "ignore")
        for m in re.finditer(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html_text, re.I):
            href = _ddg_real_url(m.group(1))
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not href or not title:
                continue
            snippet = ""
            snip_match = re.search(
                re.escape(href[:30]) + r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                html_text, re.I | re.S)
            if snip_match:
                snippet = re.sub(r'<[^>]+>', '', snip_match.group(1)).strip()[:300]
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= 5:
                break
    except Exception:
        pass
    return results


# ===== 官网联系页直取（补全升级4） =====
# 只在公司"自家官网"同域抓首页+常见联系/关于页，不跨域、不爬深；只读公开 HTML，不提交表单/不登录。
_CONTACT_PATH_CANDIDATES = (
    "/contact", "/contact-us", "/contactus", "/contacts", "/contact.html",
    "/en/contact", "/en/contact-us", "/about/contact", "/get-in-touch", "/reach-us",
    "/about", "/about-us", "/aboutus", "/about/company", "/company/contact",
    "/imprint", "/impressum", "/kontakt", "/contatti", "/contacto", "/contactez-nous",
)
# Cloudflare cf-email 反混淆
_CF_EMAIL_RE = re.compile(r'<a[^>]+class="__cf_email__"[^>]*data-cfemail="([0-9a-fA-F]+)"', re.I)
# 常见 [at] [dot] 反混淆（info [at] example [dot] com）
_AT_DOT_RE = re.compile(
    r'([A-Za-z0-9._%+\-]{2,})\s*(?:\(|\[|\{)?\s*(?:@|\bat\b|\[at\]|\(at\)|\{at\}|\s+at\s+)\s*(?:\)|\]|\})?\s*'
    r'([A-Za-z0-9.\-]+)\s*(?:\(|\[|\{)?\s*(?:\.|\bdot\b|\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*(?:\)|\]|\})?\s*'
    r'([A-Za-z]{2,})',
    re.I,
)
# 域名停放页典型特征：114–300字节极短正文 + 含 parked / for sale / buy this domain
_PARKING_PAGE_RE = re.compile(r'parked|for sale|buy this domain|domain.*park|this domain.*expired', re.I)
# 已知强反爬/JS 挑战域名：生产机房 IP 的轻量请求过不了（sgcaptcha / Cloudflare / 403），
# 自动补全拿不到联系方式，直接标记转人工。后续实测发现新的同类站点追加到这里。
_KNOWN_ANTIBOT_DOMAINS = {
    # 域名: (反爬说明, 已知联系邮箱[人工核实后登记，供前端弹窗指引；未知留空])
    "valevetequipment.co.uk": ("sgcaptcha JS 挑战", "sales@valevetequipment.co.uk"),
    "burtonsveterinary.com": ("机房 IP 直接 403", ""),
}
# 事务/平台类邮箱，不是销售联系人，抓到也丢弃
_CONTACT_EMAIL_BLOCK = (
    "sentry.io", "wixpress.com", "wordpress.com", "example.com",
    "domain.com", "email.com", "yourdomain", "godaddy", "squarespace",
    "cloudflare", "schema.org", "w3.org", "u0026",
)
_CONTACT_PHONE_RE = re.compile(
    r"(?:tel:|call(?:\s+us)?[:\s]|phone[:\s]|\+?\d[\d\s().\-]{7,}\d)"
)
# 裸电话必须带分隔结构（+国家码 / (区号) / 空格或连字符分段），避免把 JS 时间戳(1789...180)、版本号误判为电话
_PHONE_BARE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{1,5}\)[\s.\-]?)?\d{2,5}(?:[\s\-]\d{2,5}){2,4}"
)
_TEL_HREF_RE = re.compile(r'tel:\s*([+"\d][\d\s().\-]{6,}\d)', re.I)
# 反爬 / JS 挑战页特征：这类页不含真实联系方式，直接跳过不解析
_ANTIBOT_CHALLENGE_RE = re.compile(
    r"sgcaptcha|cf-challenge|cloudflare|checking your browser|enable javascript and (?:re)?check|captcha",
    re.I,
)
_CONTACT_LINKEDIN_RE = re.compile(r'https?://(?:[\w-]+\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9_.%-]+', re.I)
_CONTACT_PERSON_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+)"
    r"\s*[/,|·\-]?\s*"
    r"(?:MD|Managing Director|CEO|Founder|Owner|Director|President|General Manager|Procurement|Purchasing Manager)",
)
_CONTACT_PERSON_RE2 = re.compile(
    r"(?:MD|Managing Director|CEO|Founder|Owner|Director|President|General Manager)"
    r"\s*[/,|·\-]?\s*([A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+)",
)


def _site_root(url: str) -> str:
    """归一化出官网根（scheme://host），失败返回空。"""
    try:
        from urllib.parse import urlparse
        p = urlparse(url if "://" in url else "https://" + url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        return f"{p.scheme}://{p.netloc.lower()}"
    except Exception:
        return ""


def _decode_cf_email(h: str) -> str:
    """解码 Cloudflare cf-email data-cfemail 16 进制串。"""
    try:
        b = bytes.fromhex(h)
        r = b[0]
        return "".join(chr(x ^ r) for x in b[1:])
    except Exception:
        return ""


def _parse_contact_html(html: str, host: str) -> dict:
    """从单页 HTML 提取 邮箱/电话/LinkedIn/联系人；只在同域语境调用。
    增强：Cloudflare cf-email 解码 + [at]/[dot] 混淆还原 + 域名停放页识别。"""
    out = {"email": "", "phone": "", "linkedin": "", "decision_maker": "", "parking": False}
    if not html:
        return out
    # 反爬/JS 挑战页不含真实联系方式（且常带时间戳参数会被误认成电话），直接返回空
    if len(html) < 1200 and _ANTIBOT_CHALLENGE_RE.search(html):
        return out
    # 域名停放页（114–300 字节占位）：不是真企业站，标记后不在本域继续抓
    if 80 < len(html) < 500 and _PARKING_PAGE_RE.search(html[:2000]):
        out["parking"] = True
        return out
    # 1) 邮箱：标准正则 + cf-email 解码 + [at][dot] 混淆；职能邮箱优先
    import html as _html_mod
    txt = _html_mod.unescape(html or "")
    raw_emails = set(_EMAIL_RE.findall(txt))
    for mm in _CF_EMAIL_RE.finditer(html or ""):
        d = _decode_cf_email(mm.group(1))
        if "@" in d:
            raw_emails.add(d)
    for mm in _AT_DOT_RE.finditer(txt):
        cand = f"{mm.group(1)}@{mm.group(2)}.{mm.group(3)}"
        raw_emails.add(cand)
    func = []
    other = []
    seen_em = set()
    for e in raw_emails:
        el = e.lower().strip().strip(".,;:)'\"")
        if el in seen_em:
            continue
        seen_em.add(el)
        if el.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if any(b in el for b in _CONTACT_EMAIL_BLOCK):
            continue
        if not _is_valid_email(el):
            continue
        local = el.split("@", 1)[0]
        if local in ("sales", "info", "contact", "enquiries", "inquiry", "inquiries",
                     "office", "admin", "export", "orders", "hello", "mail"):
            func.append(el)
        else:
            other.append(el)
    if func:
        out["email"] = func[0]
    elif other:
        out["email"] = other[0]
    # 2) 电话：优先 tel: 链接，其次正文号码（取最短、最像电话的，避免抓到长串页脚）
    tel_hrefs = _TEL_HREF_RE.findall(html)
    tel_hrefs = [t.strip().strip('"').strip() for t in tel_hrefs if sum(c.isdigit() for c in t) >= 7]
    if tel_hrefs:
        out["phone"] = tel_hrefs[0]
    else:
        cands = [c.strip() for c in _PHONE_BARE_RE.findall(html) if 7 <= sum(c.isdigit() for c in c) <= 15]
        if cands:
            out["phone"] = sorted(cands, key=len)[0]
    # 3) LinkedIn 公司主页
    m = _CONTACT_LINKEDIN_RE.search(html)
    if m:
        out["linkedin"] = m.group(0).rstrip('"').rstrip("'")
    # 4) 联系人：头衔+人名
    for pat in (_CONTACT_PERSON_RE, _CONTACT_PERSON_RE2):
        m = pat.search(html)
        if m:
            name = m.group(1).strip()
            # 过滤明显非人名（含常见页脚词）
            if not re.search(r"\b(Cookie|Privacy|Terms|All Rights|Copyright|Contact Us|Find Out)\b", name, re.I):
                out["decision_maker"] = name
                break
    return out


def _fetch_official_site_contacts(website: str, company: str = "") -> dict:
    """登录官网联系页直取：返回 {email, phone, linkedin, decision_maker, antibot}。
    仅抓公司自家官网（过 _real_website 且非聚合/平台/社媒/政府），最多首页+联系/关于约 4 页，
    单页 8 秒、整步约 30 秒上限；任何失败静默返回已拿到的部分，绝不抛错。
    antibot=True 表示官网存在强反爬/JS 挑战（已知域名或实抓到挑战页），自动补全拿不到，转人工。"""
    result = {"email": "", "phone": "", "linkedin": "", "decision_maker": "",
              "antibot": False, "known_email": ""}
    root = _site_root(website or "")
    if not root:
        return result
    # 已知强反爬域名：机房 IP 的轻量请求拿不到真实页面，直接标记转人工，不浪费请求
    try:
        from urllib.parse import urlparse as _up
        _host = _up(root).netloc.lower()
        for _d, (_note, _known_email) in _KNOWN_ANTIBOT_DOMAINS.items():
            if _host == _d or _host.endswith("." + _d):
                result["antibot"] = True
                result["known_email"] = _known_email or ""
                return result
    except Exception:
        pass
    # 安全闸门：必须是真实企业站，且不是目录/平台/政府/社媒等非公司主体
    if not _real_website(root + "/"):
        return result
    if _is_junk_result_url(root + "/") or _is_gov_edu_result(root + "/"):
        return result
    try:
        from urllib.parse import urlparse
        host = urlparse(root).netloc.lower()
    except Exception:
        return result
    # 候选页：首页 + 常见联系/关于路径；首页里若出现 contact/about 链接再补（去重、同域）
    candidates = [root + "/"]
    for path in _CONTACT_PATH_CANDIDATES:
        candidates.append(root + path)
    seen_pages = set()
    import time as _time
    deadline = _time.time() + 30
    for url in candidates:
        if _time.time() > deadline or len(seen_pages) >= 5:
            break
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            html = _http_get(url, timeout=8)
        except Exception:
            continue
        if not html:
            continue
        # 实抓到反爬/JS 挑战页：标记，自动补全对此站不可用，转人工
        if len(html) < 1200 and _ANTIBOT_CHALLENGE_RE.search(html):
            result["antibot"] = True
            continue
        info = _parse_contact_html(html, host)
        # 域名停放页：不是真企业站，标记后不在本域继续抓
        if info.get("parking"):
            result["antibot"] = True
            continue
        if info["email"] and not result["email"]:
            result["email"] = info["email"]
        if info["phone"] and not result["phone"]:
            result["phone"] = info["phone"]
        if info["linkedin"] and not result["linkedin"]:
            result["linkedin"] = info["linkedin"]
        if info["decision_maker"] and not result["decision_maker"]:
            result["decision_maker"] = info["decision_maker"]
        # 首页若已拿到邮箱+电话即可提前收工，减少请求
        if result["email"] and result["phone"]:
            break
        # 从首页导航里再发现同域联系/关于链接（最多补 6 个，含多语言）
        if len(seen_pages) <= 1:
            for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
                href = m.group(1).strip().lower()
                if re.search(r"/(contact[\w\-/]*|about[\w\-/]*|imprint|impressum|kontakt|contatti|contacto|contactez-nous|get-in-touch|reach-us)(?:/)?$", href) and len(seen_pages) < 9:
                    if href.startswith("/"):
                        full = root + href
                    elif href.startswith(root):
                        full = href
                    else:
                        continue
                    if full not in seen_pages:
                        candidates.append(full)
    return result


async def light_enrich_lead(company_name: str, country: str, website_hint: str = "") -> dict:
    """轻补搜：官网、行业、邮箱格式、电话、决策人、LinkedIn
    优先直取已确认官网的联系页（登录官网抓 /contact /about），搜索引擎摘要仅作兜底。
    单条总耗时控制在可接受范围，抓取失败静默退回搜索逻辑。"""
    result = {"website": "", "industry": "", "email_pattern": "",
              "phone": "", "decision_maker": "", "linkedin": "",
              "antibot": False, "known_email": ""}
    try:
        # ---- 第一步：若已确认真实官网，直接登录联系页取联系方式（成功率远高于摘要正则） ----
        official_site = ""
        hint_root = _site_root(website_hint or "")
        if hint_root and _real_website(hint_root + "/") \
                and not _is_junk_result_url(hint_root + "/") and not _is_gov_edu_result(hint_root + "/"):
            official_site = hint_root
            result["website"] = website_hint
            site_contacts = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_official_site_contacts, website_hint, company_name)
            if site_contacts.get("email"):
                result["email_pattern"] = site_contacts["email"]
            if site_contacts.get("phone"):
                result["phone"] = site_contacts["phone"]
            if site_contacts.get("decision_maker"):
                result["decision_maker"] = site_contacts["decision_maker"]
            if site_contacts.get("linkedin"):
                result["linkedin"] = site_contacts["linkedin"]
            if site_contacts.get("antibot"):
                result["antibot"] = True
                result["known_email"] = site_contacts.get("known_email", "") or result["known_email"]

        # ---- 第二步：搜索引擎找官网 + 摘要兜底（官网未直取到联系方式时补充） ----
        queries = [
            f'"{company_name}" {country} official website',
            f'"{company_name}" {country} contact email',
        ]
        all_text = ""
        for q in queries:
            sr = await asyncio.get_event_loop().run_in_executor(
                None, _search_ddg_single, q, _ENRICH_TIMEOUT)
            for item in sr:
                u = item.get("url", "")
                # 提取官网：排除搜索引擎/社交媒体/通用目录站
                if u and company_name.lower().split()[0] in u.lower():
                    if not any(skip in u for skip in [
                            "duckduckgo.com", "wikipedia.org", "facebook.com",
                            "linkedin.com", "twitter.com", "youtube.com"]):
                        if not result["website"]:
                            result["website"] = u
                            # 搜索刚发现的官网也尝试登录直取（仅一次，避免拖慢）
                            if not official_site:
                                found_root = _site_root(u)
                                if found_root and not _is_junk_result_url(found_root + "/"):
                                    sc = await asyncio.get_event_loop().run_in_executor(
                                        None, _fetch_official_site_contacts, u, company_name)
                                    result["email_pattern"] = result["email_pattern"] or sc.get("email", "")
                                    result["phone"] = result["phone"] or sc.get("phone", "")
                                    result["decision_maker"] = result["decision_maker"] or sc.get("decision_maker", "")
                                    result["linkedin"] = result["linkedin"] or sc.get("linkedin", "")
                                    if sc.get("antibot"):
                                        result["antibot"] = True
                                        result["known_email"] = sc.get("known_email", "") or result["known_email"]
                        break
                all_text += " " + item.get("title", "") + " " + item.get("snippet", "")
            await asyncio.sleep(_ENRICH_DELAY)

        # 提取行业关键词
        industry_keywords = {
            "veterinary": "兽医/动物医疗", "animal hospital": "动物医院",
            "clinic": "诊所", "pharmaceutical": "制药", "medical device": "医疗器械",
            "distributor": "经销商", "importer": "进口商", "wholesale": "批发",
            "agriculture": "农业", "livestock": "畜牧业", "pet": "宠物",
        }
        text_lower = all_text.lower()
        found_industries = [cn for kw, cn in industry_keywords.items() if kw in text_lower]
        if found_industries:
            result["industry"] = "/".join(found_industries[:3])

        # 提取邮箱格式（官网直取已有时不再用摘要覆盖）
        if not result["email_pattern"]:
            email_match = re.search(
                r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', all_text)
            if email_match:
                email = email_match.group(0)
                # 转换为通用格式（如 info@company.com）
                domain = email.split("@")[-1]
                prefix = email.split("@")[0]
                if prefix in ["info", "contact", "sales", "office", "admin"]:
                    result["email_pattern"] = f"{prefix}@{domain}"
                else:
                    result["email_pattern"] = f"info@{domain}"

    except Exception as e:
        print(f"[enrich] 轻补搜失败 ({company_name}): {e}")
    return result


async def deep_enrich_lead(company_name: str, country: str, website: str = "") -> dict:
    """深度补搜：决策人、LinkedIn、进口记录、电话
    组合多维度搜索，提取关键商务信息；有官网时先登录联系页直取。"""
    result = {"decision_maker": "", "linkedin": "", "import_record": "", "phone": "",
              "antibot": False, "known_email": "", "website": website}
    # 官网直取优先：电话/联系人/LinkedIn 从 contact/about 页拿，搜索摘要再补充
    if website:
        try:
            site = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_official_site_contacts, website, company_name)
            result["decision_maker"] = site.get("decision_maker", "")
            result["linkedin"] = site.get("linkedin", "")
            result["phone"] = site.get("phone", "")
            result["antibot"] = bool(site.get("antibot"))
            result["known_email"] = site.get("known_email", "") or ""
            # 官网直取到的真实职能邮箱（区别于 known_email 人工登记）
            result["email"] = site.get("email", "") or ""
        except Exception:
            pass
    loop = asyncio.get_event_loop()
    # 总时限25秒：避免机房IP被免费搜索引擎限流时，多次重试把请求拖到几分钟无响应
    deadline = loop.time() + 25
    try:
        queries = [
            f'"{company_name}" {country} CEO director manager contact',
            f'"{company_name}" LinkedIn',
            f'"{company_name}" {country} import veterinary medical equipment',
        ]
        all_text = ""
        for q in queries:
            if loop.time() >= deadline:
                break
            remaining = max(2.0, deadline - loop.time())
            try:
                # 走多引擎（有Brave key用Brave，否则DDG/Bing兜底），单查询最多8秒
                sr = await asyncio.wait_for(
                    loop.run_in_executor(None, _search_ddg_leads, q, 8),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break
            for item in sr:
                all_text += " " + item.get("title", "") + " " + item.get("snippet", "")
                # 提取LinkedIn链接
                u = item.get("url", "")
                if "linkedin.com" in u and company_name.lower().split()[0] in u.lower():
                    result["linkedin"] = u
            await asyncio.sleep(min(_ENRICH_DELAY, max(0.0, deadline - loop.time())))

        # 提取决策人姓名（从标题/摘要中找常见模式）
        dm_patterns = [
            r'(?:CEO|Director|Manager|Founder|Owner|President|VP)\s*:?\s*([A-Z][a-z]+\s+[A-Z][a-z]+)',
            r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s*(?:CEO|Director|Manager|Founder)',
        ]
        for pat in dm_patterns:
            m = re.search(pat, all_text)
            if m:
                result["decision_maker"] = m.group(1).strip()
                break

        # 提取进口记录线索
        import_keywords = ["import", "distributor", "dealer", "purchase", "procurement"]
        if any(kw in all_text.lower() for kw in import_keywords):
            # 提取包含进口信息的片段
            for sentence in re.split(r'[.!?]', all_text):
                if any(kw in sentence.lower() for kw in import_keywords):
                    clean = re.sub(r'<[^>]+>', '', sentence).strip()[:200]
                    if len(clean) > 20:
                        result["import_record"] = clean
                        break

    except Exception as e:
        print(f"[enrich] 深度补搜失败 ({company_name}): {e}")
    return result


async def _enrich_all_leads_async(new_leads: list):
    """后台线程：对全量线索跑轻补搜 → 重评分 → Top5深度补搜
    1. 每条线索调 light_enrich_lead → 更新飞书表（补搜状态=已轻补）
    2. 轻补搜完成后，对所有线索重新调 call_coze_scoring_workflow
    3. 按新评分排序，取Top5调 deep_enrich_lead（补搜状态=已深度补全）
    4. 全过程中每条更新都写回飞书表，前端轮询能看到进度"""
    from concurrent.futures import ThreadPoolExecutor
    print(f"[enrich] 后台补搜启动，共 {len(new_leads)} 条线索")

    # Phase 1: 全量轻补搜
    for i, lead in enumerate(new_leads):
        record_id = lead.get("_record_id", "")
        if not record_id:
            continue
        # 标记轻补搜中
        _update_leads_record(record_id, {"补搜状态": "轻补搜中"})
        try:
            enriched = await asyncio.wait_for(
                light_enrich_lead(lead.get("company_name", ""), lead.get("country", ""),
                                  lead.get("website", "")),
                timeout=_ENRICH_TIMEOUT * 2)
            # 更新飞书表
            update_fields = {
                "补搜状态": "已轻补",
            }
            if enriched.get("website"):
                update_fields["官网"] = enriched["website"]
                lead["website"] = enriched["website"]
            if enriched.get("industry"):
                update_fields["行业"] = enriched["industry"]
                lead["industry"] = enriched["industry"]
            if enriched.get("email_pattern"):
                update_fields["邮箱格式"] = enriched["email_pattern"]
                lead["email_pattern"] = enriched["email_pattern"]
            if enriched.get("phone"):
                update_fields["电话"] = enriched["phone"]
                lead["phone"] = enriched["phone"]
            if enriched.get("decision_maker"):
                update_fields["决策人"] = enriched["decision_maker"]
                lead["decision_maker"] = enriched["decision_maker"]
            if enriched.get("linkedin"):
                update_fields["LinkedIn"] = enriched["linkedin"]
                lead["linkedin"] = enriched["linkedin"]
            _update_leads_record(record_id, update_fields)
            lead.update(enriched)
        except asyncio.TimeoutError:
            print(f"[enrich] 轻补搜超时: {lead.get('company_name')}")
            _update_leads_record(record_id, {"补搜状态": "补搜失败"})
        except Exception as e:
            print(f"[enrich] 轻补搜异常 ({lead.get('company_name')}): {e}")
            _update_leads_record(record_id, {"补搜状态": "补搜失败"})
        # 控制频率
        await asyncio.sleep(_ENRICH_DELAY)

    _invalidate_leads_cache()
    print(f"[enrich] 轻补搜完成，开始重评分")

    # Phase 2: 重新评分（用轻补搜后的丰富信息）
    for lead in new_leads:
        record_id = lead.get("_record_id", "")
        if not record_id:
            continue
        try:
            new_score, page_type, buyer_type = await call_coze_scoring_workflow(lead)
            lead["score"] = new_score
            upd = {"综合评分": new_score, "评级": _grade_from_score(new_score)}
            if page_type:
                upd["页面类型"] = page_type
                lead["page_type"] = page_type
            if buyer_type:
                upd["买家类型"] = buyer_type
                lead["buyer_type"] = buyer_type
            # 分类闸门：补搜后重评分若判为非买家/同行，同样标记系统排除
            _ex = _ai_exclude_reason(page_type, buyer_type)
            if _ex:
                upd["系统排除"] = _ex
                lead["_system_excluded"] = _ex
            _update_leads_record(record_id, upd)
        except Exception as e:
            print(f"[enrich] 重评分失败 ({lead.get('company_name')}): {e}")
    _invalidate_leads_cache()

    # Phase 3: Top5深度补搜
    sorted_leads = sorted(new_leads, key=lambda x: x.get("score", 0), reverse=True)
    top5 = sorted_leads[:5]
    print(f"[enrich] Top5深度补搜: {[l.get('company_name','') for l in top5]}")
    for lead in top5:
        record_id = lead.get("_record_id", "")
        if not record_id:
            continue
        _update_leads_record(record_id, {"补搜状态": "深度补搜中"})
        try:
            deep = await asyncio.wait_for(
                deep_enrich_lead(
                    lead.get("company_name", ""),
                    lead.get("country", ""),
                    lead.get("website", "")),
                timeout=_ENRICH_TIMEOUT * 2)
            got_any = bool(deep.get("decision_maker") or deep.get("linkedin")
                           or deep.get("phone") or deep.get("import_record")
                           or deep.get("email"))
            if got_any:
                enrich_state = "已深度补全"
            elif deep.get("antibot"):
                enrich_state = "需人工补全·官网反爬"
            else:
                enrich_state = "已检索·未找到联系方式"
            update_fields = {"补搜状态": enrich_state}
            if deep.get("decision_maker"):
                update_fields["决策人"] = deep["decision_maker"]
                lead["decision_maker"] = deep["decision_maker"]
            if deep.get("linkedin"):
                update_fields["LinkedIn"] = deep["linkedin"]
                lead["linkedin"] = deep["linkedin"]
            if deep.get("phone"):
                update_fields["电话"] = deep["phone"]
                lead["phone"] = deep["phone"]
            if deep.get("import_record"):
                update_fields["进口记录"] = deep["import_record"]
                lead["import_record"] = deep["import_record"]
            # 深度补搜阶段也回写邮箱（修复历史写入遗漏）
            if deep.get("email"):
                update_fields["联系邮箱"] = deep["email"]
                update_fields["邮箱来源"] = lead.get("website") or ""
                lead["email"] = deep["email"]
            _update_leads_record(record_id, update_fields)
        except asyncio.TimeoutError:
            _update_leads_record(record_id, {"补搜状态": "补搜失败"})
        except Exception as e:
            print(f"[enrich] 深度补搜异常 ({lead.get('company_name')}): {e}")
            _update_leads_record(record_id, {"补搜状态": "补搜失败"})
        await asyncio.sleep(_ENRICH_DELAY)

    _invalidate_leads_cache()
    print(f"[enrich] 全量补搜流程完成")


def _start_enrichment_background(leads_with_ids: list):
    """启动后台补搜线程（异步转同步桥接）"""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_enrich_all_leads_async(leads_with_ids))
        finally:
            loop.close()
    threading.Thread(target=_run, daemon=True).start()


def _recommend_product(demand: str) -> str:
    """根据需求推荐RHC产品型号"""
    if "麻醉机" in demand:
        return _RHC_PRODUCTS[0]
    if "呼吸机" in demand:
        return _RHC_PRODUCTS[1]
    if "注射泵" in demand:
        return _RHC_PRODUCTS[2]
    if "监护仪" in demand:
        return _RHC_PRODUCTS[3]
    if "手术" in demand:
        return _RHC_PRODUCTS[4]
    return _RHC_PRODUCTS[5]


def _generate_ai_suggestion(lead: dict) -> str:
    """生成一句话跟进建议"""
    company = lead.get("company_name", "对方")
    country = lead.get("country", "")
    demand = lead.get("product_demand", "")
    grade = lead.get("confidence", "C")
    if grade == "A":
        return f"{country}{company}有明确采购意向，建议优先WhatsApp/邮件联系，发送{demand}产品目录及报价"
    elif grade == "B":
        return f"建议通过官网邮箱发送产品介绍资料，重点展示{demand}在{country}市场的应用案例"
    else:
        return f"可先通过LinkedIn或展会了解{company}业务详情，再定向推荐{demand}产品"


def _dedup_with_existing_leads(new_leads: list, existing_leads: list) -> list:
    """与飞书线索表中已有线索去重（按公司名+URL），返回去重后的新线索"""
    existing_keys = set()
    for ld in existing_leads:
        company = (ld.get("公司/机构") or ld.get("company", "") or "").strip().lower()
        url = (ld.get("原文链接") or ld.get("url") or "").strip().lower()
        if company:
            existing_keys.add(company)
        if url:
            existing_keys.add(url)
    deduped = []
    for lead in new_leads:
        company_key = lead.get("company_name", "").lower().strip()
        url_key = lead.get("website", "").lower().strip()
        if company_key and company_key in existing_keys:
            continue
        if url_key and url_key in existing_keys:
            continue
        deduped.append(lead)
    return deduped


def _build_custom_deep_queries(base_queries: list):
    """custom 校准轮的深度兜底：从本轮 query 中抽取国家，围绕同一批国家用更宽的买家意图词扩展，
    保证兜底仍在本轮主题（国家/采购方）内，而不是退回默认词池。"""
    import re
    neg = f"{_COMPETITOR_SITE_SUFFIX} {_SECONDHAND_SUFFIX}"
    countries = []
    for q in base_queries:
        # 去掉 -site/-引号 等后按词比对国家表（含多词国家 Czech Republic）
        plain = re.sub(r'-\S+', ' ', q).replace('"', ' ')
        for c in _SEARCH_COUNTRIES.keys():
            if c not in countries and re.search(rf"\b{re.escape(c)}\b", plain, flags=re.I):
                countries.append(c)
    if not countries:
        return []
    broad_terms = [
        "veterinary equipment distributor",
        "animal health products distributor",
        "veterinary clinic equipment supplier",
        "animal hospital equipment supplier",
    ]
    out = [f"{t} {c} {neg}" for c in countries for t in broad_terms]
    return out[:12]


def _build_deep_search_queries():
    """首轮结果不足时的兜底搜索：只保留真实买家身份/采购意图词，
    不再用展会参展商、协会会员、行业目录、批发清单（这些多为聚合/目录页，非买家主体）。"""
    import random
    extra = []
    # 买家身份 / 采购 / 招投标意图词（{country} 占位，按抽样国家展开）
    buyer_terms = [
        "veterinary equipment importer",
        "animal hospital procurement",
        "veterinary distributor",
        "veterinary equipment tender",
        "animal health distributor",
        "veterinary clinic equipment buyer",
    ]
    countries = random.sample(
        list(_SEARCH_COUNTRIES.keys()), min(4, len(_SEARCH_COUNTRIES)))
    for term in buyer_terms:
        for country in countries:
            extra.append(f"{term} {country}")
    # 统一追加：头部竞品 -site: 排除 + 二手/翻新精确短语排除
    _neg_suffix = f"{_COMPETITOR_SITE_SUFFIX} {_SECONDHAND_SUFFIX}"
    extra = [f"{q} {_neg_suffix}" for q in extra]
    random.shuffle(extra)
    return extra


class _DiagList(list):
    """list 子类：行为与普通 list 完全一致（append/sort/切片/len 皆可用），
    但额外允许挂自定义属性。内置 list 无 __dict__，不能直接 leads._search_diag=...。"""


def _run_lead_search(max_results: int = 30, custom_queries: Optional[list] = None) -> list:
    """执行主动搜索核心逻辑，返回结构化线索列表。
    custom_queries：管理员显式指定的原始搜索词（用于轮换国家/产品线/意图词做分布校准），
    非空时完全替代默认 query 池（仍统一追加竞品/二手负词）。
    诊断计数挂在返回列表对象的 _search_diag 属性上（_DiagList 支持自定义属性）。"""
    queries = _build_search_queries(custom_queries=custom_queries)
    all_raw = []  # [{title, url, snippet}]
    used_queries = []  # 实际执行过的 query（逐个 append，真实反映轮次；不按配额预填）
    seen_urls = set()
    empty_rounds = 0  # 连续空结果轮次，用于判断是否被搜索引擎限流
    # 控制搜索轮次与节奏：Brave 免费档 1 QPS，轮次太多既慢又耗额度；
    # 免费回退(DDG)在机房第2个查询起即被限流，多发也无意义。
    use_brave = bool(os.environ.get("BRAVE_API_KEY", "").strip())
    max_queries = min(len(queries), 25 if use_brave else 15)
    gap = 1.1 if use_brave else 0.5
    deep_search_triggered = False
    try:
        _lead_search_job["total_queries"] = max_queries
        _lead_search_job["phase"] = "正在全网搜索买家线索…"
    except Exception:
        pass
    # 熔断/免费回退/硬超时控制
    import time as _st
    search_started_at = _st.monotonic()
    hard_deadline = search_started_at + 300.0      # 整轮 5 分钟硬超时，杜绝十几分钟干等
    free_mode = _brave_breaker.get("open", False)  # 开局即熔断 → 直接走受限免费回退
    breaker_event = _brave_breaker.get("reason", "") if free_mode else ""
    free_queries_used = 0
    # 免费回退 30 秒总预算起点：开局已熔断则立即起算，运行中熔断时在 except 分支起算
    free_deadline = (_st.monotonic() + _FREE_FALLBACK_BUDGET_SEC) if free_mode else None
    search_aborted = False

    def _ingest(query, results):
        if not results:
            return False
        for r in results:
            url = r.get("url", "").lower().strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                r["_query"] = query
                all_raw.append(r)
        return True

    for i, query in enumerate(queries[:max_queries]):
        # 整轮硬超时：立即收工
        if _st.monotonic() > hard_deadline:
            search_aborted = True
            print("[search] 整轮达到5分钟硬超时，提前结束")
            break
        try:
            _lead_search_job["done_queries"] = i + 1
        except Exception:
            pass
        used_queries.append(query)
        try:
            if free_mode:
                # 免费回退：最多 _FREE_FALLBACK_MAX_QUERIES 个词、总预算 30 秒
                if free_queries_used >= _FREE_FALLBACK_MAX_QUERIES or _st.monotonic() > free_deadline:
                    search_aborted = True
                    break
                results = _search_ddg_leads(query, timeout=6, skip_brave=True, deadline=free_deadline)
                free_queries_used += 1
            else:
                results = _search_ddg_leads(query, timeout=8)
        except (BraveBillingError, BraveRateLimitError, BraveCircuitOpenError) as be:
            # 首次欠费/限流/已熔断 → 打开熔断并切免费回退（仅给 3 词 / 30 秒短上限）
            if isinstance(be, BraveRateLimitError):
                _trip_brave_breaker("rate_limited", status=429,
                                    remaining=_brave_breaker.get("remaining"))
            elif isinstance(be, BraveBillingError):
                _trip_brave_breaker("billing", status=getattr(be, "status", 402))
            breaker_event = _brave_breaker.get("reason", "open")
            free_mode = True
            free_queries_used = 0
            free_deadline = _st.monotonic() + _FREE_FALLBACK_BUDGET_SEC
            print(f"[search] Brave 熔断({type(be).__name__})，切免费回退(≤3词/30秒)")
            continue
        if not _ingest(query, results):
            empty_rounds += 1
        else:
            empty_rounds = 0
        if len(all_raw) >= max_results * 2:
            break
        if empty_rounds >= 5:
            break
        if not free_mode and i < max_queries - 1:
            time.sleep(gap)

    # ===== 深度搜索兜底：首轮结果太少且未熔断/未超时时，用更广泛关键词补充 =====
    # 熔断或免费回退模式下不做深搜（省免费引擎请求、避免再拖时间）
    if len(all_raw) < 5 and not free_mode and not search_aborted \
            and _st.monotonic() < hard_deadline:
        print(f"[search] 首轮结果不足({len(all_raw)}条)，启动深度搜索补充...")
        deep_search_triggered = True
        deep_queries = (_build_custom_deep_queries(custom_queries)
                        if custom_queries else _build_deep_search_queries())
        if not deep_queries:
            deep_queries = _build_deep_search_queries()
        max_deep = min(len(deep_queries), 10)
        for j, dq in enumerate(deep_queries[:max_deep]):
            if _st.monotonic() > hard_deadline:
                search_aborted = True
                break
            try:
                results = _search_ddg_leads(dq, timeout=8)
            except (BraveBillingError, BraveRateLimitError, BraveCircuitOpenError) as be:
                if isinstance(be, BraveRateLimitError):
                    _trip_brave_breaker("rate_limited", status=429,
                                        remaining=_brave_breaker.get("remaining"))
                elif isinstance(be, BraveBillingError):
                    _trip_brave_breaker("billing", status=getattr(be, "status", 402))
                breaker_event = _brave_breaker.get("reason", "open")
                break
            used_queries.append(dq)
            _ingest(dq, results)
            if len(all_raw) >= max_results * 3:
                break
            if j < max_deep - 1:
                time.sleep(gap)
        if all_raw:
            print(f"[search] 深度搜索补充后原始结果{len(all_raw)}条")

    # ===== 优化1：内部去重（按公司名，保留第一条） =====
    # 先固化"真·原始结果数"：此前 all_raw 在下面被去重结果覆盖，导致 diag.raw_results 失真
    raw_count_total = len(all_raw)
    _internal_seen_companies = set()
    _internal_dedup_drop = 0
    _deduped_raw = []
    for r in all_raw:
        _title = r.get("title", "")
        _url = r.get("url", "")
        # 用完整提取逻辑获取公司名（与下游保持一致）
        _tmp_info = _extract_company_info(_title, r.get("snippet", ""), _url)
        _company_key = (_tmp_info.get("company_name") or "").lower().strip()
        # 公司名为空、太短、或清洗后为空的，直接丢弃
        if not _company_key or len(_company_key) < 2:
            _internal_dedup_drop += 1
            continue
        if not _clean_company_name(_company_key):
            _internal_dedup_drop += 1
            continue
        if _company_key in _internal_seen_companies:
            _internal_dedup_drop += 1
            continue
        _internal_seen_companies.add(_company_key)
        _deduped_raw.append(r)
    if _internal_dedup_drop > 0:
        print(f"[search] 内部去重: 原始{raw_count_total}条 → 去重后{len(_deduped_raw)}条（丢弃{_internal_dedup_drop}条重复公司名）")
    all_raw = _deduped_raw

    # 提取公司信息（用 _DiagList，末尾挂 _search_diag 诊断属性）
    leads = _DiagList()
    seen_companies = set()
    dirty_dropped = 0  # 脏公司名（搜索词短语）被过滤的条数
    seller_dropped = 0  # 同行卖家货架页/经销商栏目页被过滤的条数
    gov_dropped = 0  # 政府/教育/官方贸易指南页被过滤的条数
    platform_dropped = 0  # B2B/电商撮合平台页被过滤的条数
    secondhand_dropped = 0  # 二手/翻新设备页被过滤的条数
    manufacturer_dropped = 0  # 工厂/制造商卖家标题被过滤的条数
    non_company_dropped = 0  # 非公司主体页面（展会联系页/活动页）被过滤的条数
    # P2 丢弃明细：每类最多保留 _DROP_SAMPLE_PER_REASON 条样本（标题+URL+命中query），
    # 搜索结束后落飞书「搜索丢弃日志」，供 IT 核对规则是否误伤、真买家是不是被错杀。
    _drop_samples = {"gov": [], "seller": [], "platform": [], "secondhand": [],
                     "manufacturer": [], "non_company": [], "dirty": []}

    def _record_drop(reason_key: str, rec: dict):
        _bucket = _drop_samples.get(reason_key)
        if _bucket is not None and len(_bucket) < _DROP_SAMPLE_PER_REASON:
            _bucket.append({
                "title": (rec.get("title", "") or "")[:200],
                "url": (rec.get("url", "") or "")[:500],
                "query": (rec.get("_query", "") or "")[:200],
            })

    # 搜索引擎名称（用于来源字段）
    engine_name = "Brave搜索" if use_brave else "DuckDuckGo搜索"
    for r in all_raw:
        _r_url = r.get("url", "")
        _r_title = r.get("title", "")
        # 政府/军队/教育/官方贸易指南页（非买家主体），丢弃
        if _is_gov_edu_result(_r_url):
            gov_dropped += 1
            _record_drop("gov", r)
            continue
        # 电商货架/购物路径/找经销商栏目（如 /product-category/...、/distributors/）不是买家主体，丢弃
        if _is_seller_or_section_url(_r_url):
            seller_dropped += 1
            _record_drop("seller", r)
            continue
        # 标题自述为 B2B/电商撮合平台或采购门户（非单一买家），丢弃
        if _is_platform_title(_r_title):
            platform_dropped += 1
            _record_drop("platform", r)
            continue
        # 二手/翻新设备交易页，入池前直接丢弃（搜索引擎负词挡不住 "used + 插词 + equipment"）
        if _is_secondhand_result(_r_url, _r_title):
            secondhand_dropped += 1
            _record_drop("secondhand", r)
            continue
        # 工厂/制造商自述标题（同行卖家，非采购方），直接丢弃；其余制造商交由 AI 分类闸门兜底
        if _is_manufacturer_title(_r_title):
            manufacturer_dropped += 1
            _record_drop("manufacturer", r)
            continue
        # 展会联系页/活动页/目录页等非公司主体页面，丢弃
        if _is_non_company_title(_r_title):
            non_company_dropped += 1
            _record_drop("non_company", r)
            continue
        info = _extract_company_info(_r_title, r.get("snippet", ""), _r_url)
        if not info["company_name"] or len(info["company_name"]) < 3:
            dirty_dropped += 1
            _record_drop("dirty", r)
            continue
        # 脏公司名过滤：搜索词式短语（含冒号/import/多关键词/纯产品词）不是真实公司，直接丢弃，
        # 避免污染公海池、邮件主题与评分（如 "Brazil company: Veterinary ... import"）
        if not _clean_company_name(info["company_name"]):
            dirty_dropped += 1
            _record_drop("dirty", {"title": info.get("company_name", ""),
                                   "url": _r_url, "_query": r.get("_query", "")})
            continue
        company_key = info["company_name"].lower().strip()
        if company_key in seen_companies:
            continue
        seen_companies.add(company_key)
        # TLD 后缀兜底补国家：标题/正文没识别出国家时，用域名后缀推断（只补全，不硬过滤）
        _lead_country = info["country"] or _country_from_tld(_r_url)
        _lead_region = info["region"] or _SEARCH_COUNTRIES.get(_lead_country, "其他")
        grade = _rate_lead_quality(info)
        lead = {
            "company_name": info["company_name"],
            "country": _lead_country or "未知",
            "region": _lead_region or "其他",
            "product_demand": info["product_demand"],
            "recommended_product": _recommend_product(info["product_demand"]),
            "website": r["url"],
            "email": "",
            "phone": "",
            "whatsapp": "",
            "source": f"{engine_name}: {r.get('_query', '')[:60]}",
            "confidence": grade,
            "ai_suggestion": "",
            # 保留搜索命中的原始页面信息，供 Coze 判页面类型/买家类型（与飞书 原文链接/线索标题/摘要 对应）
            "page_title": (r.get("title", "") or "")[:500],
            "page_snippet": (r.get("snippet", "") or "")[:1000],
        }
        # ===== 优化2：假线索过滤 =====
        _raw_title = r.get("title", "")
        if _is_fake_lead_title(_raw_title):
            lead["_score_penalty"] = True
            lead["score"] = 3  # 假线索直接标极低分
            lead["_fake_reason"] = "fake_title_pattern"

        # ===== 优化3：标题长度检测 =====
        if _is_long_title_product_page(_raw_title):
            lead["_score_penalty"] = True
            lead["score"] = 8  # 长标题+产品词 → 新闻稿/产品页
            lead["_long_title_reason"] = "long_title_product_page"

        # 评级/话术唯一出口：一律按分数对齐（80/60），修复"低分却给 A 级建议"
        if "score" in lead:
            lead["confidence"] = _grade_from_score(lead["score"])
        lead["ai_suggestion"] = _generate_ai_suggestion(lead)

        leads.append(lead)
        if len(leads) >= max_results:
            break

    # 按质量排序 A > B > C
    grade_order = {"A": 0, "B": 1, "C": 2}
    leads.sort(key=lambda x: grade_order.get(x.get("confidence", "C"), 3))
    _lead_search_diag["ts"] = time.time()
    print(f"[search] 搜索诊断: 查询{min(len(queries), max_queries)}轮, "
          f"原始结果{len(all_raw)}条, 脏名过滤{dirty_dropped}条, 政府/教育页过滤{gov_dropped}条, "
          f"平台页过滤{platform_dropped}条, 卖家页过滤{seller_dropped}条, "
          f"二手过滤{secondhand_dropped}条, 工厂卖家过滤{manufacturer_dropped}条, "
          f"非公司页过滤{non_company_dropped}条, "
          f"有效线索{len(leads)}条, "
          f"连续空轮次{empty_rounds}, 引擎状态={_lead_search_diag['engine_status']}, "
          f"命中引擎={_lead_search_diag['last_ok_engine'] or '无'}"
          + ("（疑似被搜索引擎限流）" if empty_rounds >= 3 and not all_raw else ""))
    try:
        leads._search_diag = {
            "queries": len(used_queries),
            "query_list": used_queries,
            "raw_count": raw_count_total,
            "internal_dedup_dropped": _internal_dedup_drop,
            "raw_results": len(all_raw),
            "dirty_dropped": dirty_dropped,
            "seller_dropped": seller_dropped,
            "gov_dropped": gov_dropped,
            "platform_dropped": platform_dropped,
            "secondhand_dropped": secondhand_dropped,
            "manufacturer_dropped": manufacturer_dropped,
            "non_company_dropped": non_company_dropped,
            "valid_leads": len(leads),
            "deep_search_triggered": deep_search_triggered,
            "empty_rounds": empty_rounds,
            "engine_status": dict(_lead_search_diag["engine_status"]),
            "last_ok_engine": _lead_search_diag["last_ok_engine"],
            # Brave 熔断/免费回退可观测
            "brave_tripped": bool(free_mode),
            "brave_trip_reason": breaker_event or "",
            "free_fallback_queries": free_queries_used,
            "search_aborted": bool(search_aborted),
            # P2 丢弃明细：{原因: [{title,url,query}...]}，供落盘与误伤核对
            "drop_samples": {k: v for k, v in _drop_samples.items() if v},
        }
    except Exception as _diag_e:
        print(f"[search] 诊断信息挂载失败（不影响线索结果）: {_diag_e}")
    return leads


class LeadSearchRequest(BaseModel):
    max_results: Optional[int] = 30
    force_refresh: Optional[bool] = False
    # 仅管理员：显式指定本轮搜索词（轮换国家/产品线/意图词做分布校准），非空时替代默认词池
    queries: Optional[list] = None
    # 仅管理员：充值后「强制跑一轮」——先复位 Brave 熔断再搜
    force_run: Optional[bool] = False


@app.get("/api/leads/search/status")
async def api_leads_search_status(request: Request):
    """前端轮询：返回当前异步搜索任务的进度/结果。需登录。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    st = _lead_search_job.get("status")
    if st == "finished" and _lead_search_job.get("result") is not None:
        payload = dict(_lead_search_job["result"])
        payload["ok"] = True
        payload["status"] = "finished"
        return payload
    if st == "error":
        return {"ok": False, "status": "error",
                "message": _lead_search_job.get("error") or "搜索失败，请稍后重试"}
    # 只有真正在执行（含后台评分/补搜）才报 running；初始/空闲态必须回 idle，
    # 否则重启后 status=idle 会被误报成 running，前端永久转圈且「搜索新线索」按钮置灰。
    if not (_lead_search_job.get("running") or _search_in_progress):
        return {"ok": True, "status": "idle",
                "searching": False, "phase": "",
                "done_queries": 0, "total_queries": 0,
                "brave_breaker": dict(_brave_breaker)}
    return {
        "ok": True, "status": "running",
        "searching": _lead_search_job.get("searching", True),
        "phase": _lead_search_job.get("phase", ""),
        "done_queries": _lead_search_job.get("done_queries", 0),
        "total_queries": _lead_search_job.get("total_queries", 0),
        "brave_breaker": dict(_brave_breaker),
    }


@app.post("/api/leads/search")
async def api_leads_search(request: Request, req: Optional[LeadSearchRequest] = None):
    """AI主动搜索：立即在后台启动全网搜索任务并返回（不阻塞，避免长请求被网关掐断）。
    前端随后轮询 GET /api/leads/search/status 获取进度与结果。需登录。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)

    max_results = 30
    force_refresh = False
    custom_queries = None
    if req:
        max_results = min(req.max_results or 30, 50)
        force_refresh = req.force_refresh or False
        if req.queries:
            # 自定义 query 仅管理员可用（用于分布校准），普通销售忽略此项
            if user_info.get("role") != "admin":
                return JSONResponse(
                    {"ok": False, "message": "自定义搜索词仅管理员可用"}, status_code=403)
            cq = [str(q).strip() for q in req.queries if str(q).strip()]
            if cq:
                custom_queries = cq[:25]  # 上限 25，与默认轮次一致
        # 充值后强制跑：仅管理员，先复位 Brave 熔断
        if getattr(req, "force_run", False):
            if user_info.get("role") != "admin":
                return JSONResponse({"ok": False, "message": "强制跑一轮仅管理员可用"}, status_code=403)
            if _brave_breaker.get("open"):
                reset_brave_breaker()

    # 30 秒内已有结果且未强制刷新：直接命中缓存，无需启动后台任务（自定义 query 不走缓存）
    now = time.time()
    if not force_refresh and custom_queries is None \
            and _search_results_cache["data"] is not None \
            and now - _search_results_cache["ts"] < _SEARCH_CACHE_TTL:
        return {"ok": True, "status": "finished", "items": _search_results_cache["data"],
                "total": len(_search_results_cache["data"]), "cached": True}

    # 并发锁：上一轮（含评分+补搜）未结束则不允许重复启动
    if _search_in_progress:
        return {"ok": False, "status": "busy",
                "message": "上一轮搜索仍在进行中（含后台评分+补搜），请稍候查看进度"}

    # 初始化任务状态并启动后台线程
    global _search_round
    _search_round += 1
    round_id = _search_round
    _lead_search_job.update({
        "running": True, "searching": True, "status": "running", "round": round_id,
        "phase": "正在全网搜索买家线索…", "done_queries": 0, "total_queries": 0,
        "result": None, "error": "", "ts": time.time(),
    })
    threading.Thread(target=_run_lead_search_job,
                     args=(max_results,),
                     kwargs={"custom_queries": custom_queries, "round_id": round_id},
                     daemon=True).start()
    return {"ok": True, "status": "started",
            "message": "搜索已开始，请稍候查看结果"}


def _run_lead_search_job(max_results: int = 30, custom_queries: Optional[list] = None,
                         round_id: int = 0):
    """后台执行完整搜索管线（迁移公海→全网搜索→去重→写入飞书→启动评分/补搜）。
    全程把进度/结果写入 _lead_search_job，供状态接口轮询；不向调用方抛异常。
    round_id：轮次代号，过期轮次只更新飞书、不写全局状态，避免与新轮串台。"""
    global _search_in_progress, _last_search_record_ids

    def _current():
        return _search_round

    def _stale():
        return _current() != round_id

    _search_in_progress = True
    leads_with_record_ids = []
    coze_leads = []  # 需送 Coze 复核/定夺的真实主体（三档分流后才有后台线程持有锁）
    try:
        _lead_search_job["searching"] = True
        _lead_search_job["phase"] = "正在把上一轮线索整理进公海池…"
        if _last_search_record_ids:
            migrate_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            tid = _ensure_leads_table()
            for rid in _last_search_record_ids:
                try:
                    _feishu_api(
                        "PUT",
                        f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{rid}",
                        {"fields": {"入池时间": migrate_time}})
                except Exception as me:
                    print(f"[search] 迁移线索到公海池失败 ({rid}): {me}")
            _invalidate_leads_cache()
            print(f"[search] 已将 {len(_last_search_record_ids)} 条上轮线索推入公海池")

        # 1) 执行基础搜索（本函数已在后台线程中，直接同步调用）
        _lead_search_job["searching"] = True
        _lead_search_job["phase"] = "正在全网搜索买家线索…"
        raw_leads = _run_lead_search(max_results, custom_queries=custom_queries)
        # 搜索耗时较长：若期间已启动更新一轮，则本轮作废，只保留已写飞书的数据，不碰全局结果
        if _stale():
            print(f"[search] 轮次{round_id}已过期（当前{_current()}），放弃全局结果写入")
            return
        search_diag = getattr(raw_leads, "_search_diag", None) or {}
        _lead_search_job["searching"] = False
        _lead_search_job["phase"] = "正在去重、评分并写入线索表…"

        # P2：丢弃明细异步落飞书「搜索丢弃日志」（best-effort，不阻塞搜索/不占用锁时间）
        if search_diag.get("drop_samples"):
            threading.Thread(
                target=_persist_drop_logs, args=(search_diag, round_id),
                daemon=True).start()

        # 2) 与已有线索去重
        try:
            existing_leads = _fetch_leads(force_refresh=True)
        except Exception:
            existing_leads = []
        new_leads = _dedup_with_existing_leads(raw_leads, existing_leads)

        # 3) 写入飞书线索表（补搜状态=未补搜），记录record_id供后台补搜使用
        tid = _ensure_leads_table()
        now_iso = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        leads_with_record_ids = []
        _company_count = 0  # 规则初判为 company 主体的新线索数（供 stats 体检）
        _rule_finalized_count = 0  # 规则层已完全定性、将跳过 Coze 的新线索数（三档分流·第一档）
        for lead in new_leads:
            # 先用规则兜底分（Coze 评分改到后台异步跑，避免阻塞 API 超时）
            _pt_init = _rule_guess_page_type(
                lead.get("website", ""), lead.get("page_title", ""), lead.get("page_snippet", ""))
            _bt_init = _rule_guess_buyer_type(
                lead.get("website", ""), lead.get("page_title", ""), lead.get("page_snippet", ""))
            # 域名强制分类：竞品/平台黄页 host 不依赖 Coze 即落正确类型与固定分
            _pt_init, _bt_init = _apply_host_page_override(
                _pt_init, _bt_init, lead.get("website", ""))
            if _pt_init == "company":
                _company_count += 1
            if lead.get("_score_penalty"):
                score = lead.get("score", 5)
                print(f"[search] 假线索/长标题跳过评分: {lead.get('company_name','')[:30]} → {score}分")
            elif _is_excluded_page_type(_pt_init):
                # 规则/host 明确的非公司页（竞品5/黄页等10）：落库即固定分，不挂占位分
                # 注意：规则粗判 company+manufacturer 不在此列，降二档走兜底分并送 Coze（防误伤经销商）
                score = _finalize_lead_score(0, {}, _pt_init, _bt_init)
                lead["score"] = score
            else:
                score = _rule_fallback_score({
                    "company_name": lead.get("company_name", ""),
                    "country": lead.get("country", ""),
                    "product": lead.get("product_demand", ""),
                    "industry": "",
                    "website": "",
                    "email_pattern": "",
                    "confidence": lead.get("confidence", "C"),
                    "decision_maker": "",
                    "linkedin": "",
                }, _pt_init, _bt_init)
                lead["score"] = score
            # 三档分流标记：规则完全定性 → 后台不送 Coze、不补搜；company/unknown → 送 Coze
            lead["_rule_finalized"] = _rule_finalized_classification(_pt_init, _bt_init, lead)
            if lead["_rule_finalized"]:
                _rule_finalized_count += 1
            fields = {
                "线索标题": f"{lead.get('company_name', '')}（{lead.get('country', '')}）",
                "商机类型": "渠道动态",
                "公司/机构": lead.get("company_name", ""),
                "摘要": (lead.get("ai_suggestion", "") or "")[:2000],
                "来源": lead.get("source", "DuckDuckGo搜索")[:200],
                "原文链接": lead.get("website", ""),
                "地区": f"{lead.get('region', '')} - {lead.get('country', '')}",
                "发布日期": now_iso[:10],
                "认领状态": "未认领",
                "认领人": "",
                "认领时间": "",
                "状态": "跟进中",
                "联系邮箱": "",
                "跟进备注": "",
                "邮箱来源": "",
                "综合评分": score,
                "评级": _grade_from_score(score),
                "补搜状态": "未补搜",
                "官网": "",
                "行业": "",
                "邮箱格式": "",
                "决策人": "",
                "LinkedIn": "",
                "进口记录": "",
                "入池时间": now_iso,
                # 页面/买家类型先用规则粗判写初值，Coze 返回后由 AI 覆盖
                "页面类型": _pt_init,
                "买家类型": _bt_init,
                "电话": lead.get("phone", "") or "",
            }
            # 一档（规则完全定性、跳过 Coze）：落库即写"具体类别"系统排除（非笼统文案），
            # 并打 quality_flag=rule_finalized 便于日后误杀申诉时回溯是规则直接判定的。
            # 规则粗判 manufacturer 已降二档，这里 _rule_finalized_category 不会再给 manufacturer。
            _ex_cat = _rule_finalized_category(_pt_init, _bt_init, lead) if lead.get("_rule_finalized") else ""
            if _ex_cat:
                fields["系统排除"] = "rule:" + _ex_cat
                fields["质量标记"] = "rule_finalized"
            try:
                resp = _feishu_api(
                    "POST",
                    f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records",
                    {"fields": fields})
                record_id = resp.get("data", {}).get("record", {}).get("record_id", "")
                lead["_record_id"] = record_id
                leads_with_record_ids.append(lead)
            except Exception as we:
                print(f"[search] 写入线索表失败: {we}")
        # 记录本次搜索结果的 record_id，供下次搜索时推入公海池（仅最新轮次可写）
        if not _stale():
            _last_search_record_ids = [l["_record_id"] for l in leads_with_record_ids if l.get("_record_id")]
        _invalidate_leads_cache()

        # 4) 后台异步 Coze 评分 + 补搜（不阻塞 API 返回）
        # 三档分流（IT 2026-09-20）：规则已完全定性的（竞品/黄页/平台/新闻/导航/同行/假标题/
        # 长标题产品页）落库即固定分+系统排除，后台既不送 Coze 也不补搜，省 60-70% 无效调用、
        # 且不再拖长搜索锁；只有规则粗判 company（待复核）或 unknown（待定夺）的才送 Coze+补搜。
        coze_leads = [l for l in leads_with_record_ids if not l.get("_rule_finalized")]
        skipped_finalized = [l for l in leads_with_record_ids if l.get("_rule_finalized")]
        if coze_leads:
            print(f"[search] 本轮新线索{len(leads_with_record_ids)}条："
                  f"规则定性跳过Coze {len(skipped_finalized)}条，送Coze复核/定夺 {len(coze_leads)}条")

            def _background_score_and_enrich():
                try:
                    # 先跑 Coze 评分，更新飞书表（仅 company 粗判 / 字段全空两档）
                    for lead in coze_leads:
                        try:
                            new_score, page_type, buyer_type = asyncio.run(
                                call_coze_scoring_workflow(lead))
                            lead["score"] = new_score
                            lead["page_type"] = page_type
                            lead["buyer_type"] = buyer_type
                            if lead.get("_record_id"):
                                upd_fields = {"页面类型": page_type or "unknown",
                                              "买家类型": buyer_type or "unknown",
                                              "综合评分": lead.get("score", 0)}
                                # 评级始终按当前分数同源回写飞书「评级」列（修复撕裂数据的关键一环）
                                upd_fields["评级"] = _grade_from_score(lead.get("score", 0))
                                # 分数变化后评级与跟进话术同步对齐（修复"低分却给 A 级建议"）
                                lead["confidence"] = _grade_from_score(lead.get("score", 0))
                                lead["ai_suggestion"] = _generate_ai_suggestion(lead)
                                upd_fields["摘要"] = (lead.get("ai_suggestion", "") or "")[:2000]
                                # 分类闸门：非买家/同行 → 标记系统排除（只标记不删除，公海池过滤，ai: 前缀）
                                _ex = _ai_exclude_reason(page_type, buyer_type)
                                if _ex:
                                    _reason = "ai:" + _ex.split(":", 1)[-1]
                                    upd_fields["系统排除"] = _reason
                                    lead["_system_excluded"] = _reason
                                _feishu_api(
                                    "PUT",
                                    f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{lead['_record_id']}",
                                    {"fields": upd_fields})
                        except Exception as se:
                            print(f"[search-bg] Coze评分失败: {lead.get('company_name','')}: {se}")
                    # 再跑补搜：只补 Coze 复核/定夺过的真实主体，规则定性的非买家不补搜
                    _start_enrichment_background(coze_leads)
                except Exception as e:
                    print(f"[search-bg] 后台评分+补搜异常: {e}")
                finally:
                    # 仅最新轮次可释放锁/改全局；过期轮次的评分线程只收尾飞书写库
                    if not _stale():
                        _search_in_progress = False
                        _lead_search_job["running"] = False
                        if _lead_search_job.get("status") == "finished":
                            _lead_search_job["phase"] = "本轮 AI 评分与联系方式补全已完成"
                    print(f"[search-bg] 轮次{round_id} 后台任务收尾（stale={_stale()}），锁={'释放' if not _stale() else '保留给新轮'}")
            threading.Thread(target=_background_score_and_enrich, daemon=True).start()

        # 5) 为每条新线索自动创建消息通知（后台线程，不阻塞返回）
        if new_leads:
            def _create_messages():
                try:
                    msg_tid = _ensure_messages_table()
                    now_iso2 = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                    for lead in new_leads[:10]:
                        grade = lead.get("confidence", "C")
                        grade_emoji = "🔴" if grade == "A" else ("🟡" if grade == "B" else "⚪")
                        title = f"{grade_emoji} 新商机线索: {lead['company_name']}（{lead['country']}）"
                        content = (
                            f"公司: {lead['company_name']}\n"
                            f"国家: {lead['country']} | 地区: {lead['region']}\n"
                            f"需求: {lead['product_demand']}\n"
                            f"推荐产品: {lead['recommended_product']}\n"
                            f"质量评级: {grade}级 | 综合评分: {lead.get('score', 0)}\n"
                            f"官网: {lead.get('website', '未知')}\n"
                            f"跟进建议: {lead.get('ai_suggestion', '')}"
                        )
                        fields = {
                            "消息标题": title,
                            "消息类型": "商机线索",
                            "消息内容": content[:2000],
                            "接收人": "全部销售",
                            "已读状态": "未读",
                            "关联线索ID": "",
                            "创建时间": now_iso2,
                        }
                        try:
                            _feishu_api(
                                "POST",
                                f"/bitable/v1/apps/{FEISHU_ATK}/tables/{msg_tid}/records",
                                {"fields": fields})
                        except Exception as we:
                            print(f"[messages] 自动创建消息失败: {we}")
                except Exception as e:
                    print(f"[messages] 搜索后自动推送消息失败: {e}")
            threading.Thread(target=_create_messages, daemon=True).start()

        # 6) 缓存结果（仅最新轮次可写，防止过期轮次覆盖新轮结果/stats）
        if _stale():
            print(f"[search] 轮次{round_id}完成前已过期，放弃结果缓存与状态写入")
            return
        _search_results_cache["data"] = new_leads
        _search_results_cache["ts"] = time.time()

        # 统计
        a_count = sum(1 for l in new_leads if l.get("confidence") == "A")
        b_count = sum(1 for l in new_leads if l.get("confidence") == "B")
        c_count = sum(1 for l in new_leads if l.get("confidence") == "C")

        # 立即返回基础结果，后台补搜异步进行中
        # 无新线索时给出真实原因，避免"假无线索"
        empty_reason = ""
        if not new_leads:
            if search_diag.get("raw_results", 0) == 0:
                es = search_diag.get("engine_status", {})
                blocked = all(("限流" in str(v) or "验证" in str(v)) for v in es.values()) if es else False
                empty_reason = ("当前免费搜索引擎对服务器IP限流，稍后可重试；"
                                "系统已在定时任务中持续补搜。" if blocked
                                else "本轮各搜索引擎均未返回可解析结果，可能临时受限，请稍后重试。")
            elif not raw_leads:
                empty_reason = "搜索到的结果均为目录页/聚合页，未能识别出真实公司，已自动过滤。"
            else:
                empty_reason = "本轮搜到的线索此前均已入库，暂无新增（已自动去重）。"
        result_payload = {
            "items": new_leads,
            "total": len(new_leads),
            "cached": False,
            "empty_reason": empty_reason,
            "diag": search_diag,
            "enrichment": "started" if coze_leads else "none",
            "stats": {
                "total_found": len(raw_leads),
                "raw_count": search_diag.get("raw_count", search_diag.get("raw_results", 0)),
                "post_internal_dedup": search_diag.get("raw_results", 0),
                "internal_dedup_dropped": search_diag.get("internal_dedup_dropped", 0),
                "existing_dedup_dropped": len(raw_leads) - len(new_leads),
                "dedup_count": (search_diag.get("internal_dedup_dropped", 0)
                                + max(0, len(raw_leads) - len(new_leads))),
                "new_leads": len(new_leads),
                "company_count": _company_count,
                # 三档分流：规则已定性跳过 Coze 数 / 实际送 Coze 数（节省无效调用可追溯）
                "rule_finalized_skipped": _rule_finalized_count,
                "coze_called": len(coze_leads),
                "new_after_dedup": len(new_leads),
                "a_grade": a_count,
                "b_grade": b_count,
                "c_grade": c_count,
                "queries": search_diag.get("queries", 0),
                "query_list": search_diag.get("query_list", []),
                "deep_search_triggered": search_diag.get("deep_search_triggered", False),
                "gov_dropped": search_diag.get("gov_dropped", 0),
                "platform_dropped": search_diag.get("platform_dropped", 0),
                "secondhand_dropped": search_diag.get("secondhand_dropped", 0),
                "manufacturer_dropped": search_diag.get("manufacturer_dropped", 0),
                "seller_dropped": search_diag.get("seller_dropped", 0),
                "non_company_dropped": search_diag.get("non_company_dropped", 0),
                "dirty_dropped": search_diag.get("dirty_dropped", 0),
                "raw_results": search_diag.get("raw_count", search_diag.get("raw_results", 0)),
                "engine_status": search_diag.get("engine_status", {}),
                "last_ok_engine": search_diag.get("last_ok_engine", ""),
                "empty_rounds": search_diag.get("empty_rounds", 0),
                # Brave 熔断/免费回退可观测
                "brave_tripped": search_diag.get("brave_tripped", False),
                "brave_trip_reason": search_diag.get("brave_trip_reason", ""),
                "free_fallback_queries": search_diag.get("free_fallback_queries", 0),
                "search_aborted": search_diag.get("search_aborted", False),
            },
            "brave_breaker": dict(_brave_breaker),
            "search_time": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 有送 Coze 的真实主体时，后台评分/补搜仍在跑，先置 finished 供前端展示，
        # _search_in_progress 由评分线程 finally 释放；规则全部定性（无 Coze 任务）则立即释放锁。
        if coze_leads:
            _lead_search_job["phase"] = "搜索完成，后台正在 AI 评分与补全联系方式…"
        else:
            _search_in_progress = False
            _lead_search_job["running"] = False
        _lead_search_job["status"] = "finished"
        _lead_search_job["result"] = result_payload
        return
    except Exception as e:
        import traceback
        print(f"[search] 主动搜索失败: {e}")
        traceback.print_exc()
        if not _stale():
            _search_in_progress = False
            _lead_search_job.update({
                "running": False, "searching": False,
                "status": "error", "error": f"搜索失败：{e}"})
        return
    finally:
        # 无后台评分线程时（或搜索阶段就异常）立即释放锁；有评分线程时由其 finally 释放。
        # 过期轮次一律不动锁与全局状态（交给最新轮次管理）。
        if _stale():
            return
        if not coze_leads:
            _search_in_progress = False
            _lead_search_job["running"] = False
            if _lead_search_job.get("status") == "running":
                _lead_search_job["searching"] = False


# ============================================================
# 消息通知系统（飞书「消息通知」表 + 4个API端点）
# 门户右上角消息铃铛：未读计数、列表、标记已读、全部已读
# ============================================================
MESSAGES_TABLE_NAME = "消息通知"
MESSAGE_TYPE_OPTIONS = ("商机线索", "系统通知", "审批通知", "邮件通知")
MESSAGE_STATUS_OPTIONS = ("未读", "已读")

_messages_table_id = None
_messages_cache = {"data": None, "ts": 0.0}
_MESSAGES_CACHE_TTL = 30


def _ensure_messages_table():
    """确保多维表中存在「消息通知」表，返回 table_id；不存在则自动创建。"""
    global _messages_table_id
    if _messages_table_id:
        return _messages_table_id
    if not FEISHU_ATK:
        raise RuntimeError("FEISHU_APP_TOKEN 未配置")
    resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables?page_size=100")
    for t in resp.get("data", {}).get("items", []):
        if t.get("name") == MESSAGES_TABLE_NAME:
            _messages_table_id = t.get("table_id")
            return _messages_table_id
    fields = [
        {"field_name": "消息标题", "type": 1},   # 文本（主字段）
        {"field_name": "消息类型", "type": 3,    # 单选
         "property": {"options": [{"name": n} for n in MESSAGE_TYPE_OPTIONS]}},
        {"field_name": "消息内容", "type": 1},   # 文本
        {"field_name": "接收人", "type": 1},     # 文本
        {"field_name": "已读状态", "type": 3,    # 单选
         "property": {"options": [{"name": n} for n in MESSAGE_STATUS_OPTIONS]}},
        {"field_name": "关联线索ID", "type": 1}, # 文本
        {"field_name": "创建时间", "type": 1},   # 文本
    ]
    resp = _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables",
                       {"table": {"name": MESSAGES_TABLE_NAME,
                                  "default_view_name": "消息列表",
                                  "fields": fields}})
    _messages_table_id = resp.get("data", {}).get("table_id")
    if not _messages_table_id:
        raise RuntimeError(f"创建「{MESSAGES_TABLE_NAME}」表失败: {resp}")
    print(f"[messages] 已创建飞书消息表「{MESSAGES_TABLE_NAME}」: {_messages_table_id}")
    return _messages_table_id


def _norm_message_record(rec: dict) -> dict:
    """飞书记录 -> 归一化消息字段"""
    fl = rec.get("fields", {})
    return {
        "record_id": rec.get("record_id", ""),
        "title": _tv(fl.get("消息标题")),
        "type": _tv(fl.get("消息类型")),
        "content": _tv(fl.get("消息内容")),
        "receiver": _tv(fl.get("接收人")),
        "status": _tv(fl.get("已读状态")) or "未读",
        "lead_id": _tv(fl.get("关联线索ID")),
        "created_at": _tv(fl.get("创建时间")),
    }


def _fetch_messages(force_refresh=False) -> list:
    """读取消息表全部记录（按创建时间倒序），30秒缓存。"""
    now = time.time()
    if not force_refresh and _messages_cache["data"] is not None \
            and now - _messages_cache["ts"] < _MESSAGES_CACHE_TTL:
        return list(_messages_cache["data"])
    tid = _ensure_messages_table()
    items = []
    page_token = None
    while True:
        path = f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records?page_size=100"
        if page_token:
            path += f"&page_token={page_token}"
        resp = _feishu_api("GET", path)
        data = resp.get("data", {})
        for it in data.get("items", []):
            items.append(_norm_message_record(it))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    _messages_cache["data"] = items
    _messages_cache["ts"] = now
    return list(items)


def _invalidate_messages_cache():
    _messages_cache["data"] = None
    _messages_cache["ts"] = 0.0


def _warmup_messages_table():
    """启动后台预热消息表"""
    try:
        _fetch_messages(force_refresh=True)
        print(f"[messages] 飞书消息表初始化完成（{len(_messages_cache['data'] or [])} 条）")
    except Exception as e:
        print(f"[messages] 飞书消息表初始化失败（接口调用时将自动重试）: {e}")


class CreateMessageRequest(BaseModel):
    title: str = ""
    type: str = "系统通知"
    content: str = ""
    receiver: str = ""
    lead_id: str = ""


@app.get("/api/messages")
async def api_messages_list(request: Request, status: str = "", type: str = ""):
    """获取消息列表，支持 ?status=unread 过滤未读、?type=商机线索 按类型过滤。需登录。

    type 支持传入单个类型（如 ?type=商机线索），也支持逗号分隔多个类型
    （如 ?type=商机线索,系统通知），用于模块内通知只拉取本模块相关消息。
    """
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        messages = _fetch_messages(force_refresh=True)
        # 按状态过滤
        if status and status.lower() == "unread":
            messages = [m for m in messages if m.get("status") == "未读"]
        # 按类型过滤（支持逗号分隔多个类型）
        if type:
            wanted_types = {t.strip() for t in type.split(",") if t.strip()}
            if wanted_types:
                messages = [m for m in messages if m.get("type") in wanted_types]
        # 按接收人过滤（"全部销售"对所有人生效，否则匹配当前用户姓名）
        my_name = user_info.get("name", "")
        filtered = []
        for m in messages:
            receiver = m.get("receiver", "")
            if not receiver or receiver == "全部销售" or receiver == "全部" or \
                    my_name in receiver or user_info.get("username", "") in receiver:
                filtered.append(m)
        return {"ok": True, "items": filtered, "total": len(filtered)}
    except Exception as e:
        print(f"[messages] 读取消息列表失败: {e}")
        return JSONResponse({"ok": False, "message": f"读取消息失败：{e}"}, status_code=502)


@app.get("/api/messages/unread-count")
async def api_messages_unread_count(request: Request, type: str = ""):
    """获取当前用户未读消息数量。需登录。

    支持 ?type=商机线索 按类型统计（模块内通知角标用），也支持逗号分隔多类型。
    """
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        messages = _fetch_messages(force_refresh=True)
        # 按类型过滤（支持逗号分隔多个类型）
        wanted_types = None
        if type:
            wanted_types = {t.strip() for t in type.split(",") if t.strip()}
        my_name = user_info.get("name", "")
        unread = 0
        for m in messages:
            if m.get("status") != "未读":
                continue
            if wanted_types and m.get("type") not in wanted_types:
                continue
            receiver = m.get("receiver", "")
            if not receiver or receiver == "全部销售" or receiver == "全部" or \
                    my_name in receiver or user_info.get("username", "") in receiver:
                unread += 1
        return {"ok": True, "count": unread}
    except Exception as e:
        print(f"[messages] 读取未读计数失败: {e}")
        return JSONResponse({"ok": False, "message": f"读取未读计数失败：{e}"}, status_code=502)


@app.put("/api/messages/{record_id}/read")
async def api_messages_mark_read(record_id: str, request: Request):
    """标记单条消息为已读。需登录。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        tid = _ensure_messages_table()
        _feishu_api(
            "PUT",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}",
            {"fields": {"已读状态": "已读"}})
        _invalidate_messages_cache()
        return {"ok": True, "message": "已标记为已读"}
    except Exception as e:
        print(f"[messages] 标记已读失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"标记已读失败：{e}"}, status_code=502)


@app.post("/api/messages/read-all")
async def api_messages_read_all(request: Request):
    """标记当前用户所有消息为已读（批量更新）。需登录。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        messages = _fetch_messages(force_refresh=True)
        tid = _ensure_messages_table()
        my_name = user_info.get("name", "")
        updated = 0
        for m in messages:
            if m.get("status") != "未读":
                continue
            receiver = m.get("receiver", "")
            if not receiver or receiver == "全部销售" or receiver == "全部" or \
                    my_name in receiver or user_info.get("username", "") in receiver:
                try:
                    _feishu_api(
                        "PUT",
                        f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{m['record_id']}",
                        {"fields": {"已读状态": "已读"}})
                    updated += 1
                except Exception:
                    pass
        _invalidate_messages_cache()
        return {"ok": True, "message": f"已标记 {updated} 条消息为已读", "updated": updated}
    except Exception as e:
        print(f"[messages] 全部标记已读失败: {e}")
        return JSONResponse({"ok": False, "message": f"标记已读失败：{e}"}, status_code=502)


@app.post("/api/messages")
async def api_messages_create(req: CreateMessageRequest, request: Request):
    """手动创建消息通知（系统通知/审批通知等）。需登录，admin可发给指定人。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    title = (req.title or "").strip()
    if not title:
        return JSONResponse({"ok": False, "message": "消息标题不能为空"}, status_code=400)
    msg_type = (req.type or "系统通知").strip()
    if msg_type not in MESSAGE_TYPE_OPTIONS:
        return JSONResponse({"ok": False, "message": f"消息类型仅支持：{'/'.join(MESSAGE_TYPE_OPTIONS)}"},
                            status_code=400)
    try:
        tid = _ensure_messages_table()
        now_iso = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        fields = {
            "消息标题": title,
            "消息类型": msg_type,
            "消息内容": (req.content or "")[:2000],
            "接收人": (req.receiver or "全部销售").strip(),
            "已读状态": "未读",
            "关联线索ID": (req.lead_id or "").strip(),
            "创建时间": now_iso,
        }
        resp = _feishu_api(
            "POST",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records",
            {"fields": fields})
        _invalidate_messages_cache()
        rec = resp.get("data", {}).get("record", {})
        return {"ok": True, "message": _norm_message_record(rec) if rec else fields}
    except Exception as e:
        print(f"[messages] 创建消息失败: {e}")
        return JSONResponse({"ok": False, "message": f"创建消息失败：{e}"}, status_code=502)

# ============================================================
# 开发信（冷邮件）：飞书「开发信记录」表 + SMTP 发信 + AI/模板生成
# ============================================================
EMAILS_TABLE_NAME = "开发信记录"
EMAIL_STATUS_OPTIONS = ("成功", "失败")
EMAIL_GEN_OPTIONS = ("AI", "模板")

_emails_table_id = None


def _settings_val(name: str, default=""):
    """读取 settings 配置，兼容 config.py 大写字段名与 main.py 兜底 Settings 小写字段名。"""
    v = getattr(settings, name, None)
    if v in (None, ""):
        v = getattr(settings, name.upper(), None)
    return v if v not in (None, "") else default


def _ensure_emails_table():
    """确保多维表中存在「开发信记录」表，返回 table_id；不存在则自动创建。"""
    global _emails_table_id
    if _emails_table_id:
        return _emails_table_id
    if not FEISHU_ATK:
        raise RuntimeError("FEISHU_APP_TOKEN 未配置")
    resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables?page_size=100")
    for t in resp.get("data", {}).get("items", []):
        if t.get("name") == EMAILS_TABLE_NAME:
            _emails_table_id = t.get("table_id")
            return _emails_table_id
    fields = [
        {"field_name": "客户公司", "type": 1},   # 文本（主字段）
        {"field_name": "收件邮箱", "type": 1},   # 文本（逗号分隔多个）
        {"field_name": "主题", "type": 1},       # 文本
        {"field_name": "正文", "type": 1},       # 文本（长文本）
        {"field_name": "发件人", "type": 1},     # 文本（实际发件邮箱/姓名+邮箱）
        {"field_name": "发送状态", "type": 3,    # 单选
         "property": {"options": [{"name": n} for n in EMAIL_STATUS_OPTIONS]}},
        {"field_name": "发送时间", "type": 1},   # 文本（北京时间）
        {"field_name": "线索record_id", "type": 1},  # 文本（关联商机线索记录）
        {"field_name": "生成方式", "type": 3,    # 单选：AI/模板
         "property": {"options": [{"name": n} for n in EMAIL_GEN_OPTIONS]}},
    ]
    resp = _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables",
                       {"table": {"name": EMAILS_TABLE_NAME,
                                  "default_view_name": "开发信列表",
                                  "fields": fields}})
    _emails_table_id = resp.get("data", {}).get("table_id")
    if not _emails_table_id:
        raise RuntimeError(f"创建「{EMAILS_TABLE_NAME}」表失败: {resp}")
    print(f"[emails] 已创建飞书开发信记录表「{EMAILS_TABLE_NAME}」: {_emails_table_id}")
    return _emails_table_id


def _norm_email_record(rec: dict) -> dict:
    """飞书开发信记录 -> 归一化英文字段（供前端使用，另附 record_id）。"""
    fl = rec.get("fields", {})
    return {
        "record_id": rec.get("record_id", ""),
        "company_name": _tv(fl.get("客户公司")),
        "recipient_email": _tv(fl.get("收件邮箱")),
        "subject": _tv(fl.get("主题")),
        "body": _tv(fl.get("正文")),
        "sender": _tv(fl.get("发件人")),
        "status": _tv(fl.get("发送状态")) or "成功",
        "sent_at": _tv(fl.get("发送时间")),
        "lead_record_id": _tv(fl.get("线索record_id")),
        "generated_by": _tv(fl.get("生成方式")),
    }


def _fetch_emails() -> list:
    """读取开发信记录表全部记录（按发送时间倒序）。写入量低，直接实时查询。"""
    tid = _ensure_emails_table()
    items = []
    page_token = None
    while True:
        path = f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records?page_size=100"
        if page_token:
            path += f"&page_token={page_token}"
        resp = _feishu_api("GET", path)
        data = resp.get("data", {})
        for it in data.get("items", []):
            items.append(_norm_email_record(it))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    items.sort(key=lambda x: x.get("sent_at", ""), reverse=True)
    return items


def _warmup_emails_table():
    """启动后台预热开发信记录表（自动建表），失败不影响服务启动。"""
    try:
        n = len(_fetch_emails())
        print(f"[emails] 飞书开发信记录表初始化完成（{n} 条）")
    except Exception as e:
        print(f"[emails] 飞书开发信记录表初始化失败（接口调用时将自动重试）: {e}")


# ------------------------------------------------------------
# SMTP 发信（标准库 smtplib + email.mime，SSL 465，零新依赖）
# ------------------------------------------------------------
def _split_mail_addrs(s) -> list:
    """逗号/分号分隔的收件人字符串 -> 邮箱列表。"""
    if isinstance(s, (list, tuple)):
        return [str(a).strip() for a in s if str(a).strip()]
    return [a.strip() for a in re.split(r"[;,]", s or "") if a.strip()]


def _smtp_send_mail(to_addrs, subject, body, cc=None,
                    sender_email=None, sender_password=None) -> str:
    """同步 SMTP 发信（阻塞，必须在 asyncio.to_thread 中调用）。
    支持 SSL 465 / STARTTLS 587；成功返回实际发件地址，失败抛 RuntimeError。"""
    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr

    to_list = _split_mail_addrs(to_addrs)
    cc_list = _split_mail_addrs(cc)
    if not to_list:
        raise RuntimeError("收件人邮箱不能为空")
    # 请求体传入 sender_email/sender_password 时以该销售邮箱登录（多销售多邮箱预留），
    # 否则用系统默认配置 settings.smtp_*。
    smtp_user = (sender_email or "").strip() or _settings_val("smtp_user", "")
    smtp_password = (sender_password or "").strip() or _settings_val("smtp_password", "")
    smtp_host = _settings_val("smtp_host", "smtp.exmail.qq.com")
    try:
        smtp_port = int(_settings_val("smtp_port", "465") or 465)
    except (TypeError, ValueError):
        smtp_port = 465
    smtp_from_name = _settings_val("smtp_from_name", "RHC Veterinary Medical")
    if not smtp_user or not smtp_password:
        raise RuntimeError("发件邮箱未配置（缺少 SMTP_USER 或 SMTP_PASSWORD/客户端授权码）")

    msg = MIMEText(body or "", "plain", "utf-8")
    msg["From"] = formataddr((smtp_from_name, smtp_user))
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject or ""
    rcpts = to_list + cc_list
    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as srv:
                srv.login(smtp_user, smtp_password)
                srv.sendmail(smtp_user, rcpts, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                srv.login(smtp_user, smtp_password)
                srv.sendmail(smtp_user, rcpts, msg.as_string())
    except Exception as e:
        raise RuntimeError(
            f"SMTP 发信失败（{smtp_user} → {', '.join(to_list)}）：{e}")
    return smtp_user


async def send_email(to_addrs, subject, body, cc=None,
                     sender_email=None, sender_password=None) -> str:
    """异步发信。优先走 Resend（HTTPS/443，适配 Railway 封出站 SMTP 的环境）；
    未配置 RESEND_API_KEY 或显式传入销售自己的 SMTP 账号时，回退原生 SMTP。
    返回实际发件地址。"""
    # 显式传入 sender_email/password（多销售用自己邮箱登录 SMTP）时，走 SMTP
    use_personal_smtp = bool((sender_email or "").strip() and (sender_password or "").strip())
    resend_key = (os.environ.get("RESEND_API_KEY", "") or "").strip()
    if resend_key and not use_personal_smtp:
        try:
            return await asyncio.to_thread(
                _resend_send_mail, to_addrs, subject, body, cc, resend_key)
        except Exception as e:
            # Resend 失败不再回退 SMTP（Railway 下 SMTP 必然超时，只会拖30秒）；直接抛出明确错误
            print(f"[email] Resend 发信失败: {e}")
            raise RuntimeError(f"Resend 发信失败：{e}")
    return await asyncio.to_thread(
        _smtp_send_mail, to_addrs, subject, body, cc,
        sender_email, sender_password)


def _resend_send_mail(to_addrs, subject, body, cc, api_key) -> str:
    """同步走 Resend HTTPS API（443）。返回发件邮箱。Railway 等只放行443的环境可用。"""
    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue
    to_list = _split_mail_addrs(to_addrs)
    cc_list = _split_mail_addrs(cc)
    if not to_list:
        raise RuntimeError("收件人邮箱不能为空")
    smtp_user = _settings_val("smtp_user", "")
    from_name = _settings_val("smtp_from_name", "RHC Veterinary Medical")
    # 发件人：Resend 域名验证通过后可用该域名下任意地址；默认沿用系统配置的发件账号
    from_addr = (os.environ.get("RESEND_FROM_EMAIL", "").strip()
                 or smtp_user or "onboarding@resend.dev")
    sender = f"{from_name} <{from_addr}>"
    payload = {
        "from": sender,
        "to": to_list,
        "subject": subject or "",
        "text": body or "",
    }
    if cc_list:
        payload["cc"] = cc_list
    # Resend 沙箱/测试开关：RESEND_DRY_RUN=1 时不真实投递
    if (os.environ.get("RESEND_DRY_RUN", "").strip().lower() in ("1", "true", "yes")):
        print(f"[email] RESEND_DRY_RUN 跳过真实投递 -> {to_list} | {subject}")
        return from_addr
    req = _ur.Request(
        "https://api.resend.com/emails",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "rhc-marketing/1.0",
        },
        method="POST")
    try:
        with _ur.urlopen(req, timeout=20) as r:
            resp = _json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise RuntimeError(f"Resend HTTP {e.code}: {detail}")
    print(f"[email] Resend 已发送 id={resp.get('id', '')} -> {', '.join(to_list)}")
    return from_addr


# ------------------------------------------------------------
# 开发信生成：Coze 工作流优先，本地高质量英文模板兜底
# ------------------------------------------------------------
# 纯国家/地区名（不能当公司名）
_COUNTRY_NAMES_ONLY = {
    "chile", "china", "brazil", "india", "russia", "japan", "korea",
    "germany", "france", "italy", "spain", "mexico", "argentina", "colombia",
    "peru", "ecuador", "venezuela", "bolivia", "paraguay", "uruguay",
    "panama", "costa rica", "guatemala", "honduras", "nicaragua", "el salvador",
    "dominican republic", "cuba", "puerto rico", "haiti", "jamaica",
    "united states", "usa", "us", "canada", "australia", "new zealand",
    "united kingdom", "uk", "england", "scotland", "ireland", "wales",
    "netherlands", "holland", "belgium", "switzerland", "austria", "poland",
    "czech republic", "hungary", "romania", "bulgaria", "croatia", "serbia",
    "greece", "turkey", "israel", "saudi arabia", "uae", "egypt",
    "south africa", "nigeria", "kenya", "morocco", "tunisia",
    "thailand", "vietnam", "indonesia", "malaysia", "philippines", "singapore",
    "myanmar", "cambodia", "laos", "brunei", "east timor", "papua new guinea",
    "pakistan", "bangladesh", "sri lanka", "nepal", "bhutan", "maldives",
    "north america", "south america", "latin america", "europe", "asia",
    "africa", "oceania", "middle east", "central america", "caribbean",
    "southeast asia", "east asia", "south asia", "central asia",
    "home", "about", "contact", "services", "products", "solutions",
    "our distributors", "our partners", "our clients", "our suppliers",
    "our team", "our story", "our mission", "our vision",
}

def _clean_company_name(raw: str) -> str:
    """清洗脏公司名：搜索词式短语（含多关键词/冒号/import等）不当公司名用，返回空串。
    真实公司名通常是 1-4 个词的专名，常见法律后缀 Ltd/Inc/LLC/GmbH/S.A./Corp 等。"""
    if not raw:
        return ""
    name = raw.strip()
    low = name.lower().strip()
    
    # 0. 纯国家/地区名/通用导航词 → 直接拒绝
    if low in _COUNTRY_NAMES_ONLY:
        return ""
    
    # 1. 明显是搜索词/句子的特征
    bad_markers = (":", "：", " import ", " import,", " company:", " companies ",
                   " supplier", " manufacturers ", " for sale", " price", " buy ",
                   " wholesale", " distributor of", " looking for", " wanted",
                   " manufacturer of", " list of", " list:", " directory of",
                   " top 10", " top 20", " top 50", " top 100",
                   " and more", " and other", " etc.", " and beyond",
                   " in the world", " in the us", " in the uk")
    if any(m in low for m in bad_markers):
        return ""
    
    # 2. 以通用词开头且整体不像公司名的模式
    bad_starts = ("list of", "list:", "directory of", "catalog of",
                  "manufacturer of", "distributor of", "supplier of",
                  "wholesale", "importer of", "exporter of",
                  "home", "about us", "contact us", "our",
                  "welcome to", "visit our", "find a", "find your",
                  "top 10", "top 20", "top 50", "top 100",
                  "best veterinary", "leading veterinary", "top veterinary")
    if any(low.startswith(p) for p in bad_starts):
        return ""
    
    # 3. 中文整句（含多个中文且无法律后缀）多半是搜索词
    legal_suffix = ("ltd", "inc", "llc", "gmbh", "corp", "co.", "company", "s.a",
                    "s.a.", "sa ", "limited", "co.,", "group", "holdings", "importadora",
                    "comercio", "medical", "vet", "hospital", "clinic", "laboratorio",
                    "laboratories", "health", "healthcare", "animal", "pet", "pharm",
                    "veterinary", "pharma", "biotech", "sciences", "technologies",
                    "international", "trading")
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", name))
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", name))
    if has_cjk and cjk_count >= 4 and not any(s in low for s in legal_suffix):
        return ""
    
    # 4. 词数过多（>6）且无法律后缀，视为描述性短语
    words = re.findall(r"[A-Za-z0-9\.]+", name)
    if len(words) > 6 and not any(s in low for s in legal_suffix):
        return ""
    
    # 5. 只有1个词且不是常见公司名缩写形式（如 Corp., Inc.），太短不像公司名
    if len(words) == 1 and len(low) <= 3 and not low.endswith(("." ,)):
        return ""
    
    # 6. 纯产品/行业描述词，没有任何品牌专名
    if _is_product_phrase(name):
        return ""
    
    return name[:80]


def _build_local_cold_email(info: dict):
    """本地兜底：生成地道 B2B 英文冷邮件，返回 (subject, body)。
    结构：自我介绍（RHC=中国兽用医疗器械制造商）+ 国家/行业个性化切入
    + 针对需求产品的价值点 + 轻 CTO + 落款。"""
    company = (info.get("company_name") or "").strip()
    country = (info.get("country") or "").strip()
    product = (info.get("product") or "").strip() or "veterinary medical equipment"
    industry = (info.get("industry") or "").strip()
    decision_maker = (info.get("decision_maker") or "").strip()
    tone = (info.get("tone") or "正式").strip()
    selling_points = (info.get("selling_points") or "").strip()
    sender_name = (info.get("sender_name") or "").strip() or "Ella Chen"
    is_friendly = tone in ("友好", "friendly", "Friendly")
    is_concise = tone in ("简洁", "concise", "Concise")

    first_name = decision_maker.split()[0] if decision_maker else ""
    greeting = (f"Hi {first_name}," if first_name
                else (f"Hello {company} team," if company else "Hello,"))

    # 一句话自我介绍
    if is_friendly:
        intro = ("I'm Ella from RHC Medical, a China-based manufacturer of veterinary "
                 "medical equipment, working with clinics and distributors in over 60 countries.")
    else:
        intro = ("This is Ella from RHC Medical, a China-based manufacturer of veterinary "
                 "anesthesia, monitoring and surgical equipment, supplying clinics and "
                 "distributors worldwide.")

    # 结合客户国家/行业的个性化切入
    if country and industry:
        hook = (f"We understand the {country} veterinary market is growing quickly, and "
                f"businesses in the {industry} space like {company} are looking for "
                f"reliable equipment partners with competitive pricing.")
    elif country:
        hook = (f"We understand the {country} veterinary market is growing quickly, and "
                f"clinics and distributors there are increasingly looking for reliable, "
                f"cost-effective equipment with responsive after-sales support.")
    elif industry:
        hook = (f"As a business in the {industry} space, {company} may find a "
                f"factory-direct equipment partner with solid export experience useful.")
    else:
        hook = ("Clinics and distributors we work with value a factory-direct partner with "
                "consistent quality and responsive after-sales support.")

    # 针对需求产品的价值点（销售自定义卖点优先嵌入）
    value_parts = []
    if selling_points:
        value_parts.append(selling_points.rstrip(". "))
    value_parts.append("CE-certified quality with factory-direct pricing")
    value_parts.append("flexible MOQ and OEM/customization options")
    value_line = "; ".join(value_parts) + "."
    product_line = (f"Our {product} range is among the most requested by our partners, "
                    f"shipping with full English documentation.")

    # 轻 CTO
    if is_friendly:
        cto = ("Would it be worth sending you our latest catalog and a price list for "
               "reference?")
    else:
        cto = ("Would you be open to reviewing our product catalog and a quotation tailored "
               "to your market?")

    if is_concise:
        subject = (f"RHC {product} supply – {company}"
                   if company else f"RHC {product} supply")
        body = (
            f"{greeting}\n\n"
            f"{intro}\n\n"
            f"{value_line} {product_line}\n\n"
            f"{cto}\n\n"
            f"Best regards,\n{sender_name}\nRHC Medical"
        )
        return subject, body

    if is_friendly:
        subject = (f"A quick note on veterinary equipment supply for {company}"
                   if company else "A quick note on veterinary equipment supply")
    else:
        subject = (f"Veterinary equipment supply for {company}"
                   if company else "Veterinary anesthesia & monitoring equipment supply")
    body = (
        f"{greeting}\n\n"
        f"{intro}\n\n"
        f"{hook}\n\n"
        f"For your reference: {value_line} {product_line}\n\n"
        f"{cto}\n\n"
        f"Best regards,\n{sender_name}\nRHC Medical"
    )
    return subject, body


def _extract_email_payload(data: dict) -> dict:
    """从工作流返回中提取 subject/body。
    兼容：(a)结束节点各字段已是独立值 (b)某字段装着整份JSON字符串（含中英文 key）。"""
    def _maybe_load(v):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("{"):
                try:
                    return json.loads(s)
                except Exception:
                    return None
        return None

    # 情况b：任一字符串字段里装着整份结果 JSON
    for v in data.values():
        emb = _maybe_load(v)
        if isinstance(emb, dict) and any(
                k in emb for k in ("subject", "body", "主题", "正文", "content")):
            return {
                "subject": emb.get("subject") or emb.get("主题") or "",
                "body": emb.get("body") or emb.get("正文") or emb.get("content") or "",
            }
    # 情况a：字段已各归各位
    return {
        "subject": data.get("subject") or data.get("主题") or "",
        "body": data.get("body") or data.get("正文") or data.get("content") or "",
    }


class EmailGenerateRequest(BaseModel):
    record_id: str = ""
    company_name: str = ""
    country: str = ""
    product: str = ""
    industry: str = ""
    website: str = ""
    email_pattern: str = ""
    decision_maker: str = ""
    recipient_email: str = ""
    tone: str = "正式"          # 正式 / 友好 / 简洁
    selling_points: str = ""
    sender_name: str = ""


@app.post("/api/emails/generate")
async def api_emails_generate(req: EmailGenerateRequest, request: Request):
    """生成开发信（不落库、不发送）。Coze 工作流优先，失败自动回退本地英文模板。需JWT认证。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    info = {
        "company_name": _clean_company_name((req.company_name or "").strip()),
        "country": (req.country or "").strip(),
        "product": (req.product or "").strip(),
        "industry": (req.industry or "").strip(),
        "website": (req.website or "").strip(),
        "email_pattern": (req.email_pattern or "").strip(),
        "decision_maker": (req.decision_maker or "").strip(),
        "recipient_email": (req.recipient_email or "").strip(),
        "tone": (req.tone or "正式").strip() or "正式",
        "selling_points": (req.selling_points or "").strip(),
        "sender_name": (req.sender_name or "").strip(),
    }
    if not info["company_name"] and not (req.record_id or "").strip():
        return JSONResponse({"ok": False, "message": "缺少客户公司名称或线索 record_id"},
                            status_code=400)
    generated_by = "template"
    subject, body = "", ""
    try:
        wf_id = _settings_val("coze_email_workflow_id", "")
        pat = (_get_sales_coze_pat()
               or getattr(settings, "COZE_PAT", "") or getattr(settings, "coze_pat", "") or "")
        if wf_id and pat and (info["company_name"] or info["product"] or info["country"]):
            try:
                parameters = {
                    "company_name": info["company_name"],
                    "country": info["country"],
                    "product": info["product"],
                    "industry": info["industry"],
                    "website": info["website"],
                    "decision_maker": info["decision_maker"],
                    "recipient_email": info["recipient_email"],
                    "tone": info["tone"],
                    "selling_points": info["selling_points"],
                    "sender_name": info["sender_name"],
                }
                data = await asyncio.wait_for(
                    asyncio.to_thread(_coze_workflow_run, wf_id, parameters, 60),
                    timeout=70)
                parsed = _extract_email_payload(data)
                subject = str(parsed.get("subject") or "").strip()
                body = str(parsed.get("body") or "").strip()
                if subject and body:
                    generated_by = "AI"
                else:
                    print(f"[email] AI工作流返回缺 subject/body，回退模板: {str(data)[:200]}")
            except Exception as e:
                print(f"[email] AI开发信生成失败，回退本地模板（{info['company_name']}）: {e}")
        if generated_by != "AI":
            subject, body = _build_local_cold_email(info)
        return {"ok": True, "subject": subject, "body": body,
                "generated_by": generated_by}
    except Exception as e:
        # 兜底中的兜底：任何异常都不返回 5xx，保证前端拿到可用内容
        print(f"[email] 开发信生成异常，使用最简模板: {e}")
        try:
            subject, body = _build_local_cold_email(info)
        except Exception:
            subject = f"Veterinary equipment supply from RHC"
            body = (f"Hello,\n\nThis is Ella from RHC Medical, a China-based manufacturer "
                    f"of veterinary medical equipment. May I send you our catalog and a "
                    f"quotation?\n\nBest regards,\n"
                    f"{info['sender_name'] or 'Ella Chen'}\nRHC Medical")
        return {"ok": True, "subject": subject, "body": body,
                "generated_by": "template"}


class EmailSendRequest(BaseModel):
    record_id: str = ""
    company_name: str = ""
    recipient_email: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""
    sender_name: str = ""
    sender_email: str = ""     # 可选：用销售自己的邮箱登录 SMTP（多邮箱预留）
    sender_password: str = ""  # 可选：该邮箱的客户端授权码
    generated_by: str = ""     # 可选：AI / template（随生成结果带回）


@app.post("/api/emails/send")
async def api_emails_send(req: EmailSendRequest, request: Request):
    """真实发送开发信并落库：先发信，成功后写「开发信记录」+ 更新线索跟进字段。需JWT认证。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    record_id = (req.record_id or "").strip()
    company_name = (req.company_name or "").strip()
    recipient = (req.recipient_email or "").strip()
    subject = (req.subject or "").strip()
    body = (req.body or "").strip()
    if not recipient:
        return JSONResponse({"ok": False, "message": "收件人邮箱不能为空"}, status_code=400)
    if not subject or not body:
        return JSONResponse({"ok": False, "message": "邮件主题和正文不能为空"}, status_code=400)

    # 1) 真实发送；失败直接返回错误，不落库
    try:
        actual_sender = await send_email(
            recipient, subject, body, cc=(req.cc or "").strip() or None,
            sender_email=(req.sender_email or "").strip() or None,
            sender_password=(req.sender_password or "").strip() or None)
    except Exception as e:
        print(f"[email] 开发信发送失败 -> {recipient}: {e}")
        return JSONResponse({"ok": False, "message": str(e)}, status_code=502)

    now_bj = datetime.now(timezone(timedelta(hours=8)))
    sent_at = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    sent_min = now_bj.strftime("%Y-%m-%d %H:%M")
    sender_display = (req.sender_name or "").strip()
    sender_field = f"{sender_display} <{actual_sender}>" if sender_display else actual_sender

    # 2) 写开发信记录（生成方式：AI→AI，template→模板，缺省不写该字段）
    gen_mode = ""
    if (req.generated_by or "").strip().upper() == "AI":
        gen_mode = "AI"
    elif (req.generated_by or "").strip().lower() == "template":
        gen_mode = "模板"
    email_fields = {
        "客户公司": company_name,
        "收件邮箱": recipient,
        "主题": subject,
        "正文": body,
        "发件人": sender_field,
        "发送状态": "成功",
        "发送时间": sent_at,
        "线索record_id": record_id,
    }
    if gen_mode:
        email_fields["生成方式"] = gen_mode
    email_rec_id = ""
    try:
        tid = _ensure_emails_table()
        resp = _feishu_api(
            "POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records",
            {"fields": email_fields})
        email_rec_id = resp.get("data", {}).get("record", {}).get("record_id", "")
    except Exception as e:
        # 邮件已真实发出，落库失败不回滚发送，仅记录日志
        print(f"[email] 开发信记录落库失败（邮件已发送）: {e}")

    # 3) 更新线索跟进字段（发信次数读不到当 0 → 1）
    sent_count = 1
    if record_id:
        try:
            ltid = _ensure_leads_table()
            old_count = 0
            try:
                rresp = _feishu_api(
                    "GET",
                    f"/bitable/v1/apps/{FEISHU_ATK}/tables/{ltid}/records/{record_id}")
                old_val = rresp.get("data", {}).get("record", {}).get("fields", {}).get("发信次数")
                if old_val not in (None, ""):
                    old_count = int(float(str(_tv(old_val)).strip() or 0))
            except Exception:
                old_count = 0
            sent_count = old_count + 1
            _feishu_api(
                "PUT",
                f"/bitable/v1/apps/{FEISHU_ATK}/tables/{ltid}/records/{record_id}",
                {"fields": {
                    "跟进状态": "开发信已发",
                    "发件邮箱": actual_sender,
                    "最近发信时间": sent_min,
                    "发信次数": sent_count,
                }})
            _invalidate_leads_cache()
        except Exception as e:
            print(f"[email] 更新线索跟进字段失败（{record_id}，邮件已发送）: {e}")

    return {"ok": True, "message_id": email_rec_id, "sent_at": sent_at,
            "sent_count": sent_count, "sender_email": actual_sender}


@app.get("/api/emails")
async def api_emails_list(request: Request, record_id: str = ""):
    """开发信发信历史，可按 ?record_id= 过滤线索；按发送时间倒序。需JWT认证。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        items = _fetch_emails()
        rid = (record_id or "").strip()
        if rid:
            items = [m for m in items if m.get("lead_record_id") == rid]
        return {"ok": True, "items": items, "total": len(items)}
    except Exception as e:
        print(f"[emails] 发信历史读取失败: {e}")
        return JSONResponse({"ok": False, "message": f"发信历史读取失败：{e}"}, status_code=502)

# ============================================================
# 公海池/私海池 API + 定时搜索任务
# ============================================================

class ClaimByRecordRequest(BaseModel):
    claimer: str = ""


@app.get("/api/leads/public")
async def api_leads_public(request: Request):
    """公海池：返回已进入公海池且在30天有效期内的线索，按综合评分降序。需JWT认证。"""
    token = _get_token_from_request(request)
    if not token or not _verify_token(token):
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        all_leads = _fetch_leads(force_refresh=True)
        # 过滤：只返回有「入池时间」且在30天内的线索
        now = datetime.now(timezone(timedelta(hours=8)))
        cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        leads = []
        for ld in all_leads:
            # AI/规则判定为非买家（目录/平台/新闻/导航/同行制造商）的记录只标记不删除，公海池不展示
            if (ld.get("系统排除") or "").strip():
                continue
            pool_time = (ld.get("入池时间") or "").strip()
            if pool_time and pool_time >= cutoff:
                leads.append(ld)
        leads.sort(key=lambda x: float(x.get("综合评分") or 0), reverse=True)
        return {"ok": True, "items": leads, "stats": {"total": len(leads)}}
    except Exception as e:
        print(f"[leads] 公海池读取失败: {e}")
        return JSONResponse({"ok": False, "message": f"读取公海池失败：{e}"}, status_code=502)


@app.get("/api/leads/my")
async def api_leads_my(request: Request):
    """私海池：返回当前销售认领的线索，按认领时间降序。需JWT认证。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    # 身份候选：优先前端当前认领身份(X-Sales-Id)，其次JWT姓名/账号；
    # 归一化（去空白+忽略大小写）后任一命中即视为本人，兼容手输名与登录名差异
    def _norm_name(s):
        return "".join(str(s or "").split()).casefold()

    candidates = []
    header_sales = request.headers.get("X-Sales-Id", "").strip()
    if header_sales:
        candidates.append(header_sales)
    for _k in ("name", "username"):
        _v = (user_info.get(_k) or "").strip()
        if _v:
            candidates.append(_v)
    norm_candidates = [_norm_name(c) for c in candidates if _norm_name(c)]
    if not norm_candidates:
        return {"ok": True, "items": [], "stats": {"total": 0}}
    try:
        leads = _fetch_leads(force_refresh=True)
        my_leads = [l for l in leads
                    if l.get("认领状态") == "已认领"
                    and _norm_name(l.get("认领人")) in norm_candidates]
        my_leads.sort(key=lambda x: x.get("认领时间", ""), reverse=True)
        return {"ok": True, "items": my_leads, "stats": {"total": len(my_leads)}}
    except Exception as e:
        print(f"[leads] 私海池读取失败: {e}")
        return JSONResponse({"ok": False, "message": f"读取私海池失败：{e}"}, status_code=502)


@app.post("/api/leads/claim/{record_id}")
async def api_leads_claim_by_record(record_id: str, req: ClaimByRecordRequest, request: Request):
    """公海池认领线索（先到先得）。需JWT认证。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    claimer = (req.claimer or "").strip()
    if not claimer:
        claimer = user_info.get("name", "") or user_info.get("username", "")
    if not claimer:
        return JSONResponse({"ok": False, "message": "缺少认领人信息，请在请求体传入claimer字段"}, status_code=400)
    try:
        tid = _ensure_leads_table()
        # 先查当前线索状态
        resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}")
        rec = resp.get("data", {}).get("record", {})
        fields = rec.get("fields", {})
        current_status = _tv(fields.get("认领状态"))
        current_claimer = _tv(fields.get("认领人"))
        if current_status == "已认领" and current_claimer:
            return JSONResponse({"ok": False, "error": f"已被{current_claimer}认领"}, status_code=409)
        # 未认领，执行认领
        now_iso = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        _feishu_api(
            "PUT",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}",
            {"fields": {"认领状态": "已认领", "认领人": claimer, "认领时间": now_iso}})
        _invalidate_leads_cache()
        return {"ok": True, "message": f"已成功认领线索", "claimer": claimer, "claim_time": now_iso}
    except Exception as e:
        print(f"[leads] 认领失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"认领失败：{e}"}, status_code=502)


class ReleaseByRecordRequest(BaseModel):
    releaser: str = ""


@app.post("/api/leads/release/{record_id}")
async def api_leads_release_by_record(record_id: str, req: ReleaseByRecordRequest, request: Request):
    """释放已认领线索，退回公海池。仅当前认领人本人可释放；跟进状态与开发信历史保留。需JWT认证。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    releaser = (req.releaser or "").strip()
    if not releaser:
        releaser = (request.headers.get("X-Sales-Id", "").strip()
                    or user_info.get("name", "") or user_info.get("username", ""))
    try:
        tid = _ensure_leads_table()
        resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}")
        rec = resp.get("data", {}).get("record", {})
        fields = rec.get("fields", {})
        current_status = _tv(fields.get("认领状态"))
        current_claimer = _tv(fields.get("认领人"))
        if current_status != "已认领" or not current_claimer:
            return JSONResponse({"ok": False, "message": "该线索当前未被认领，无需释放"}, status_code=400)
        if releaser and current_claimer.strip() != releaser:
            return JSONResponse({"ok": False, "message": f"仅认领人（{current_claimer}）可释放该客户"}, status_code=403)
        # 释放：清空认领状态/认领人/认领时间；跟进状态、发件历史、评分等全部保留
        _feishu_api(
            "PUT",
            f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}",
            {"fields": {"认领状态": "未认领", "认领人": "", "认领时间": ""}})
        _invalidate_leads_cache()
        return {"ok": True, "message": "已释放回公海池"}
    except Exception as e:
        print(f"[leads] 释放失败（{record_id}）: {e}")
        return JSONResponse({"ok": False, "message": f"释放失败：{e}"}, status_code=502)


class EnrichLeadRequest(BaseModel):
    depth: str = "light"  # "light" 或 "deep"


@app.post("/api/admin/leads/backfill-grades")
async def api_admin_backfill_grades(request: Request):
    """一次性运维：按综合评分回填历史线索「评级」。仅 admin。
    body {"apply": false} 默认干跑返回差异；{"apply": true} 才写库。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info or user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "需要管理员权限"}, status_code=403)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    apply = bool(body.get("apply", False))
    try:
        result = await asyncio.to_thread(backfill_lead_grades, apply)
        if apply:
            _invalidate_leads_cache()
        return {"ok": True, **result}
    except Exception as e:
        print(f"[backfill] 评级回填失败: {e}")
        return JSONResponse({"ok": False, "message": f"回填失败：{e}"}, status_code=502)


@app.post("/api/admin/leads/{record_id}/reset-score")
async def api_admin_reset_lead_score(record_id: str, request: Request):
    """一次性运维：把单条线索综合评分归位（默认 59）。仅 admin。
    只动「综合评分」+ 与其同源的「评级」，并在「跟进备注」追加一条系统留痕（给认领销售看）。
    body 可带 {"score": 59, "note": "..."}。返回变更前后对比，先 dry 看、确认后才由人工调用。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info or user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "需要管理员权限"}, status_code=403)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    target_score = body.get("score", 59)
    try:
        target_score = int(float(target_score))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "message": "score 必须是整数"}, status_code=400)
    target_score = max(0, min(100, target_score))
    note_extra = str(body.get("note", "")).strip()
    try:
        tid = _ensure_leads_table()
        resp = _feishu_api(
            "GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}")
        rec = resp.get("data", {}).get("record", {})
        fields = rec.get("fields", {})
        if not fields:
            return JSONResponse({"ok": False, "message": "线索不存在"}, status_code=404)
        company = _tv(fields.get("公司/机构"))
        old_score_raw = _tv(fields.get("综合评分"))
        old_grade = _tv(fields.get("评级"))
        try:
            old_score = int(float(old_score_raw))
        except (TypeError, ValueError):
            old_score = None
        new_grade = _grade_from_score(target_score)
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        base_note = (f"[{stamp} 系统] 综合评分由 {old_score_raw or '空'} 归位为 {target_score}"
                     f"（可达性闸门59档：company+经销类买家+有官网+暂无强联系方式）；评级 {new_grade}。")
        if note_extra:
            base_note += note_extra
        old_note = _tv(fields.get("跟进备注"))
        merged_note = (old_note + "\n" + base_note).strip() if old_note else base_note
        _update_leads_record(record_id, {
            "综合评分": target_score,
            "评级": new_grade,
            "跟进备注": merged_note[:5000],
        })
        _invalidate_leads_cache()
        return {
            "ok": True, "record_id": record_id, "company": company,
            "before": {"score": old_score, "grade": old_grade},
            "after": {"score": target_score, "grade": new_grade},
            "claimer": _tv(fields.get("认领人")),
            "note_appended": base_note,
        }
    except Exception as e:
        print(f"[admin] 线索分数归位失败: {e}")
        return JSONResponse({"ok": False, "message": f"归位失败：{e}"}, status_code=502)


@app.get("/api/admin/brave-breaker")
async def api_brave_breaker_status(request: Request):
    """查看 Brave 熔断状态（仅 admin）。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info or user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "需要管理员权限"}, status_code=403)
    return {"ok": True, "breaker": dict(_brave_breaker),
            "scheduled_enabled": os.environ.get(
                "ENABLE_SCHEDULED_SEARCH", "").strip().lower() in ("1", "true", "yes", "on")}


@app.post("/api/admin/brave-breaker/reset")
async def api_brave_breaker_reset(request: Request):
    """人工复位 Brave 熔断（充值后点「强制跑一轮」前调用，仅 admin）。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info or user_info.get("role") != "admin":
        return JSONResponse({"ok": False, "message": "需要管理员权限"}, status_code=403)
    before = dict(_brave_breaker)
    reset_brave_breaker()
    return {"ok": True, "before": before, "after": dict(_brave_breaker)}


@app.post("/api/leads/{record_id}/enrich")
async def api_leads_enrich(record_id: str, req: Optional[EnrichLeadRequest] = None, request: Request = None):
    """手动触发单条线索补全信息。depth: light=轻补搜, deep=深度补搜。需JWT认证。"""
    token = _get_token_from_request(request)
    if not token or not _verify_token(token):
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    depth = "light"
    if req:
        depth = req.depth if req.depth in ("light", "deep") else "light"
    try:
        tid = _ensure_leads_table()
        # 读取当前线索信息
        resp = _feishu_api("GET", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/{record_id}")
        rec = resp.get("data", {}).get("record", {})
        fields = rec.get("fields", {})
        company_name = _tv(fields.get("公司/机构"))
        country = _tv(fields.get("地区", "")).split(" - ")[-1] if _tv(fields.get("地区")) else ""
        website = _tv(fields.get("官网")) or _tv(fields.get("原文链接"))
        if not company_name:
            return JSONResponse({"ok": False, "message": "线索不存在或缺少公司名称"}, status_code=400)

        if depth == "light":
            _update_leads_record(record_id, {"补搜状态": "轻补搜中"})
            enriched = await light_enrich_lead(company_name, country, website)
            light_got = bool(enriched.get("email_pattern") or enriched.get("phone")
                             or enriched.get("decision_maker") or enriched.get("linkedin"))
            if enriched.get("antibot") and not light_got:
                update_fields = {"补搜状态": "需人工补全·官网反爬"}
            else:
                update_fields = {"补搜状态": "已轻补"}
            if enriched.get("website"):
                update_fields["官网"] = enriched["website"]
            if enriched.get("industry"):
                update_fields["行业"] = enriched["industry"]
            if enriched.get("email_pattern"):
                update_fields["邮箱格式"] = enriched["email_pattern"]
            if enriched.get("phone"):
                update_fields["电话"] = enriched["phone"]
            if enriched.get("decision_maker"):
                update_fields["决策人"] = enriched["decision_maker"]
            if enriched.get("linkedin"):
                update_fields["LinkedIn"] = enriched["linkedin"]
            # 重评分。反爬且未取得任何实际联系方式时跳过重评分：
            # 此时没有任何新增可触达信号，重算只会因"有官网+行业"虚加分（如 Vale 45→56），属 bug。
            try:
                old_score = int(float(_tv(fields.get("综合评分"))))
            except (TypeError, ValueError):
                old_score = None
            if enriched.get("antibot") and not light_got:
                new_score = old_score if old_score is not None else 45
                # 分数不动，评级与现有分数保持同源
                update_fields["评级"] = _grade_from_score(new_score)
            else:
                lead_info = {**{k: _tv(v) for k, v in fields.items()}, **enriched}
                new_score, page_type, buyer_type = await call_coze_scoring_workflow(lead_info)
                update_fields["综合评分"] = new_score
                update_fields["评级"] = _grade_from_score(new_score)
                if page_type:
                    update_fields["页面类型"] = page_type
                if buyer_type:
                    update_fields["买家类型"] = buyer_type
                _ex = _ai_exclude_reason(page_type, buyer_type)
                if _ex:
                    update_fields["系统排除"] = _ex
                # 更新市场优先级
                _country = enriched.get("country") or _tv(fields.get("地区", ""))
                update_fields["市场优先级"] = _get_market_priority(_country)
            _update_leads_record(record_id, update_fields)
            _invalidate_leads_cache()
            return {"ok": True, "message": "轻补搜完成", "data": enriched, "new_score": new_score,
                    "antibot": bool(enriched.get("antibot")),
                    "known_email": enriched.get("known_email", "") or ""}
        else:
            _update_leads_record(record_id, {"补搜状态": "深度补搜中"})
            deep = await deep_enrich_lead(company_name, country, website)
            found_any = bool(deep.get("decision_maker") or deep.get("linkedin")
                             or deep.get("phone") or deep.get("import_record")
                             or deep.get("email"))
            # 状态分三档：拿到信息=已深度补全；官网强反爬=需人工补全；其余=已检索未找到
            if found_any:
                enrich_status = "已深度补全"
                ret_msg = "深度补搜完成"
            elif deep.get("antibot"):
                enrich_status = "需人工补全·官网反爬"
                ret_msg = "官网有强反爬验证，自动补全无法读取，请人工到官网联系页获取"
            else:
                enrich_status = "已检索·未找到联系方式"
                ret_msg = "已检索，暂未找到决策人/LinkedIn/电话/进口记录"
            update_fields = {"补搜状态": enrich_status}
            if deep.get("decision_maker"):
                update_fields["决策人"] = deep["decision_maker"]
            if deep.get("linkedin"):
                update_fields["LinkedIn"] = deep["linkedin"]
            if deep.get("phone"):
                update_fields["电话"] = deep["phone"]
            if deep.get("import_record"):
                update_fields["进口记录"] = deep["import_record"]
            # 深度补搜也回写邮箱和官网（与批量路径保持一致）
            if deep.get("email"):
                update_fields["联系邮箱"] = deep["email"]
                update_fields["邮箱来源"] = website or ""
            # 官网：优先用 deep 返回值，否则用调用时传入的 website（可能来自原文链接）
            if deep.get("website"):
                update_fields["官网"] = deep["website"]
            elif website and not _tv(fields.get("官网")):
                update_fields["官网"] = website
            # 重评分：有新增联系方式时重新计算综合评分
            if found_any or deep.get("email"):
                try:
                    lead_info = {**{k: _tv(v) for k, v in fields.items()}, **deep}
                    new_score, page_type, buyer_type = await call_coze_scoring_workflow(lead_info)
                    update_fields["综合评分"] = new_score
                    update_fields["评级"] = _grade_from_score(new_score)
                    if page_type:
                        update_fields["页面类型"] = page_type
                    if buyer_type:
                        update_fields["买家类型"] = buyer_type
                    _ex = _ai_exclude_reason(page_type, buyer_type)
                    if _ex:
                        update_fields["系统排除"] = _ex
                    # 更新市场优先级
                    _country = deep.get("country") or _tv(fields.get("地区", ""))
                    update_fields["市场优先级"] = _get_market_priority(_country)
                except Exception as e2:
                    print(f"[enrich] 深度补搜重评分失败 ({record_id}): {e2}")
            _update_leads_record(record_id, update_fields)
            _invalidate_leads_cache()
            return {"ok": True, "message": ret_msg, "data": deep, "found_any": found_any,
                    "antibot": bool(deep.get("antibot")),
                    "known_email": deep.get("known_email", "") or ""}
    except Exception as e:
        print(f"[enrich] 手动补搜失败 ({record_id}): {e}")
        _update_leads_record(record_id, {"补搜状态": "补搜失败"})
        return JSONResponse({"ok": False, "message": f"补搜失败：{e}"}, status_code=502)


async def _scheduled_leads_search():
    """定时任务（维持性覆盖，默认关闭→纯手动；设环境变量 LEADS_SCHEDULED_SEARCH=1 开启）。
    Brave 熔断打开时自动跳过，避免额度到顶后空跑/烧免费引擎。"""
    now_h = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    if os.environ.get("ENABLE_SCHEDULED_SEARCH", "").strip().lower() not in ("1", "true", "yes", "on"):
        print(f"[scheduler] {now_h} 定时搜索已关闭（纯手动模式，点「搜索新线索」触发）")
        return
    if _brave_breaker.get("open"):
        print(f"[scheduler] {now_h} Brave 熔断中（{_brave_breaker.get('reason')}），定时搜索自动跳过")
        return
    print(f"[scheduler] 定时搜索开始: {now_h}")
    try:
        raw_leads = await asyncio.to_thread(_run_lead_search, 30)
        try:
            existing_leads = _fetch_leads(force_refresh=True)
        except Exception:
            existing_leads = []
        new_leads = _dedup_with_existing_leads(raw_leads, existing_leads)
        if not new_leads:
            print("[scheduler] 本轮搜索无新线索")
            return
        tid = _ensure_leads_table()
        now_iso = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        written = 0
        leads_with_record_ids = []
        for lead in new_leads:
            # 优化2&3：假线索/长标题产品页直接低分，跳过 Coze 评分节省调用
            if lead.get("_score_penalty"):
                page_type = _rule_guess_page_type(
                    lead.get("website", ""), lead.get("page_title", ""), lead.get("page_snippet", ""))
                buyer_type = _rule_guess_buyer_type(
                    lead.get("website", ""), lead.get("page_title", ""), lead.get("page_snippet", ""))
                # 域名强制分类兜底（假线索分支不跑 Coze）
                page_type, buyer_type = _apply_host_page_override(
                    page_type, buyer_type, lead.get("website", ""))
                # 规则即可判定的非公司页/同行：固定 5/10 分；其余假线索保留极低罚分
                if _is_excluded_page_type(page_type) or (page_type == "company" and buyer_type == "manufacturer"):
                    score = _finalize_lead_score(0, {}, page_type, buyer_type)
                else:
                    score = lead.get("score", 5)
            else:
                score, page_type, buyer_type = await call_coze_scoring_workflow(lead)
                lead["score"] = score
            fields = {
                "线索标题": f"{lead.get('company_name', '')}（{lead.get('country', '')}）",
                "商机类型": "渠道动态",
                "公司/机构": lead.get("company_name", ""),
                "摘要": (lead.get("ai_suggestion", "") or "")[:2000],
                "来源": lead.get("source", "DuckDuckGo搜索")[:200],
                "原文链接": lead.get("website", ""),
                "地区": f"{lead.get('region', '')} - {lead.get('country', '')}",
                "发布日期": now_iso[:10],
                "认领状态": "未认领",
                "认领人": "",
                "认领时间": "",
                "状态": "跟进中",
                "联系邮箱": "",
                "跟进备注": "",
                "邮箱来源": "",
                "综合评分": score,
                "评级": _grade_from_score(score),
                "补搜状态": "未补搜",
                "官网": "",
                "行业": "",
                "邮箱格式": "",
                "决策人": "",
                "LinkedIn": "",
                "进口记录": "",
                "电话": lead.get("phone", "") or "",
                "页面类型": page_type or "unknown",
                "买家类型": buyer_type or "unknown",
                "市场优先级": _get_market_priority(lead.get("country", "")),
            }
            _ex = _ai_exclude_reason(page_type, buyer_type)
            if _ex:
                fields["系统排除"] = ("rule:" + _ex.split(":", 1)[-1]) if lead.get("_score_penalty") else _ex
            try:
                resp = _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records", {"fields": fields})
                record_id = resp.get("data", {}).get("record", {}).get("record_id", "")
                lead["_record_id"] = record_id
                leads_with_record_ids.append(lead)
                written += 1
            except Exception as we:
                print(f"[scheduler] 写入线索失败: {we}")
        _invalidate_leads_cache()
        # 启动后台补搜
        if leads_with_record_ids:
            _start_enrichment_background(leads_with_record_ids)
        print(f"[scheduler] 定时搜索完成，新增 {written}/{len(new_leads)} 条线索，补搜已启动")
    except Exception as e:
        print(f"[scheduler] 定时搜索异常: {e}")


@app.on_event("startup")
async def startup_scheduler():
    """启动APScheduler定时任务"""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        # 主动获客默认「纯手动」：点「搜索新线索」才跑，不挂定时（省 Brave 额度）。
        # 如需恢复"维持性覆盖"自动搜索：设 ENABLE_SCHEDULED_SEARCH=true → 每天凌晨 03:00 低峰跑 1 次；
        # 且 Brave 熔断打开时该任务会自动跳过。
        if os.environ.get("ENABLE_SCHEDULED_SEARCH", "").strip().lower() in ("1", "true", "yes", "on"):
            scheduler.add_job(_scheduled_leads_search, CronTrigger(hour=3, minute=0), id="leads_search_3am")
            scheduler.start()
            print("[scheduler] APScheduler 已启动，定时主动搜索: 每天 03:00 北京时间（维持性覆盖）")
        else:
            scheduler.start()
            print("[scheduler] 定时主动搜索已关闭·纯手动（点「搜索新线索」触发）；恢复设 ENABLE_SCHEDULED_SEARCH=true")
    except Exception as e:
        print(f"[scheduler] APScheduler 启动失败: {e}")
    # 启动后静默校正"可达性闸门"上线前的历史虚高评分（幂等，只跑一次）
    try:
        asyncio.create_task(_migrate_legacy_scores())
    except Exception as e:
        print(f"[score-migrate] 迁移任务启动失败: {e}")
    # 启动后复位因部署被杀掉而卡在"补搜中"的线索（幂等）
    try:
        asyncio.create_task(_reset_stuck_enrichment())
    except Exception as e:
        print(f"[enrich-reset] 复位任务启动失败: {e}")


# 启动时后台预热飞书「系统账号」表（建表+种子数据），不阻塞服务启动
threading.Thread(target=_warmup_account_table, daemon=True).start()
# 启动时后台预热飞书「商机线索」表（自动建表），不阻塞服务启动
threading.Thread(target=_warmup_leads_table, daemon=True).start()
# 启动时后台预热飞书「消息通知」表（自动建表），不阻塞服务启动
threading.Thread(target=_warmup_messages_table, daemon=True).start()
# 启动时后台预热飞书「开发信记录」表（自动建表），不阻塞服务启动
threading.Thread(target=_warmup_emails_table, daemon=True).start()




# ============================================================
# 临时清理接口（用完即删）：备份全量线索 → 删除所有线索
# 密钥：rhc-clean-2026-0916
# ============================================================
_CLEANUP_KEY = "rhc-clean-2026-0916"

@app.post("/api/admin/cleanup-all-leads")
async def api_cleanup_all_leads(request: Request):
    """临时清理：备份全量线索 → 删除所有线索。需密钥。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = (body or {}).get("key", "")
    if key != _CLEANUP_KEY:
        return JSONResponse({"ok": False, "message": "密钥错误"}, status_code=403)
    
    action = (body or {}).get("action", "scan")  # scan=预览, delete=删除
    
    tid = _ensure_leads_table()
    
    # 拉取全量线索
    all_records = []
    page_token = ""
    while True:
        path = f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records?page_size=100"
        if page_token:
            path += f"&page_token={page_token}"
        try:
            resp = _feishu_api("GET", path)
            items = resp.get("data", {}).get("items", [])
            all_records.extend(items)
            has_more = resp.get("data", {}).get("has_more", False)
            page_token = resp.get("data", {}).get("page_token", "")
            if not has_more:
                break
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"拉取失败：{e}"}, status_code=500)
    
    if action == "scan":
        return {"ok": True, "action": "scan", "total": len(all_records), "preview": all_records[:5]}
    
    if action == "delete":
        # 备份到本地
        from datetime import datetime, timezone, timedelta
        timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        backup_file = f"RHC 运维备份/线索全量备份_{timestamp}.json"
        os.makedirs("RHC 运维备份", exist_ok=True)
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump({"timestamp": timestamp, "total": len(all_records), "records": all_records}, f, ensure_ascii=False, indent=2)
        
        # 批量删除线索
        record_ids = [r["record_id"] for r in all_records if "record_id" in r]
        deleted = 0
        for i in range(0, len(record_ids), 500):
            batch = record_ids[i:i+500]
            try:
                _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{tid}/records/batch_delete", {"records": batch})
                deleted += len(batch)
            except Exception as e:
                return JSONResponse({"ok": False, "message": f"删除线索失败：{e}", "deleted": deleted}, status_code=500)
        
        # 清除线索缓存
        _invalidate_leads_cache()
        
        # 同时清理消息通知表
        msg_deleted = 0
        try:
            msg_tid = _ensure_messages_table()
            msg_records = []
            page_token = ""
            while True:
                path = f"/bitable/v1/apps/{FEISHU_ATK}/tables/{msg_tid}/records?page_size=100"
                if page_token:
                    path += f"&page_token={page_token}"
                resp = _feishu_api("GET", path)
                items = resp.get("data", {}).get("items", [])
                msg_records.extend(items)
                if not resp.get("data", {}).get("has_more"):
                    break
                page_token = resp.get("data", {}).get("page_token", "")
            
            msg_ids = [r["record_id"] for r in msg_records if "record_id" in r]
            for i in range(0, len(msg_ids), 500):
                batch = msg_ids[i:i+500]
                _feishu_api("POST", f"/bitable/v1/apps/{FEISHU_ATK}/tables/{msg_tid}/records/batch_delete", {"records": batch})
                msg_deleted += len(batch)
        except Exception as e:
            # 消息清理失败不影响线索删除结果
            pass
        
        return {"ok": True, "action": "delete", "deleted": deleted, "messages_deleted": msg_deleted, "backup": backup_file}
    
    return JSONResponse({"ok": False, "message": "未知 action"}, status_code=400)



class BatchExclusionRequest(BaseModel):
    items: list  # [{"rid": "recxxx", "reason": "junk:low_score"}, ...]

class BatchRegionRequest(BaseModel):
    items: list  # [{"rid": "recxxx", "region": "非洲 - South Africa"}, ...]

@app.get("/favicon.ico", include_in_schema=False)
async def _favicon():
    """避免浏览器请求 /favicon.ico 时返回 404 JSON 报错。"""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.post("/api/admin/batch-exclusion")
async def api_batch_exclusion(req: BatchExclusionRequest, request: Request):
    """批量写入系统排除字段（用于公海池规则打标）"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    updated, errors = 0, 0
    for item in req.items:
        rid = item.get("rid", "")
        reason = item.get("reason", "")
        if not rid or not reason:
            continue
        try:
            _update_leads_record(rid, {"系统排除": reason})
            updated += 1
        except Exception as e:
            errors += 1
            print(f"[batch-exclusion] 更新失败 {rid}: {e}")
        import time; time.sleep(0.1)  # 限流
    _invalidate_leads_cache()
    return {"ok": True, "updated": updated, "errors": errors, "total": len(req.items)}


@app.post("/api/admin/backfill-market-priority")
async def api_backfill_market_priority(request: Request):
    """批量回填所有线索的市场优先级字段（P0/P1/P2），基于地区字段中的国家名计算"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    leads = _fetch_leads(force_refresh=True)
    updated, skipped, errors = 0, 0, 0
    for ld in leads:
        rid = ld.get("record_id") or ld.get("_record_id") or ""
        if not rid:
            skipped += 1
            continue
        region = (ld.get("地区") or "").strip()
        current_mp = (ld.get("市场优先级") or "").strip()
        want_mp = _get_market_priority(region)
        if current_mp == want_mp:
            skipped += 1
            continue
        try:
            _update_leads_record(rid, {"市场优先级": want_mp})
            updated += 1
        except Exception as e:
            errors += 1
            print(f"[backfill-market-priority] 更新失败 {rid}: {e}")
        import time; time.sleep(0.1)  # 限流
    _invalidate_leads_cache()
    return {"ok": True, "updated": updated, "skipped": skipped, "errors": errors, "total": len(leads)}@app.post("/api/admin/batch-region")
async def api_batch_region(req: BatchRegionRequest, request: Request):
    """批量更新线索地区字段（用于修复TLD映射缺失）"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    updated, errors = 0, 0
    for item in req.items:
        rid = item.get("rid", "")
        region = item.get("region", "")
        if not rid or not region:
            continue
        try:
            _update_leads_record(rid, {"地区": region})
            updated += 1
        except Exception as e:
            errors += 1
            print(f"[batch-region] 更新失败 {rid}: {e}")
        import time; time.sleep(0.1)  # 限流
    _invalidate_leads_cache()
    return {"ok": True, "updated": updated, "errors": errors, "total": len(req.items)}


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
