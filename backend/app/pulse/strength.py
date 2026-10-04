"""M3 分时买卖力道: 逐分钟主买 / 主卖。

上游
====
``cli.helpers.buy_sell_strength(code)`` -> ``MinuteAuxSeries.points``, 240 点
(09:31 ~ 15:00), 实测 0.06s。每点字段::

    time_label: "09:31"
    series_a / buy_commission  : 该分钟主买量
    series_b / sell_commission : 该分钟主卖量

``series_a`` 与 ``buy_commission`` 实测数值相同(互为别名), 取后者语义最明确。
``cumulative_volume`` 系列字段在上游恒为 None, 不参与计算。

量纲
====
单位与日K volume 一致(**手**), 但**绝对值只占总成交的一部分**: 实测 600519
主买 5455 + 主卖 5262 = 10717 手, 而当日总成交 26366 手 —— 推测只统计主动成交
(被动挂单成交不计)。⇒ **只能做同标的内的相对强弱比较, 不要拿它去和总成交量换算占比**。
"""

from __future__ import annotations

import logging
from typing import Any

from app.plugins.eltdx.provider import app_to_eltdx
from app.pulse.gateway import client

logger = logging.getLogger(__name__)


def _f(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v


def fetch_strength(symbol: str) -> dict:
    """取单只标的当日逐分钟买卖力道。

    返回 ``{symbol, points:[{minute,buy,sell,delta,cum_delta}], summary}``。
    失败/无数据时 ``points`` 为空, 不抛异常(前端直接显示"暂无")。
    """
    code = app_to_eltdx(symbol)
    empty: dict[str, Any] = {"symbol": symbol, "points": [], "summary": None}
    if code is None:
        return empty
    try:
        series = client().helpers.buy_sell_strength(code)
    except Exception as e:
        logger.warning("eltdx 买卖力道失败 %s: %s: %s", symbol, type(e).__name__, e)
        return empty

    points: list[dict] = []
    cum = 0.0
    for p in getattr(series, "points", ()) or ():
        buy = _f(getattr(p, "buy_commission", None))
        sell = _f(getattr(p, "sell_commission", None))
        if buy is None:
            buy = _f(getattr(p, "series_a", None))
        if sell is None:
            sell = _f(getattr(p, "series_b", None))
        if buy is None and sell is None:
            continue
        buy = buy or 0.0
        sell = sell or 0.0
        cum += buy - sell
        points.append({
            "minute": str(getattr(p, "time_label", "") or ""),
            "buy": buy,
            "sell": sell,
            "delta": buy - sell,
            "cum_delta": cum,
        })

    if not points:
        return empty
    return {"symbol": symbol, "points": points, "summary": _summary(points)}


def _summary(points: list[dict]) -> dict:
    """力道汇总: 总量 / 净额 / 强弱比 / 最大单分钟冲击。"""
    buy = sum(p["buy"] for p in points)
    sell = sum(p["sell"] for p in points)
    net = buy - sell
    total = buy + sell
    # 强弱比: (买-卖)/(买+卖), 落在 -1(全卖) ~ +1(全买)。
    ratio = net / total if total else 0.0
    peak = max(points, key=lambda p: abs(p["delta"]))
    # 后半场净力道: 尾盘 60 分钟更能反映资金当日意图。
    tail = points[-60:]
    tail_net = sum(p["delta"] for p in tail)
    return {
        "buy_total": buy,
        "sell_total": sell,
        "net": net,
        "strength_ratio": ratio,
        "tail_net": tail_net,
        "peak_minute": peak["minute"],
        "peak_delta": peak["delta"],
        "minutes": len(points),
    }
