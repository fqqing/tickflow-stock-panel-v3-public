"""策略评分字段解析。"""
from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from typing import Any

import polars as pl

from app.factors import gtja191, ops_pl

logger = logging.getLogger(__name__)

SCORING_DIRECTION_HIGH = "high"
SCORING_DIRECTION_LOW = "low"
SCORING_DIRECTIONS = frozenset({SCORING_DIRECTION_HIGH, SCORING_DIRECTION_LOW})

#: 声明式公式因子注册表 (GTJA Alpha191)。依赖与预热天数由表达式树自动推导,
#: 新增因子只要写进 ``app/factors/gtja191.py`` 就会自动接上评分与回测两条路径。
GTJA_FACTOR_DEFS: dict[str, Any] = dict(gtja191.SKELETON_BY_ID)


def gtja_plan(name: str) -> ops_pl.Plan | None:
    """公式化因子 → Polars 编译计划; 非公式因子返回 ``None``。

    返回 :class:`~app.factors.ops_pl.Plan` 而不是 ``pl.Expr``, 因为这类因子
    常常需要「先做时序变换、再按日排名」, 必须靠中间列拆开嵌套 ``.over()``
    (详见 ``ops_pl`` 模块顶部)。
    """
    definition = GTJA_FACTOR_DEFS.get(str(name))
    return ops_pl.evaluate(definition.expr) if definition is not None else None


VIRTUAL_SCORING_DEPENDENCIES: dict[str, frozenset[str]] = {
    **{
        f"ma{period}_bias": frozenset({"close", f"ma{period}"})
        for period in (5, 10, 20, 30, 60)
    },
    **{
        f"ema{period}_bias": frozenset({"close", f"ema{period}"})
        for period in (5, 10, 20, 30, 60)
    },
    "macd_dif_pct": frozenset({"close", "macd_dif"}),
    "macd_dea_pct": frozenset({"close", "macd_dea"}),
    "macd_hist_pct": frozenset({"close", "macd_hist"}),
    "boll_position": frozenset({"close", "boll_upper", "boll_lower"}),
    "atr_pct": frozenset({"close", "atr_14"}),
    "boll_width": frozenset({"ma20", "boll_upper", "boll_lower"}),
    "vol_ratio_10d": frozenset({"volume"}),
    "vol_trend_5_10": frozenset({"vol_ma5", "vol_ma10"}),
    "turnover_ratio_5d": frozenset({"turnover_rate"}),
    "log_amount": frozenset({"amount"}),
    "amount_ratio_5d": frozenset({"amount"}),
    "gap_return": frozenset({"open", "prev_close"}),
    "intraday_return": frozenset({"open", "close"}),
    "close_position": frozenset({"high", "low", "close"}),
    "distance_to_high_60d": frozenset({"close", "high_60d"}),
    "distance_from_low_60d": frozenset({"close", "low_60d"}),
    "max_ret_20d": frozenset({"close"}),
    "ret_skew_20d": frozenset({"close"}),
    "up_days_20d": frozenset({"close"}),
    "amihud_20d": frozenset({"close", "amount"}),
    "turnover_z_60d": frozenset({"turnover_rate"}),
    "vol_price_corr_20d": frozenset({"close", "volume"}),
    "vwap_bias": frozenset({"close", "volume", "amount"}),
    "vol_trend_5_60": frozenset({"volume"}),
    "limit_up_count_20d": frozenset({"consecutive_limit_ups"}),
    "limit_up_count_60d": frozenset({"consecutive_limit_ups"}),
    # Alpha191 公式因子: 依赖由表达式树推导 (例如 gtja013 依赖 high/low/amount/volume)
    **{
        factor_id: definition.dependency_fields
        for factor_id, definition in GTJA_FACTOR_DEFS.items()
    },
}

_ROLLING_SCORING_WARMUP: dict[str, int] = {
    "vol_ratio_10d": 11,
    "turnover_ratio_5d": 6,
    "amount_ratio_5d": 6,
    "max_ret_20d": 21,
    "ret_skew_20d": 21,
    "up_days_20d": 21,
    "amihud_20d": 21,
    "turnover_z_60d": 61,
    "vol_price_corr_20d": 21,
    "vol_trend_5_60": 60,
    "limit_up_count_20d": 21,
    "limit_up_count_60d": 61,
    # Alpha191 公式因子: 预热天数按嵌套窗口累加 (见 ir.FactorDef.warmup)
    **{
        factor_id: definition.warmup
        for factor_id, definition in GTJA_FACTOR_DEFS.items()
    },
}


def effective_scoring(
    defaults: Mapping[str, Any] | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """解析有效评分；新配置可完整替换，历史配置保持局部覆盖。"""
    override_values = (overrides or {}).get("scoring")
    if (overrides or {}).get("scoring_replace") is True:
        return dict(override_values) if isinstance(override_values, Mapping) else {}
    scoring = dict(defaults or {})
    if isinstance(override_values, Mapping):
        scoring.update(override_values)
    return scoring


def effective_scoring_directions(overrides: Mapping[str, Any] | None) -> dict[str, str]:
    values = (overrides or {}).get("scoring_directions")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(name): str(direction)
        for name, direction in values.items()
        if direction in SCORING_DIRECTIONS
    }


def scoring_warmup_bars(scoring: Mapping[str, Any]) -> int:
    return max(
        (_ROLLING_SCORING_WARMUP.get(str(name), 1) for name, weight in scoring.items() if weight),
        default=1,
    )


def scoring_dependencies(scoring: Mapping[str, Any]) -> set[str]:
    """把受控虚拟评分字段展开为实际数据依赖。"""
    dependencies: set[str] = set()
    for name, weight in scoring.items():
        if not weight:
            continue
        dependencies.update(VIRTUAL_SCORING_DEPENDENCIES.get(str(name), {str(name)}))
    return dependencies


def scoring_value_expr(columns: Collection[str], name: str) -> pl.Expr | None:
    """返回评分值表达式；依赖不完整时返回 None。"""
    available = set(columns)
    if name in available:
        return pl.col(name)
    dependencies = VIRTUAL_SCORING_DEPENDENCIES.get(name)
    if dependencies is None or not dependencies.issubset(available):
        return None
    if name in GTJA_FACTOR_DEFS:
        # 多阶段公式因子: 单个 pl.Expr 表达不了嵌套 .over(), 必须先由
        # materialize_scoring_columns 落成真实列。走到这里说明调用方漏了物化,
        # 直接返回 None 会让评分静默丢因子, 所以至少留下告警。
        logger.warning(
            "公式因子 %s 尚未物化, 已从评分中跳过; "
            "请先调用 materialize_scoring_columns(panel, {%r})",
            name,
            name,
        )
        return None
    if name.startswith("ma") and name.endswith("_bias"):
        period = name.removeprefix("ma").removesuffix("_bias")
        if period.isdigit():
            return _relative(pl.col("close"), pl.col(f"ma{period}"))
    if name.startswith("ema") and name.endswith("_bias"):
        period = name.removeprefix("ema").removesuffix("_bias")
        if period.isdigit():
            return _relative(pl.col("close"), pl.col(f"ema{period}"))
    if name in {"macd_dif_pct", "macd_dea_pct", "macd_hist_pct"}:
        source = name.removesuffix("_pct")
        return _ratio(pl.col(source), pl.col("close"))
    if name == "atr_pct":
        return _ratio(pl.col("atr_14"), pl.col("close"))
    if name == "boll_position":
        return _ratio(
            pl.col("close") - pl.col("boll_lower"),
            pl.col("boll_upper") - pl.col("boll_lower"),
        )
    if name == "boll_width":
        return _ratio(pl.col("boll_upper") - pl.col("boll_lower"), pl.col("ma20"))
    if name == "vol_ratio_10d":
        return _ratio(
            pl.col("volume"),
            pl.col("volume").shift(1).rolling_mean(10).over("symbol"),
        )
    if name == "vol_trend_5_10":
        return _relative(pl.col("vol_ma5"), pl.col("vol_ma10"))
    if name == "turnover_ratio_5d":
        return _relative(
            pl.col("turnover_rate"),
            pl.col("turnover_rate").shift(1).rolling_mean(5).over("symbol"),
        )
    if name == "log_amount":
        return pl.when(pl.col("amount") >= 0).then((pl.col("amount") + 1).log()).otherwise(None)
    if name == "amount_ratio_5d":
        return _relative(
            pl.col("amount"),
            pl.col("amount").shift(1).rolling_mean(5).over("symbol"),
        )
    if name == "gap_return":
        return _relative(pl.col("open"), pl.col("prev_close"))
    if name == "intraday_return":
        return _relative(pl.col("close"), pl.col("open"))
    if name == "close_position":
        return _ratio(pl.col("close") - pl.col("low"), pl.col("high") - pl.col("low"))
    if name == "distance_to_high_60d":
        return _relative(pl.col("close"), pl.col("high_60d"))
    if name == "distance_from_low_60d":
        return _relative(pl.col("close"), pl.col("low_60d"))
    if name in {
        "max_ret_20d", "ret_skew_20d", "up_days_20d",
        "amihud_20d", "vol_price_corr_20d",
    }:
        change = _daily_change_expr()
        if name == "max_ret_20d":
            return change.rolling_max(20, min_samples=20).over("symbol")
        if name == "ret_skew_20d":
            return change.rolling_skew(20, bias=True).over("symbol")
        if name == "up_days_20d":
            return (
                (change > 0).cast(pl.Float64)
                .rolling_sum(20, min_samples=20).over("symbol")
            )
        if name == "amihud_20d":
            illiquidity = _ratio(change.abs(), pl.col("amount") / 1e8)
            return illiquidity.rolling_mean(20, min_samples=20).over("symbol")
        volume = pl.col("volume")
        product = change * volume
        return _rolling_corr_expr(change, volume, product, 20).over("symbol")
    if name == "turnover_z_60d":
        baseline = pl.col("turnover_rate").shift(1)
        mean = baseline.rolling_mean(60, min_samples=60)
        std = baseline.rolling_std(60, min_samples=60)
        return (
            pl.when(std > 0).then((pl.col("turnover_rate") - mean) / std)
            .otherwise(None)
            .over("symbol")
        )
    if name == "vwap_bias":
        vwap = _ratio(pl.col("amount"), pl.col("volume") * 100.0)
        return _relative(pl.col("close"), vwap)
    if name == "vol_trend_5_60":
        fast = pl.col("volume").rolling_mean(5)
        slow = pl.col("volume").rolling_mean(60)
        return _relative(fast, slow).over("symbol")
    if name in {"limit_up_count_20d", "limit_up_count_60d"}:
        window = 20 if name == "limit_up_count_20d" else 60
        hit = (pl.col("consecutive_limit_ups").fill_null(0) > 0).cast(pl.Float64)
        return hit.rolling_sum(window, min_samples=window).over("symbol")
    return None


def _daily_change_expr() -> pl.Expr:
    previous = pl.col("close").shift(1)
    return _ratio(pl.col("close"), previous) - 1.0


def _rolling_corr_expr(
    left: pl.Expr, right: pl.Expr, product: pl.Expr, window: int
) -> pl.Expr:
    """Pearson correlation over a rolling window, matching the matrix kernel formula."""
    mean_left = left.rolling_mean(window, min_samples=window)
    mean_right = right.rolling_mean(window, min_samples=window)
    mean_product = product.rolling_mean(window, min_samples=window)
    mean_left_sq = (left * left).rolling_mean(window, min_samples=window)
    mean_right_sq = (right * right).rolling_mean(window, min_samples=window)
    covariance = mean_product - mean_left * mean_right
    variance_left = mean_left_sq - mean_left * mean_left
    variance_right = mean_right_sq - mean_right * mean_right
    return pl.when(
        (variance_left > 0) & (variance_right > 0)
    ).then(
        covariance / (variance_left * variance_right).sqrt()
    ).otherwise(None)


def materialize_scoring_columns(
    frame: pl.DataFrame,
    names: Collection[str],
) -> pl.DataFrame:
    """把缺失的评分字段落成真实列。

    两类字段走不同路径:
    - 单表达式虚拟字段 (``ma5_bias`` / ``vwap_bias`` ...) 用 ``with_columns`` 一次算完;
    - Alpha191 公式因子要先编译成 :class:`~app.factors.ops_pl.Plan` 再逐阶段物化,
      因为它们的嵌套 ``.over()`` 没法塞进单个表达式 (见 ``ops_pl`` 模块顶部)。
    """
    pending = [str(name) for name in names if str(name) not in frame.columns]
    if not pending:
        return frame

    work = frame
    expressions: list[pl.Expr] = []
    plans: list[tuple[str, ops_pl.Plan]] = []
    for name in pending:
        plan = gtja_plan(name)
        if plan is not None:
            plans.append((name, plan))
            continue
        expression = scoring_value_expr(work.columns, name)
        if expression is not None:
            expressions.append(expression.alias(name))
    if expressions:
        work = work.with_columns(expressions)
    if plans:
        # NaN 只归一化一次; Plan.apply 会丢弃自己的中间列, 不污染调用方的表。
        work = ops_pl.normalize_missing(work)
        for name, plan in plans:
            work = plan.apply(work, alias=name, normalize=False)
    return work


def _ratio(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return pl.when(denominator.is_not_null() & (denominator != 0)).then(
        numerator / denominator
    ).otherwise(None)


def _relative(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return _ratio(numerator, denominator) - 1.0
