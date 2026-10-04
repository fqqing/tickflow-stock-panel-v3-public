"""M4 题材: 个股所属题材 + 全市场题材强度排行。

上游
====
``cli.helpers.stock_topics(code)`` -> ``StockTopics.topics``(**0.5s**, 单只)::

    topic_id / topic_name / relation_level(关联度, 越大越核心)
    selected_date / topic_date / reason(入选理由原文) / category_raw

``cli.helpers.theme_strength_rank(None, count=N)`` -> ``ThemeStrengthTable``
(**137s**, 必须走缓存)::

    trade_date, rows[{rank, topic_id, topic_name, limit_up_count,
    highest_ladder_level, lianban_count, total_seal_amount,
    leader_code, leader_ladder_level}]

⚠️ 上游伪题材
=============
实测排行前几名是 **"最近情绪指数" / "昨日涨停" / "昨日首板" / "昨日连板"** 这类
**统计标签而非真题材**(且前两名数值完全重复: 都是 52/7/12)。它们会污染榜单,
默认按名称前缀过滤掉(``_PSEUDO_PREFIXES``), 需要看全量时传 ``include_pseudo=True``。
"""

from __future__ import annotations

import logging
from typing import Any

from app.plugins.eltdx.provider import app_to_eltdx, eltdx_to_app, market_meta
from app.pulse.gateway import cached_slow, client, invalidate

logger = logging.getLogger(__name__)

#: 题材强度排行缓存 key。
_RANK_KEY = "theme_strength_rank"

#: 盘中 TTL(秒)。排行 137s 才能拉一次, 5 分钟缓存是合理折中。
_RANK_TTL = 300.0

#: 拉全量时请求的条数。上游按 count 截断, 给足即可。
_RANK_COUNT = 3000

#: 上游返回的"统计标签"(不是真概念板块)。
_PSEUDO_PREFIXES = ("昨日", "最近", "今日", "本周", "本月", "近")
_PSEUDO_EXACT = {"情绪指数", "强势股", "弱势股", "创富通", "通达信88"}


def _full_code_to_symbol(full: object) -> str | None:
    """``sh600825`` -> ``600825.SH``。上游榜单里的代码都是这种带前缀全码。"""
    text = str(full or "").strip().lower()
    if len(text) < 3:
        return None
    return eltdx_to_app(text[:2], text[2:])


def _is_pseudo(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return True
    if text in _PSEUDO_EXACT:
        return True
    return any(text.startswith(p) for p in _PSEUDO_PREFIXES)


def fetch_stock_topics(symbol: str) -> dict:
    """单只标的的题材列表(0.5s, 可实时调)。"""
    code = app_to_eltdx(symbol)
    if code is None:
        return {"symbol": symbol, "topics": []}
    try:
        data = client().helpers.stock_topics(code)
    except Exception as e:
        logger.warning("eltdx 个股题材失败 %s: %s: %s", symbol, type(e).__name__, e)
        return {"symbol": symbol, "topics": []}

    topics: list[dict] = []
    for t in getattr(data, "topics", ()) or ():
        topics.append({
            "topic_id": str(getattr(t, "topic_id", "") or ""),
            "topic_name": str(getattr(t, "topic_name", "") or ""),
            "relation_level": getattr(t, "relation_level", None),
            "selected_date": str(getattr(t, "selected_date", "") or ""),
            "reason": str(getattr(t, "reason", "") or ""),
        })
    # 关联度: 上游越小越核心(2 < 3 < 4), 但语义不严谨, 只做次要排序键。
    topics.sort(key=lambda t: (t["relation_level"] if t["relation_level"] is not None else 99))
    return {"symbol": symbol, "topics": topics}


def _load_rank() -> dict:
    table = client().helpers.theme_strength_rank(None, count=_RANK_COUNT)
    meta = market_meta()
    rows: list[dict] = []
    for r in getattr(table, "rows", ()) or ():
        symbol = _full_code_to_symbol(getattr(r, "leader_code", None))
        name = (meta.get(symbol) or {}).get("name") if symbol else None
        rows.append({
            "rank": getattr(r, "rank", None),
            "topic_id": str(getattr(r, "topic_id", "") or ""),
            "topic_name": str(getattr(r, "topic_name", "") or ""),
            "limit_up_count": getattr(r, "limit_up_count", None),
            "highest_ladder_level": getattr(r, "highest_ladder_level", None),
            "lianban_count": getattr(r, "lianban_count", None),
            "total_seal_amount": getattr(r, "total_seal_amount", None),
            "leader_symbol": symbol,
            "leader_name": name,
            "leader_ladder_level": getattr(r, "leader_ladder_level", None),
        })
    return {"trade_date": str(getattr(table, "trade_date", "") or ""), "rows": rows}


def fetch_topic_rank(include_pseudo: bool = False, force: bool = False) -> dict:
    """题材强度排行。**命中过期缓存会立刻返回旧值并后台刷新**。"""
    if force:
        invalidate(_RANK_KEY)
    payload: Any
    payload, stale, stamp = cached_slow(_RANK_KEY, _load_rank, _RANK_TTL)
    if payload is None:
        return {"trade_date": None, "rows": [], "stale": True, "stamp": None}
    rows = [r for r in payload["rows"]
            if include_pseudo or not _is_pseudo(r["topic_name"])]
    return {
        "trade_date": payload.get("trade_date"),
        "rows": rows,
        "stale": stale,
        "stamp": stamp,
        "filtered_pseudo": (not include_pseudo),
    }
