"""eltdx 网关: 共享客户端 + 慢接口 TTL 缓存(stale-while-revalidate)。

两个职责
========
1. **共享 client**。eltdx 内部是连接池, 每处各建一个会白白多占 7709 连接
   (实测每线程自建比共享慢约 30 倍)。插件 provider 已有一个进程级实例, 这里复用。
2. **慢接口缓存**。``theme_strength_rank`` 137s / ``limit_ladder`` 113s 这种量级
   绝不能放在请求线程里同步跑。缓存策略:

   - 新鲜(未过 TTL) => 直接返回, 标 ``stale=False``。
   - 过期 => **立刻返回旧值**并标 ``stale=True``, 同时后台线程刷新(不阻塞用户)。
   - 冷启动(无旧值) => 只能同步等, 但结果落盘, 重启后端也能复用
     (否则 113s 的接口每次重启都要重跑一遍)。

落盘目录 ``<data_dir>/intraday_cache/<key>.json``。存的是解析后的纯 JSON 结构,
不存 eltdx 对象(上游改版时旧缓存反序列化会炸, 故带 ``_schema`` 版本号)。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 缓存结构版本。上游字段语义变了就 +1, 旧盘自动失效。
_SCHEMA = 1

_CACHE_LOCK = threading.Lock()
_REFRESHING: set[str] = set()

#: {key: (写入时刻, payload)}。只做进程内索引, 真正的内容在磁盘上。
_INDEX: dict[str, tuple[float, Any]] = {}


def client() -> Any:
    """进程共享的 eltdx TdxClient(与插件 provider 同一个实例)。"""
    from app.plugins.eltdx.provider import shared_client

    return shared_client()


def cache_dir() -> Path:
    """慢接口缓存目录。"""
    from app.config import settings

    path = Path(settings.data_dir) / "intraday_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(key: str) -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in key)
    return cache_dir() / f"{safe}.json"


def _read_disk(key: str) -> tuple[float, Any] | None:
    path = _path(key)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("盘中缓存 %s 读取失败(将重新拉取): %s", key, e)
        return None
    if not isinstance(raw, dict) or raw.get("_schema") != _SCHEMA:
        return None
    return float(raw.get("_stamp") or 0.0), raw.get("payload")


def _write_disk(key: str, payload: Any, stamp: float) -> None:
    try:
        _path(key).write_text(
            json.dumps({"_schema": _SCHEMA, "_stamp": stamp, "payload": payload},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("盘中缓存 %s 落盘失败(仅影响重启复用): %s", key, e)


def _refresh(key: str, loader: Callable[[], Any]) -> tuple[Any, float] | None:
    try:
        payload = loader()
    except Exception as e:
        logger.warning("盘中缓存 %s 刷新失败: %s: %s", key, type(e).__name__, e)
        return None
    stamp = time.time()
    with _CACHE_LOCK:
        _INDEX[key] = (stamp, payload)
    _write_disk(key, payload, stamp)
    return payload, stamp


def cached_slow(
    key: str,
    loader: Callable[[], Any],
    ttl: float,
) -> tuple[Any, bool, float | None]:
    """取一个慢接口结果。返回 ``(payload, stale, stamp)``。

    - 新鲜命中 -> ``(payload, False, stamp)``
    - 过期但内存/磁盘有旧值 -> ``(old, True, stamp)`` 并后台刷新
    - 完全无值 -> 同步拉取(会阻塞!)

    ``stale=True`` 时前端应提示"数据刷新中"; ``stamp=None`` 表示从未成功拉到。
    """
    now = time.time()
    with _CACHE_LOCK:
        hit = _INDEX.get(key)
    if hit is None:
        hit = _read_disk(key)
        if hit is not None:
            with _CACHE_LOCK:
                _INDEX[key] = hit

    if hit is not None:
        stamp, payload = hit
        if now - stamp < ttl:
            return payload, False, stamp
        # 过期: 先给旧值, 后台刷新
        if key not in _REFRESHING:
            _REFRESHING.add(key)

            def _run() -> None:
                try:
                    _refresh(key, loader)
                finally:
                    _REFRESHING.discard(key)

            threading.Thread(target=_run, name=f"intraday-{key}", daemon=True).start()
        return payload, True, stamp

    got = _refresh(key, loader)
    if got is None:
        return None, True, None
    return got[0], False, got[1]


def invalidate(key: str) -> None:
    """丢弃某个 key 的缓存(前端"强制刷新"用)。"""
    with _CACHE_LOCK:
        _INDEX.pop(key, None)
    try:
        _path(key).unlink(missing_ok=True)
    except Exception as e:
        logger.debug("盘中缓存 %s 删除失败: %s", key, e)
