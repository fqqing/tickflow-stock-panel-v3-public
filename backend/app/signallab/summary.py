"""信号台账的统计汇总与分桶归因。

输入是 ``outcome.build_signal_outcomes`` 产出的事件台账(每行一条信号), 输出:

1. ``summarize_outcomes`` — 整体战绩: 胜率, 盈亏比, 期望收益, 最大浮盈浮亏分布。
2. ``attribute_outcomes`` — 按形态特征分桶, 找出「哪些样本赚钱, 哪些样本亏钱」。
   这是需求 2(AI 提炼形态共性)与需求 3(剔除低胜率分支)共同依赖的事实基础。

统计口径的硬约束
----------------
- null 收益(停牌 / 数据缺失 / 前瞻窗口被截断)一律**不计入分母**, 也不按 0 补齐。
  把它算成 0 会把「没观测到」当成「收益 0%」, 系统性低估策略。
- 胜率的分母是「有收益观测的样本数」而不是总信号数, 两列都输出以便核对。
- ``profit_factor`` = 盈利总和 / 亏损绝对值总和; 完全没有亏损样本时返回 null 而不是
  无穷大(无穷大在排序里会永远排第一, 是典型的坑)。
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import polars as pl

from app.signallab.outcome import excess_column, ret_column

# 数值型特征默认的分桶数量(按分位数切, 保证每桶样本量可比)。
_DEFAULT_BUCKETS = 4


def _return_exprs(
    horizon: int,
    *,
    column: str,
    prefix: str,
    with_path: bool = True,
) -> list[pl.Expr]:
    """某一持有期收益列的一组聚合表达式。"""
    value = pl.col(column)
    observable = value.is_not_null() & value.is_finite()
    clean = pl.when(observable).then(value).otherwise(None)
    gains = pl.when(clean > 0).then(clean).otherwise(0.0).sum()
    losses = pl.when(clean < 0).then(-clean).otherwise(0.0).sum()
    wins = (clean > 0).sum()
    observed = observable.sum()
    result = [
        observed.alias(f"{prefix}_n"),
        wins.alias(f"{prefix}_wins"),
        (wins / observed).alias(f"{prefix}_win_rate"),
        clean.mean().alias(f"{prefix}_mean"),
        clean.median().alias(f"{prefix}_median"),
        clean.std().alias(f"{prefix}_std"),
        pl.when(losses > 0).then(gains / losses).otherwise(None).alias(f"{prefix}_profit_factor"),
    ]
    if with_path:
        # 期望收益 / 平均最大浮亏: 每承受 1 单位浮亏能换来多少收益, 用来横向比较
        # 采用不同止损位的策略。
        result.append(
            pl.when(pl.col("mae").abs().mean() > 0)
            .then(clean.mean() / pl.col("mae").abs().mean())
            .otherwise(None)
            .alias(f"{prefix}_return_per_drawdown")
        )
    return result


def _path_exprs() -> list[pl.Expr]:
    """持有路径相关的聚合: 最大浮盈浮亏与「先涨后跌」占比。"""
    mfe = pl.col("mfe")
    mae = pl.col("mae")
    return [
        mfe.mean().alias("mfe_mean"),
        mfe.median().alias("mfe_median"),
        mae.mean().alias("mae_mean"),
        mae.median().alias("mae_median"),
        # 最大浮盈出现在最大浮亏之后 = 扛过浮亏才赚到钱, 这类形态对止损很敏感。
        (pl.col("mae_bar") < pl.col("mfe_bar")).mean().alias("recover_first_ratio"),
    ]


def summarize_outcomes(
    frame: pl.DataFrame,
    horizons: Sequence[int] = (1, 3, 5, 10, 20, 60),
    *,
    group_by: Sequence[str] | None = None,
    with_excess: bool = True,
    with_path: bool = True,
) -> pl.DataFrame:
    """汇总信号台账。

    Args:
        frame: 事件台账, 每行一条信号。
        horizons: 要统计的持有期; 台账里没有对应列则跳过。
        group_by: 分组列(如 strategy_id, entry_signal_id, 形态档位)。
        with_excess: 是否同时统计相对全市场基准的超额收益。
        with_path: 是否统计持有路径(MFE/MAE)。
    """
    if frame.is_empty():
        return pl.DataFrame()

    expressions: list[pl.Expr] = [pl.len().alias("n_signals")]
    if "filled" in frame.columns:
        expressions.append(pl.col("filled").sum().alias("n_filled"))
    if "truncated" in frame.columns:
        expressions.append(pl.col("truncated").sum().alias("n_truncated"))
    has_path = "mae" in frame.columns
    for horizon in horizons:
        ret_col = ret_column(horizon)
        if ret_col in frame.columns:
            expressions.extend(
                _return_exprs(horizon, column=ret_col, prefix=f"ret{horizon}", with_path=has_path)
            )
        if with_excess:
            exc_col = excess_column(horizon)
            if exc_col in frame.columns:
                expressions.extend(
                    _return_exprs(
                        horizon, column=exc_col, prefix=f"exc{horizon}", with_path=has_path
                    )
                )
    if with_path and {"mfe", "mae", "mae_bar", "mfe_bar"}.issubset(set(frame.columns)):
        expressions.extend(_path_exprs())

    builder = frame.group_by(list(group_by)) if group_by else frame.select([])
    result = builder.agg(expressions) if group_by else frame.select(expressions)
    if group_by:
        result = result.sort(list(group_by))
    return result


def quantile_bucket_specs(
    frame: pl.DataFrame,
    columns: Sequence[str],
    *,
    buckets: int = _DEFAULT_BUCKETS,
) -> dict[str, list[float]]:
    """为数值型特征计算分位切点, 供 ``categorize_features`` 使用。

    返回 {列名: [q1, q2, ...]} 升序切点。样本不足或取值退化的列不会出现在结果里,
    调用方据此跳过这些列。
    """
    if buckets < 2:
        raise ValueError("buckets 至少为 2")
    available = [c for c in columns if c in frame.columns and frame[c].dtype.is_numeric()]
    specs: dict[str, list[float]] = {}
    for column in available:
        values = frame[column].drop_nulls().to_numpy().astype(float)
        values = values[np.isfinite(values)]
        if values.size < buckets * 2:
            continue
        edges = np.quantile(values, np.linspace(0.0, 1.0, buckets + 1)[1:-1])
        edges = np.unique(np.round(edges, 8))
        # 去重后只剩一个切点说明取值高度集中, 分桶没有信息量。
        if edges.size < 1:
            continue
        specs[column] = [float(e) for e in edges]
    return specs


def add_feature_buckets(
    frame: pl.DataFrame,
    columns: Sequence[str],
    *,
    specs: dict[str, list[float]] | None = None,
    buckets: int = _DEFAULT_BUCKETS,
) -> tuple[pl.DataFrame, dict[str, list[float]]]:
    """给台账补上形态分档列 ``{列}_bucket``。

    数值列按分位数切成有序档(Rank 1 到 Rank N); 布尔列与非数值列直接按取值分组。
    返回 (新 DataFrame, 实际使用的切点)。
    """
    if frame.is_empty():
        return frame, {}
    targets = [c for c in columns if c in frame.columns]
    if not targets:
        return frame, {}

    numeric = [
        c for c in targets
        if frame[c].dtype.is_numeric() and frame[c].dtype != pl.Boolean
    ]
    effective = specs if specs is not None else quantile_bucket_specs(frame, numeric, buckets=buckets)
    expressions: list[pl.Expr] = []
    for column in numeric:
        edges = effective.get(column)
        if not edges:
            continue
        expr = pl.lit("Rank 1", dtype=pl.Utf8)
        for i, edge in enumerate(edges):
            expr = pl.when(pl.col(column) > edge).then(pl.lit(f"Rank {i + 2}")).otherwise(expr)
        name = f"{column}_bucket"
        expressions.append(
            pl.when(pl.col(column).is_null())
            .then(None)
            .otherwise(expr)
            .alias(name)
        )
    for column in targets:
        if column in numeric:
            continue
        expressions.append(pl.col(column).cast(pl.Utf8).alias(f"{column}_bucket"))
    if not expressions:
        return frame, effective
    result = frame.with_columns(expressions)
    return result, effective


def attribute_outcomes(
    frame: pl.DataFrame,
    features: Sequence[str],
    horizon: int,
    *,
    column: str | None = None,
    buckets: int = _DEFAULT_BUCKETS,
    min_samples: int = 20,
    top_n: int = 0,
) -> pl.DataFrame:
    """按形态特征分桶统计某一持有期的表现, 并按期望收益排序。

    Args:
        frame: 事件台账(需先由 ``add_feature_buckets`` 或外部逻辑补好形态列)。
        features: 形态特征列名。**必须全部是信号当时就已知的量**(位置, 量能, 中枢结构等),
            严禁传入含未来信息的列, 否则归因结论必然是假的。
        horizon: 统计的持有期。
        column: 收益列名, 默认 ``ret_{horizon}d``。
        buckets: 数值型特征的切分桶数。
        min_samples: 样本量不足的桶会被剔除(小样本的分位数没有意义)。
        top_n: 大于 0 时只保留每特征期望收益最高/最低的若干档。
    """
    if frame.is_empty():
        return pl.DataFrame()
    target = column or ret_column(horizon)
    if target not in frame.columns:
        raise ValueError(f"台账缺少收益列 {target}")

    bucketed, _ = add_feature_buckets(frame, features, buckets=buckets)
    bucket_columns = [f"{c}_bucket" for c in features if f"{c}_bucket" in bucketed.columns]
    if not bucket_columns:
        return pl.DataFrame()

    frames: list[pl.DataFrame] = []
    for raw, bucket_column in zip(features, bucket_columns, strict=False):
        layer = bucketed.filter(pl.col(bucket_column).is_not_null()).group_by(bucket_column).agg(
            _return_exprs(
                horizon, column=target, prefix="ret", with_path="mae" in frame.columns
            )
        )
        enough = layer.filter(pl.col("ret_n") >= min_samples)
        if enough.is_empty():
            continue
        spread = pl.col("ret_mean").max() - pl.col("ret_mean").min()
        summary = enough.sort("ret_mean", descending=True).with_columns(
            pl.lit(raw).alias("feature"),
            pl.col(bucket_column).alias("bucket"),
            pl.len().alias("n_buckets"),
            spread.alias("mean_spread"),
        )
        if top_n and summary.height > top_n * 2:
            summary = pl.concat([summary.head(top_n), summary.tail(top_n)])
        frames.append(summary.select(
            ["feature", "bucket", "n_buckets", "mean_spread"]
            + [c for c in summary.columns if c.startswith("ret")]
        ))
    if not frames:
        return pl.DataFrame()
    result = pl.concat(frames, how="diagonal_relaxed")
    return result.sort(["feature", "ret_mean"], descending=[False, True])
