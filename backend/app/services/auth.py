"""访问认证 — 单管理员多用户, 自托管场景。

设计:
  - 密码用 PBKDF2-HMAC-SHA256 哈希(标准库 hashlib, 无新依赖), 加随机 salt。
  - 会话用随机 token(token_urlsafe), 内存 + 文件双存(支持多进程/重启不丢失),
    且每个会话绑定用户名(token → username)。
  - 存储: data/user_data/auth.json (chmod 0600)。
    auth.json 结构:
      {
        "users": { "<username>": {"salt","hash","is_admin","created_at"} },
        "sessions": { "<token>": {"u": username, "e": expire_ts} }
      }

安全要点:
  - 用户名规范化: 字母开头, 3-32 位, 仅 [A-Za-z0-9_-], 杜绝路径穿越(用户名用作目录名)。
  - 设密码接口必须限制本机/内网(见 auth router), 防黑客抢占域名抢先设密码。
  - 登录限流: 错5次锁5分钟(见 auth router 内存计数)。
  - 主管理员(settings.admin_username)不可删除; 至少保留一个管理员。

数据隔离: 会话 token 绑定用户名后, 下游服务经 user_scope 按用户定位私有目录。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import threading
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# PBKDF2 参数(NIST 推荐, 单次校验 ~100ms, 兼顾安全与响应)
_PBKDF2_ITER = 200_000
_SALT_LEN = 16
_TOKEN_BYTES = 32

# 会话有效期: 30 天(自托管, 长一点减少重登频率)
SESSION_TTL = 30 * 24 * 3600

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,31}$")

_lock = threading.Lock()
# 内存中的有效会话: { token: {"u": username, "e": expire_ts} }。进程重启后从磁盘恢复。
_sessions: dict[str, dict] = {}

# 「是否已有用户」缓存: 每个 /api/ 请求都要判定, auth_middleware 原先每次 read_text
# 磁盘 (阻塞事件循环)。此处懒加载缓存, 用户增删/改密后失效重算。
_configured_cache: bool | None = None


def _path() -> Path:
    p = settings.data_dir / "user_data" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("auth.json malformed: %s", e)
            return {}
        # 旧单密码格式迁移: {password_hash, password_salt, sessions:{token:expire}}
        if "users" not in d and "password_hash" in d:
            d = _migrate_legacy(d)
            _save(d)
        return d
    return {}


def _save(data: dict) -> None:
    p = _path()
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _migrate_legacy(d: dict) -> dict:
    """旧单密码 auth.json → 新多用户格式。现有密码归属到主管理员名下。"""
    admin = settings.admin_username
    d["users"] = {
        admin: {
            "salt": d.get("password_salt", ""),
            "hash": d["password_hash"],
            "is_admin": True,
            "created_at": int(time.time()),
        }
    }
    old_sessions = d.get("sessions") or {}
    d["sessions"] = {t: {"u": admin, "e": e} for t, e in old_sessions.items()}
    d.pop("password_hash", None)
    d.pop("password_salt", None)
    d.pop("updated_at", None)
    logger.info("auth.json migrated: single-password -> multi-user (admin=%s)", admin)
    return d


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。salt 为 None 时生成新 salt。"""
    if salt is None:
        salt = os.urandom(_SALT_LEN)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return salt.hex(), dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """恒定时间比较, 防时序攻击。"""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return _secrets.compare_digest(actual, expected)


def _normalize_username(username: str) -> str:
    """规范化用户名: 字母开头, 3-32 位, 仅 [A-Za-z0-9_-]。杜绝路径穿越。"""
    u = (username or "").strip()
    if not _USERNAME_RE.fullmatch(u):
        raise ValueError("用户名需 3-32 位, 字母开头, 仅含字母/数字/下划线/中划线")
    return u


# ================================================================
# 查询
# ================================================================

def is_configured() -> bool:
    """是否已有用户 (带缓存)。热路径, 命中缓存不碰磁盘。"""
    global _configured_cache
    if _configured_cache is None:
        d = _load()
        _configured_cache = bool(d.get("users"))
    return _configured_cache


def list_users() -> list[dict]:
    """列出全部用户 (不含密码哈希)。管理员在前。"""
    d = _load()
    out = []
    for u, info in (d.get("users") or {}).items():
        out.append({
            "username": u,
            "is_admin": bool(info.get("is_admin")),
            "created_at": info.get("created_at", 0),
        })
    out.sort(key=lambda x: (not x["is_admin"], x["username"]))
    return out


def is_admin_user(username: str | None) -> bool:
    """指定用户是否为管理员。"""
    if not username:
        return False
    d = _load()
    info = (d.get("users") or {}).get(username)
    return bool(info and info.get("is_admin"))


def user_exists(username: str) -> bool:
    d = _load()
    return username in (d.get("users") or {})


# ================================================================
# 用户管理
# ================================================================

def set_password(password: str) -> None:
    """设置/重置主管理员密码。首次调用即创建 admin 账号。清空所有会话。"""
    global _configured_cache
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    admin = settings.admin_username
    salt_hex, hash_hex = _hash_password(password)
    with _lock:
        d = _load()
        users = d.get("users") or {}
        users[admin] = {
            "salt": salt_hex,
            "hash": hash_hex,
            "is_admin": True,
            "created_at": int(time.time()),
        }
        d["users"] = users
        d["sessions"] = {}
        _save(d)
        _sessions.clear()
    _configured_cache = None
    logger.info("admin password set")


def create_user(username: str, password: str, is_admin: bool = False) -> dict:
    """创建用户(普通或管理员)。"""
    global _configured_cache
    username = _normalize_username(username)
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    salt_hex, hash_hex = _hash_password(password)
    with _lock:
        d = _load()
        users = d.get("users") or {}
        if username in users:
            raise ValueError("用户名已存在")
        users[username] = {
            "salt": salt_hex,
            "hash": hash_hex,
            "is_admin": bool(is_admin),
            "created_at": int(time.time()),
        }
        d["users"] = users
        _save(d)
    _configured_cache = None
    logger.info("user created: %s (admin=%s)", username, is_admin)
    return {"username": username, "is_admin": bool(is_admin)}


def delete_user(username: str) -> None:
    """删除用户(连同其会话)。主管理员不可删; 至少保留一个管理员。"""
    global _configured_cache
    username = _normalize_username(username)
    with _lock:
        d = _load()
        users = d.get("users") or {}
        if username not in users:
            raise ValueError("用户不存在")
        if username == settings.admin_username:
            raise ValueError("不能删除主管理员账号")
        if users[username].get("is_admin"):
            admin_count = sum(1 for i in users.values() if i.get("is_admin"))
            if admin_count <= 1:
                raise ValueError("至少保留一个管理员账号")
        del users[username]
        d["users"] = users
        d["sessions"] = {t: s for t, s in (d.get("sessions") or {}).items() if s.get("u") != username}
        _save(d)
        # 内存会话同步清除该用户
        for t in [t for t, s in _sessions.items() if s.get("u") == username]:
            _sessions.pop(t, None)
    _configured_cache = None
    logger.info("user deleted: %s", username)


def reset_password(username: str, new_password: str) -> None:
    """重置指定用户密码(admin 操作)。清空该用户全部会话。"""
    global _configured_cache
    username = _normalize_username(username)
    if len(new_password) < 6:
        raise ValueError("密码至少 6 位")
    salt_hex, hash_hex = _hash_password(new_password)
    with _lock:
        d = _load()
        users = d.get("users") or {}
        if username not in users:
            raise ValueError("用户不存在")
        users[username]["salt"] = salt_hex
        users[username]["hash"] = hash_hex
        d["users"] = users
        d["sessions"] = {t: s for t, s in (d.get("sessions") or {}).items() if s.get("u") != username}
        _save(d)
        for t in [t for t, s in _sessions.items() if s.get("u") == username]:
            _sessions.pop(t, None)
    _configured_cache = None
    logger.info("user password reset: %s", username)


# ================================================================
# 会话
# ================================================================

def bootstrap_from_env() -> bool:
    """首次初始化: 若环境变量 AUTH_PASSWORD 已配置且尚无用户, 则创建 admin。

    公网服务器部署场景: 避免每次都要 SSH 端口转发才能设首个密码。
    明文密码只在内存/配置中, 经 set_password() 哈希后写入 auth.json (chmod 0600)。
    一旦设置成功, 后续重启不再覆盖 (用户改密码走 UI, 不受环境变量影响)。
    """
    from app.config import _ENV_FILE

    pwd = (settings.auth_password or "").strip()
    if _ENV_FILE.is_file():
        from dotenv import dotenv_values
        raw_pwd = dotenv_values(_ENV_FILE, encoding="utf-8", interpolate=False).get("AUTH_PASSWORD")
        if isinstance(raw_pwd, str) and raw_pwd.strip():
            pwd = raw_pwd.strip()
    if not pwd:
        return False
    if is_configured():
        return False
    try:
        set_password(pwd)
        logger.info("admin password bootstrapped from AUTH_PASSWORD env (one-time)")
        return True
    except ValueError as e:
        logger.warning("AUTH_PASSWORD bootstrap skipped: %s", e)
        return False


def verify_and_create_session(username: str, password: str) -> str | None:
    """验证用户名密码, 成功则创建会话并返回 token, 失败返回 None。"""
    username = (username or "").strip()
    d = _load()
    info = (d.get("users") or {}).get(username)
    if not info:
        return None
    if not _verify_password(password, info.get("salt", ""), info.get("hash", "")):
        return None
    token = _secrets.token_urlsafe(_TOKEN_BYTES)
    expire = time.time() + SESSION_TTL
    with _lock:
        _sessions[token] = {"u": username, "e": expire}
        _persist_sessions_locked()
    return token


def revoke_session(token: str) -> None:
    """注销会话(登出)。"""
    with _lock:
        _sessions.pop(token, None)
        _persist_sessions_locked()


def get_session_user(token: str) -> str | None:
    """返回会话绑定的用户名; 无效/过期返回 None(过期则清理)。"""
    if not token:
        return None
    with _lock:
        s = _sessions.get(token)
        if s is None:
            return None
        if time.time() > s.get("e", 0):
            _sessions.pop(token, None)
            _persist_sessions_locked()
            return None
        return s.get("u")


def is_valid_session(token: str) -> bool:
    """检查会话是否有效。"""
    return get_session_user(token) is not None


def _persist_sessions_locked() -> None:
    """把当前内存会话写回 auth.json(需持锁调用)。"""
    d = _load()
    d["sessions"] = {t: {"u": s["u"], "e": s["e"]} for t, s in _sessions.items()}
    _save(d)


def _restore_sessions() -> None:
    """启动时从 auth.json 恢复未过期会话(支持进程重启不丢登录态)。"""
    with _lock:
        d = _load()
        now = time.time()
        saved = d.get("sessions") or {}
        for token, s in saved.items():
            if isinstance(s, dict) and s.get("e") and s["e"] > now:
                _sessions[token] = {"u": s.get("u", settings.admin_username), "e": s["e"]}
        if len(_sessions) != len(saved):
            _persist_sessions_locked()


# 模块加载时恢复会话
try:
    _restore_sessions()
except Exception as e:  # noqa: BLE001
    logger.warning("restore sessions failed: %s", e)
