"""M5 涨停梯队 / 连板: 全市场涨停股快照(实为短线指标全集)。

上游
====
``cli.helpers.limit_ladder(None, count=N)`` -> ``LimitLadderTable``
(**113~126s**, 必须走缓存): ``trade_date`` + ``rows[ShortlineIndicator]``。

这接口名字叫"涨停梯队", 实际把短线能用的一组指标一次给全了:

- 梯队: ``ladder_level`` / ``limit_board_text``("7天7板") / ``limit_up_streak_days``
- 封板: ``limit_status``("sealed") / ``seal_amount`` / ``seal_to_amount_ratio``
  / ``seal_to_float_ratio`` / ``seal_prev_ratio``
- 开盘: ``open_price`` / ``pre_close`` / ``open_change_pct`` / ``open_amount``
  / ``open_volume_hand`` / ``open_volume_ratio`` / ``opening_rush``
- 估值/弹性: ``pe_ttm`` / ``beta_60d`` / ``float_market_value`` / ``free_float_market_value``
- 历史: ``year_limit_up_days`` / ``limit_up_count_in_stat_days`` / ``limit_stat_days``

量纲
====
``open_change_pct`` 是**百分数**(9.9894 表示 9.9894%), 契约要小数 => /100。
金额一律元, 股本单位一律股, 成交量以 ``_hand`` 结尾即手。
"""

from __future__ import annotations

import logging
from typing import Any

from app.plugins.eltdx.provider import eltdx_to_app, market_meta
from app.pulse.gateway import cached_slow, client, invalidate

logger = logging.getLogger(__name__)

_LADDER_KEY = "limit_ladder"
_LADDER_TTL = 300.0
_LADDER_COUNT = 5000


def _f(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v


def _load() -> dict:
    table = client().helpers.limit_ladder(None, count=_LADDER_COUNT)
    meta = market_meta()
    rows: list[dict] = []
    for r in getattr(table, "rows", ()) or ():
        symbol = eltdx_to_app(getattr(r, "exchange", ""), getattr(r, "code", ""))
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "name": (meta.get(symbol) or {}).get("name"),
            "limit_status": str(getattr(r, "limit_status", "") or ""),
            "ladder_level": getattr(r, "ladder_level", None),
            "limit_board_text": str(getattr(r, "limit_board_text", "") or ""),
            "limit_up_streak_days": getattr(r, "limit_up_streak_days", None),
            "year_limit_up_days": getattr(r, "year_limit_up_days", None),
            "open_price": _f(getattr(r, "open_price", None)),
            "pre_close": _f(getattr(r, "pre_close", None)),
            # 百分数 -> 小数
            "open_change_pct": (_f(getattr(r, "open_change_pct", None)) or 0.0) / 100.0,
            "open_amount": _f(getattr(r, "open_amount", None)),
            "open_volume_hand": _f(getattr(r, "open_volume_hand", None)),
            "open_volume_ratio": _f(getattr(r, "open_volume_ratio", None)),
            "opening_rush": _f(getattr(r, "opening_rush", None)),
            "seal_amount": _f(getattr(r, "seal_amount", None)),
            "seal_to_amount_ratio": _f(getattr(r, "seal_to_amount_ratio", None)),
            "seal_to_float_ratio": _f(getattr(r, "seal_to_float_ratio", None)),
            "seal_prev_ratio": _f(getattr(r, "seal_prev_ratio", None)),
            "pe_ttm": _f(getattr(r, "pe_ttm", None)),
            "beta_60d": _f(getattr(r, "beta_60d", None)),
            "float_market_value": _f(getattr(r, "float_market_value", None)),
            "free_float_market_value": _f(getattr(r, "free_float_market_value", None)),
        })
    return {"trade_date": str(getattr(table, "trade_date", "") or ""), "rows": rows}


def fetch_ladder(
    min_level: int = 1,
    only_sealed: bool = False,
    limit: int = 200,
    force: bool = False,
) -> dict:
    """涨停梯队。**命中过期缓存会立刻返回旧值并后台刷新**。

    ``min_level`` 过滤连板高度(1=首板及以上); ``only_sealed`` 只要当前封住的。
    """
    if force:
        invalidate(_LADDER_KEY)
    payload: Any
    payload, stale, stamp = cached_slow(_LADDER_KEY, _load, _LADDER_TTL)
    if payload is None:
        return {"trade_date": None, "rows": [], "stale": True, "stamp": None}

    rows = payload["rows"]
    if only_sealed:
        rows = [r for r in rows if r["limit_status"] == "sealed"]
    if min_level > 1:
        rows = [r for r in rows if (r["ladder_level"] or 0) >= min_level]
    # 先按连板高度, 再按封单额 —— 高标 + 封得死的是盘眼。
    rows.sort(key=lambda r: ((r["ladder_level"] or 0), r["seal_amount"] or 0.0), reverse=True)
    return {
        "trade_date": payload.get("trade_date"),
        "rows": rows[: max(1, limit)],
        "total": len(payload["rows"]),
        "stale": stale,
        "stamp": stamp,
    }


def ladder_stats(rows: list[dict]) -> dict:
    """梯队分布: 各层级家数 + 封单总额。做情绪温度计用。"""
    by_level: dict[int, int] = {}
    sealed = 0
    seal_total = 0.0
    for r in rows:
        level = int(r["ladder_level"] or 0)
        by_level[level] = by_level.get(level, 0) + 1
        if r["limit_status"] == "sealed":
            sealed += 1
        seal_total += float(r["seal_amount"] or 0.0)
    return {
        "count": len(rows),
        "sealed": sealed,
        "seal_total_amount": seal_total,
        "max_level": max(by_level) if by_level else 0,
        "by_level": dict(sorted(by_level.items())),
    }
