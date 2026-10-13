"""访问认证 API — 单管理员多用户。

端点:
  GET  /api/auth/status                    — 是否已有用户、当前会话是否有效、当前用户名
  POST /api/auth/setup                     — 首次设置管理员密码(仅限本机/内网, 防公网抢占)
  POST /api/auth/login                     — 登录(用户名+密码 → 会话 token, 含限流)
  POST /api/auth/logout                    — 注销当前会话
  POST /api/auth/change-password           — 修改自己密码(需已登录)
  GET  /api/auth/users                     — 列出用户(admin)
  POST /api/auth/users                     — 创建用户(admin)
  DELETE /api/auth/users/{username}        — 删除用户(admin)
  POST /api/auth/users/{username}/reset-password — 重置用户密码(admin)

安全:
  - setup 端点只接受本机/内网请求(request.client.host), 公网请求 403。
  - login 限流: 同一来源 IP 连续失败 5 次, 锁 5 分钟(内存计数)。
  - admin 端点要求当前会话用户为管理员。
  - 会话 token 通过 HttpOnly cookie 下发, 前端无需手动管理。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tf_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 与 SESSION_TTL 一致

# 限流: { ip: (fail_count, lock_until_ts) }
_fail_counter: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
_fail_lock = Lock()
_MAX_FAILS = 5
_LOCK_SECONDS = 300


def _is_local_network(host: str | None) -> bool:
    """是否本机或内网请求。"""
    if not host:
        return False
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。仅当直连 peer 是回环/内网时采信 X-Forwarded-For。"""
    direct = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    if xff and direct and _is_local_network(direct):
        return xff.split(",")[0].strip()
    return direct or "unknown"


def _check_login_rate_limit(ip: str) -> None:
    with _fail_lock:
        count, until = _fail_counter.get(ip, (0, 0.0))
        now = time.time()
        if until > now:
            wait = int(until - now)
            raise HTTPException(status_code=429, detail=f"登录失败次数过多, 请 {wait} 秒后重试")
        if until and until <= now:
            _fail_counter.pop(ip, None)


def _record_login_fail(ip: str) -> None:
    with _fail_lock:
        if len(_fail_counter) > 1000:
            now = time.time()
            for stale in [k for k, (_, u) in _fail_counter.items() if u <= now]:
                _fail_counter.pop(stale, None)
        count, until = _fail_counter.get(ip, (0, 0.0))
        count += 1
        if count >= _MAX_FAILS:
            until = time.time() + _LOCK_SECONDS
            logger.warning("auth login locked for %s after %d fails", ip, count)
        _fail_counter[ip] = (count, until)


def _clear_login_fails(ip: str) -> None:
    with _fail_lock:
        _fail_counter.pop(ip, None)


def _current_username(request: Request) -> str | None:
    """从 cookie 解析当前登录用户名(未登录返回 None)。"""
    token = request.cookies.get(COOKIE_NAME)
    return auth.get_session_user(token) if token else None


def _require_admin(request: Request) -> str:
    """要求当前会话为管理员, 返回管理员用户名, 否则 403。"""
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="请先登录")
    if not auth.is_admin_user(username):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return username


# ================================================================
# 端点
# ================================================================

class PasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class CreateUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    is_admin: bool = False


class ResetPasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


@router.get("/status")
def auth_status(request: Request) -> dict:
    """认证状态: 是否已有用户 + 当前请求是否已登录 + 当前用户名。"""
    token = request.cookies.get(COOKIE_NAME)
    username = auth.get_session_user(token) if token else None
    return {
        "configured": auth.is_configured(),
        "authenticated": username is not None,
        "username": username,
        "is_admin": auth.is_admin_user(username) if username else False,
    }


@router.post("/setup")
def setup_password(req: PasswordIn, request: Request) -> dict:
    """首次设置管理员密码。仅限本机/内网请求(防公网抢占)。"""
    client_ip = _client_ip(request)
    if not _is_local_network(client_ip):
        logger.warning("setup rejected from non-local ip: %s", client_ip)
        raise HTTPException(
            status_code=403,
            detail="首次设置密码仅允许本机或内网访问,请通过 SSH/本地浏览器操作",
        )
    if auth.is_configured():
        raise HTTPException(status_code=409, detail="密码已设置,如需修改请登录后使用改密码功能")
    auth.set_password(req.password)
    logger.info("admin password set up from %s", client_ip)
    return {"ok": True, "configured": True}


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    """登录: 用户名+密码 → 会话 token(写 HttpOnly cookie)。含失败限流。"""
    ip = _client_ip(request)
    _check_login_rate_limit(ip)

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    token = auth.verify_and_create_session(req.username, req.password)
    if not token:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    _clear_login_fails(ip)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,
    )
    return {"ok": True, "authenticated": True, "username": req.username}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """注销当前会话。"""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordIn, request: Request, response: Response) -> dict:
    """修改自己密码: 需验证旧密码, 成功后清空该用户全部会话(需重新登录)。"""
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="请先登录")

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    # 验证旧密码
    new_token = auth.verify_and_create_session(username, req.old_password)
    if not new_token:
        ip = _client_ip(request)
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="旧密码错误")
    auth.revoke_session(new_token)  # 临时 token 用完即弃

    auth.reset_password(username, req.new_password)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True, "message": "密码已修改, 请重新登录"}


# ================================================================
# 用户管理 (admin only)
# ================================================================

@router.get("/users")
def list_users(request: Request) -> dict:
    _require_admin(request)
    return {"users": auth.list_users()}


@router.post("/users")
def create_user(req: CreateUserIn, request: Request) -> dict:
    _require_admin(request)
    try:
        user = auth.create_user(req.username, req.password, req.is_admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "user": user}


@router.delete("/users/{username}")
def delete_user(username: str, request: Request) -> dict:
    _require_admin(request)
    try:
        auth.delete_user(username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.post("/users/{username}/reset-password")
def reset_password(username: str, req: ResetPasswordIn, request: Request) -> dict:
    _require_admin(request)
    try:
        auth.reset_password(username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}
