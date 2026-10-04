"""M6 逐笔成交 / 订单流 / 足迹图。

上游
====
``cli.trades.today(code, start=0, count=1800)`` -> ``TradePage.ticks``(实测 0.05s)::

    index / absolute_index / time_minutes / time_label("13:23")
    price(元) / volume(手) / order_count(该笔拆成的笔数)
    side: "buy" | "sell" | "neutral"(竞价撮合)
    status_raw / event_kind("trade" | "opening_match")

⚠️ ``start`` 是**从最新往回数的偏移**, 不是从开盘往后: 实测 start=0 给
13:23~15:29, start=2000 给 09:48~13:13。且偏移量过大时会抛
``ProtocolError(record count 64672 exceeds payload capacity)``。
⇒ 分页只能 ``start += len(ticks)`` 逐页往前翻, **遇到异常就停**(保留已抓到的)。

落盘
====
``<data_dir>/ticks/<YYYY-MM-DD>/<symbol>.parquet``。逐笔是**当日**数据(源端不留存
历史), 故按交易日分目录; 抓过一次当天就不再打网络(盘中重复看图要秒开)。
重跑同一天同一只默认走缓存, ``force=True`` 强制重抓。

量纲: price=元 / volume=手 / amount 自行按 price x volume x 100 推导。
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_today
from app.plugins.eltdx.provider import app_to_eltdx
from app.pulse.gateway import client

logger = logging.getLogger(__name__)

_TICK_COLUMNS = ["index", "time", "price", "volume", "order_count", "side", "kind"]

#: 单页条数。上游默认 1800, 也是实测稳定的值。
_PAGE = 1800

#: 最多翻多少页(1800 x 40 = 7.2 万笔, 覆盖最活跃的票也够)。
_MAX_PAGES = 40


def tick_dir() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "ticks"


def resolve_day(preferred: Any = None) -> Any:
    """未指定日期时的默认日 = **最近一个交易日**。

    为什么不能用 ``cn_today()``: 源端只保留最近一个交易日的逐笔, 而本机日期到
    午夜就翻页(实测 09-30 收盘后凌晨 ``cn_today()`` = 10-01, 上游还停在 09-30),
    直接查会拿空并落一个错误的日期分区。

    ⚠️ 也不要"优先用已落盘的最新日": 盘中首次查新票时缓存里还是昨天, 会把今天
    误判成昨天。正确信号是 **eltdx 自带的交易日历** ``cli.workdays`` —— 它知道
    节假日(2026-10-01 国庆实测 ``previous_workday()`` 正确给出 09-30)。
    """
    if preferred:
        return preferred
    try:
        wd = client().workdays
        if wd.today_is_workday():
            return wd.today()
        return wd.previous_workday() or cn_today()
    except Exception as e:
        logger.debug("eltdx 交易日历不可用, 退回本机日期: %s: %s", type(e).__name__, e)
        return cn_today()


def _path(symbol: str, day: Any) -> Path:
    return tick_dir() / str(day) / f"{symbol}.parquet"


def _f(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v


def _fetch_pages(code: str) -> list[dict]:
    """逐页往前翻, 直到返回不足一页 / 报错 / 到页数上限。"""
    rows: list[dict] = []
    start = 0
    for _ in range(_MAX_PAGES):
        try:
            page = client().trades.today(code, start=start, count=_PAGE)
        except Exception as e:
            # 偏移量过大是上游常态(见模块 docstring), 不算错误, 静默收尾。
            logger.debug("eltdx 逐笔 %s start=%d 停止: %s: %s", code, start, type(e).__name__, e)
            break
        ticks = list(getattr(page, "ticks", ()) or ())
        if not ticks:
            break
        for t in ticks:
            price = _f(getattr(t, "price", None))
            volume = _f(getattr(t, "volume", None))
            if price is None or volume is None:
                continue
            rows.append({
                "index": getattr(t, "absolute_index", None) or getattr(t, "index", None),
                "time": str(getattr(t, "time_label", "") or ""),
                "price": price,
                "volume": volume,
                "order_count": getattr(t, "order_count", None),
                "side": str(getattr(t, "side", "") or ""),
                "kind": str(getattr(t, "event_kind", "") or ""),
            })
        if len(ticks) < _PAGE:
            break
        start += len(ticks)
    return rows


def fetch_ticks(symbol: str, day: Any = None, force: bool = False) -> pl.DataFrame:
    """取某只标的当日逐笔。落盘缓存, 重复调用不打网络(除非 force)。"""
    return fetch_ticks_meta(symbol, day=day, force=force)[0]


def fetch_ticks_meta(symbol: str, day: Any = None, force: bool = False) -> tuple[pl.DataFrame, Any]:
    """同 ``fetch_ticks``, 但连**实际使用的交易日**一并返回。

    API 需要它: 跨午夜时请求的是"今天", 实际拿到的是最近一个交易日, 回包里
    必须告诉前端真实日期, 否则前端会把 09-30 的数据标成 10-01。
    """
    code = app_to_eltdx(symbol)
    empty = pl.DataFrame(schema={
        "index": pl.Int64, "time": pl.Utf8, "price": pl.Float64, "volume": pl.Float64,
        "order_count": pl.Int64, "side": pl.Utf8, "kind": pl.Utf8,
    })
    if code is None:
        return empty, day or cn_today()

    day = resolve_day(day)
    path = _path(symbol, day)
    if path.exists() and not force:
        try:
            return pl.read_parquet(path), day
        except Exception as e:
            logger.warning("逐笔缓存 %s 读取失败, 重新抓取: %s", path, e)

    t0 = time.perf_counter()
    rows = _fetch_pages(code)
    if not rows and str(day) == str(cn_today()):
        # 跨午夜 / 假期: 本机日期已翻页, 但上游还停在最近一个交易日
        # (实测 09-30 收盘后凌晨查 10-01 拿到 0 笔)。回退到已落盘的最新日。
        fallback = cached_days()[0] if cached_days() else None
        if fallback and str(fallback) != str(day):
            logger.info("eltdx 逐笔 %s: %s 无数据, 回退到 %s", symbol, day, fallback)
            day = fallback
            path = _path(symbol, day)
            if path.exists() and not force:
                try:
                    return pl.read_parquet(path), day
                except Exception as e:
                    logger.warning("逐笔缓存 %s 读取失败: %s", path, e)
            rows = _fetch_pages(code)
    logger.info("eltdx 逐笔 %s @%s: %d 笔, %.2fs", symbol, day, len(rows), time.perf_counter() - t0)

    if not rows:
        return empty, day
    df = pl.DataFrame(rows, schema={
        "index": pl.Int64, "time": pl.Utf8, "price": pl.Float64, "volume": pl.Float64,
        "order_count": pl.Int64, "side": pl.Utf8, "kind": pl.Utf8,
    }, strict=False)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(path)
    except Exception as e:
        logger.warning("逐笔落盘失败(仅影响缓存): %s", e)
    return df, day


def orderflow(df: pl.DataFrame) -> dict:
    """订单流: 逐时间序列的 Delta / CumDelta / 主动买卖量。

    ``side`` 为 neutral 的(竞价撮合)不计入买卖, 单列统计。
    """
    if df.is_empty():
        return {"points": [], "summary": None}

    buy_v = df.filter(pl.col("side") == "buy")["volume"].sum() or 0.0
    sell_v = df.filter(pl.col("side") == "sell")["volume"].sum() or 0.0
    neutral_v = df.filter(pl.col("side") == "neutral")["volume"].sum() or 0.0

    # 按分钟聚合(逐笔上万条直接给前端会卡)。
    grouped = (
        df.with_columns(pl.col("time").str.slice(0, 5).alias("minute"))
        .group_by("minute")
        .agg([
            # fill_null 而不是 `or 0`: 对 Expr 取布尔值会抛 TypeError。
            pl.col("volume").filter(pl.col("side") == "buy").sum().fill_null(0).alias("buy"),
            pl.col("volume").filter(pl.col("side") == "sell").sum().fill_null(0).alias("sell"),
            pl.col("volume").sum().alias("volume"),
            pl.col("price").mean().alias("price"),
            pl.len().alias("trades"),
        ])
        .sort("minute")
    )
    points: list[dict] = []
    cum = 0.0
    for row in grouped.to_dicts():
        delta = float(row["buy"] or 0.0) - float(row["sell"] or 0.0)
        cum += delta
        points.append({
            "minute": row["minute"],
            "buy": float(row["buy"] or 0.0),
            "sell": float(row["sell"] or 0.0),
            "delta": delta,
            "cum_delta": cum,
            "volume": float(row["volume"] or 0.0),
            "price": float(row["price"] or 0.0),
            "trades": int(row["trades"] or 0),
        })

    total = buy_v + sell_v
    return {
        "points": points,
        "summary": {
            "trades": int(df.height),
            "buy_volume": float(buy_v),
            "sell_volume": float(sell_v),
            "neutral_volume": float(neutral_v),
            "delta": float(buy_v - sell_v),
            "delta_ratio": float((buy_v - sell_v) / total) if total else 0.0,
            "avg_price": float(df["price"].mean() or 0.0),
        },
    }


def footprint(df: pl.DataFrame, rows: int = 40, bucket_minutes: int = 5) -> dict:
    """足迹图: 价格(纵) x 时间(横) 的买卖量矩阵。

    每格给 ``buy`` / ``sell`` / ``delta``; 同时给出 POC(成交最密集价) 与 VWAP。
    ``rows`` 是价格分档数, ``bucket_minutes`` 是时间桶宽度。
    """
    if df.is_empty():
        return {"price_levels": [], "time_buckets": [], "cells": [], "poc": None, "vwap": None}

    work = df.filter(pl.col("price") > 0)
    if work.is_empty():
        return {"price_levels": [], "time_buckets": [], "cells": [], "poc": None, "vwap": None}

    low = float(work["price"].min())
    high = float(work["price"].max())
    if high <= low:
        high = low + 0.01
    step = (high - low) / max(1, rows)

    # 时间桶: "09:31" -> 桶起点标签("09:30"), 直接给前端当横轴刻度用。
    def bucket_of(text: str) -> str:
        try:
            hh, mm = str(text)[:5].split(":")
            slot = ((int(hh) * 60 + int(mm)) // max(1, bucket_minutes)) * bucket_minutes
            return f"{slot // 60:02d}:{slot % 60:02d}"
        except Exception:
            return ""

    minutes = [bucket_of(t) for t in work["time"].to_list()]
    prices = work["price"].to_list()
    volumes = work["volume"].to_list()
    sides = work["side"].to_list()

    grid: dict[tuple[str, int], dict[str, float]] = {}
    for b, p, v, s in zip(minutes, prices, volumes, sides, strict=False):
        if not b or not p:
            continue
        level = min(rows - 1, int((p - low) / step))
        key = (b, level)
        cell = grid.setdefault(key, {"buy": 0.0, "sell": 0.0, "volume": 0.0})
        cell["volume"] += float(v or 0.0)
        if s == "buy":
            cell["buy"] += float(v or 0.0)
        elif s == "sell":
            cell["sell"] += float(v or 0.0)

    time_buckets = sorted({b for b, _ in grid})
    price_levels = [round(low + (i + 0.5) * step, 4) for i in range(rows)]
    cells = [
        {"t": b, "p": lv, "buy": round(c["buy"], 2), "sell": round(c["sell"], 2),
         "delta": round(c["buy"] - c["sell"], 2), "volume": round(c["volume"], 2)}
        for (b, lv), c in sorted(grid.items())
    ]

    # POC: 全时段成交量最大的价格档。
    by_level: dict[int, float] = {}
    for bucket, lv in grid:
        by_level[lv] = by_level.get(lv, 0.0) + grid[(bucket, lv)]["volume"]
    poc_level = max(by_level, key=lambda k: by_level[k]) if by_level else None

    notional = float((work["price"] * work["volume"]).sum() or 0.0)
    total_vol = float(work["volume"].sum() or 0.0)
    return {
        "price_levels": price_levels,
        "time_buckets": time_buckets,
        "cells": cells,
        "poc": price_levels[poc_level] if poc_level is not None else None,
        "vwap": (notional / total_vol) if total_vol else None,
        "low": low,
        "high": high,
        "step": step,
        "bucket_minutes": bucket_minutes,
    }


def price_distribution(df: pl.DataFrame, max_rows: int = 60, tick_size: float = 0.01) -> dict:
    """分价表: 按价格档聚合成交量 / 成交额 / 笔数 / 主动买卖 + 占比。

    档位自适应(这是关键): A 股一天的成交价位可以有 1000+ 个(茅台 2026-09-30 实测
    1349 个), 逐价位全给前端既没意义也渲染不动; 但低价股(3.00~3.10)只有十几个
    价位, 硬分成 60 档反而看不出真实成交价。

    ⇒ 步长取 **tick 的整数倍**、且尽量贴近 ``(high-low)/max_rows``:
      - 茅台: 31.55/60 = 0.526 -> step 0.53 -> 约 60 档
      - 低价股: 0.10/60 = 0.0017 -> step 0.01 -> 10 档(自然退化成逐价位)

    量纲: volume=手 / amount=元(price x volume x 100)。
    ⚠️ df 是**倒序**(index 0 = 最新一笔, 见 _fetch_pages), 当前价取第一行。
    """
    empty = {
        "rows": [], "poc": None, "vwap": None, "current": None,
        "low": None, "high": None, "step": None,
        "total_volume": 0.0, "total_amount": 0.0,
        "buy_volume": 0.0, "sell_volume": 0.0, "neutral_volume": 0.0,
    }
    if df.is_empty():
        return empty

    work = df.filter(pl.col("price") > 0)
    if work.is_empty():
        return empty

    low = float(work["price"].min())
    high = float(work["price"].max())
    # ⚠️ 全程用 **tick 的整数倍** 做整数运算。直接拿浮点算档位有两个坑(都实测踩过):
    #   1. ceil(0.3/5/0.01) = ceil(6.000000000000001) = 7, 步长凭空多一档;
    #   2. (3.01-3.00)/0.01 = 0.9999999999999563, floor 后把 3.01 并进 3.00 档。
    span_ticks = int(round((high - low) / tick_size))
    step_ticks = max(1, math.ceil(span_ticks / max(1, max_rows)))
    step = step_ticks * tick_size
    levels = span_ticks // step_ticks + 1 if span_ticks > 0 else 1

    binned = work.with_columns(
        (((pl.col("price") - low) / tick_size).round(0).cast(pl.Int64) // step_ticks)
        .clip(0, levels - 1)
        .alias("lv")
    )
    grouped = (
        binned.group_by("lv")
        .agg([
            pl.col("volume").sum().alias("volume"),
            pl.len().alias("trades"),
            pl.col("volume").filter(pl.col("side") == "buy").sum().fill_null(0).alias("buy"),
            pl.col("volume").filter(pl.col("side") == "sell").sum().fill_null(0).alias("sell"),
            pl.col("volume").filter(pl.col("side") == "neutral").sum().fill_null(0).alias("neutral"),
            (pl.col("price") * pl.col("volume")).sum().alias("notional"),
        ])
        .sort("lv")
    )

    total_volume = float(work["volume"].sum() or 0.0)
    rows: list[dict] = []
    cum = 0.0
    for row in grouped.to_dicts():
        lv = int(row["lv"])
        vol = float(row["volume"] or 0.0)
        ratio = vol / total_volume if total_volume else 0.0
        cum += ratio
        mid = low + (lv + 0.5) * step
        rows.append({
            # 档中值(前端主列) + 档区间(悬停/分组用)
            "price": round(mid, 4),
            "low": round(low + lv * step, 4),
            "high": round(low + (lv + 1) * step, 4),
            "volume": vol,
            # notional 是 price(元) x volume(手), 乘 100 才是元
            "amount": float(row["notional"] or 0.0) * 100.0,
            "trades": int(row["trades"] or 0),
            "buy": float(row["buy"] or 0.0),
            "sell": float(row["sell"] or 0.0),
            "neutral": float(row["neutral"] or 0.0),
            "ratio": round(ratio, 6),
            "cum_ratio": round(cum, 6),
        })

    poc_row = max(rows, key=lambda r: r["volume"]) if rows else None
    notional_total = float((work["price"] * work["volume"]).sum() or 0.0)
    buy_total = sum(r["buy"] for r in rows)
    sell_total = sum(r["sell"] for r in rows)
    neutral_total = sum(r["neutral"] for r in rows)

    return {
        "rows": rows,
        "poc": poc_row["price"] if poc_row else None,
        "vwap": (notional_total / total_volume) if total_volume else None,
        # df 倒序: 第一行就是最新一笔
        "current": float(work["price"][0]) if work.height else None,
        "low": low,
        "high": high,
        "step": step,
        "total_volume": total_volume,
        "total_amount": notional_total * 100.0,
        "buy_volume": buy_total,
        "sell_volume": sell_total,
        "neutral_volume": neutral_total,
    }


def cached_days() -> list[str]:
    """已落盘的交易日列表(最新在前)。前端用它给出可选日期。"""
    root = tick_dir()
    if not root.exists():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)


def last_updated(symbol: str, day: Any) -> str | None:
    """某只某天逐笔的落盘时间(ISO)。没有则 None。"""
    path = _path(symbol, day)
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
