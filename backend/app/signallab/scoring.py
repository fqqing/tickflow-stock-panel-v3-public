"""当日候选打分 — 把台账归因学到的「形态档位 -> 历史收益」搬到今天。

Signal Lab 的前两步回答的是「历史上什么形态的信号更好」(summary/attribution), 这一步
回答「今天选出来的这批票, 谁落到了历史上赚钱的档位」。

做法
----
1. 在**台账**上按分位数切档(quantile_bucket_specs), 统计每档的历史 N 日平均收益 /
   胜率 / 盈亏比 —— 这一步与 ``/api/signallab/attribution`` 完全同口径;
2. 取**今天**该策略选出的候选, 用**同一套切点**把它们分到各自档位;
3. 每只候选的得分 = 各特征所在档位历史平均收益的**样本量加权平均**。

为什么用「样本量加权平均」而不是简单平均: 档位样本量差一个量级时(有的档 300 条,
有的档 25 条), 等权会让小样本档位的噪声和可靠档位一样有话语权。

口径硬约束
----------
- 特征只能是**信号当时已知**的量(:data:`app.signallab.lab.CONTEXT_FEATURES`)。
  ``mfe/mae/ret_*`` 含未来信息, 传进来会得到必然赚钱的假结论。
- 分档切点必须来自台账(``build_bucket_model`` 返回的 edges), 不能在今日数据上重算 ——
  否则「历史上的 Rank 4」和「今天的 Rank 4」不是同一个区间, 归因结论无法迁移。
- 今日形态特征用 enriched 日线重算, 与 ``lab.attach_context_features`` 同定义
  (60 日高低点 / 20 日均量 / 14 日 ATR / MA20)。差异: lab 版在矩阵上按**有效 bar**
  滚动(停牌日不占窗口), 这里用 polars 按行滚动; enriched 日线本身不含停牌日,
  两者等价。窗口不足(次新股)的特征为 null, 该特征不参与打分而不是记 0。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import polars as pl

from app.signallab.lab import CONTEXT_FEATURES
from app.signallab.outcome import ret_column
from app.signallab.summary import add_feature_buckets, quantile_bucket_specs, summarize_outcomes

logger = logging.getLogger(__name__)

#: 特征的中文标签(前端展示用)
FEATURE_LABELS: dict[str, str] = {
    "ctx_drawdown_from_high": "距60日高点回撤",
    "ctx_rally_from_low": "距60日低点涨幅",
    "ctx_vol_ratio": "20日量比",
    "ctx_atr_pct": "14日ATR%",
    "ctx_ma_bias": "MA20乖离",
}

#: 取多少**交易日**的 enriched 历史来算今日形态特征(60 日窗口 + 余量)
FEATURE_LOOKBACK_DAYS = 80

_OHLCV = {"symbol", "date", "open", "high", "low", "close", "volume"}


def _f(value: Any) -> float | None:
    """polars -> JSON: null/NaN/inf 一律转 None。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


# ---------------------------------------------------------------------------
# 今日形态特征
# ---------------------------------------------------------------------------

def context_features_for(hist: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """给当日截面补形态特征列, 返回 [symbol, *CONTEXT_FEATURES]。

    hist 是 enriched 日线(含 symbol/date/OHLCV), 覆盖到 as_of 为止。
    数据缺列 / 当日无行时返回空 DataFrame —— 宁可没特征也不要错行。
    """
    if hist.is_empty() or not _OHLCV.issubset(set(hist.columns)):
        return pl.DataFrame()

    df = hist.sort(["symbol", "date"])
    prev_close = pl.col("close").shift(1).over("symbol")
    true_range = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - prev_close).abs(),
        (pl.col("low") - prev_close).abs(),
    )
    df = df.with_columns([
        (pl.col("close") / pl.col("high").rolling_max(60).over("symbol") - 1.0)
        .alias("ctx_drawdown_from_high"),
        (pl.col("close") / pl.col("low").rolling_min(60).over("symbol") - 1.0)
        .alias("ctx_rally_from_low"),
        (pl.col("volume") / pl.col("volume").rolling_mean(20).over("symbol"))
        .alias("ctx_vol_ratio"),
        (true_range.rolling_mean(14).over("symbol") / pl.col("close"))
        .alias("ctx_atr_pct"),
        (pl.col("close") / pl.col("close").rolling_mean(20).over("symbol") - 1.0)
        .alias("ctx_ma_bias"),
    ])

    target = df.filter(pl.col("date") == as_of)
    if target.is_empty() and df["date"].dtype != pl.Date:
        # enriched 的 date 可能是字符串 / datetime(分钟档), 统一按 YYYY-MM-DD 前缀匹配
        target = df.filter(
            pl.col("date").cast(pl.Utf8).str.slice(0, 10) == as_of.isoformat()
        )
    return target.select(["symbol", *CONTEXT_FEATURES])


# ---------------------------------------------------------------------------
# 台账 -> 档位模型
# ---------------------------------------------------------------------------

def build_bucket_model(
    ledger: pl.DataFrame,
    features: list[str],
    horizon: int,
    *,
    buckets: int = 4,
    min_samples: int = 20,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[float]]]:
    """在台账上学「档位 -> 历史表现」, 返回 (model, specs)。

    model: {feature: {"label", "edges", "buckets": [{bucket, n, win_rate, mean,
            profit_factor}, ...]}} —— buckets 按历史平均收益降序。
    specs: {feature: [切点]} —— **打分时必须复用**, 否则今日档位与历史档位不同义。
    """
    if ledger.is_empty():
        return {}, {}
    column = ret_column(horizon)
    if column not in ledger.columns:
        raise ValueError(f"台账缺少收益列 {column}")

    usable = [f for f in features if f in ledger.columns]
    numeric = [
        f for f in usable
        if ledger[f].dtype.is_numeric() and ledger[f].dtype != pl.Boolean
    ]
    specs = quantile_bucket_specs(ledger, numeric, buckets=buckets)
    frame, used = add_feature_buckets(ledger, usable, specs=specs, buckets=buckets)

    n_key, mean_key = f"ret{horizon}_n", f"ret{horizon}_mean"
    win_key, pf_key = f"ret{horizon}_win_rate", f"ret{horizon}_profit_factor"

    model: dict[str, dict[str, Any]] = {}
    for feature in usable:
        bucket_column = f"{feature}_bucket"
        if bucket_column not in frame.columns:
            continue
        grouped = summarize_outcomes(
            frame.filter(pl.col(bucket_column).is_not_null()),
            horizons=[horizon],
            group_by=[bucket_column],
            with_excess=False,
            with_path=False,
        )
        if n_key not in grouped.columns:
            continue
        kept = grouped.filter(pl.col(n_key) >= min_samples)
        if kept.is_empty():
            continue
        model[feature] = {
            "label": FEATURE_LABELS.get(feature, feature),
            "edges": used.get(feature, []),
            "buckets": [
                {
                    "bucket": row[bucket_column],
                    "n": int(row[n_key] or 0),
                    "win_rate": _f(row.get(win_key)),
                    "mean": _f(row.get(mean_key)),
                    "profit_factor": _f(row.get(pf_key)),
                }
                for row in kept.sort(mean_key, descending=True).to_dicts()
            ],
        }
    return model, used


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------

def score_candidates(
    frame: pl.DataFrame,
    model: dict[str, dict[str, Any]],
    specs: dict[str, list[float]],
    features: list[str],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """给候选打分排序。

    frame 需含 symbol 与各特征列(缺失的特征列会被跳过)。返回按 score 降序的
    dict 列表, 每条形如::

        {"symbol", "score", "n_features", "buckets": {feature: 档位},
         "reasons": [{feature, label, bucket, mean, win_rate, n}, ...]}
    """
    if frame.is_empty() or not model:
        return []
    usable = [f for f in features if f in frame.columns and f in model]
    if not usable:
        return []
    bucketed, _ = add_feature_buckets(frame, usable, specs=specs)

    rows: list[dict[str, Any]] = []
    for row in bucketed.to_dicts():
        buckets: dict[str, str] = {}
        reasons: list[dict[str, Any]] = []
        weighted, weight = 0.0, 0.0
        for feature in usable:
            bucket = row.get(f"{feature}_bucket")
            if bucket is None:
                continue
            hit = next(
                (b for b in model[feature]["buckets"] if b["bucket"] == bucket), None
            )
            if hit is None or hit["mean"] is None:
                continue
            buckets[feature] = bucket
            reasons.append({
                "feature": feature,
                "label": model[feature]["label"],
                "bucket": bucket,
                "mean": hit["mean"],
                "win_rate": hit["win_rate"],
                "n": hit["n"],
            })
            w = max(hit["n"], 1)
            weighted += hit["mean"] * w
            weight += w
        if weight <= 0:
            continue
        rows.append({
            "symbol": row.get("symbol"),
            "score": round(weighted / weight, 6),
            "n_features": len(reasons),
            "buckets": buckets,
            "reasons": sorted(reasons, key=lambda item: -item["n"]),
        })

    rows.sort(key=lambda item: item["score"], reverse=True)
    return rows[:limit] if limit > 0 else rows


def score_today(
    repo: Any,
    engine: Any,
    data_dir: Any,
    ledger: pl.DataFrame,
    strategy_id: str,
    as_of: date,
    *,
    horizon: int,
    features: list[str] | None = None,
    buckets: int = 4,
    min_samples: int = 20,
    limit: int = 50,
) -> dict[str, Any]:
    """端到端: 台账学档位 -> 取今日候选 -> 打分排序。

    返回 {as_of, horizon, features, model, rows, n_candidates, n_scored, notes}。
    """
    from app.services.screener import ScreenerService
    from app.strategy import config as strategy_config

    wanted = list(features) if features else list(CONTEXT_FEATURES)
    model, specs = build_bucket_model(
        ledger, wanted, horizon, buckets=buckets, min_samples=min_samples
    )
    out: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "horizon": horizon,
        "features": list(model),
        "model": model,
        "rows": [],
        "n_candidates": 0,
        "n_scored": 0,
        "notes": [],
    }
    if not model:
        out["notes"].append(
            f"台账在 {horizon} 日持有期下没有样本量 >= {min_samples} 的档位, 无法打分 "
            "(可放宽 min_samples 或换一个持有期)"
        )
        return out

    # 1) 今日候选 (口径与 /api/screener/run_preset 一致)
    if engine is None:
        raise RuntimeError("策略引擎未初始化")
    svc = ScreenerService(repo, asset_type="stock", market="cn")
    overrides = strategy_config.load_override(data_dir, strategy_id) or {}
    params = dict(overrides.get("params") or {})
    context = svc.build_strategy_context(
        engine,
        as_of,
        [strategy_id],
        timeframe="1d",
        params_map={strategy_id: params},
        overrides_map={strategy_id: overrides},
    )
    result = engine.run(strategy_id, context, params=params, overrides=overrides or None)
    candidates = [dict(row) for row in (getattr(result, "rows", None) or [])]
    out["n_candidates"] = len(candidates)
    if not candidates:
        out["notes"].append(f"{as_of} 该策略没有选出个股")
        return out

    # 2) 今日形态特征
    hist = repo.get_enriched_history(as_of, FEATURE_LOOKBACK_DAYS)
    frame = pl.DataFrame(candidates)
    if hist is not None and not hist.is_empty():
        ctx = context_features_for(hist, as_of)
        if not ctx.is_empty():
            keep = [c for c in frame.columns if c not in CONTEXT_FEATURES]
            frame = frame.select(keep).join(ctx, on="symbol", how="left")
        else:
            out["notes"].append(f"enriched 历史里没有 {as_of} 的行, 形态特征缺失")
    else:
        out["notes"].append("enriched 历史缓存不可用, 形态特征缺失")

    # 3) 打分
    scored = score_candidates(frame, model, specs, list(model), limit=limit)
    by_symbol = {str(row.get("symbol")): row for row in candidates}
    for item in scored:
        src = by_symbol.get(str(item["symbol"]), {})
        item["name"] = src.get("name")
        item["close"] = _f(src.get("close"))
        item["change_pct"] = _f(src.get("change_pct"))
    out["rows"] = scored
    out["n_scored"] = len(scored)
    if len(scored) < len(candidates):
        out["notes"].append(
            f"{len(candidates) - len(scored)} 只候选因形态特征不全(次新股/窗口不足)未参与打分"
        )
    return out
