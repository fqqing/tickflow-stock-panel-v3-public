"""当前用户作用域 — 多用户数据隔离的地基。

单管理员多用户模型: 每个用户一份私有数据目录 data/user_data/{username}/。

设计:
  - 用 contextvars.ContextVar 传递「当前请求用户」。FastAPI 中间件在请求进来时
    set (见 main.py auth_middleware), 下游服务通过 get_current_user() 读取。
  - 后台任务(盘后管道 / 监控引擎 / 行情轮询等)没有请求上下文, ContextVar 落在
    默认值 admin —— 即后台任务运行在管理员作用域, 读管理员的配置。
  - 隔离范围(私有数据): watchlist / preferences / strategy_overrides /
    custom_signals / ai_reports / alerts / last_backtest_trades / research_candidates。
    不隔离(全局共享): secrets.json(服务器级数据源/AI 凭证)、strategy_cache(计算缓存)、
    monitor_rules(仅 admin 可配置, 后台监控引擎全局生效)。
"""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

from app.config import settings

# 默认 admin: 后台任务无请求上下文时落到管理员作用域
_current_user: ContextVar[str] = ContextVar("current_user", default="admin")


def set_current_user(username: str | None):
    """设置当前请求用户(供中间件调用), 返回 ContextVar reset token。

    None/空串回落 admin。调用方应在请求结束时用返回的 token 调 reset_current_user。
    """
    return _current_user.set((username or "").strip() or "admin")


def reset_current_user(token) -> None:
    """恢复 ContextVar 到请求前状态(中间件 finally 里调用)。"""
    _current_user.reset(token)


def get_current_user() -> str:
    """返回当前用户。后台任务(无请求)返回 admin。"""
    return _current_user.get() or "admin"


def is_admin(username: str | None = None) -> bool:
    """当前(或指定)用户是否为管理员。"""
    from app.services import auth
    return auth.is_admin_user(username or get_current_user())


def user_data_dir(username: str | None = None) -> Path:
    """返回当前(或指定)用户的私有数据目录 data/user_data/{username}/。

    不自动创建目录 —— 由调用方按需 mkdir(与现有各服务的 _path() 行为一致)。
    """
    u = (username or get_current_user()).strip() or "admin"
    return settings.data_dir / "user_data" / u


def global_data_dir() -> Path:
    """返回全局(跨用户共享)的 user_data 根 data/user_data/。

    用于 secrets.json / strategy_cache 等不隔离的数据(保持既有路径不变)。
    """
    return settings.data_dir / "user_data"
