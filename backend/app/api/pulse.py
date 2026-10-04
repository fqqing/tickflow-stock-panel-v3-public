"""盘中脉搏 API(M1~M6): 资金流 / 竞价 / 买卖力道 / 题材 / 涨停梯队 / 逐笔订单流。

全部能力来自 eltdx(通达信 7709)。**慢接口(题材排行 137s / 涨停梯队 113s)一律走
TTL 缓存**, 命中过期会立刻返回旧值并标 ``stale=True``, 后端后台刷新 —— 用户
永远不会为了看榜单干等两分钟。

统一量纲: 金额=元, 比率=小数(0.0968 即 9.68%), 成交量=手。

⚠️ 前缀为什么叫 pulse 而不是 intraday: ``/api/intraday`` 已经被引擎的行情状态 /
SSE 推送占用(``app/api/intraday.py``), 两者不能撞。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.plugins.eltdx.provider import market_meta
from app.pulse import auction, ladder, moneyflow, strength, tick, topic

router = APIRouter(prefix="/api/pulse", tags=["pulse"])

#: 全市场批量接口的扫描上限(7251 只全扫约 3s, 留足余量)。
_MAX_RANK_SYMBOLS = 8000


def _split(text: str | None) -> list[str]:
    return [s.strip() for s in (text or "").split(",") if s.strip()]


def _market_symbols(limit: int) -> list[str]:
    """本地维表里的 A 股 + ETF(**不含指数**), 用于全市场排行。"""
    syms = sorted(s for s, m in market_meta().items() if not m.get("is_index"))
    return syms[: max(1, min(limit, _MAX_RANK_SYMBOLS))]


def _require(symbol: str | None) -> str:
    sym = (symbol or "").strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol 不能为空")
    return sym


# ---- M1 资金流 ----


@router.get("/moneyflow")
def get_moneyflow(
    symbols: str = Query(..., description="逗号分隔, 如 600519.SH,000001.SZ"),
    days: int = Query(5, ge=1, le=60, description="每只保留最近几个交易日的记录"),
):
    """多只标的最近 N 日资金流(原始逐日记录)。"""
    syms = _split(symbols)
    if not syms:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    if len(syms) > 500:
        raise HTTPException(status_code=400, detail="单次最多 500 只")
    return {"symbols": syms, "days": days, "rows": moneyflow.fetch_moneyflow(syms, days=days)}


@router.get("/moneyflow/rank")
def get_moneyflow_rank(
    limit: int = Query(50, ge=1, le=2000, description="返回前 N 名"),
    days: int = Query(1, ge=1, le=20),
    scan: int = Query(2000, ge=10, le=_MAX_RANK_SYMBOLS, description="扫描多少只标的"),
    ascending: bool = Query(False, description="True=净流出榜"),
):
    """全市场主力净额排行。扫 2000 只约 1s 级, 全市场 7251 只约 3s。"""
    syms = _market_symbols(scan)
    rows = moneyflow.fetch_moneyflow(syms, days=days)
    out = moneyflow.summarize(rows)["rows"]
    out.sort(key=lambda r: (r["main_net"] or 0.0), reverse=not ascending)
    return {"scanned": len(syms), "days": days, "rows": out[:limit]}


# ---- M2 集合竞价 ----


@router.get("/auction")
def get_auction(symbol: str = Query(...)):
    """单只标的当日集合竞价序列 + 开盘量额 + 强度评分。"""
    return auction.fetch_auction(_require(symbol))


@router.get("/auction/scan")
def get_auction_scan(
    scan: int = Query(500, ge=10, le=_MAX_RANK_SYMBOLS, description="扫描多少只标的"),
    mode: str = Query("score", description="score=竞价强度榜 / repair=低开走强榜"),
    limit: int = Query(100, ge=1, le=2000),
    ascending: bool = Query(False),
    with_snapshot: bool = Query(True, description="是否合并实时快照(低开走强榜需要)"),
    min_open_pct: float | None = Query(None, description="开盘涨幅下限(小数, -0.03 即 -3%)"),
    max_open_pct: float | None = Query(None, description="开盘涨幅上限(小数)"),
):
    """全市场竞价扫描。

    - ``mode=score`` 竞价强度榜: 按自建评分排序(加速/撤单/稳定/量能)。
    - ``mode=repair`` 低开走强榜: **只保留竞价低开的票**, 按日内修复幅度
      (当前涨幅 - 开盘涨幅)排序 —— 这就是「低开后有没有走强/冲板」。

    ⚠️ 上游竞价序列只保留最近一个交易日, 非交易日取到的是上一交易日的数据。
    扫 500 只约 8s, 5000 只约 74s(8 并发实测)。
    """
    if mode not in ("score", "repair"):
        raise HTTPException(status_code=400, detail="mode 只能是 score 或 repair")
    syms = _market_symbols(scan)
    data = auction.fetch_auction_scan(syms, with_snapshot=with_snapshot)
    data["mode"] = mode
    data["rows"] = auction.rank_auction(
        data["rows"],
        mode=mode,
        ascending=ascending,
        limit=limit,
        min_open_pct=min_open_pct,
        max_open_pct=max_open_pct,
    )
    return data


# ---- M3 分时买卖力道 ----


@router.get("/strength")
def get_strength(symbol: str = Query(...)):
    """单只标的当日逐分钟主买/主卖力道。"""
    return strength.fetch_strength(_require(symbol))


# ---- M4 题材 ----


@router.get("/topics")
def get_stock_topics(symbol: str = Query(...)):
    """个股所属题材(0.5s, 可实时调)。"""
    return topic.fetch_stock_topics(_require(symbol))


@router.get("/topics/rank")
def get_topic_rank(
    include_pseudo: bool = Query(False, description="是否保留「昨日涨停」这类统计标签"),
    force: bool = Query(False, description="忽略缓存强制重拉(会阻塞约 137s)"),
):
    """全市场题材强度排行。首次调用会阻塞(137s), 之后走缓存。"""
    return topic.fetch_topic_rank(include_pseudo=include_pseudo, force=force)


# ---- M5 涨停梯队 ----


@router.get("/ladder")
def get_ladder(
    min_level: int = Query(1, ge=1, le=20, description="最低连板高度, 1=含首板"),
    only_sealed: bool = Query(False),
    limit: int = Query(200, ge=1, le=5000),
    force: bool = Query(False, description="忽略缓存强制重拉(会阻塞约 113s)"),
):
    """涨停梯队。首次调用会阻塞(113s), 之后走缓存。"""
    data = ladder.fetch_ladder(
        min_level=min_level, only_sealed=only_sealed, limit=limit, force=force,
    )
    data["stats"] = ladder.ladder_stats(data["rows"])
    return data


# ---- M6 逐笔 / 订单流 / 足迹图 ----


@router.get("/ticks")
def get_ticks(
    symbol: str = Query(...),
    day: Annotated[str | None, Query(description="YYYY-MM-DD, 默认今天")] = None,
    force: bool = Query(False, description="忽略当日落盘缓存重抓"),
    limit: int = Query(2000, ge=1, le=100000, description="最多返回多少笔(取最新)"),
):
    """逐笔明细。抓过一次当天就走本地 parquet(秒开)。"""
    sym = _require(symbol)
    df, day_key = tick.fetch_ticks_meta(sym, day=day, force=force)
    if df.is_empty():
        return {"symbol": sym, "day": str(day_key), "rows": [], "total": 0, "updated": None}
    return {
        "symbol": sym,
        # 实际使用的交易日(跨午夜时会自动回退到最近一个有数据的交易日)。
        "day": str(day_key),
        "total": df.height,
        "rows": df.tail(limit).to_dicts(),
        "updated": tick.last_updated(sym, day_key),
    }


@router.get("/orderflow")
def get_orderflow(
    symbol: str = Query(...),
    day: Annotated[str | None, Query(description="YYYY-MM-DD, 默认今天")] = None,
    force: bool = Query(False),
):
    """订单流: 按分钟的 Delta / CumDelta / 主动买卖量。"""
    sym = _require(symbol)
    df, day_key = tick.fetch_ticks_meta(sym, day=day, force=force)
    data = tick.orderflow(df)
    data["symbol"] = sym
    data["day"] = str(day_key)
    return data


@router.get("/footprint")
def get_footprint(
    symbol: str = Query(...),
    day: Annotated[str | None, Query(description="YYYY-MM-DD, 默认今天")] = None,
    rows: int = Query(40, ge=5, le=200, description="价格分档数"),
    bucket: int = Query(5, ge=1, le=60, description="时间桶宽度(分钟)"),
    force: bool = Query(False),
):
    """足迹图矩阵: 价格 x 时间的买卖量网格 + POC + VWAP。"""
    sym = _require(symbol)
    df, day_key = tick.fetch_ticks_meta(sym, day=day, force=force)
    data = tick.footprint(df, rows=rows, bucket_minutes=bucket)
    data["symbol"] = sym
    data["day"] = str(day_key)
    return data


@router.get("/price-dist")
def get_price_dist(
    symbol: str = Query(...),
    day: Annotated[str | None, Query(description="YYYY-MM-DD, 默认今天")] = None,
    rows: int = Query(60, ge=5, le=200, description="最多多少个价格档(步长取 tick 整数倍)"),
    force: bool = Query(False),
):
    """分价表: 按价格档聚合成交量 / 成交额 / 笔数 / 主动买卖 + 占比与 POC。"""
    sym = _require(symbol)
    df, day_key = tick.fetch_ticks_meta(sym, day=day, force=force)
    data = tick.price_distribution(df, max_rows=rows)
    data["symbol"] = sym
    data["day"] = str(day_key)
    data["updated"] = tick.last_updated(sym, day_key)
    return data


@router.get("/tick-days")
def get_tick_days():
    """已落盘逐笔的交易日列表(最新在前)。"""
    return {"days": tick.cached_days()}
