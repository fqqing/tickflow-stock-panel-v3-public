"""盘中实时行情 -- 复用 v1 的腾讯 qt 快照 provider.

为什么不是 stockdb
==================
free-stockdb 是**每日同步的静态数据包**: 数据源 -> 数据更新.exe(定时 15:50)
-> 本地 /data, SDK 只有 ``get_data`` 这一个 K 线入口, 没有任何实时接口。
它补历史极好(全历史日线 + 250 交易日的分钟), 但盘中永远是昨天的收盘价。

所以分工是: **历史归 stockdb, 盘中归 v1 的腾讯源**。

为什么不复制一份代码
====================
v1 的 ``app/plugins/tencent/provider.py`` 是纯标准库实现(urllib + 线程池),
用 importlib 直接加载那个文件即可, v1 升级会自动跟随。它 import 的两个符号
``app.data_providers.base.AssetType`` / ``app.market_time.CN_TZ`` 来自 v1 的
业务包, 这里不加载整个 app 包(重), 而是先注入两个等价桩再 exec 模块。

⚠️ 加载顺序坑: dataclass 在定义时会去 ``sys.modules[cls.__module__]`` 取命名
空间, 所以**必须先把空模块塞进 sys.modules 再 exec_module**, 否则报
``AttributeError: 'NoneType' object has no attribute '__dict__'``。

实测(2026-10-02): 全市场 7550 只 A 股 + ETF 快照 2.14s, 零鉴权。
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import time
import types
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_V1_BACKEND = Path("D:/project/GP/tickflow-stock-panel/backend")
PROVIDER_RELPATH = "app/plugins/tencent/provider.py"

#: 快照缓存时间(秒)。盘中最快 3s 一笔行情, 但选股页刷新的粒度是「翻页/改筛选」,
#: 60s 足以避免每次交互都打一次网络请求。
CACHE_TTL = float(os.environ.get("TICKFLOW_QUOTE_TTL", "60"))

#: at = perf_counter(判过期用, 单调) / wall = time.time(展示用, 墙上时钟)
_CACHE: dict[str, Any] = {"at": 0.0, "wall": 0.0, "rows": {}, "ok": False, "detail": ""}

_MODULE: types.ModuleType | None = None
_LOAD_ERROR = ""


def v1_backend_dir() -> Path:
    """v1 仓库 backend 目录: 显式环境变量优先, 否则用本机默认路径."""
    raw = os.environ.get("TICKFLOW_V1_BACKEND", "")
    return Path(raw) if raw else DEFAULT_V1_BACKEND


def _load_provider_module() -> types.ModuleType | None:
    """加载 v1 的 tencent provider 文件. 失败返回 None 并把原因留在 _LOAD_ERROR.

    v1 不在(机器迁移/仓库改名)时实时功能只是不可用, 不能因此让后端起不来.
    """
    global _MODULE, _LOAD_ERROR
    if _MODULE is not None:
        return _MODULE

    path = v1_backend_dir() / PROVIDER_RELPATH
    if not path.is_file():
        _LOAD_ERROR = f"找不到 v1 的腾讯 provider: {path}"
        return None

    backend = v1_backend_dir()

    def _pkg(name: str, *parts: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = [str(backend.joinpath(*parts))]  # type: ignore[attr-defined]
        return mod

    class _AssetType:
        """v1 的 AssetType 只用得到这几个成员, 桩够用即可."""

        EQUITY = "equity"
        INDEX = "index"
        ETF = "etf"

    base = types.ModuleType("app.data_providers.base")
    base.AssetType = _AssetType  # type: ignore[attr-defined]
    market_time = types.ModuleType("app.market_time")
    market_time.CN_TZ = timezone(timedelta(hours=8))  # type: ignore[attr-defined]

    stubs = {
        "app": _pkg("app", "app"),
        "app.data_providers": _pkg("app.data_providers", "app", "data_providers"),
        "app.data_providers.base": base,
        "app.market_time": market_time,
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)

    name = "_tickflow_v1_tencent_provider"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            _LOAD_ERROR = f"无法构造 module spec: {path}"
            return None
        module = importlib.util.module_from_spec(spec)
        # dataclass 定义时要查 sys.modules[__module__], 必须先注册再 exec
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _MODULE = module
    except Exception as exc:  # 加载失败一律降级, 不向上传播
        _LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        sys.modules.pop(name, None)
    finally:
        for key, old in saved.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
    return _MODULE


def _fetch_all() -> tuple[dict[str, dict], str]:
    """拉一次全市场快照. 返回 (symbol -> 行情, 错误说明)."""
    module = _load_provider_module()
    if module is None:
        return {}, _LOAD_ERROR
    try:
        provider = module.TencentMinuteProvider()
    except Exception as exc:  # provider 初始化依赖 v1 的标的维表, 缺了也要能降级
        return {}, f"provider 初始化失败: {type(exc).__name__}: {exc}"
    try:
        rows = provider.get_realtime() or []
    except Exception as exc:
        return {}, f"快照请求失败: {type(exc).__name__}: {exc}"
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    out: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if not sym:
            continue
        out[str(sym)] = {
            "name": row.get("name") or "",
            "last_price": row.get("last_price"),
            "prev_close": row.get("prev_close"),
            "change_pct": row.get("change_pct"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "volume": row.get("volume"),
            "amount": row.get("amount"),
        }
    return out, "" if out else "快照返回 0 行(网络不通或被风控)"


def quotes(symbols: list[str] | None = None, ttl: float | None = None) -> dict:
    """取实时快照.

    ``symbols`` 为空时返回全市场(约 7500 只, 首次 ~2s)。带进程内 TTL 缓存,
    缓存未过期就不打网络。任何失败都返回 ``ok=False`` + ``detail``, 由调用方
    决定是显示「实时不可用」还是直接隐藏实时列 —— 实时挂掉不该拖垮选股页。
    """
    limit = CACHE_TTL if ttl is None else ttl
    fresh = (time.perf_counter() - _CACHE["at"]) < limit and _CACHE["rows"]
    if not fresh:
        rows, detail = _fetch_all()
        if rows:
            _CACHE.update(
                {
                    "at": time.perf_counter(),
                    "wall": time.time(),
                    "rows": rows,
                    "ok": True,
                    "detail": "",
                }
            )
        else:
            # 拉失败时保留上一次成功的快照(收盘后/网络抖动时页面不至于空白),
            # 但把 ok 置 False 让前端能提示
            _CACHE.update(
                {
                    "at": time.perf_counter(),
                    "wall": time.time(),
                    "ok": False,
                    "detail": detail,
                }
            )

    pool: dict[str, dict] = _CACHE.get("rows") or {}
    picked = (
        {s: pool[s] for s in symbols if s in pool} if symbols else dict(pool)
    )
    return {
        "ok": bool(_CACHE["ok"]),
        "detail": _CACHE["detail"],
        "updated_at": time.strftime("%H:%M:%S", time.localtime(_CACHE["wall"]))
        if _CACHE["wall"]
        else None,
        "source": "tencent/qt (v1)",
        "quotes": picked,
    }


def invalidate() -> None:
    """强制下一请求重拉. 给测试与「手动刷新」按钮用."""
    _CACHE["at"] = 0.0
