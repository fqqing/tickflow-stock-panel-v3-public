"""U3: 市场环境 (regime) 自算 —— v1 ``regime_builder`` 的 v2 版.

为什么必须自算
==============
``regime`` 是「市场情绪」那一页的唯一数据源, 而情绪页是**每天都要看**的。
镜像里已经搬了 2018-01-02 起的 2142 行, 但镜像是**冻结的**: v1 一天不跑,
它就永远停在迁移那天, 而且旧得悄无声息。所以这里照抄 U1 (指数) 的路子:

    自建库只补「镜像之后的新日期」, 读的时候 own + mirror 合并去重。

这样一次只要扫最近几十个交易日的日线 (秒级), 而不是全量重算 8 年。

口径照抄 v1
==========
评分模型、四个子维度、状态阈值、梯队字段全部照搬 v1
``app/services/regime_builder.py`` + ``market_phase.py``, 一个系数都没改 ——
改了就没法和镜像里的历史行对齐, 曲线会在接缝处断层。

两处有意不做
============
1. **``phase`` (周期阶段) 不重算。** v1 的 ``refresh_phase_labels`` 要拿
   **完整日序**做 EMA 平滑再回写全表, 只补几天根本算不出来。自建行的
   phase 留 null, 合并时沿用镜像最后一次标的值; 前端显示「沿用 X 日标注」。
2. **涨停判定必须走交易所口径, 不能用涨跌幅比值近似。** 实测 (2026-09-28~30):
   比值法配 0.008 容差会把炸板从 15 只算成 30 只 —— 因为「摸到涨停价」和
   「涨了 9.2%」是两回事。正确做法是拿**不复权**价算
   ``涨停价 = round(昨收 x (1+rate), 2)`` 再比高低价。与 v1 逐日对拍:
   09-28 涨停 35/跌停 55/炸板 15 (v1: 33/59/15), 09-29 55/11/12 (v1: 57/11/12)。
   ⚠️ 涨跌幅 (``pct``) 仍然用前复权比值 —— 那是真实涨跌幅, 除权日才失真。
3. **判涨跌幅限制必须带股票名。** ST 股限价是 5%, 而 ``board_rate`` 只有
   看到名字里的 "ST" 才会给 5%。只传代码的话, 每天会漏掉几只 ST 的涨跌停 ——
   实测 09-30 涨停数 55 (漏 ST) vs 57 (正确, 与涨停梯队页一致),
   复盘页和涨停页会互相打架。所以这里从镜像 ``instruments`` 取名再判。

量纲
====
与镜像完全一致: ``index_pct`` / ``avg_pct`` / ``median_pct`` 是**小数**
(0.0167 = +1.67%), ``*_pct`` 里的占比类 (``above_ma20_pct`` / ``up_ratio``
除外) 是**百分数** (31.66 = 31.66%), ``total_amount`` 是**元**,
四个子分与综合分是 0-100 整数。
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from chanlab import v1mirror
from chanlab.loader import resolve_own_dir
from chanlab.market import board_rate

logger = logging.getLogger(__name__)

#: 自建库落盘位置。与镜像同级但不是镜像 —— 每天会被追加。
STORE_PATH = Path(__file__).resolve().parents[2] / "data" / "regime" / "part.parquet"

#: 一次最多补多少个**自然日**。默认只补镜像之后的缺口, 这个上限是防呆:
#: 万一自建库被删了, 不至于去扫 8 年的日线把内存打爆。
MAX_BACKFILL_DAYS = int(os.environ.get("TICKFLOW_REGIME_BACKFILL_DAYS", "500"))

#: MA20 要 20 根、连板要 12 根、晋级率要前一日, 预热给 60 个自然日足够。
WARMUP_DAYS = 60

#: 晋级率的最小样本量, 照抄 v1 market_phase.PROMO_MIN_POOL。
PROMO_MIN_POOL = 10

# ───────────────────────── 状态分类阈值 (照抄 v1) ─────────────────────────

WEIGHTS = {
    "profit": 0.35,
    "speculation": 0.25,
    "resilience": 0.20,
    "trend": 0.20,
}

STATE_STRONG = 70
STATE_LEAN_STRONG = 55
STATE_RANGE = 45
STATE_LEAN_WEAK = 30

STATE_LABELS = {
    "strong": "强势",
    "lean_strong": "偏强",
    "range": "震荡",
    "lean_weak": "偏弱",
    "weak": "弱势",
}

#: 持久化列。顺序与镜像表一致, 合并时才能直接 vstack。
COLUMNS: list[tuple[str, pl.DataType]] = [
    ("date", pl.Utf8),
    ("state", pl.Utf8),
    ("score", pl.Int64),
    ("limit_up", pl.Int64),
    ("limit_down", pl.Int64),
    ("broken_limit", pl.Int64),
    ("max_consecutive", pl.Int64),
    ("seal_rate", pl.Float64),
    ("up_count", pl.Int64),
    ("down_count", pl.Int64),
    ("up_ratio", pl.Float64),
    ("index_pct", pl.Float64),
    ("above_ma20_pct", pl.Float64),
    ("total_amount", pl.Float64),
    ("avg_turnover", pl.Float64),
    ("avg_pct", pl.Float64),
    ("median_pct", pl.Float64),
    ("strong_up_pct", pl.Float64),
    ("strong_down_pct", pl.Float64),
    ("profit_score", pl.Int64),
    ("speculation_score", pl.Int64),
    ("resilience_score", pl.Int64),
    ("trend_score", pl.Int64),
    ("first_board", pl.Int64),
    ("ge2_count", pl.Int64),
    ("ge3_count", pl.Int64),
    ("ge5_count", pl.Int64),
    ("ladder_completeness", pl.Float64),
    ("promo_pool", pl.Int64),
    ("promo_rate", pl.Float64),
    ("phase", pl.Utf8),
]


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=dict(COLUMNS))


def _score(value: float, low: float, high: float) -> float:
    """v1 同款归一化: [low, high] 线性映射到 [0, 100], 钳制边界。"""
    if high <= low:
        return 50.0
    return float(max(0, min(100, round((value - low) / (high - low) * 100))))


def subscores(metrics: dict[str, Any]) -> dict[str, float]:
    """四个子维度分 + 综合分。照抄 v1 ``_compute_subscores``。

    ``metrics`` 需要的键见 :func:`classify_state`。
    """
    profit = (
        _score(metrics.get("up_pct", 50), 21, 75) * 0.45
        + _score(metrics.get("avg_pct", 0) * 100, -1.2, 1.3) * 0.25
        + _score(metrics.get("median_pct", 0) * 100, -1.2, 1.3) * 0.20
        + _score(metrics.get("strong_diff_pct", 0), -13, 14) * 0.10
    )
    speculation = (
        _score(metrics.get("limit_up", 0), 35, 97) * 0.30
        + _score((metrics.get("seal_rate", 0.5) or 0.5) * 100, 57, 75) * 0.40
        + _score(metrics.get("max_consecutive", 0), 4, 9) * 0.30
    )
    resilience = 100 - _score(metrics.get("strong_down_pct", 0), 2, 18)
    trend = (
        _score(metrics.get("index_pct", 0) * 100, -2.5, 2.5) * 0.50
        + _score((metrics.get("above_ma20_pct", 0.5) or 0.5) * 100, 22, 76) * 0.50
    )
    score = (
        profit * WEIGHTS["profit"]
        + speculation * WEIGHTS["speculation"]
        + resilience * WEIGHTS["resilience"]
        + trend * WEIGHTS["trend"]
    )
    return {
        "profit": profit,
        "speculation": speculation,
        "resilience": resilience,
        "trend": trend,
        "score": max(0, min(100, score)),
    }


def classify_state(metrics: dict[str, Any]) -> tuple[str, int]:
    """规则引擎: 指标 → (离散状态, 综合分)。照抄 v1 ``classify_state``。

    ``metrics`` 键: ``up_pct`` ``down_pct`` ``avg_pct`` ``median_pct``
    ``strong_up_pct`` ``strong_down_pct`` ``strong_diff_pct`` ``limit_up``
    ``seal_rate`` ``max_consecutive`` ``index_pct`` ``above_ma20_pct``。
    """
    sub = subscores(metrics)
    score = max(0, min(100, round(sub["score"])))
    if score >= STATE_STRONG:
        state = "strong"
    elif score >= STATE_LEAN_STRONG:
        state = "lean_strong"
    elif score >= STATE_RANGE:
        state = "range"
    elif score >= STATE_LEAN_WEAK:
        state = "lean_weak"
    else:
        state = "weak"
    return state, score


def _ladder_row(r: dict[str, Any], height: int) -> dict[str, Any]:
    """v1 ``finalize_ladder_row``: 梯队原始值 → 持久化字段。"""
    rungs = int(r.get("rungs_filled") or 0)
    completeness = (rungs / (height - 1)) if height >= 3 else 0.0
    pool = int(r.get("promo_pool") or 0)
    ok = int(r.get("promo_ok") or 0)
    promo = (ok / pool) if pool >= PROMO_MIN_POOL else None
    return {
        "first_board": int(r.get("first_board") or 0),
        "ge2_count": int(r.get("ge2_count") or 0),
        "ge3_count": int(r.get("ge3_count") or 0),
        "ge5_count": int(r.get("ge5_count") or 0),
        "ladder_completeness": round(completeness, 4),
        "promo_pool": pool,
        "promo_rate": round(promo, 4) if promo is not None else None,
    }


# ---------------------------------------------------------------- 日线聚合


def _daily_frame(start: date, end: date) -> pl.DataFrame:
    """扫日线库 [start-WARMUP, end], 算出聚合所需的派生列。

    一次 lazy scan + 只投影 7 列。逐只 read_parquet 要 70 秒, 这样 1 秒出头。
    """
    store = resolve_own_dir()
    lo = start - timedelta(days=WARMUP_DAYS)
    lf = pl.scan_parquet(str(store / "**" / "*.parquet"))
    df = (
        lf.filter((pl.col("date") >= lo) & (pl.col("date") <= end))
        .select(["symbol", "date", "close", "raw_close", "raw_high", "amount", "volume"])
        .sort(["symbol", "date"])
        .collect()
    )
    return _derive(df)


def _limit_rates(symbols: list[str]) -> dict[str, float]:
    """symbol -> 涨跌幅限制。

    ⚠️ 必须带**名字**: ST 股的限价是 5%, 而 `board_rate` 只有看到名字里的
    "ST" 才会给 5%。漏了这一步, 每天会有几只 ST 的涨跌停判不出来 ——
    实测 09-30 涨停数会从 55 变成 57, 复盘页与涨停页对不上。
    """
    names: dict[str, str] = {}
    try:
        df = v1mirror.load("instruments")
        if not df.is_empty() and "name" in df.columns:
            names = {
                str(r[0]).upper(): str(r[1] or "")
                for r in df.select(["symbol", "name"]).iter_rows()
            }
    except Exception:  # 元数据拿不到就退化成按代码前缀判, 只是 ST 会漏
        logger.warning("regime: instruments 不可用, ST 限价按普通板块处理", exc_info=True)
    return {s: board_rate(s, names.get(s, "")) for s in symbols}


def _derive(df: pl.DataFrame) -> pl.DataFrame:
    """给日线加上 pct / ma20 / 三涨停标记 / 连板数。

    单独拆出来是因为**连板数最容易写错**: 最自然的写法
    ``is_lu.cum_sum()`` 会跨过非涨停日继续累加, 周一涨停、周二不涨、
    周三涨停会被算成 2 连板。这里按「False 打断」分组, 组内才累加。
    """
    if df.is_empty():
        return df

    rates = _limit_rates(df["symbol"].unique().to_list())
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1) - 1.0).over("symbol").alias("pct"),
        pl.col("close").rolling_mean(20).over("symbol").alias("ma20"),
        pl.col("raw_close").shift(1).over("symbol").alias("prev_raw"),
        pl.col("symbol").replace_strict(rates, default=0.10).alias("rate"),
    )
    df = df.with_columns(
        # 交易所口径: 涨停/跌停价按昨收四舍五入到分。比值近似会把炸板算多一倍。
        ((pl.col("prev_raw") * (1.0 + pl.col("rate")) * 100).round(0) / 100).alias("lu_price"),
        ((pl.col("prev_raw") * (1.0 - pl.col("rate")) * 100).round(0) / 100).alias("ld_price"),
    )
    df = df.with_columns(
        (pl.col("raw_close") >= pl.col("lu_price") - 0.001).fill_null(False).alias("is_lu"),
        (pl.col("raw_close") <= pl.col("ld_price") + 0.001).fill_null(False).alias("is_ld"),
        # 炸板: 盘中摸到涨停价但没收在涨停
        (
            (pl.col("raw_high") >= pl.col("lu_price") - 0.001)
            & (pl.col("raw_close") < pl.col("lu_price") - 0.001)
        )
        .fill_null(False)
        .alias("is_broken"),
    )

    # 连板数: 见 :func:`_derive` 的说明。
    df = df.with_columns(
        (pl.lit(1) - pl.col("is_lu").cast(pl.Int32))
        .cum_sum()
        .over("symbol")
        .alias("_grp")
    )
    df = df.with_columns(
        pl.when(pl.col("is_lu"))
        .then(pl.col("is_lu").cast(pl.Int32).cum_sum().over(["symbol", "_grp"]))
        .otherwise(0)
        .alias("consecutive_limit_ups")
    )
    return df


def _index_pct_map(days: int = 400) -> dict[str, float]:
    """{日期字符串: 上证指数涨跌幅(小数)}。自建库优先, 回退镜像。"""
    from chanlab.index_source import daily as index_daily

    try:
        payload = index_daily("000001.SH", days)
    except Exception:  # 指数源挂了不该让情绪页整体打不开
        logger.warning("regime: 指数日线不可用, index_pct 按 0 处理", exc_info=True)
        return {}
    rows = payload.get("rows") or []
    out: dict[str, float] = {}
    prev: float | None = None
    for row in rows:
        close = row.get("close")
        day = str(row.get("date") or "")[:10]
        if close is None or not day:
            continue
        close = float(close)
        if prev:
            out[day] = close / prev - 1.0
        prev = close
    return out


def aggregate(df: pl.DataFrame, *, start: date, index_pct: dict[str, float] | None = None) -> pl.DataFrame:
    """把日线聚合成按日的 regime 行。

    ``df`` 由 :func:`_daily_frame` 产出 (含 pct/ma20/is_lu/is_ld/is_broken/
    consecutive_limit_ups)。``start`` 之前的日期只作预热, 不产出。
    """
    if df.is_empty():
        return _empty()
    index_pct = index_pct or {}
    # ⚠️ 晋级率要的是「昨天的连板数」, 必须在**掐掉预热段之前** shift。
    # 先 filter 再 shift 的话, 窗口第一天永远拿不到前一日, promo_pool 会恒为 0。
    df = df.with_columns(
        pl.col("consecutive_limit_ups").shift(1).over("symbol").alias("_prev_consec")
    )
    df = df.filter(pl.col("date") >= start)
    if df.is_empty():
        return _empty()
    consec = pl.col("consecutive_limit_ups")
    prev = pl.col("_prev_consec")
    grouped = (
        df.group_by("date")
        .agg(
            pl.col("pct").gt(0).sum().alias("up_count"),
            pl.col("pct").lt(0).sum().alias("down_count"),
            pl.len().alias("total_count"),
            pl.col("pct").mean().alias("avg_pct"),
            pl.col("pct").median().alias("median_pct"),
            pl.col("pct").ge(0.03).sum().alias("strong_up_count"),
            pl.col("pct").le(-0.03).sum().alias("strong_down_count"),
            pl.col("is_lu").sum().alias("limit_up"),
            pl.col("is_ld").sum().alias("limit_down"),
            pl.col("is_broken").sum().alias("broken_limit"),
            consec.max().alias("max_consecutive"),
            pl.col("amount").sum().alias("total_amount"),
            pl.col("amount").mean().alias("avg_turnover"),
            pl.when(pl.col("ma20").is_not_null() & (pl.col("ma20") > 0) & (pl.col("close") > pl.col("ma20")))
            .then(1)
            .otherwise(None)
            .sum()
            .alias("_above_cnt"),
            pl.when(pl.col("ma20").is_not_null() & (pl.col("ma20") > 0))
            .then(1)
            .otherwise(None)
            .sum()
            .alias("_valid_cnt"),
            consec.eq(1).sum().alias("first_board"),
            consec.ge(2).sum().alias("ge2_count"),
            consec.ge(3).sum().alias("ge3_count"),
            consec.ge(5).sum().alias("ge5_count"),
            consec.filter(consec.ge(2)).n_unique().alias("rungs_filled"),
            prev.ge(1).sum().alias("promo_pool"),
            (prev.ge(1) & consec.eq(prev + 1)).sum().alias("promo_ok"),
        )
        .sort("date")
    )

    rows: list[dict[str, Any]] = []
    for r in grouped.iter_rows(named=True):
        up = int(r.get("up_count") or 0)
        down = int(r.get("down_count") or 0)
        total = int(r.get("total_count") or 0)
        limit_up = int(r.get("limit_up") or 0)
        broken = int(r.get("broken_limit") or 0)
        valid_cnt = int(r.get("_valid_cnt") or 0)
        above_cnt = int(r.get("_above_cnt") or 0)
        ma20_above = (above_cnt / valid_cnt) if valid_cnt > 0 else 0.0
        up_pct = (up / total * 100) if total else 0.0
        strong_up_pct = ((int(r.get("strong_up_count") or 0)) / total * 100) if total else 0.0
        strong_down_pct = ((int(r.get("strong_down_count") or 0)) / total * 100) if total else 0.0
        avg_pct = float(r.get("avg_pct") or 0.0)
        median_pct = float(r.get("median_pct") or 0.0)
        metrics = {
            "limit_up": limit_up,
            "limit_down": int(r.get("limit_down") or 0),
            "broken_limit": broken,
            "max_consecutive": int(r.get("max_consecutive") or 0),
            "seal_rate": (limit_up / (limit_up + broken)) if (limit_up + broken) > 0 else 0.5,
            "up_count": up,
            "down_count": down,
            "up_ratio": (up / down) if down > 0 else (float(up) if up > 0 else 1.0),
            "index_pct": index_pct.get(str(r["date"]), 0.0),
            "above_ma20_pct": ma20_above,
            "total_amount": float(r.get("total_amount") or 0.0),
            "avg_turnover": float(r.get("avg_turnover") or 0.0),
            "up_pct": up_pct,
            "down_pct": (down / total * 100) if total else 0.0,
            "avg_pct": avg_pct,
            "median_pct": median_pct,
            "strong_up_pct": strong_up_pct,
            "strong_down_pct": strong_down_pct,
            "strong_diff_pct": strong_up_pct - strong_down_pct,
        }
        state, score = classify_state(metrics)
        sub = subscores(metrics)
        rows.append(
            {
                "date": str(r["date"]),
                "state": state,
                "score": score,
                "limit_up": limit_up,
                "limit_down": metrics["limit_down"],
                "broken_limit": broken,
                "max_consecutive": metrics["max_consecutive"],
                "seal_rate": round(metrics["seal_rate"], 4),
                "up_count": up,
                "down_count": down,
                "up_ratio": round(metrics["up_ratio"], 4),
                "index_pct": round(metrics["index_pct"], 4),
                "above_ma20_pct": round(ma20_above, 4),
                "total_amount": metrics["total_amount"],
                "avg_turnover": metrics["avg_turnover"],
                "avg_pct": round(avg_pct, 4),
                "median_pct": round(median_pct, 4),
                "strong_up_pct": round(strong_up_pct, 4),
                "strong_down_pct": round(strong_down_pct, 4),
                "profit_score": round(sub["profit"]),
                "speculation_score": round(sub["speculation"]),
                "resilience_score": round(sub["resilience"]),
                "trend_score": round(sub["trend"]),
                **_ladder_row(r, metrics["max_consecutive"]),
                "phase": None,
            }
        )
    if not rows:
        return _empty()
    return pl.DataFrame(rows, schema=dict(COLUMNS))


# ---------------------------------------------------------------- 自建库读写


def own_frame() -> pl.DataFrame:
    """读自建库。不存在/损坏都返回空表, 不抛。"""
    if not STORE_PATH.is_file():
        return _empty()
    try:
        df = pl.read_parquet(STORE_PATH)
    except Exception:
        logger.warning("regime: 自建库读不出来, 按空处理", exc_info=True)
        return _empty()
    missing = [c for c, _ in COLUMNS if c not in df.columns]
    if missing:
        return _empty()
    return df.select([c for c, _ in COLUMNS])


def mirror_frame() -> pl.DataFrame:
    """读 v1 镜像。没有镜像就返回空表。"""
    try:
        df = v1mirror.load("regime")
    except Exception:
        return _empty()
    if df.is_empty() or "date" not in df.columns:
        return _empty()
    keep = [c for c, _ in COLUMNS if c in df.columns]
    df = df.select(keep)
    for column, dtype in COLUMNS:
        if column not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(column))
    df = df.select([c for c, _ in COLUMNS])
    # 镜像的 date 是 Date, 自建库存的是字符串 —— 不统一就没法 join
    if df.schema["date"] != pl.Utf8:
        df = df.with_columns(pl.col("date").cast(pl.Utf8))
    return df


def _write_own(df: pl.DataFrame) -> int:
    """覆盖写自建库 (原子替换)。"""
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df = df.sort("date")
    tmp = STORE_PATH.with_suffix(".tmp.parquet")
    df.write_parquet(tmp)
    os.replace(tmp, STORE_PATH)
    return df.height


def last_date(frame: pl.DataFrame | None = None) -> str:
    """已有数据的最后一天 (自建库 + 镜像取最大)。"""
    df = frame if frame is not None else combined()
    if df.is_empty():
        return ""
    return str(df["date"].max())


def combined() -> pl.DataFrame:
    """自建库 + 镜像合并, 同日期以**自建库为准**。

    ⚠️ 不能用 ``unique(subset=["date"], keep="last")``: polars 在非
    maintain_order 下 "last" 是哈希顺序的行, 实测会把旧行留下、新行丢掉。
    这里显式 anti-join 剔除镜像里的重复日期。
    """
    own = own_frame()
    mirror = mirror_frame()
    if mirror.is_empty():
        return own
    if own.is_empty():
        return mirror
    # 镜像行里凡是日期已在自建库的, 整条丢掉
    dup = own.select("date").unique()
    mirror = mirror.join(dup, on="date", how="anti")
    return pl.concat([mirror, own], how="vertical").sort("date")


def build(*, days: int = 7, force: bool = False) -> dict[str, Any]:
    """补算缺失的日期并落盘。返回 ``{written, dates, last_date, source}``。

    默认只补「最后一天之后」; ``days`` 也可以用来**回补**最近 N 个自然日
    (自建库被删/口径改了时用)。
    """
    today = date.today()
    if force or days > 1:
        start = today - timedelta(days=min(days, MAX_BACKFILL_DAYS))
    else:
        have = combined()
        prev = str(have["date"].max()) if not have.is_empty() else ""
        if prev:
            start = date.fromisoformat(prev[:10]) + timedelta(days=1)
        else:
            start = today - timedelta(days=min(days, MAX_BACKFILL_DAYS))
        if start > today:
            return {"written": 0, "dates": [], "last_date": prev, "source": "noop"}

    df = _daily_frame(start, today)
    if df.is_empty():
        return {"written": 0, "dates": [], "last_date": last_date(), "source": "empty"}
    new = aggregate(df, start=start, index_pct=_index_pct_map())
    if new.is_empty():
        return {"written": 0, "dates": [], "last_date": last_date(), "source": "empty"}

    own = own_frame()
    if not own.is_empty():
        # 同日期覆盖: 先剔掉自建库里的旧行, 再拼新的
        dup = new.select("date").unique()
        own = own.join(dup, on="date", how="anti")
        merged = pl.concat([own, new], how="vertical").sort("date")
    else:
        merged = new.sort("date")
    _write_own(merged)
    return {
        "written": new.height,
        "dates": new["date"].to_list(),
        "last_date": str(merged["date"].max()),
        "source": "own",
    }


def history(days: int = 250) -> dict[str, Any]:
    """合并后的时序, 带 ``as_of`` 与 ``source``。前端必须显示数据截止日。"""
    df = combined()
    if df.is_empty():
        return {"rows": [], "as_of": "", "source": "none"}
    tail = df.tail(days) if days > 0 else df
    return {
        "rows": list(tail.iter_rows(named=True)),
        "as_of": str(tail["date"].max()) if not tail.is_empty() else "",
        "source": "own+mirror" if not own_frame().is_empty() else "mirror",
    }


def _above_ma20_ratio() -> tuple[float, int]:
    """用最近日线算「收盘在 MA20 上方」的占比。返回 (占比 0-1, 有效样本数)。"""
    from chanlab import market

    bars = market.recent_bars()
    if bars.is_empty():
        return 0.0, 0
    last = (
        bars.sort(["symbol", "date"])
        .group_by("symbol")
        .agg(
            pl.col("close").tail(20).mean().alias("ma20"),
            pl.col("close").last().alias("close"),
        )
    )
    valid = last.filter(pl.col("ma20") > 0)
    if valid.is_empty():
        return 0.0, 0
    above = int(valid.select((pl.col("close") > pl.col("ma20")).sum()).item())
    return above / valid.height, valid.height


def live() -> dict[str, Any]:
    """盘中实时情绪: 用实时快照算**同一套**指标。

    ⚠️ 这是「此刻」的值, 不是盘后定稿 —— 收盘前它会一直变。返回带
    ``provisional=True``, 前端必须标注, 别让用户拿 10:30 的数当收盘结论。
    """
    from chanlab import market
    from chanlab.index_source import quotes as index_quotes

    rows, meta = market.snapshot_rows()
    ladder = market.limit_up_ladder(limit=1)
    stats = ladder.get("stats") or {}
    if not rows:
        return {"ok": False, "detail": meta.get("detail") or "实时快照不可用"}

    pcts = [
        float(r["change_pct"]) / 100.0
        for r in rows
        if isinstance(r.get("change_pct"), int | float)
    ]
    total = len(pcts)
    up = sum(1 for p in pcts if p > 0)
    down = sum(1 for p in pcts if p < 0)
    strong_up = sum(1 for p in pcts if p >= 0.03)
    strong_down = sum(1 for p in pcts if p <= -0.03)
    ordered = sorted(pcts)
    mid = ordered[total // 2] if total else 0.0

    try:
        idx = index_quotes(["000001.SH"]).get("quotes") or {}
        raw = (idx.get("000001.SH") or {}).get("change_pct")
        index_pct = float(raw) / 100.0 if isinstance(raw, int | float) else 0.0
    except Exception:
        index_pct = 0.0

    above_ratio, valid = _above_ma20_ratio()
    limit_up = int(stats.get("sealed") or 0)
    broken = int(stats.get("broken") or 0)
    metrics = {
        "limit_up": limit_up,
        "limit_down": int(stats.get("limit_down") or 0),
        "broken_limit": broken,
        "max_consecutive": int(stats.get("max_boards") or 0),
        "seal_rate": (limit_up / (limit_up + broken)) if (limit_up + broken) > 0 else 0.5,
        "up_count": up,
        "down_count": down,
        "up_ratio": (up / down) if down > 0 else (float(up) if up > 0 else 1.0),
        "index_pct": index_pct,
        "above_ma20_pct": above_ratio,
        "total_amount": float(sum(float(r.get("amount") or 0.0) for r in rows)),
        "avg_turnover": (
            float(sum(float(r.get("amount") or 0.0) for r in rows)) / total if total else 0.0
        ),
        "up_pct": (up / total * 100) if total else 0.0,
        "down_pct": (down / total * 100) if total else 0.0,
        "avg_pct": (sum(pcts) / total) if total else 0.0,
        "median_pct": mid,
        "strong_up_pct": (strong_up / total * 100) if total else 0.0,
        "strong_down_pct": (strong_down / total * 100) if total else 0.0,
        "strong_diff_pct": (
            (strong_up - strong_down) / total * 100 if total else 0.0
        ),
    }
    # 日线库已经收了今天 -> 实时快照就是收盘价本身, 不算"盘中未定稿"
    settled = str(ladder.get("as_of") or "") == str(date.today())
    state, score = classify_state(metrics)
    sub = subscores(metrics)
    return {
        "ok": True,
        "provisional": not settled,
        "date": str(date.today()),
        "snapshot_date": str(ladder.get("as_of") or ""),
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "score": score,
        "metrics": metrics,
        "subscores": {k: round(v) for k, v in sub.items()},
        "counts": {"total": total, "above_ma20_valid": valid},
        "as_of": meta.get("updated_at"),
        "detail": meta.get("detail") or "",
    }


def latest() -> dict[str, Any]:
    """最后一天的 regime 行 (带中文状态名与子维度)。没有就返回空 dict。"""
    payload = history(days=1)
    rows = payload.get("rows") or []
    if not rows:
        return {}
    row = dict(rows[-1])
    row["state_label"] = STATE_LABELS.get(row.get("state") or "", row.get("state") or "")
    row["as_of"] = payload.get("as_of", "")
    row["source"] = payload.get("source", "")
    return row
