"""表达式 IR 的 Polars 后端。

用于策略评分与盘中增量: 输入是 ``(symbol, date, ...)`` 长表, 时序算子在
``.over(symbol)`` 内滚动, 横截面算子在 ``.over(date)`` 内排名 —— 与 NumPy
后端的 ``axis=1`` / ``axis=0`` 一一对应。

调用方需保证表已按 ``(symbol, date)`` 排序, 否则滚动窗口会取错行
(项目其它模块的既有约定)。

为什么 ``evaluate`` 返回 :class:`Plan` 而不是单个 ``pl.Expr``
------------------------------------------------------------
Polars 对嵌套 ``.over()`` 的处理不可靠, 两条实测结论:

1. **聚合型** ``.over()`` 套 ``.over()`` —— 外层分组被**静默忽略**, 不报错、
   不告警::

       inner = pl.col("volume").log() - pl.col("volume").log().shift(1).over("symbol")
       df.with_columns(inner.rank().over("date"))        # 全 null
       df.with_columns(inner.count().over("date"))       # 全 0
       df.with_columns(inner.abs().sum().over("date"))   # 全 0

2. **滚动型** 单独看没事, 但一旦和 ``when/then``、乘法等组合进同一个表达式,
   也会整列退化成 null —— ``(-1 * CORR(RANK(DELTA(LOG(VOLUME),1)),
   RANK((CLOSE-OPEN)/OPEN), 6))`` 就是这样全 null 的。

而 Alpha191 里 ``RANK(DELTA(...))`` / ``RANK(TSMAX(...))`` / ``CORR(RANK(..))``
这类跨轴组合极其常见。所以这里采用一条硬不变式:

    **任何单个表达式里最多只出现一层 ``.over()``。**

实现方式是编译成「若干已物化的中间列 + 最终表达式」, 由 :class:`Plan` 串起来;
凡是自身会产生 ``.over()`` 的算子 (时序 + 横截面), 其入参若已含 ``.over()``
就先落成中间列 (见 :meth:`_Compiler._windowed_input`)。

``evaluate()`` 用法::

    plan = ops_pl.evaluate(definition.expr)
    frame = plan.apply(frame, alias="gtja001")

缺失语义
--------
与 NumPy 后端严格对齐 (NumPy 侧用 ``NaN``, Polars 侧用 ``null``, 由
:func:`normalize_missing` 在入口把 ``NaN`` 归一成 ``null``):

- 滚动算子一律 ``min_samples=window_size`` —— 窗口内任一位缺失则结果缺失;
- ``ewm_mean`` / ``sma`` 用 ``ignore_nulls=True`` —— 缺失处输出缺失, 且**不更新**
  递推状态 (NumPy ``_ewm`` 同式);
- ``iif`` 条件缺失时输出缺失 (与 Polars 原生 null 比较、NumPy 侧显式遮罩一致);
- 相关系数分母设方差下限 ``_VARIANCE_FLOOR``, 避免离散输入下两端浮点残差
  (0.0 vs 1e-17) 导致一处给值一处给缺失。

性能说明
--------
``decay_linear`` / ``wma`` / ``regbeta`` / ``regresi`` / ``ts_rank`` /
``ts_argmax`` / ``ts_argmin`` / ``ts_prod`` 目前走 ``rolling_map``, 逐个窗口回调
Python, 在几十万行规模上够用, 全市场逐轮重算会偏慢, 属 P2 优化项
(可行方向: 位置加权滚动和可拆成「绝对索引加权累积和 - 偏移量 * 累积和」,
``ts_rank`` 可换 Polars 原生 ``rolling_rank``)。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

from .ir import BinOp, Call, Cmp, Const, Field, Node, UnOp

#: 缺失值在两端统一用 null 表示。
_MISSING = None

#: 相关系数分母的方差下限, 与 ``ops_np._VARIANCE_FLOOR`` 必须一致。
_VARIANCE_FLOOR = 1e-12

#: 聚合型横截面算子 —— 唯一会踩「聚合 ``.over()`` 里再套 ``.over()``」的算子类别。
_CROSS_SECTIONAL = frozenset({"cs_rank", "cs_scale", "indneutralize"})

#: 物化中间列的前缀。每个 :class:`_Compiler` 实例拿一个独立序号, 避免
#: 同一张表上先后应用多个 Plan 时中间列互相覆盖。
_STAGE_PREFIX = "__factor_stage_"
_prefix_counter = itertools.count()


@dataclass(frozen=True)
class Plan:
    """编译结果: 需要先物化的中间列 + 最终表达式。"""

    stages: tuple[tuple[str, pl.Expr], ...]
    final: pl.Expr

    def apply(
        self,
        frame: pl.DataFrame,
        alias: str = "value",
        *,
        normalize: bool = True,
    ) -> pl.DataFrame:
        """依次物化中间列, 再把最终表达式写成 ``alias`` 列。

        参数:
            alias:     输出列名。
            normalize: 是否先把浮点列的 ``NaN`` 归一成 ``null`` (见
                :func:`normalize_missing`)。批量跑因子时调用方可以自己归一化一次
                再传 ``False``, 省掉每个因子一遍全表扫描。

        中间列在算完最终表达式后会被丢掉, 不会污染调用方的表。
        """
        out = normalize_missing(frame) if normalize else frame
        for name, expr in self.stages:
            out = out.with_columns(expr.alias(name))
        out = out.with_columns(self.final.alias(alias))
        if self.stages:
            out = out.drop([name for name, _ in self.stages])
        return out


def normalize_missing(frame: pl.DataFrame) -> pl.DataFrame:
    """把浮点列里的 ``NaN`` 统一成 ``null``。

    **Polars 里 NaN 与 null 是两个不同的东西**: 从 numpy 构造 DataFrame 时
    ``np.nan`` 会落成 **NaN 值**而不是缺失, 于是 ``count`` / ``rank`` /
    ``rolling_*`` 的 ``min_samples`` 全都把它当成有效样本, 结果与 NumPy 后端
    整体漂移 (实测 ``cs_rank`` 的分母会从 38 变成 40)。而 ``ewm_mean`` 的
    ``ignore_nulls`` 也拦不住 NaN —— 一个 NaN 会把整条递推链污染成 NaN。

    项目线上数据走 Parquet 是原生 null, 但盘中增量 / 临时拼接 / 上游 numpy
    计算都可能带进 NaN, 所以在这里一次性归一化, 让后端对输入形态不敏感。
    """
    float_columns = [
        name
        for name, dtype in frame.schema.items()
        if dtype in (pl.Float32, pl.Float64)
    ]
    if not float_columns:
        return frame
    return frame.with_columns(
        [pl.col(name).fill_nan(None) for name in float_columns]
    )


def evaluate(
    node: Node,
    groups: dict[str, pl.Expr] | None = None,
    *,
    symbol_column: str = "symbol",
    date_column: str = "date",
) -> Plan:
    """把表达式树编译成 :class:`Plan`。

    参数:
        node:          表达式树根节点。
        groups:        虚拟字段名 → 分组表达式, 供 ``indneutralize`` 使用。
        symbol_column: 标的列名, 时序算子的分组键。
        date_column:   日期列名, 横截面算子的分组键。
    """
    return _Compiler(groups or {}, symbol_column, date_column).run(node)


# ================================================================
# 元素级辅助
# ================================================================
def _safe_div(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    """除零 / 缺失一律给缺失, 与 NumPy 侧 ``_safe_div`` 对齐。"""
    return (
        pl.when(denominator.is_null() | (denominator == 0))
        .then(_MISSING)
        .otherwise(numerator / denominator)
    )


def _both_present(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    return left.is_not_null() & right.is_not_null()


def _max_horizontal(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    return (
        pl.when(~_both_present(left, right))
        .then(_MISSING)
        .when(left >= right)
        .then(left)
        .otherwise(right)
    )


def _min_horizontal(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    return (
        pl.when(~_both_present(left, right))
        .then(_MISSING)
        .when(left <= right)
        .then(left)
        .otherwise(right)
    )


def _signed_power(value: pl.Expr, exponent: float) -> pl.Expr:
    """``sign(x) * |x|^e`` —— null 在 sign / abs / pow / 乘法里自然传播。"""
    return value.sign() * value.abs().pow(exponent)


# ================================================================
# 窗口归约 (rolling_map 兜底)
# ================================================================
def _window_array(series: pl.Series) -> np.ndarray | None:
    """把窗口 Series 转成 float 数组; 含缺失时返回 None。"""
    array = series.to_numpy().astype(np.float64, copy=False)
    if not np.isfinite(array).all():
        return None
    return array


def _map_decay_linear(window: int) -> Any:
    weights = np.arange(1.0, window + 1.0)
    weights = weights / weights.sum()

    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        return float((array * weights).sum())

    return apply


def _map_wma(window: int) -> Any:
    weights = 0.9 ** np.arange(window - 1, -1, -1, dtype=np.float64)
    weights = weights / weights.sum()

    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        return float((array * weights).sum())

    return apply


def _map_ts_rank(window: int) -> Any:
    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        return float((array <= array[-1]).sum() / window)

    return apply


def _map_argmax(window: int) -> Any:
    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        return float((window - 1) - int(array.argmax()))

    return apply


def _map_argmin(window: int) -> Any:
    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        return float((window - 1) - int(array.argmin()))

    return apply


def _map_regbeta(window: int) -> Any:
    positions = np.arange(1.0, window + 1.0)
    sum_t = positions.sum()
    sum_t2 = float((positions * positions).sum())
    denominator = window * sum_t2 - sum_t * sum_t

    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        sum_x = array.sum()
        sum_tx = float((array * positions).sum())
        return float((window * sum_tx - sum_t * sum_x) / denominator)

    return apply


def _map_regresi(window: int) -> Any:
    positions = np.arange(1.0, window + 1.0)
    sum_t = positions.sum()
    sum_t2 = float((positions * positions).sum())
    denominator = window * sum_t2 - sum_t * sum_t

    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        sum_x = array.sum()
        sum_tx = float((array * positions).sum())
        slope = (window * sum_tx - sum_t * sum_x) / denominator
        intercept = (sum_x - slope * sum_t) / window
        return float(array[-1] - (intercept + slope * window))

    return apply


def _map_prod(window: int) -> Any:
    def apply(series: pl.Series) -> float:
        array = _window_array(series)
        if array is None:
            return float("nan")
        return float(array.prod())

    return apply


_MAP_BUILDERS: dict[str, Any] = {
    "decay_linear": _map_decay_linear,
    "wma": _map_wma,
    "ts_rank": _map_ts_rank,
    "ts_argmax": _map_argmax,
    "ts_argmin": _map_argmin,
    "regbeta": _map_regbeta,
    "regresi": _map_regresi,
    "ts_prod": _map_prod,
}

#: 有原生实现的滚动算子。
_SIMPLE_ROLLING: dict[str, Any] = {
    "ts_sum": lambda x, n: x.rolling_sum(window_size=n, min_samples=n),
    "ts_mean": lambda x, n: x.rolling_mean(window_size=n, min_samples=n),
    "ts_std": lambda x, n: x.rolling_std(window_size=n, min_samples=n, ddof=0),
    "ts_min": lambda x, n: x.rolling_min(window_size=n, min_samples=n),
    "ts_max": lambda x, n: x.rolling_max(window_size=n, min_samples=n),
}


def _rolling_corr(left: pl.Expr, right: pl.Expr, window: int) -> pl.Expr:
    """滚动 Pearson 相关, 与 ``scoring.py::_rolling_corr_expr`` 及 NumPy 侧同式。"""
    mean_left = left.rolling_mean(window, min_samples=window)
    mean_right = right.rolling_mean(window, min_samples=window)
    mean_product = (left * right).rolling_mean(window, min_samples=window)
    mean_left_sq = (left * left).rolling_mean(window, min_samples=window)
    mean_right_sq = (right * right).rolling_mean(window, min_samples=window)
    covariance = mean_product - mean_left * mean_right
    var_left = mean_left_sq - mean_left * mean_left
    var_right = mean_right_sq - mean_right * mean_right
    return (
        pl.when((var_left > _VARIANCE_FLOOR) & (var_right > _VARIANCE_FLOOR))
        .then(covariance / (var_left * var_right).sqrt())
        .otherwise(_MISSING)
    )


# ================================================================
# 编译器
# ================================================================
class _Compiler:
    """把表达式树编译成 ``Plan``。

    ``_compile`` 返回 ``(表达式, 是否已含 .over())``; 后者用于判断横截面算子的
    入参是否需要先物化 (见模块 docstring 的嵌套陷阱说明)。
    """

    def __init__(
        self,
        groups: dict[str, pl.Expr],
        symbol_column: str,
        date_column: str,
    ) -> None:
        self._groups = groups
        self._symbol = symbol_column
        self._date = date_column
        self._stages: list[tuple[str, pl.Expr]] = []
        self._seq = 0
        self._prefix = f"{_STAGE_PREFIX}{next(_prefix_counter)}_"

    # ── 对外 ──
    def run(self, node: Node) -> Plan:
        final, _ = self._compile(node)
        return Plan(tuple(self._stages), final)

    # ── 中间列 ──
    def _materialize(self, expr: pl.Expr) -> pl.Expr:
        name = f"{self._prefix}{self._seq}"
        self._seq += 1
        self._stages.append((name, expr))
        return pl.col(name)

    def _windowed_input(self, node: Node) -> pl.Expr:
        """编译一个「即将被 ``.over()`` 包裹」的入参, 必要时先物化。

        这是整个后端的不变式: **任何单个表达式里最多只出现一层 ``.over()``**。
        凡是自身会产生 ``.over()`` 的算子 (时序 + 横截面), 其入参若已经含
        ``.over()``, 就必须先落成中间列再喂进来 —— Polars 对嵌套 ``.over()``
        的处理不可靠: 聚合型会**静默丢弃**外层分组 (实测全 null / count 全 0),
        滚动型虽然看起来正常, 但一旦和 ``when/then``、乘法等组合进同一个表达式,
        同样会整列退化成 null (gtja001 的 ``CORR(RANK(DELTA(...)), RANK(...), 6)``
        就是这么全 null 的)。
        """
        expr, has_over = self._compile(node)
        return self._materialize(expr) if has_over else expr

    # ── 递归 ──
    def _compile(self, node: Node) -> tuple[pl.Expr, bool]:
        if isinstance(node, Const):
            return pl.lit(node.value), False

        if isinstance(node, Field):
            return pl.col(node.name), False

        if isinstance(node, BinOp):
            left, left_window = self._compile(node.left)
            right, right_window = self._compile(node.right)
            if node.op == "+":
                expr = left + right
            elif node.op == "-":
                expr = left - right
            elif node.op == "*":
                expr = left * right
            elif node.op == "/":
                expr = _safe_div(left, right)
            elif node.op == "^":
                # sign(x) * |x|^e, 与 NumPy 侧一致。
                expr = left.abs().pow(right) * left.sign()
            else:
                raise ValueError(f"未知二元算子: {node.op}")
            return expr, left_window or right_window

        if isinstance(node, Cmp):
            left, left_window = self._compile(node.left)
            right, right_window = self._compile(node.right)
            if node.op == ">":
                expr = left > right
            elif node.op == ">=":
                expr = left >= right
            elif node.op == "<":
                expr = left < right
            elif node.op == "<=":
                expr = left <= right
            elif node.op == "==":
                expr = left == right
            elif node.op == "!=":
                expr = left != right
            else:
                raise ValueError(f"未知比较算子: {node.op}")
            return expr, left_window or right_window

        if isinstance(node, UnOp):
            operand, operand_window = self._compile(node.operand)
            if node.op == "neg":
                expr = -operand
            elif node.op == "abs":
                expr = operand.abs()
            elif node.op == "log":
                expr = pl.when(operand > 0).then(operand.log()).otherwise(_MISSING)
            elif node.op == "sign":
                expr = operand.sign()
            elif node.op == "sqrt":
                expr = pl.when(operand >= 0).then(operand.sqrt()).otherwise(_MISSING)
            else:
                raise ValueError(f"未知一元算子: {node.op}")
            return expr, operand_window

        if isinstance(node, Call):
            return self._compile_call(node)

        raise TypeError(f"不支持的节点类型: {type(node).__name__}")

    def _compile_call(self, node: Call) -> tuple[pl.Expr, bool]:
        name = node.name
        window = node.window

        # ── 横截面: 自身产生 .over(date) ──
        if name in _CROSS_SECTIONAL:
            payload = self._windowed_input(node.args[0])
            return self._compile_cross_sectional(name, node, payload), True

        # ── 元素级: 不新增 .over(), 只向下传播 ──
        if name == "signedpower":
            value, value_window = self._compile(node.args[0])
            return _signed_power(value, node.extra[0]), value_window

        if name == "iif":
            condition, condition_window = self._compile(node.args[0])
            when_true, true_window = self._compile(node.args[1])
            when_false, false_window = self._compile(node.args[2])
            expr = (
                pl.when(condition.is_null())
                .then(_MISSING)
                .when(condition)
                .then(when_true)
                .otherwise(when_false)
            )
            return expr, condition_window or true_window or false_window

        if name == "max":
            left, left_window = self._compile(node.args[0])
            right, right_window = self._compile(node.args[1])
            return _max_horizontal(left, right), left_window or right_window

        if name == "min":
            left, left_window = self._compile(node.args[0])
            right, right_window = self._compile(node.args[1])
            return _min_horizontal(left, right), left_window or right_window

        # ── 以下算子自身会产生 .over(symbol) ──
        if name == "ewm_mean":
            return self._compile_ewm(self._windowed_input(node.args[0]), node.extra[0]), True

        if name == "sma":
            # SMA(A, n, m) 等价 ewm alpha = m/n, 递推语义与 NumPy 侧 _ewm 一致。
            if window is None:
                raise ValueError("算子 sma 需要窗口参数 n")
            value = self._windowed_input(node.args[0])
            return self._compile_ewm(value, node.extra[0] / window), True

        if window is None:
            raise ValueError(f"算子 {name} 需要窗口参数")

        if name == "delay":
            value = self._windowed_input(node.args[0])
            return value.shift(window).over(self._symbol), True

        if name == "delta":
            value = self._windowed_input(node.args[0])
            return value - value.shift(window).over(self._symbol), True

        if name == "ts_corr":
            left = self._windowed_input(node.args[0])
            right = self._windowed_input(node.args[1])
            return _rolling_corr(left, right, window).over(self._symbol), True

        simple = _SIMPLE_ROLLING.get(name)
        if simple is not None:
            value = self._windowed_input(node.args[0])
            return simple(value, window).over(self._symbol), True

        builder = _MAP_BUILDERS.get(name)
        if builder is not None:
            value = self._windowed_input(node.args[0])
            expr = value.rolling_map(
                builder(window), window_size=window, min_samples=window
            ).over(self._symbol)
            return expr, True

        raise NotImplementedError(f"Polars 后端尚未实现算子: {name}")

    # ── 专项 ──
    def _compile_ewm(self, value: pl.Expr, alpha: float) -> pl.Expr:
        """递推指数加权。

        ``ignore_nulls=True`` 让缺失处**不更新**递推状态, 输出缺失 ——
        与 NumPy 侧 ``_ewm`` 同式 (缺失输入不污染后续)。
        """
        return value.ewm_mean(
            alpha=alpha, adjust=False, min_samples=1, ignore_nulls=True
        ).over(self._symbol)

    def _compile_cross_sectional(
        self, name: str, node: Call, payload: pl.Expr
    ) -> pl.Expr:
        if name == "cs_rank":
            # 组内样本数 < 2 时排名无意义, 两端统一给缺失 (对齐 ops_np._cs_rank)。
            # Polars rank 只在非缺失样本间排 1..k 且缺失保持 null, 与 NumPy 一致。
            count = payload.count().over(self._date)
            ranked = payload.rank(method="average").over(self._date)
            return pl.when(count >= 2).then(ranked / count).otherwise(_MISSING)

        if name == "cs_scale":
            total = payload.abs().sum().over(self._date)
            return pl.when(total > 0).then(payload / total).otherwise(_MISSING)

        if name == "indneutralize":
            key = node.args[1].name if isinstance(node.args[1], Field) else None
            if key is None or key not in self._groups:
                raise KeyError(
                    f"indneutralize 需要分组字段 {key!r}, 可用分组: {sorted(self._groups)}"
                )
            return payload - payload.mean().over([self._date, self._groups[key]])

        raise NotImplementedError(f"Polars 后端尚未实现横截面算子: {name}")
