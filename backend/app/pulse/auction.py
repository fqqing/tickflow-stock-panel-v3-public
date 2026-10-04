"""M2 集合竞价: 09:15~09:25 匹配序列 + 开盘量额 + 强度评分。

上游
====
``cli.auctions.series(code)``  -> ``AuctionSeries.points``, 118 点(约每 5 秒一点)::

    time_label      : "09:15:01"
    price           : 当前撮合价(元)
    matched_volume  : **累计**已匹配量(手)
    unmatched_volume: 当前未匹配量(手), 即挂着没成交的单
    unmatched_direction_raw: 未匹配方向

``cli.helpers.auction_data(code)`` -> ``AuctionData``(本次实测 1.33s, 含一次 series 调用)::

    trading_date / pre_close_price / open_price / open_volume / open_amount
    open_change_pct : **百分数**(0.3197 表示 0.3197%), 契约要小数 => /100
    snapshot_0925   : 09:25 的撮合快照

强度评分口径(自建, 非上游提供)
==============================
竞价是"真金白银"最集中的十分钟, 评分看四件事:

1. **加速** accel: 后 1/3 时段的匹配量增量 / 前 1/3 增量。>1 说明越到后面越有人抢。
2. **撤单** cancel_rate: 未匹配量从峰值回落的比例, **只在 09:20 之前统计**。
   09:20 之后规则上不允许撤单, 末端未匹配量必然被撮合掉(实测 600519 末端只剩 1 手),
   把全时段算进去会得到 98% 这种"人人都在撤单"的荒谬结论。
3. **价格稳定** stability: 撮合价相对前收的极差占比, 越小越稳。
4. **开盘量能** open_turnover_bp: 开盘成交额 / 流通市值(基点)。衡量竞价资金体量。

综合分 0~100 = 加速 30 + (1-撤单) 25 + 稳定 20 + 量能 25。分项原样返回,
权重可以后面调 —— **先给分项再加总, 别只给一个数**(用户能自己判断权重是否合理)。

全市场竞价扫描 (2026-10-01 新增)
================================
``fetch_auction`` 只能一只一只查, 但用户想看的是「今天全市场竞价谁最强 / 谁低开
后走强」。实测(见 scripts/probe_auction_scan.py): 8 并发下单只中位 0.12s,
5000 只约 74s —— 与资金流排行同量级, 完全可做全市场。

⚠️ 竞价数据的时效性:
    上游只保留**最近一个交易日**的竞价序列(实测 10-01 取到的 trading_date 是
    09-30)。隔天即被覆盖 => 历史样本不会自动积累, 要建模必须每天盘后主动落盘。

低开 -> 走强为什么必须合并快照:
    竞价数据只给**开盘**价, 不知道盘中走到哪。所以「低开后有没有修复 / 有没有
    冲板」只能靠实时快照补: repair_pct = 当前涨幅 - 开盘涨幅。两者口径不同源,
    开盘涨幅取竞价(权威), 当前涨幅取快照(实时)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from app.plugins.eltdx.provider import _snap_fetch, app_to_eltdx, market_meta
from app.pulse.gateway import client

logger = logging.getLogger(__name__)

#: 扫描并发数。实测 8 并发稳定; 再高上游开始 sporadic ProtocolError。
_SCAN_WORKERS = 8

#: 涨停判定余量(涨幅小数)。快照 change_pct 有精度损失, 实际涨停是 10/20/30,
#: 判定时留 0.2% 余量更稳 (9.8% / 19.8% / 29.8%)。
_LIMIT_UP_TOLERANCE = 0.002


@lru_cache(maxsize=8192)
def _limit_up_threshold(symbol: str) -> float:
    """涨停判定阈值(涨幅小数)。

    板块口径**不在这里维护** —— 走 ``app.markets.market_limit_pct``, 它是全仓库
    涨跌停规则的单一事实源 (指标流水线 / 回测 / API 同源)。此前这里的平行实现
    用 ("688","689","300","301","43","83","87","88","920") 前缀硬匹配, 与
    price_limits 的 (300,301,688,689) 前缀 + .BJ 后缀口径不同源, 改规则时容易漏改。

    港美股无涨跌停 (market_limit_pct 返回 None) → 返回 +inf, 判定恒 False,
    而不是退回 A 股主板的 10%。
    """
    from app.markets import market_limit_pct

    pct = market_limit_pct(symbol)
    if pct is None:
        return math.inf
    return round(pct / 100.0 - _LIMIT_UP_TOLERANCE, 4)


def _f(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v


def _float_shares(symbol: str) -> float | None:
    """流通股本(股)。取不到返回 None(量能分项会退化成 0 而不是报错)。"""
    try:
        from app.plugins.eltdx.provider import market_meta
    except Exception:  # pragma: no cover - 维表不可用时仅降级
        return None
    meta = market_meta().get(symbol) or {}
    return _f(meta.get("float_shares"))


def fetch_auction(symbol: str) -> dict:
    """取单只标的当日集合竞价序列与强度评分。"""
    code = app_to_eltdx(symbol)
    empty: dict[str, Any] = {"symbol": symbol, "points": [], "summary": None, "score": None}
    if code is None:
        return empty
    try:
        data = client().helpers.auction_data(code)
    except Exception as e:
        logger.warning("eltdx 竞价数据失败 %s: %s: %s", symbol, type(e).__name__, e)
        return empty

    series = getattr(data, "series", None)
    points: list[dict] = []
    for p in getattr(series, "points", ()) or ():
        points.append({
            "time": str(getattr(p, "time_label", "") or ""),
            "price": _f(getattr(p, "price", None)),
            "matched": _f(getattr(p, "matched_volume", None)),
            "unmatched": _f(getattr(p, "unmatched_volume", None)),
            "unmatched_dir": getattr(p, "unmatched_direction_raw", None),
        })

    pre_close = _f(getattr(data, "pre_close_price", None))
    open_price = _f(getattr(data, "open_price", None))
    open_volume = _f(getattr(data, "open_volume", None))
    open_amount = _f(getattr(data, "open_amount", None))
    change_pct = _f(getattr(data, "open_change_pct", None))
    return {
        "symbol": symbol,
        "date": str(getattr(data, "trading_date", "") or ""),
        "pre_close": pre_close,
        "open_price": open_price,
        "open_volume": open_volume,
        "open_amount": open_amount,
        # 上游是百分数(0.3197), 契约要小数。
        "open_change_pct": (change_pct or 0.0) / 100.0,
        "points": points,
        "score": _score(points, pre_close, open_price, open_amount, _float_shares(symbol)),
    }


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _score(
    points: list[dict],
    pre_close: float | None,
    open_price: float | None,
    open_amount: float | None,
    float_shares: float | None,
) -> dict | None:
    """竞价强度评分。points 太少(<6)时返回 None(数据不足以判断)。"""
    if len(points) < 6:
        return None

    matched = [p["matched"] for p in points if p["matched"] is not None]
    prices = [p["price"] for p in points if p["price"] is not None]

    # 1) 加速: 后 1/3 增量 / 前 1/3 增量。
    accel = None
    if len(matched) >= 6:
        third = max(1, len(matched) // 3)
        head = matched[third] - matched[0]
        tail = matched[-1] - matched[-third]
        accel = (tail / head) if head > 0 else (2.0 if tail > 0 else 0.0)

    # 2) 撤单: 只统计**可撤单期**(09:20 之前)的未匹配量回落。
    #    09:20 后不允许撤单, 末端归零是撮合完成的必然结果, 不是撤单。
    cancel_rate = None
    revocable = [p["unmatched"] for p in points
                 if p["unmatched"] is not None and str(p["time"] or "") < "09:20"]
    if len(revocable) >= 6:
        peak = max(revocable)
        end = revocable[-1]
        cancel_rate = (peak - end) / peak if peak > 0 else 0.0

    # 3) 价格稳定: 撮合价极差 / 前收。
    stability = None
    if len(prices) >= 6 and pre_close:
        spread = (max(prices) - min(prices)) / pre_close
        stability = 1.0 - _clamp01(spread / 0.05)  # 5% 以上极差视为完全不稳

    # 4) 开盘量能: 竞价成交额 / 流通市值(基点)。实测 600519 是 0.13bp,
    #    活跃小票能到 1~2bp, 故满分线定在 2bp(30bp 会让所有票都拿 0 分)。
    turnover_bp = None
    if open_amount is not None and float_shares and pre_close:
        turnover_bp = open_amount / (pre_close * float_shares) * 10_000.0

    def part(value: float | None, full: float) -> float:
        return _clamp01(value / full) if value is not None else 0.0

    s_accel = part(accel, 2.0) * 30.0
    s_cancel = (1.0 - _clamp01(cancel_rate or 0.0)) * 25.0
    s_stable = (stability or 0.0) * 20.0
    s_volume = part(turnover_bp, 2.0) * 25.0  # 2bp 视为满分量能

    return {
        "total": round(s_accel + s_cancel + s_stable + s_volume, 1),
        "accel": accel,
        "cancel_rate": cancel_rate,
        "stability": stability,
        "open_turnover_bp": turnover_bp,
        "parts": {
            "accel": round(s_accel, 1),
            "cancel": round(s_cancel, 1),
            "stability": round(s_stable, 1),
            "volume": round(s_volume, 1),
        },
    }


# ---------------------------------------------------------------------------
# 全市场竞价扫描
# ---------------------------------------------------------------------------


def _scan_one(symbol: str) -> dict | None:
    """拉单只竞价并压成一行。失败 / 点数不足返回 None(不阻断整批)。"""
    code = app_to_eltdx(symbol)
    if code is None:
        return None
    try:
        data = client().helpers.auction_data(code)
    except Exception as e:  # 单只失败只丢这一只, 不能连累整批
        logger.debug("eltdx 竞价扫描跳过 %s: %s: %s", symbol, type(e).__name__, e)
        return None

    series = getattr(data, "series", None)
    points: list[dict] = []
    for p in getattr(series, "points", ()) or ():
        points.append({
            "time": str(getattr(p, "time_label", "") or ""),
            "price": _f(getattr(p, "price", None)),
            "matched": _f(getattr(p, "matched_volume", None)),
            "unmatched": _f(getattr(p, "unmatched_volume", None)),
        })

    pre_close = _f(getattr(data, "pre_close_price", None))
    open_price = _f(getattr(data, "open_price", None))
    open_amount = _f(getattr(data, "open_amount", None))
    change_pct = _f(getattr(data, "open_change_pct", None))
    # 评分点数不足时 score=None, 该行仍保留(开盘涨幅等指标本身有用), 只是不参与强度榜
    score = _score(points, pre_close, open_price, open_amount, _float_shares(symbol))
    return {
        "symbol": symbol,
        "date": str(getattr(data, "trading_date", "") or ""),
        "pre_close": pre_close,
        "open_price": open_price,
        "open_change_pct": (change_pct or 0.0) / 100.0,
        "open_volume": _f(getattr(data, "open_volume", None)),
        "open_amount": open_amount,
        "n_points": len(points),
        "score": score["total"] if score else None,
        "accel": score["accel"] if score else None,
        "cancel_rate": score["cancel_rate"] if score else None,
        "stability": score["stability"] if score else None,
        "open_turnover_bp": score["open_turnover_bp"] if score else None,
    }


def fetch_auction_scan(
    symbols: list[str],
    *,
    with_snapshot: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """全市场竞价扫描: 并发拉竞价 + 可选合并实时快照。

    - ``score`` 为 None 的行 = 竞价序列点数不足(<6), 开盘涨幅等指标仍有效,
      但不参与「竞价强度」排序(排在最后)。
    - ``repair_pct`` = 当前涨幅 - 开盘涨幅, 衡量**低开后的日内修复幅度**。
      开盘涨幅取竞价(权威), 当前涨幅取快照(实时), 两者不同源。
    """
    pairs = [(s, app_to_eltdx(s)) for s in symbols or ()]
    todo = [s for s, c in pairs if c]
    if not todo:
        return {"date": "", "scanned": 0, "scored": 0, "rows": [], "snapshot_ts": None}

    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
        for row in pool.map(_scan_one, todo):
            done += 1
            if row:
                rows.append(row)
            if on_progress:
                on_progress(done, len(todo))

    # 名称 + 快照(合并后才知道低开有没有修复/冲板)
    meta = market_meta()
    for r in rows:
        r["name"] = (meta.get(r["symbol"]) or {}).get("name")

    snapshot_ts = None
    if with_snapshot and rows:
        try:
            snap_rows = _snap_fetch([r["symbol"] for r in rows])
        except Exception as e:  # 快照失败不阻断: 竞价榜本身仍可用
            logger.warning("竞价扫描合并快照失败: %s: %s", type(e).__name__, e)
            snap_rows = []
        by_symbol = {s["symbol"]: s for s in snap_rows}
        stamps = [s["timestamp"] for s in snap_rows if s.get("timestamp")]
        snapshot_ts = max(stamps) if stamps else None
        for r in rows:
            s = by_symbol.get(r["symbol"])
            if not s:
                continue
            cur = s.get("change_pct") or 0.0
            r["last_price"] = s.get("last_price")
            r["change_pct"] = cur
            r["repair_pct"] = cur - (r["open_change_pct"] or 0.0)
            r["is_limit_up"] = cur >= _limit_up_threshold(r["symbol"])

    scored = sum(1 for r in rows if r["score"] is not None)
    date = max((r.get("date") or "" for r in rows), default="")
    return {
        "date": date,
        "scanned": len(todo),
        "scored": scored,
        "rows": rows,
        "snapshot_ts": snapshot_ts,
    }


def rank_auction(
    rows: list[dict],
    mode: str = "score",
    *,
    ascending: bool = False,
    limit: int = 100,
    min_open_pct: float | None = None,
    max_open_pct: float | None = None,
) -> list[dict]:
    """按模式排序并筛选。

    - ``score``: 竞价强度榜(score 高的在前; None 永远垫底)。
    - ``repair``: 低开走强榜(repair_pct 大的在前) —— **自动只留竞价低开的行**
      (open_change_pct < 0), 否则"高开高走"会混进来盖住真正的低开修复。
    - ``min_open_pct`` / ``max_open_pct``: 开盘涨幅区间(小数), 用于人工圈定
      "小幅低开""深跌低开"这类范围。
    """
    out = rows
    if min_open_pct is not None:
        out = [r for r in out if (r.get("open_change_pct") or 0.0) >= min_open_pct]
    if max_open_pct is not None:
        out = [r for r in out if (r.get("open_change_pct") or 0.0) <= max_open_pct]

    if mode == "repair":
        # 低开走强: 只保留竞价低开的行(高开高走修复幅度再大也不算)
        low = [r for r in out if (r.get("open_change_pct") or 0.0) < 0]
        low.sort(key=lambda r: r.get("repair_pct") or 0.0, reverse=not ascending)
        return low[:limit]

    # 强度榜: score=None(竞价点数不足)必须**始终垫底**。
    # ⚠️ 不能用 (分组标记, 分数) 单键 + reverse=True —— 降序时分组标记 1 会被
    # 翻到最前面, 变成"无评分的排在榜首"。显式拆两段拼接才不会随升降序漂移。
    scored = [r for r in out if r.get("score") is not None]
    unscored = [r for r in out if r.get("score") is None]
    scored.sort(key=lambda r: r["score"], reverse=not ascending)
    return (scored + unscored)[:limit]
