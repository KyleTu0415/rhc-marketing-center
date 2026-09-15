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
    "官网": "官网",
    "行业": "行业",
    "邮箱格式": "邮箱格式",
    "决策人": "决策人",
    "LinkedIn": "LinkedIn",
    "进口记录": "进口记录",
    "补搜状态": "补搜状态",
    "跟进状态": "跟进状态",
    "发件邮箱": "发件邮箱",
    "最近发信时间": "最近发信时间",
    "发信次数": "发信次数",
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
        {"field_name": "官网", "type": 1},       # 文本
        {"field_name": "行业", "type": 1},       # 文本
        {"field_name": "邮箱格式", "type": 1},   # 文本
        {"field_name": "决策人", "type": 1},     # 文本
        {"field_name": "LinkedIn", "type": 1},   # 文本
        {"field_name": "进口记录", "type": 1},   # 文本
        {"field_name": "补搜状态", "type": 3,    # 单选
         "property": {"options": [
             {"name": "未补搜"}, {"name": "轻补搜中"},
             {"name": "已轻补"}, {"name": "深度补搜中"},
             {"name": "已深度补全"}, {"name": "补搜失败"}
         ]}},
        {"field_name": "跟进状态", "type": 3,    # 单选（开发信跟进）
         "property": {"options": [{"name": n} for n in LEAD_FOLLOW_STATUS_OPTIONS]}},
        {"field_name": "发件邮箱", "type": 1},   # 文本（实际发件销售邮箱）
        {"field_name": "最近发信时间", "type": 1},  # 文本
        {"field_name": "发信次数", "type": 2},   # 数字
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
    {"field_name": "官网", "type": 1},
    {"field_name": "行业", "type": 1},
    {"field_name": "邮箱格式", "type": 1},
    {"field_name": "决策人", "type": 1},
    {"field_name": "LinkedIn", "type": 1},
    {"field_name": "进口记录", "type": 1},
    {"field_name": "补搜状态", "type": 3,
     "property": {"options": [
         {"name": "未补搜"}, {"name": "轻补搜中"},
         {"name": "已轻补"}, {"name": "深度补搜中"},
         {"name": "已深度补全"}, {"name": "补搜失败"}
     ]}},
    {"field_name": "跟进状态", "type": 3,
     "property": {"options": [{"name": n} for n in LEAD_FOLLOW_STATUS_OPTIONS]}},
    {"field_name": "发件邮箱", "type": 1},
    {"field_name": "最近发信时间", "type": 1},
    {"field_name": "发信次数", "type": 2},
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
            new_score = await call_coze_scoring_workflow(ld)
            if new_score != old:
                _update_leads_record(rid, {"综合评分": new_score})
                fixed += 1
                print(f"[score-migrate] 校正虚高分：{ld.get('公司/机构','')} {old} -> {new_score}")
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"[score-migrate] 单条校正失败（{rid}）: {e}")
    if fixed:
        _invalidate_leads_cache()
    print(f"[score-migrate] 历史评分迁移完成，共校正 {fixed} 条不可触达线索")


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
    "Italy": "欧洲", "Poland": "欧洲", "Netherlands": "欧洲",
    # 东南亚
    "Thailand": "东南亚", "Vietnam": "东南亚", "Philippines": "东南亚",
    "Indonesia": "东南亚", "Malaysia": "东南亚", "Myanmar": "东南亚",
}
_RHC_PRODUCTS = [
    "RHC-V500 兽用麻醉机", "RHC-V300 兽用呼吸机",
    "RHC-IP600 兽用注射泵", "RHC-PM800 兽用监护仪",
    "RHC-SE200 兽用手术设备", "RHC-AH 动物医院整体方案",
]

_search_results_cache = {"data": None, "ts": 0.0}
_SEARCH_CACHE_TTL = 30  # 搜索结果30秒缓存


def _build_search_queries():
    """构建搜索词列表：产品关键词 + 目标市场 + importer/distributor/hospital"""
    queries = []
    # 每个产品关键词 + 每个目标市场 组合太多，选取代表性组合
    for product in _SEARCH_PRODUCTS[:3]:  # 取前3个核心产品
        for country in list(_SEARCH_COUNTRIES.keys())[:8]:  # 每轮取8个市场
            queries.append(f'"{product}" importer {country}')
    # 补充经销商/医院类搜索
    for product in _SEARCH_PRODUCTS[3:]:
        for country in list(_SEARCH_COUNTRIES.keys())[8:16]:
            queries.append(f'"{product}" distributor {country}')
    # 动物医院搜索（覆盖更多市场）
    for country in list(_SEARCH_COUNTRIES.keys()):
        queries.append(f'animal hospital equipment supplier {country}')
    return queries


def _search_ddg_leads(query: str, timeout: int = 8) -> list:
    """DuckDuckGo HTML搜索，返回 [{title, url, snippet}]"""
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
            html_text = r.read(1_500_000).decode("utf-8", "ignore")
        for m in re.finditer(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html_text, re.I):
            href = _ddg_real_url(m.group(1))
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not href or not title:
                continue
            # 找对应的 snippet
            snippet = ""
            snip_match = re.search(
                re.escape(href[:30]) + r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                html_text, re.I | re.S)
            if snip_match:
                snippet = re.sub(r'<[^>]+>', '', snip_match.group(1)).strip()[:300]
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= 8:
                break
    except _ue.HTTPError:
        pass
    except Exception:
        pass
    return results


def _extract_company_info(title: str, snippet: str, url: str) -> dict:
    """从搜索结果中提取公司信息"""
    text = f"{title} {snippet}".lower()
    # 提取公司名（title中第一个有意义的词组）
    company = title.split("|")[0].split("-")[0].split(",")[0].strip()
    # 去掉通用词
    for word in ["veterinary", "animal", "hospital", "equipment", "supplier",
                  "importer", "distributor", "wholesale", "official"]:
        company = company.replace(word, " ").strip()
    company = re.sub(r'\s+', ' ', company).strip()
    if len(company) < 3:
        company = title[:40].strip()

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


def _rate_lead_quality(info: dict) -> str:
    """质量评级：A=明确进口/采购+目标市场匹配，B=动物医院+目标市场，C=其他"""
    if info.get("is_importer") and info.get("country"):
        return "A"
    if info.get("is_hospital") and info.get("country"):
        return "B"
    if info.get("country"):
        return "B"
    return "C"


_BAD_SITE_MARKS = ("duckduckgo.com", "duck.com", "google.com/search", "bing.com/search",
                   "facebook.com/search", "amazon.com/s", "youtube.com/results")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


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


def _apply_score_gate(score, lead_info: dict) -> int:
    """评分出口闸门：无法触达的线索一律压到50分以下（封顶45）。"""
    try:
        s = int(float(score))
    except (TypeError, ValueError):
        s = 30
    s = max(0, min(100, s))
    if not _lead_is_reachable(lead_info):
        s = min(s, 45)
    return s


def _rule_fallback_score(lead_info: dict) -> int:
    """规则兜底打分：Coze工作流不可用时使用。A=90/B=60/C=30 + 补搜信息加分；
    出口同样经过可达性闸门（无法触达封顶45）。"""
    grade = _lead_val(lead_info, "confidence", "评级") or "C"
    base_score = {"A": 90, "B": 60, "C": 30}.get(grade, 30)
    bonus = 0
    if _real_website(_lead_val(lead_info, "website", "官网", "原文链接")):
        bonus += 5
    if _lead_val(lead_info, "industry", "行业"):
        bonus += 3
    if _real_email(_lead_val(lead_info, "email_pattern", "邮箱格式", "联系邮箱", "email")):
        bonus += 5
    return _apply_score_gate(min(100, base_score + bonus), lead_info)


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
        }
    # 情况a：字段已各归各位
    return {
        "total_score": total,
        "breakdown": data.get("breakdown", ""),
        "recommendation": data.get("recommendation", ""),
    }


async def call_coze_scoring_workflow(lead_info: dict) -> int:
    """调用Coze Lead_Score工作流AI打分，返回0-100整数。
    - 入参兼容英文/中文字段名；
    - 打分前清洗脏公司名（搜索词短语），且脏名不向 product 白嫖关键词；
    - 出口经过可达性闸门：无邮箱/官网/决策人/LinkedIn 的线索封顶45；
    工作流不可用/解析失败/超时 → 规则兜底，保证主流程不中断。"""
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

    if not pat or not wf_id:
        return _rule_fallback_score(norm)
    parameters = {
        "company_name": clean_company,
        "country": country,
        "product": product,
        "industry": industry,
        "website": website,
        "email_pattern": email_pattern,
        "grade": grade,
    }
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_coze_workflow_run, wf_id, parameters, 60),
            timeout=70)
        parsed = _extract_scoring_payload(data)
        score = parsed.get("total_score")
        score = int(float(score))
        if 0 <= score <= 100:
            return _apply_score_gate(score, norm)
        return _rule_fallback_score(norm)
    except asyncio.TimeoutError:
        print(f"[score] Coze打分超时，规则兜底: {clean_company or raw_company}")
        return _rule_fallback_score(norm)
    except Exception as e:
        print(f"[score] Coze打分失败({e})，规则兜底: {clean_company or raw_company}")
        return _rule_fallback_score(norm)


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


async def light_enrich_lead(company_name: str, country: str) -> dict:
    """轻补搜：官网、行业、邮箱格式
    用DuckDuckGo搜索，提取官网URL、行业关键词、邮箱格式
    单条总耗时控制在3-5秒"""
    result = {"website": "", "industry": "", "email_pattern": ""}
    try:
        # 搜索官网
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
                        result["website"] = u
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

        # 提取邮箱格式
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
    """深度补搜：决策人、LinkedIn、进口记录
    组合多维度搜索，提取关键商务信息"""
    result = {"decision_maker": "", "linkedin": "", "import_record": ""}
    try:
        queries = [
            f'"{company_name}" {country} CEO director manager contact',
            f'"{company_name}" LinkedIn',
            f'"{company_name}" {country} import veterinary medical equipment',
        ]
        all_text = ""
        for q in queries:
            sr = await asyncio.get_event_loop().run_in_executor(
                None, _search_ddg_single, q, _ENRICH_TIMEOUT)
            for item in sr:
                all_text += " " + item.get("title", "") + " " + item.get("snippet", "")
                # 提取LinkedIn链接
                u = item.get("url", "")
                if "linkedin.com" in u and company_name.lower().split()[0] in u.lower():
                    result["linkedin"] = u
            await asyncio.sleep(_ENRICH_DELAY)

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
                light_enrich_lead(lead.get("company_name", ""), lead.get("country", "")),
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
            new_score = await call_coze_scoring_workflow(lead)
            lead["score"] = new_score
            _update_leads_record(record_id, {"综合评分": new_score})
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
            update_fields = {"补搜状态": "已深度补全"}
            if deep.get("decision_maker"):
                update_fields["决策人"] = deep["decision_maker"]
                lead["decision_maker"] = deep["decision_maker"]
            if deep.get("linkedin"):
                update_fields["LinkedIn"] = deep["linkedin"]
                lead["linkedin"] = deep["linkedin"]
            if deep.get("import_record"):
                update_fields["进口记录"] = deep["import_record"]
                lead["import_record"] = deep["import_record"]
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


def _run_lead_search(max_results: int = 30) -> list:
    """执行主动搜索核心逻辑，返回结构化线索列表。
    诊断计数挂在返回列表对象的 _search_diag 属性上（列表可挂自定义属性）。"""
    queries = _build_search_queries()
    all_raw = []  # [{title, url, snippet}]
    seen_urls = set()
    empty_rounds = 0  # 连续空结果轮次，用于判断是否被搜索引擎限流
    # 限制搜索轮次，避免超时
    max_queries = min(len(queries), 20)
    for i, query in enumerate(queries[:max_queries]):
        results = _search_ddg_leads(query, timeout=6)
        if not results:
            empty_rounds += 1
        else:
            empty_rounds = 0
        for r in results:
            url = r.get("url", "").lower().strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                r["_query"] = query
                all_raw.append(r)
        if len(all_raw) >= max_results * 2:
            break
        # 每轮搜索间隔，避免被封
        if i > 0 and i % 5 == 0:
            time.sleep(0.5)

    # 提取公司信息
    leads = []
    seen_companies = set()
    dirty_dropped = 0  # 脏公司名（搜索词短语）被过滤的条数
    for r in all_raw:
        info = _extract_company_info(r["title"], r.get("snippet", ""), r["url"])
        if not info["company_name"] or len(info["company_name"]) < 3:
            continue
        # 脏公司名过滤：搜索词式短语（含冒号/import/多关键词）不是真实公司，直接丢弃，
        # 避免污染公海池、邮件主题与评分（如 "Brazil company: Veterinary ... import"）
        if not _clean_company_name(info["company_name"]):
            dirty_dropped += 1
            continue
        company_key = info["company_name"].lower().strip()
        if company_key in seen_companies:
            continue
        seen_companies.add(company_key)
        grade = _rate_lead_quality(info)
        lead = {
            "company_name": info["company_name"],
            "country": info["country"] or "未知",
            "region": info["region"] or "其他",
            "product_demand": info["product_demand"],
            "recommended_product": _recommend_product(info["product_demand"]),
            "website": r["url"],
            "email": "",
            "phone": "",
            "whatsapp": "",
            "source": f"DuckDuckGo搜索: {r.get('_query', '')[:60]}",
            "confidence": grade,
            "ai_suggestion": "",
        }
        lead["ai_suggestion"] = _generate_ai_suggestion(lead)
        leads.append(lead)
        if len(leads) >= max_results:
            break

    # 按质量排序 A > B > C
    grade_order = {"A": 0, "B": 1, "C": 2}
    leads.sort(key=lambda x: grade_order.get(x.get("confidence", "C"), 3))
    print(f"[search] 搜索诊断: 查询{min(len(queries), max_queries)}轮, "
          f"原始结果{len(all_raw)}条, 脏名过滤{dirty_dropped}条, 有效线索{len(leads)}条, "
          f"连续空轮次{empty_rounds}" + ("（疑似被搜索引擎限流）" if empty_rounds >= 3 and not all_raw else ""))
    return leads


class LeadSearchRequest(BaseModel):
    max_results: Optional[int] = 30
    force_refresh: Optional[bool] = False


@app.post("/api/leads/search")
async def api_leads_search(request: Request, req: Optional[LeadSearchRequest] = None):
    """AI主动搜索全网潜在客户，返回结构化线索列表。
    自动去重已有线索、质量评级、生成跟进建议。需登录。"""
    token = _get_token_from_request(request)
    user_info = _verify_token(token) if token else None
    if not user_info:
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)

    max_results = 30
    force_refresh = False
    if req:
        max_results = min(req.max_results or 30, 50)
        force_refresh = req.force_refresh or False

    # 缓存检查
    now = time.time()
    if not force_refresh and _search_results_cache["data"] is not None \
            and now - _search_results_cache["ts"] < _SEARCH_CACHE_TTL:
        return {"ok": True, "items": _search_results_cache["data"],
                "total": len(_search_results_cache["data"]),
                "cached": True}

    try:
        # 1) 执行基础搜索
        raw_leads = _run_lead_search(max_results=max_results)

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
        for lead in new_leads:
            score = await call_coze_scoring_workflow(lead)
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
                "补搜状态": "未补搜",
                "官网": "",
                "行业": "",
                "邮箱格式": "",
                "决策人": "",
                "LinkedIn": "",
                "进口记录": "",
            }
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
        _invalidate_leads_cache()

        # 4) 启动后台补搜线程（轻补搜→重评分→Top5深度补搜）
        if leads_with_record_ids:
            _start_enrichment_background(leads_with_record_ids)

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

        # 6) 缓存结果
        _search_results_cache["data"] = new_leads
        _search_results_cache["ts"] = time.time()

        # 统计
        a_count = sum(1 for l in new_leads if l.get("confidence") == "A")
        b_count = sum(1 for l in new_leads if l.get("confidence") == "B")
        c_count = sum(1 for l in new_leads if l.get("confidence") == "C")

        # 立即返回基础结果，后台补搜异步进行中
        return {
            "ok": True,
            "items": new_leads,
            "total": len(new_leads),
            "cached": False,
            "enrichment": "started" if leads_with_record_ids else "none",
            "stats": {
                "total_found": len(raw_leads),
                "new_after_dedup": len(new_leads),
                "a_grade": a_count,
                "b_grade": b_count,
                "c_grade": c_count,
            },
            "search_time": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        print(f"[search] 主动搜索失败: {e}")
        return JSONResponse({"ok": False, "message": f"搜索失败：{e}"}, status_code=502)


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
    """异步发信：to_thread 包装同步 SMTP，返回实际发件地址。"""
    return await asyncio.to_thread(
        _smtp_send_mail, to_addrs, subject, body, cc,
        sender_email, sender_password)


# ------------------------------------------------------------
# 开发信生成：Coze 工作流优先，本地高质量英文模板兜底
# ------------------------------------------------------------
def _clean_company_name(raw: str) -> str:
    """清洗脏公司名：搜索词式短语（含多关键词/冒号/import等）不当公司名用，返回空串。
    真实公司名通常是 1-4 个词的专名，常见法律后缀 Ltd/Inc/LLC/GmbH/S.A./Corp 等。"""
    if not raw:
        return ""
    name = raw.strip()
    low = name.lower()
    # 明显是搜索词/句子的特征
    bad_markers = (":", "：", " import ", " import,", " company:", " companies ",
                   " supplier", " manufacturers ", " for sale", " price", " buy ",
                   " wholesale", " distributor of", " looking for", " wanted")
    if any(m in low for m in bad_markers):
        return ""
    # 中文整句（含多个中文且无法律后缀）多半是搜索词
    legal_suffix = ("ltd", "inc", "llc", "gmbh", "corp", "co.", "company", "s.a",
                    "s.a.", "sa ", "limited", "co.,", "group", "holdings", "importadora",
                    "comercio", "medical", "vet", "hospital", "clinic", "laboratorio",
                    "laboratories", "health", "healthcare", "animal", "pet", "pharm")
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", name))
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", name))
    if has_cjk and cjk_count >= 4 and not any(s in low for s in legal_suffix):
        return ""
    # 词数过多（>6）且无法律后缀，视为描述性短语
    words = re.findall(r"[A-Za-z0-9\.]+", name)
    if len(words) > 6 and not any(s in low for s in legal_suffix):
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
    """公海池：返回全部线索（含已认领的），按综合评分降序。需JWT认证。"""
    token = _get_token_from_request(request)
    if not token or not _verify_token(token):
        return JSONResponse({"ok": False, "message": "未登录或登录已过期"}, status_code=401)
    try:
        leads = _fetch_leads(force_refresh=True)
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
    # 优先从请求头取销售ID，其次从JWT
    sales_id = request.headers.get("X-Sales-Id", "").strip()
    if not sales_id:
        sales_id = user_info.get("name", "") or user_info.get("username", "")
    if not sales_id:
        return {"ok": True, "items": [], "stats": {"total": 0}}
    try:
        leads = _fetch_leads(force_refresh=True)
        my_leads = [l for l in leads if (l.get("认领人") or "").strip() == sales_id and l.get("认领状态") == "已认领"]
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
            enriched = await light_enrich_lead(company_name, country)
            update_fields = {"补搜状态": "已轻补"}
            if enriched.get("website"):
                update_fields["官网"] = enriched["website"]
            if enriched.get("industry"):
                update_fields["行业"] = enriched["industry"]
            if enriched.get("email_pattern"):
                update_fields["邮箱格式"] = enriched["email_pattern"]
            # 重评分
            lead_info = {**{k: _tv(v) for k, v in fields.items()}, **enriched}
            new_score = await call_coze_scoring_workflow(lead_info)
            update_fields["综合评分"] = new_score
            _update_leads_record(record_id, update_fields)
            _invalidate_leads_cache()
            return {"ok": True, "message": "轻补搜完成", "data": enriched, "new_score": new_score}
        else:
            _update_leads_record(record_id, {"补搜状态": "深度补搜中"})
            deep = await deep_enrich_lead(company_name, country, website)
            update_fields = {"补搜状态": "已深度补全"}
            if deep.get("decision_maker"):
                update_fields["决策人"] = deep["decision_maker"]
            if deep.get("linkedin"):
                update_fields["LinkedIn"] = deep["linkedin"]
            if deep.get("import_record"):
                update_fields["进口记录"] = deep["import_record"]
            _update_leads_record(record_id, update_fields)
            _invalidate_leads_cache()
            return {"ok": True, "message": "深度补搜完成", "data": deep}
    except Exception as e:
        print(f"[enrich] 手动补搜失败 ({record_id}): {e}")
        _update_leads_record(record_id, {"补搜状态": "补搜失败"})
        return JSONResponse({"ok": False, "message": f"补搜失败：{e}"}, status_code=502)


async def _scheduled_leads_search():
    """定时任务：每天9:00/15:00自动搜索线索并写入公海池，后台启动补搜"""
    print(f"[scheduler] 定时搜索开始: {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        raw_leads = _run_lead_search(max_results=30)
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
            score = await call_coze_scoring_workflow(lead)
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
                "补搜状态": "未补搜",
                "官网": "",
                "行业": "",
                "邮箱格式": "",
                "决策人": "",
                "LinkedIn": "",
                "进口记录": "",
            }
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
        scheduler.add_job(_scheduled_leads_search, CronTrigger(hour=9, minute=0), id="leads_search_9am")
        scheduler.add_job(_scheduled_leads_search, CronTrigger(hour=15, minute=0), id="leads_search_3pm")
        scheduler.start()
        print("[scheduler] APScheduler 已启动，定时任务: 每天 09:00 / 15:00 北京时间")
    except Exception as e:
        print(f"[scheduler] APScheduler 启动失败: {e}")
    # 启动后静默校正"可达性闸门"上线前的历史虚高评分（幂等，只跑一次）
    try:
        asyncio.create_task(_migrate_legacy_scores())
    except Exception as e:
        print(f"[score-migrate] 迁移任务启动失败: {e}")


# 启动时后台预热飞书「系统账号」表（建表+种子数据），不阻塞服务启动
threading.Thread(target=_warmup_account_table, daemon=True).start()
# 启动时后台预热飞书「商机线索」表（自动建表），不阻塞服务启动
threading.Thread(target=_warmup_leads_table, daemon=True).start()
# 启动时后台预热飞书「消息通知」表（自动建表），不阻塞服务启动
threading.Thread(target=_warmup_messages_table, daemon=True).start()
# 启动时后台预热飞书「开发信记录」表（自动建表），不阻塞服务启动
threading.Thread(target=_warmup_emails_table, daemon=True).start()

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
