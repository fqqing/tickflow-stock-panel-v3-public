"""U2: 涨停梯队 / 异动 —— v2 自己算, 不依赖 v1 进程.

为什么能自算
============
涨停梯队和异动看起来像"必须有 v1 的采集管道", 其实两块输入 v2 全都有:

* **盘中快照** —— :mod:`chanlab.quote` 拉的腾讯 qt 全市场快照 (~7500 只, 2s),
  带 现价/昨收/开高低/成交量/成交额。
* **历史日线** —— v2 自建库 ``data/kline_daily`` (5510 只, 2001 起)。
  连板数和量比都从这里算, v1 没有这两样的现成产物也能做。

关键在于**别按 symbol 循环读日线**: 5510 只逐只 ``read_parquet`` 要 70 秒以上,
交互式接口根本受不了。改成一次 lazy scan + 日期过滤 (只投影 7 列),
实测 **0.9 秒**拿到全市场最近 30 个交易日, 之后进程内缓存。

两个口径坑
==========
1. **涨跌幅限制不是统一的 10%。** 创业板/科创板 20%, 北交所 30%, ST 5%。
   按代码前缀判断, 不能一刀切, 否则创业板的涨停永远判不出来。
2. **日线库可能比实时快照慢一天。** 收盘后同步过, 最后一根就是"今天";
   盘中没同步, 最后一根是"昨天"。靠 ``raw_close`` 与实时 ``prev_close``
   是否相等来判别 (见 :func:`_is_new_day`), 判断错了连板数会整体差 1。

量纲
====
对外输出的 ``change_pct`` / ``amplitude`` / ``turnover`` **统一是百分数**
(1.86 表示 +1.86%), 与 :class:`ScanRow` / :class:`IndexItem` 一致;
只有 :class:`Quote` 那种 qt 原样透传的还是小数。``amount`` 是**元**。
"""

from __future__ import annotations

import contextlib
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from chanlab import v1mirror
from chanlab.loader import resolve_own_dir
from chanlab.quote import quotes

CN_TZ = timezone(timedelta(hours=8))

#: 连板回溯最多看多少根。12 足够: 超过 12 连板的票一年没几只, 再往前还容易
#: 撞上停牌/复牌把口径搞乱。
MAX_BOARDS = 12

#: 读最近多少**自然日**的日线。45 天能盖住 30 个交易日, 够算 5 日均量 + 12 连板。
RECENT_DAYS = 45

#: "日线库最后一根" 与 "实时昨收" 的容差。同一天的收盘价必然相等,
#: 差一分钱以上就说明不是同一天。
DAY_EPS = 0.005

RECENT_TTL = float(os.environ.get("TICKFLOW_MARKET_TTL", "300"))

#: 落盘的扫描结果能用多久。日线一天才变一次, 但首次全市场 scan 要 11 秒
#: (5510 个 parquet 逐个开 footer), 每次冷启动都付一遍太蠢 —— 存成单文件后
#: 读回来只要 0.1 秒。
DISK_TTL = float(os.environ.get("TICKFLOW_MARKET_DISK_TTL", "1800"))

#: 落盘位置。放在 v2 自己的 data/ 下, 与镜像同级但不是镜像 —— 它每天会被重写。
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "market" / "recent.parquet"

# symbol -> 板块涨跌幅限制
_RATE_CACHE: dict[str, float] = {}

# 最近 N 天日线的进程内缓存
_RECENT: dict[str, Any] = {"at": 0.0, "frame": None}

MOVER_KINDS = ("gain", "loss", "amplitude", "turnover", "amount", "volume_ratio")

MOVER_LABELS = {
    "gain": "涨幅榜",
    "loss": "跌幅榜",
    "amplitude": "振幅榜",
    "turnover": "换手榜",
    "amount": "成交额榜",
    "volume_ratio": "量比榜",
}


# ---------------------------------------------------------------- 板块与价格


def board_rate(symbol: str, name: str = "") -> float:
    """板块涨跌幅限制: 0.10 / 0.20 / 0.30 / 0.05(ST)."""
    hit = _RATE_CACHE.get(symbol)
    if hit is not None:
        return hit
    code = symbol.split(".")[0]
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    if suffix == "BJ":
        rate = 0.30
    elif code[:3] in ("688", "689", "300", "301"):
        rate = 0.20
    elif "ST" in (name or "").upper():
        rate = 0.05
    else:
        rate = 0.10
    _RATE_CACHE[symbol] = rate
    return rate


def limit_prices(prev_close: float, rate: float) -> tuple[float, float]:
    """(涨停价, 跌停价)。A 股按昨收四舍五入到分。"""
    up = round(prev_close * (1.0 + rate), 2)
    down = round(prev_close * (1.0 - rate), 2)
    return up, down


def _elapsed_minutes(now: datetime | None = None) -> int:
    """当天已开盘分钟数 (0~240)。

    量比必须折算: 早上 10 点拿"当天累计量"跟"过去 5 日全天量"比, 永远显得
    缩量。收市/非交易时段按 240 算 (折算系数 = 1)。
    """
    now = now or datetime.now(CN_TZ)
    if now.weekday() >= 5:
        return 240
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return 0
    if minutes <= 11 * 60 + 30:
        return minutes - (9 * 60 + 30)
    if minutes < 13 * 60:
        return 120
    if minutes >= 15 * 60:
        return 240
    return 120 + (minutes - 13 * 60)


# ---------------------------------------------------------------- 历史日线


def recent_bars(*, ttl: float | None = None, refresh: bool = False) -> pl.DataFrame:
    """全市场最近 ~30 个交易日的日线 (带 pct / avg5 两列衍生量)。

    返回列: ``symbol`` / ``date`` / ``close``(前复权) / ``high`` / ``volume``
    / ``amount`` / ``raw_close``(不复权) / ``pct``(小数) / ``avg5``(前 5 日均量)。
    """
    limit = RECENT_TTL if ttl is None else ttl
    frame = _RECENT["frame"]
    if frame is not None and not refresh and (time.perf_counter() - _RECENT["at"]) < limit:
        return frame

    df = _scan_recent(refresh)
    if not df.is_empty():
        df = df.with_columns(
            # 前复权序列里相邻两日的比值等于真实涨跌幅, 只有除权日当天会失真;
            # 除权日的 pct 通常远离 ±10%, 不会被误判成涨停。
            (pl.col("close") / pl.col("close").shift(1) - 1.0).over("symbol").alias("pct"),
            pl.col("volume").shift(1).rolling_mean(5).over("symbol").alias("avg5"),
        )
    _RECENT["at"] = time.perf_counter()
    _RECENT["frame"] = df
    return df


def _disk_frame() -> pl.DataFrame | None:
    """读落盘缓存。文件不存在/过期/损坏都返回 None, 不抛。"""
    if not CACHE_PATH.is_file():
        return None
    if (time.time() - CACHE_PATH.stat().st_mtime) > DISK_TTL:
        return None
    try:
        return pl.read_parquet(CACHE_PATH)
    except Exception:  # 半截写或被手删, 重新扫一遍就是了
        return None


def _scan_recent(refresh: bool) -> pl.DataFrame:
    """全市场扫最近 RECENT_DAYS 天, 优先走落盘缓存。"""
    if not refresh:
        cached = _disk_frame()
        if cached is not None:
            return cached

    store = resolve_own_dir()
    cutoff = date.today() - timedelta(days=RECENT_DAYS)
    lf = pl.scan_parquet(str(store / "**" / "*.parquet"))
    df = (
        lf.filter(pl.col("date") >= cutoff)
        .select(["symbol", "date", "close", "high", "volume", "amount", "raw_close"])
        .sort(["symbol", "date"])
        .collect()
    )
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(CACHE_PATH)
    except Exception:  # 缓存写不进去不影响主流程, 只是下次慢一点
        pass
    return df


def invalidate() -> None:
    """强制下一次请求重算 (给测试和「手动刷新」用)。"""
    _RECENT["at"] = 0.0
    with contextlib.suppress(Exception):
        CACHE_PATH.unlink()


def _symbols_in_store() -> set[str]:
    frame = recent_bars()
    return set(frame["symbol"].unique().to_list())


def rank_rows(rows: list[dict[str, Any]], field: str, *, desc: bool = True) -> None:
    """原地排序。缺值一律沉底 —— 拿 None 比大小在 py3 是 TypeError, 而且把 '-'
    排在榜首会让整张榜看起来像坏了。"""
    inf = math.inf

    def key(row: dict[str, Any]) -> float:
        raw = row.get(field)
        if not isinstance(raw, int | float) or not math.isfinite(raw):
            return inf
        return -float(raw) if desc else float(raw)

    rows.sort(key=key)


def _trailing_streak(flags: list[bool]) -> int:
    """从末尾往前数连续 True 的个数。"""
    n = 0
    for flag in reversed(flags):
        if not flag:
            break
        n += 1
    return n


def _history_side() -> tuple[dict[str, list[bool]], dict[str, float], dict[str, float], Any]:
    """按 symbol 汇总历史侧的三样东西.

    返回 ``(lu_flags, avg5, last_raw_close, last_date)``:
    ``lu_flags[symbol]`` 是最近 MAX_BOARDS 根是否涨停(时间正序),
    ``avg5`` 是前 5 日均量(手), ``last_raw_close`` 是最后一根的不复权收盘价。
    """
    df = recent_bars()
    if df.is_empty():
        return {}, {}, {}, None

    last_date = df["date"].max()
    rates = {s: board_rate(s) for s in df["symbol"].unique().to_list()}
    # 涨幅达到限制即算涨停。留 0.008 的容差是因为四舍五入到分之后,
    # 真实涨停幅度会在 (rate-0.5%, rate] 之间浮动。
    thr = df["symbol"].replace_strict(rates, default=0.10) - 0.008
    df = df.with_columns((pl.col("pct") >= thr).alias("is_lu"))

    lu: dict[str, list[bool]] = {}
    avg5: dict[str, float] = {}
    raws: dict[str, float] = {}
    for row in df.select(["symbol", "is_lu", "avg5", "raw_close"]).iter_rows():
        sym, flag, mean, raw = row
        bucket = lu.setdefault(sym, [])
        bucket.append(bool(flag) if flag is not None else False)
        if len(bucket) > MAX_BOARDS:
            del bucket[0]
        if mean is not None:
            avg5[sym] = float(mean)
        if raw is not None:
            raws[sym] = float(raw)
    return lu, avg5, raws, last_date


def _is_new_day(prev_close: float | None, last_raw_close: float | None) -> bool:
    """实时快照的昨收 与 日线库最后一根收盘 是不是同一天.

    相等 => 日线库最后一根是"昨天", 实时价格属于"今天"还没落库;
    不等 => 日线库已经收了今天, 实时就是那根本身。
    """
    if prev_close is None or last_raw_close is None:
        return False
    return abs(float(prev_close) - float(last_raw_close)) <= DAY_EPS


# ---------------------------------------------------------------- 实时侧


def _industry_map() -> dict[str, str]:
    """symbol -> 同花顺行业。整表建一次 map, 别按 symbol 逐只 filter。"""
    df = v1mirror.load("industry")
    if df.is_empty() or v1mirror.INDUSTRY_COL not in df.columns:
        return {}
    out: dict[str, str] = {}
    for row in df.select(["symbol", v1mirror.INDUSTRY_COL]).iter_rows():
        out[row[0]] = str(row[1] or "")
    return out


def snapshot_rows(*, symbols: list[str] | None = None) -> tuple[list[dict[str, Any]], dict]:
    """合并实时快照 + 标的元数据, 得到异动/涨停共用的行.

    返回 ``(rows, meta)``。``rows`` 只含**日线库里有的 A 股**: 腾讯快照还会
    返回 ETF 和指数, 那些没有 float_shares 也算不了换手。
    """
    snap = quotes(symbols)
    pool = snap.get("quotes") or {}
    universe = _symbols_in_store()
    keep = [s for s in pool if s in universe] if universe else sorted(pool)
    if symbols:
        keep = [s for s in keep if s in set(symbols)]

    meta_map = v1mirror.instrument_map(keep) if keep else {}
    industry = _industry_map()

    rows: list[dict[str, Any]] = []
    for sym in keep:
        q = pool[sym]
        info = meta_map.get(sym) or {}
        prev_close = q.get("prev_close")
        price = q.get("last_price")
        high = q.get("high")
        low = q.get("low")
        volume = q.get("volume") or 0.0
        amount = q.get("amount") or 0.0
        float_shares = info.get("float_shares")
        name = q.get("name") or info.get("name") or ""

        rate = board_rate(sym, name)
        limit_up, limit_down = (
            limit_prices(prev_close, rate) if prev_close else (None, None)
        )
        amplitude = (
            (high - low) / prev_close * 100.0
            if prev_close and high is not None and low is not None
            else None
        )
        turnover = (
            volume * 100.0 / float_shares * 100.0
            if float_shares
            else None
        )
        rows.append(
            {
                "symbol": sym,
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "open": q.get("open"),
                "high": high,
                "low": low,
                # 对外统一百分数: 1.86 表示 +1.86%
                "change_pct": (q.get("change_pct") or 0.0) * 100.0,
                "amplitude": amplitude,
                "turnover": turnover,
                "volume": volume,
                "amount": amount,
                "float_shares": float_shares,
                "float_mv": price * float_shares if (price and float_shares) else None,
                "rate": rate,
                "limit_up": limit_up,
                "limit_down": limit_down,
                "industry": industry.get(sym, ""),
            }
        )

    meta = {
        "ok": bool(snap.get("ok")),
        "detail": snap.get("detail") or "",
        "updated_at": snap.get("updated_at"),
        "source": snap.get("source") or "",
        "count": len(rows),
    }
    return rows, meta


# ---------------------------------------------------------------- 涨停梯队


def limit_up_ladder(*, limit: int = 300) -> dict[str, Any]:
    """涨停梯队: 按连板数分组.

    ``status`` 区分 **封板**(现价在涨停价上) 与 **炸板**(盘中摸到涨停价但
    现价掉了下来) —— 梯队看的是封住的, 炸板单独计数, 别混在一起。
    """
    rows, meta = snapshot_rows()
    lu_flags, avg5, raws, last_date = _history_side()
    elapsed = _elapsed_minutes()

    ladder: list[dict[str, Any]] = []
    sealed = 0
    broken = 0
    limit_down = 0
    buckets: dict[int, int] = {}

    for r in rows:
        price = r["price"]
        limit_up = r["limit_up"]
        limit_dn = r["limit_down"]
        high = r["high"]
        if not price or not limit_up:
            continue

        touched = high is not None and high >= limit_up - DAY_EPS
        on_limit = price >= limit_up - DAY_EPS
        if not on_limit and not touched:
            if limit_dn and price <= limit_dn + DAY_EPS:
                limit_down += 1
            continue

        streak = _trailing_streak(lu_flags.get(r["symbol"], []))
        if on_limit and _is_new_day(r["prev_close"], raws.get(r["symbol"])):
            boards = streak + 1
        else:
            boards = streak
        if on_limit and boards < 1:
            boards = 1

        if on_limit:
            sealed += 1
            buckets[boards] = buckets.get(boards, 0) + 1
        else:
            broken += 1

        mean = avg5.get(r["symbol"])
        factor = 240.0 / elapsed if elapsed > 0 else 1.0
        ladder.append(
            {
                **r,
                "boards": boards,
                "status": "sealed" if on_limit else "broken",
                "volume_ratio": (r["volume"] * factor / mean) if mean else None,
            }
        )

    ladder.sort(key=lambda x: (-x["boards"], -(x["amount"] or 0)))
    return {
        "rows": ladder[:limit],
        "total": len(ladder),
        "as_of": str(last_date) if last_date else None,
        "stats": {
            "sealed": sealed,
            "broken": broken,
            "limit_down": limit_down,
            "max_boards": max(buckets) if buckets else 0,
            "buckets": [
                {"boards": b, "count": buckets[b]} for b in sorted(buckets, reverse=True)
            ],
        },
        **meta,
    }


# ---------------------------------------------------------------- 异动


def movers(kind: str = "gain", *, limit: int = 50) -> dict[str, Any]:
    """异动榜。

    ``volume_ratio``(量比) 需要历史 5 日均量, 只有 :func:`recent_bars` 里有,
    所以这里统一补上; 折算见 :func:`_elapsed_minutes`。
    """
    if kind not in MOVER_KINDS:
        raise ValueError(f"未知异动类型 {kind!r}, 可选 {list(MOVER_KINDS)}")

    rows, meta = snapshot_rows()
    _lu, avg5, _raws, last_date = _history_side()
    elapsed = _elapsed_minutes()
    factor = 240.0 / elapsed if elapsed > 0 else 1.0

    out: list[dict[str, Any]] = []
    for r in rows:
        mean = avg5.get(r["symbol"])
        volume_ratio = (
            r["volume"] * factor / mean if mean else None
        )
        # 停牌/零成交的量比没有意义, 排榜时会把整张榜灌满垃圾
        if kind == "volume_ratio" and (not volume_ratio or r["volume"] <= 0):
            continue
        if kind in ("gain", "loss", "amplitude", "turnover", "amount") and r["volume"] <= 0:
            continue
        out.append({**r, "volume_ratio": volume_ratio})

    keys = {
        "gain": ("change_pct", True),
        "loss": ("change_pct", False),
        "amplitude": ("amplitude", True),
        "turnover": ("turnover", True),
        "amount": ("amount", True),
        "volume_ratio": ("volume_ratio", True),
    }
    field, desc = keys[kind]
    rank_rows(out, field, desc=desc)
    return {
        "rows": out[:limit],
        "kind": kind,
        "label": MOVER_LABELS[kind],
        "as_of": str(last_date) if last_date else None,
        "elapsed": elapsed,
        **meta,
    }
