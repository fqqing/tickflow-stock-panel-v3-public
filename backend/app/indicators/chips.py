"""筹码分布(成本分布) —— 三角分布 + 换手衰减模型。

为什么不用逐笔:
  M6 已能落逐笔 ``data/ticks/<date>/``, 但只覆盖最近 2~5 个交易日(见 MEMORY-DETAIL
  「历史分钟/逐笔无法回补」), 而筹码分布要看的是**几个月到几年的持仓成本堆积**,
  逐笔样本根本不够。所以走日K近似 —— 这是所有免费行情软件(含通达信/东财)的
  通用做法。

模型
====
1. 每根 K 线的成交量按**三角形**摊到 ``[low, high]``: 峰在当日均价
   ``amount / (volume * 100)``(amount 元 / volume 手), 面积 = 当日成交量。
   一字形(low == high, 一字板)时全部压在该价位。
2. 历史筹码按当日换手率衰减: ``chips *= (1 - turnover * decay)``。
   换手 100% 意味着流通盘全换一遍, 老筹码清零 —— 这就是衰减系数的物理含义。
   ``decay=1`` 即经典模型; 想让老筹码消散得慢一点可调小(0.5~0.8)。

局限(写清楚, 免得后面误用):
  - 日内成交价分布是**假设**的三角形, 不是真实成交明细;
  - 换手率用 enriched 的 ``turnover_rate``(百分数), 缺失时退回
    ``volume * 100 / float_shares``; 两者都没有时按 0 处理(不衰减)。
"""
from __future__ import annotations

import math

import numpy as np
import polars as pl

#: 默认价格档数 —— 60 档在 1080p 上每档约 10px, 既能看出峰谷又不至于噪声太多
_DEFAULT_BINS = 60

_REQUIRED = ("high", "low", "close", "volume")


def _turnover_series(df: pl.DataFrame, float_shares: float | None) -> np.ndarray:
    """每根 K 线的换手率(小数)。turnover_rate 优先, 否则用流通股本折算。"""
    n = df.height
    if "turnover_rate" in df.columns:
        vals = df["turnover_rate"].to_list()
        out = np.array(
            [float(v) / 100.0 if v is not None and math.isfinite(float(v)) else 0.0 for v in vals],
            dtype=np.float64,
        )
        # enriched 偶尔整列为空(早期分区没算换手), 全 0 时退回股本折算
        if out.sum() > 0:
            return np.clip(out, 0.0, 1.0)
    if float_shares and float_shares > 0 and "volume" in df.columns:
        vol = np.array(
            [float(v) if v is not None else 0.0 for v in df["volume"].to_list()], dtype=np.float64
        )
        # volume 单位「手」-> 股: x100
        return np.clip(vol * 100.0 / float(float_shares), 0.0, 1.0)
    return np.zeros(n, dtype=np.float64)


def _avg_price_series(df: pl.DataFrame) -> np.ndarray:
    """当日均价: amount/(volume*100) 优先, 否则 (h+l+c)/3。"""
    if "amount" in df.columns and "volume" in df.columns:
        amt = np.array(
            [float(v) if v is not None and math.isfinite(float(v)) else 0.0
             for v in df["amount"].to_list()], dtype=np.float64,
        )
        vol = np.array(
            [float(v) if v is not None and math.isfinite(float(v)) else 0.0
             for v in df["volume"].to_list()], dtype=np.float64,
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            avg = np.where(vol > 0, amt / (vol * 100.0), np.nan)
        if np.isfinite(avg).any():
            return avg
    return np.array(
        [(float(h) + float(lo) + float(c)) / 3.0 for h, lo, c in
         zip(df["high"].to_list(), df["low"].to_list(), df["close"].to_list(), strict=False)],
        dtype=np.float64,
    )


def _triangle_weights(lo: float, hi: float, mid: float, centers: np.ndarray) -> np.ndarray:
    """以 mid 为峰、[lo, hi] 为底的三角形权重(面积归一)。mid 越界时退化为均匀分布。"""
    w = np.zeros_like(centers)
    if hi <= lo:  # 一字形: 全部压在最近的那一档
        idx = int(np.argmin(np.abs(centers - lo)))
        w[idx] = 1.0
        return w
    mid = min(max(mid, lo), hi)
    # ⚠️ 必须用区间 && 夹取: 只写 `centers <= mid` 会让 lo 左侧的档拿到**负权重**
    # (实测: 当日区间 20.0~20.5 时, 10 元档被算成 -39.5), 归一化后筹码被摊平到
    # 整个价格区间 —— 表现为「所有档占比相等」的假象。
    left = (centers >= lo) & (centers <= mid)
    right = (centers > mid) & (centers <= hi)
    if mid > lo:
        w[left] = (centers[left] - lo) / (mid - lo)
    if hi > mid:
        w[right] = (hi - centers[right]) / (hi - mid)
    total = w.sum()
    if total <= 0:  # 极端情况(区间内一档都没有) -> 均匀摊到全部档
        return np.full_like(centers, 1.0 / centers.size)
    return w / total


def compute_chips(
    df: pl.DataFrame,
    *,
    bins: int = _DEFAULT_BINS,
    decay: float = 1.0,
    float_shares: float | None = None,
) -> dict:
    """按日K近似计算当前筹码分布。

    返回 ``{bins: [{price, ratio}], step, low, high, close, avg_cost, profit_ratio,
    peak_price, concentration: {p70, p90}, total_volume, dates}``。
    数据不足(空表 / 缺列 / 价格区间退化)时返回 ``{"ok": False, "reason": ...}`` 之外
    的字段仍给全(前端容错), 调用方按 bins 是否为空判断。
    """
    empty = {
        "bins": [], "step": 0.0, "low": None, "high": None, "close": None,
        "avg_cost": None, "profit_ratio": None, "peak_price": None,
        "concentration": {"p70": None, "p90": None}, "total_volume": 0.0,
        "dates": [], "ok": False,
    }
    if df.is_empty() or not all(c in df.columns for c in _REQUIRED):
        return empty

    work = df.sort("date") if "date" in df.columns else df
    highs = np.array([float(v) if v is not None else np.nan for v in work["high"].to_list()])
    lows = np.array([float(v) if v is not None else np.nan for v in work["low"].to_list()])
    closes = np.array([float(v) if v is not None else np.nan for v in work["close"].to_list()])
    vols = np.array([float(v) if v is not None else 0.0 for v in work["volume"].to_list()])

    ok_mask = np.isfinite(highs) & np.isfinite(lows) & np.isfinite(closes)
    if not ok_mask.any():
        return empty
    lo = float(np.nanmin(lows[ok_mask]))
    hi = float(np.nanmax(highs[ok_mask]))
    if not math.isfinite(lo) or not math.isfinite(hi) or hi < lo:
        return empty
    if hi == lo:
        # 全区间一字形(罕见): 给一个极小的跨度, 保证有档位可画
        hi = lo + max(lo * 1e-4, 0.01)

    n_bins = max(10, min(int(bins), 400))
    edges = np.linspace(lo, hi, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    step = float((hi - lo) / n_bins)

    avgs = _avg_price_series(work)
    turns = _turnover_series(work, float_shares)

    chips = np.zeros(n_bins, dtype=np.float64)
    for i in range(work.height):
        if not ok_mask[i]:
            continue
        day_lo, day_hi = float(lows[i]), float(highs[i])
        day_avg = float(avgs[i]) if np.isfinite(avgs[i]) else float(closes[i])
        # 当日均价可能落在 [low, high] 之外(数据源口径问题), 夹回区间内
        day_avg = min(max(day_avg, day_lo), day_hi)
        w = _triangle_weights(day_lo, day_hi, day_avg, centers)
        chips *= max(0.0, 1.0 - float(turns[i]) * decay)
        chips += float(vols[i]) * w

    total = float(chips.sum())
    if total <= 0:
        return empty

    ratio = chips / total
    close = float(closes[ok_mask][-1])
    avg_cost = float(np.sum(centers * chips) / total)
    below = centers < close
    profit_ratio = float(chips[below].sum() / total)
    peak_idx = int(np.argmax(chips))
    peak_price = float(centers[peak_idx])

    # 集中度: 按筹码量从大到小累加, 覆盖 70% / 90% 筹码所需的价格区间
    order = np.argsort(chips)[::-1]
    cum = np.cumsum(chips[order]) / total

    def _range_for(target: float) -> list[float] | None:
        take = order[: int(np.searchsorted(cum, target)) + 1]
        if take.size == 0:
            return None
        return [round(float(centers[take].min()), 4), round(float(centers[take].max()), 4)]

    dates = (
        [str(d)[:10] for d in work["date"].to_list()] if "date" in work.columns else []
    )
    return {
        "bins": [
            {"price": round(float(p), 4), "ratio": round(float(r), 6)}
            for p, r in zip(centers, ratio, strict=False)
        ],
        "step": round(step, 6),
        "low": round(lo, 4),
        "high": round(hi, 4),
        "close": round(close, 4),
        "avg_cost": round(avg_cost, 4),
        "profit_ratio": round(profit_ratio, 6),
        "peak_price": round(peak_price, 4),
        "concentration": {"p70": _range_for(0.70), "p90": _range_for(0.90)},
        "total_volume": round(total, 2),
        "dates": dates,
        "ok": True,
    }
