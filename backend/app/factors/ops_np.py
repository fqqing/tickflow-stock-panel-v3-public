"""表达式 IR 的 NumPy 矩阵后端。

数据布局与 ``backtest/matrix.py`` 一致: 形状 ``(n_symbols, n_dates)``,
时序算子沿 ``axis=1`` 滚动, 横截面算子沿 ``axis=0``。

缺失值语义
----------
窗口内**任一**位置缺失, 结果即为缺失 (NaN)。这与 Polars 侧的
``rolling_*(window, min_samples=window)`` 严格对应, 是双后端数值一致的前提。

性能说明
--------
滚动实现走 ``sliding_window_view`` + 分块。视图本身不占内存, 但归约时会物化
``(chunk, n_dates - n + 1, n)``, 因此按块处理把峰值控制在 ``_ROLL_BLOCK_BYTES``。
``ts_rank`` / ``decay_linear`` / ``regbeta`` 目前是直接的窗口归约, 足够骨架与
中等规模回测使用; 若后续要跑全市场 191 因子, 应换成累加和闭式或 numba 内核
(见 ``backtest/matrix.py`` 的 ``_valid_rolling_kernel`` 先例)。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from .ir import (
    AVAILABLE_FIELDS,
    BinOp,
    Call,
    Cmp,
    Const,
    Field,
    Node,
    UnOp,
)

#: 单块滑动窗口数组的字节上限, 用于控制分块粒度。
_ROLL_BLOCK_BYTES = 32 * 1024 * 1024

#: 相关系数分母的方差下限。
#: 用 ``> 0`` 判据会在浮点残差下两端不一致 —— 例如 TSRANK 的输出是离散值
#: (0.2/0.4/.../1.0), 窗口内取值全相同时方差在数学上是 0, 但两个后端的
#: 归约顺序不同, 一个得 0.0、一个得 1e-17, 于是同一处一个给值一个给缺失。
#: 取一个远小于业务方差的绝对下限, 让两端得到相同判定。
_VARIANCE_FLOOR = 1e-12


# ================================================================
# 滚动基础设施
# ================================================================
def _rolling(
    values: np.ndarray,
    window: int,
    reduce_fn: Callable[[np.ndarray], np.ndarray],
) -> np.ndarray:
    """沿 axis=1 滚动并归约。

    ``reduce_fn`` 收到形状 ``(chunk, n_dates - window + 1, window)`` 的窗口数组,
    返回 ``(chunk, n_dates - window + 1)``。前 ``window-1`` 列恒为 NaN。
    """
    array = np.asarray(values, dtype=np.float64)
    n_symbols, n_dates = array.shape
    out = np.full((n_symbols, n_dates), np.nan, dtype=np.float64)
    if window <= 0 or window > n_dates:
        return out

    width = n_dates - window + 1
    per_row_bytes = width * window * 8
    block = max(1, int(_ROLL_BLOCK_BYTES / max(per_row_bytes, 1)))

    for start in range(0, n_symbols, block):
        stop = min(n_symbols, start + block)
        windows = np.lib.stride_tricks.sliding_window_view(
            array[start:stop], window, axis=1
        )
        with np.errstate(all="ignore"):
            out[start:stop, window - 1 :] = reduce_fn(windows)
    return out


def _reduce_mean(windows: np.ndarray) -> np.ndarray:
    return windows.mean(axis=-1)


def _reduce_std(windows: np.ndarray) -> np.ndarray:
    # ddof=0 (总体标准差), 与项目 matrix 内核及 Polars rolling_std 默认一致。
    return windows.std(axis=-1)


def _reduce_sum(windows: np.ndarray) -> np.ndarray:
    return windows.sum(axis=-1)


def _reduce_prod(windows: np.ndarray) -> np.ndarray:
    return windows.prod(axis=-1)


def _reduce_min(windows: np.ndarray) -> np.ndarray:
    return windows.min(axis=-1)


def _reduce_max(windows: np.ndarray) -> np.ndarray:
    return windows.max(axis=-1)


def _reduce_ts_rank(windows: np.ndarray) -> np.ndarray:
    """末位值在窗口内的百分比排名 (count(<= last) / n)。"""
    window = windows.shape[-1]
    complete = np.isfinite(windows).all(axis=-1)
    last = windows[..., -1:]
    hits = (windows <= last).sum(axis=-1).astype(np.float64)
    return np.where(complete, hits / window, np.nan)


def _reduce_argmax(windows: np.ndarray) -> np.ndarray:
    """窗口内最大值距当前的天数 (0 = 当天)。"""
    window = windows.shape[-1]
    complete = np.isfinite(windows).all(axis=-1)
    safe = np.where(np.isfinite(windows), windows, -np.inf)
    distance = (window - 1) - safe.argmax(axis=-1)
    return np.where(complete, distance.astype(np.float64), np.nan)


def _reduce_argmin(windows: np.ndarray) -> np.ndarray:
    window = windows.shape[-1]
    complete = np.isfinite(windows).all(axis=-1)
    safe = np.where(np.isfinite(windows), windows, np.inf)
    distance = (window - 1) - safe.argmin(axis=-1)
    return np.where(complete, distance.astype(np.float64), np.nan)


def _reduce_decay_linear(windows: np.ndarray) -> np.ndarray:
    """权重 1..n, 最新值权重最大, 权重和归一化为 1。"""
    window = windows.shape[-1]
    weights = np.arange(1.0, window + 1.0)
    weights = weights / weights.sum()
    return windows.sum(axis=-1) if window == 1 else (windows * weights).sum(axis=-1)


def _reduce_wma(windows: np.ndarray) -> np.ndarray:
    """WMA 权重 0.9^i (i 为距当前的天数), 最新值权重最大。"""
    window = windows.shape[-1]
    weights = 0.9 ** np.arange(window - 1, -1, -1, dtype=np.float64)
    weights = weights / weights.sum()
    return (windows * weights).sum(axis=-1)


def _reduce_regbeta(windows: np.ndarray) -> np.ndarray:
    """窗口内序列对 1..n 做一元线性回归的斜率 (闭式解)。"""
    window = windows.shape[-1]
    if window < 2:
        return np.full(windows.shape[:-1], np.nan, dtype=np.float64)
    positions = np.arange(1.0, window + 1.0)
    sum_t = positions.sum()
    sum_t2 = float((positions * positions).sum())
    denominator = window * sum_t2 - sum_t * sum_t
    sum_x = windows.sum(axis=-1)
    sum_tx = (windows * positions).sum(axis=-1)
    return (window * sum_tx - sum_t * sum_x) / denominator


def _reduce_regresi(windows: np.ndarray) -> np.ndarray:
    """上述回归在窗口末位处的残差。"""
    window = windows.shape[-1]
    if window < 2:
        return np.full(windows.shape[:-1], np.nan, dtype=np.float64)
    positions = np.arange(1.0, window + 1.0)
    sum_t = positions.sum()
    sum_t2 = float((positions * positions).sum())
    denominator = window * sum_t2 - sum_t * sum_t
    sum_x = windows.sum(axis=-1)
    sum_tx = (windows * positions).sum(axis=-1)
    slope = (window * sum_tx - sum_t * sum_x) / denominator
    intercept = (sum_x - slope * sum_t) / window
    fitted_last = intercept + slope * window
    return windows[..., -1] - fitted_last


def _corr_kernel(left: np.ndarray, right: np.ndarray, window: int) -> np.ndarray:
    """滚动 Pearson 相关系数, 与 ``strategy/scoring.py::_rolling_corr_expr`` 同式。"""
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    n_symbols, n_dates = a.shape
    out = np.full((n_symbols, n_dates), np.nan, dtype=np.float64)
    if window <= 1 or window > n_dates:
        return out

    width = n_dates - window + 1
    per_row_bytes = width * window * 8
    block = max(1, int(_ROLL_BLOCK_BYTES / max(per_row_bytes, 1)))

    for start in range(0, n_symbols, block):
        stop = min(n_symbols, start + block)
        wa = np.lib.stride_tricks.sliding_window_view(a[start:stop], window, axis=1)
        wb = np.lib.stride_tricks.sliding_window_view(b[start:stop], window, axis=1)
        complete = np.isfinite(wa).all(axis=-1) & np.isfinite(wb).all(axis=-1)
        with np.errstate(all="ignore"):
            mean_a = wa.mean(axis=-1)
            mean_b = wb.mean(axis=-1)
            mean_ab = (wa * wb).mean(axis=-1)
            mean_a2 = (wa * wa).mean(axis=-1)
            mean_b2 = (wb * wb).mean(axis=-1)
        covariance = mean_ab - mean_a * mean_b
        var_a = mean_a2 - mean_a * mean_a
        var_b = mean_b2 - mean_b * mean_b
        with np.errstate(all="ignore"):
            corr = covariance / np.sqrt(var_a * var_b)
        corr = np.where(
            (var_a > _VARIANCE_FLOOR) & (var_b > _VARIANCE_FLOOR), corr, np.nan
        )
        out[start:stop, window - 1 :] = np.where(complete, corr, np.nan)
    return out


def _ewm(values: np.ndarray, alpha: float) -> np.ndarray:
    """递推指数加权: ``y_t = alpha * x_t + (1 - alpha) * y_{t-1}``。

    首值用第一个有效观测初始化 (``adjust=False`` 口径)。
    缺失语义: 缺失处输出缺失, 且**不更新递推状态** —— 缺失输入不会污染后续。
    与 Polars 侧 ``ewm_mean(adjust=False, ignore_nulls=True)`` 严格对应。
    """
    array = np.asarray(values, dtype=np.float64)
    n_symbols, n_dates = array.shape
    out = np.full((n_symbols, n_dates), np.nan, dtype=np.float64)
    state = np.full(n_symbols, np.nan, dtype=np.float64)
    started = np.zeros(n_symbols, dtype=bool)

    for t in range(n_dates):
        column = array[:, t]
        present = np.isfinite(column)
        with np.errstate(all="ignore"):
            updated = np.where(
                started, alpha * column + (1.0 - alpha) * state, column
            )
        state = np.where(present, updated, state)
        out[:, t] = np.where(present, state, np.nan)
        started = started | present
    return out


def _finite_mask(value: Any) -> Any:
    """返回与 ``value`` 同形 (或标量) 的「有限值」布尔遮罩。"""
    return np.isfinite(value)


# ================================================================
# 横截面算子
# ================================================================
def _cs_rank(values: np.ndarray) -> np.ndarray:
    """当日截面百分比排名, 并列取平均名次 (与 Polars rank 默认一致)。"""
    from scipy.stats import rankdata

    array = np.asarray(values, dtype=np.float64)
    out = np.full(array.shape, np.nan, dtype=np.float64)
    for column_index in range(array.shape[1]):
        column = array[:, column_index]
        present = np.isfinite(column)
        count = int(present.sum())
        if count < 2:
            continue
        ranks = rankdata(column[present], method="average")
        out[present, column_index] = ranks / count
    return out


def _cs_scale(values: np.ndarray) -> np.ndarray:
    """截面归一化: a / sum(|a|)。"""
    array = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(array)
    total = np.where(finite, np.abs(array), 0.0).sum(axis=0)
    with np.errstate(all="ignore"):
        scaled = array / np.where(total > 0, total, np.nan)
    return np.where(finite, scaled, np.nan)


def _indneutralize(values: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """按行业分组去均值。

    ``groups`` 为逐标的的整型行业编号 (形状 ``(n_symbols,)``),
    或与 ``values`` 同形的矩阵。骨架阶段仅支持前者。
    """
    array = np.asarray(values, dtype=np.float64)
    codes = np.asarray(groups)
    if codes.ndim != 1 or codes.shape[0] != array.shape[0]:
        raise NotImplementedError(
            "indneutralize 目前只支持 (n_symbols,) 形状的行业编号"
        )
    out = np.full(array.shape, np.nan, dtype=np.float64)
    for code in np.unique(codes[np.isfinite(codes)]):
        rows = codes == code
        block = array[rows]
        finite = np.isfinite(block)
        count = finite.sum(axis=0)
        mean = np.where(count > 0, np.where(finite, block, 0.0).sum(axis=0) / np.maximum(count, 1), np.nan)
        out[rows] = np.where(finite, block - mean, np.nan)
    return out


# ================================================================
# 元素级运算
# ================================================================
def _safe_div(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        result = numerator / denominator
    return np.where(np.isfinite(result), result, np.nan)


def _apply_unop(op: str, value: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        if op == "neg":
            return -value
        if op == "abs":
            return np.abs(value)
        if op == "log":
            return np.where(value > 0, np.log(np.where(value > 0, value, 1.0)), np.nan)
        if op == "sign":
            return np.where(np.isfinite(value), np.sign(value), np.nan)
        if op == "sqrt":
            return np.where(value >= 0, np.sqrt(np.where(value >= 0, value, 0.0)), np.nan)
    raise ValueError(f"未知一元算子: {op}")


def _apply_binop(op: str, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return _safe_div(left, right)
        if op == "^":
            return np.power(np.abs(left), right) * np.where(left < 0, -1.0, 1.0)
    raise ValueError(f"未知二元算子: {op}")


def _apply_cmp(op: str, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """元素比较。NaN 参与比较时结果为 False (调用方需另算有效遮罩)。"""
    with np.errstate(all="ignore"):
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
    raise ValueError(f"未知比较算子: {op}")


# ================================================================
# 求值入口
# ================================================================
def evaluate(
    node: Node,
    fields: Mapping[str, np.ndarray],
    groups: Mapping[str, np.ndarray] | None = None,
    *,
    _memo: dict[Node, Any] | None = None,
) -> np.ndarray:
    """把表达式树求值成矩阵。

    参数:
        node:   表达式树根节点。
        fields: 列名 → ``(n_symbols, n_dates)`` 矩阵。
        groups: 虚拟字段名 → ``(n_symbols,)`` 分组编号, 供 ``indneutralize`` 使用。
    """
    memo: dict[Node, Any] = {} if _memo is None else _memo
    cached = memo.get(node)
    if cached is not None:
        return cached

    result = _evaluate_node(node, fields, groups or {}, memo)
    memo[node] = result
    return result


def _evaluate_node(
    node: Node,
    fields: Mapping[str, np.ndarray],
    groups: Mapping[str, np.ndarray],
    memo: dict[Node, Any],
) -> np.ndarray:
    if isinstance(node, Const):
        # 标量广播由具体算子处理, 这里先按标量保存。
        return np.float64(node.value)

    if isinstance(node, Field):
        if node.name in fields:
            return np.asarray(fields[node.name], dtype=np.float64)
        if node.name not in AVAILABLE_FIELDS:
            raise KeyError(f"表达式引用了未知字段: {node.name!r}")
        raise KeyError(
            f"面板缺少字段 {node.name!r}。可用字段: {sorted(fields)}"
        )

    if isinstance(node, BinOp):
        left = _scalar_or_array(evaluate(node.left, fields, groups, _memo=memo))
        right = _scalar_or_array(evaluate(node.right, fields, groups, _memo=memo))
        return _apply_binop(node.op, left, right)

    if isinstance(node, Cmp):
        left = _scalar_or_array(evaluate(node.left, fields, groups, _memo=memo))
        right = _scalar_or_array(evaluate(node.right, fields, groups, _memo=memo))
        return _apply_cmp(node.op, left, right)

    if isinstance(node, UnOp):
        operand = _scalar_or_array(evaluate(node.operand, fields, groups, _memo=memo))
        return _apply_unop(node.op, operand)

    if isinstance(node, Call):
        return _evaluate_call(node, fields, groups, memo)

    raise TypeError(f"不支持的节点类型: {type(node).__name__}")


def _scalar_or_array(value: Any) -> Any:
    """Const 求值结果是 0 维数组, 交给 numpy 广播即可, 这里原样返回。"""
    return value


def _array_arg(
    node: Node,
    fields: Mapping[str, np.ndarray],
    groups: Mapping[str, np.ndarray],
    memo: dict[Node, Any],
) -> np.ndarray:
    value = evaluate(node, fields, groups, _memo=memo)
    if np.ndim(value) == 0:
        return np.float64(value)
    return value


def _evaluate_call(
    node: Call,
    fields: Mapping[str, np.ndarray],
    groups: Mapping[str, np.ndarray],
    memo: dict[Node, Any],
) -> np.ndarray:
    name = node.name
    window = node.window

    # ── 不涉及窗口的元素级 / 横截面算子 ──
    if name == "cs_rank":
        return _cs_rank(_array_arg(node.args[0], fields, groups, memo))

    if name == "cs_scale":
        return _cs_scale(_array_arg(node.args[0], fields, groups, memo))

    if name == "indneutralize":
        values = _array_arg(node.args[0], fields, groups, memo)
        key = node.args[1].name if isinstance(node.args[1], Field) else None
        if key is None or key not in groups:
            raise KeyError(
                f"indneutralize 需要分组字段 {key!r}, 可用分组: {sorted(groups)}"
            )
        return _indneutralize(values, groups[key])

    if name == "signedpower":
        values = _array_arg(node.args[0], fields, groups, memo)
        exponent = node.extra[0]
        return _apply_unop("abs", values) ** exponent * np.where(values < 0, -1.0, 1.0)

    if name == "iif":
        # 条件缺失时结果缺失 —— 与 Polars 原生 null 比较行为一致。
        # np.where 会把 NaN 条件当 True 处理, 所以必须显式算有效遮罩。
        condition_node = node.args[0]
        if isinstance(condition_node, Cmp):
            left = _array_arg(condition_node.left, fields, groups, memo)
            right = _array_arg(condition_node.right, fields, groups, memo)
            valid = _finite_mask(left) & _finite_mask(right)
            condition = _apply_cmp(condition_node.op, left, right)
        else:
            condition = _array_arg(condition_node, fields, groups, memo)
            valid = _finite_mask(condition)
        when_true = _array_arg(node.args[1], fields, groups, memo)
        when_false = _array_arg(node.args[2], fields, groups, memo)
        return np.where(valid, np.where(condition, when_true, when_false), np.nan)

    if name == "max":
        left = _array_arg(node.args[0], fields, groups, memo)
        right = _array_arg(node.args[1], fields, groups, memo)
        # 必须用 maximum 而不是 fmax: fmax 会「吞掉」NaN, 破坏缺失传播约定。
        return np.maximum(left, right)

    if name == "min":
        left = _array_arg(node.args[0], fields, groups, memo)
        right = _array_arg(node.args[1], fields, groups, memo)
        return np.minimum(left, right)

    if name == "ewm_mean":
        return _ewm(_array_arg(node.args[0], fields, groups, memo), node.extra[0])

    if name == "sma":
        # SMA(A, n, m) 等价 ewm_mean(alpha = m/n)
        return _ewm(
            _array_arg(node.args[0], fields, groups, memo), node.extra[0] / window
        )

    # ── 时序算子 (需要有窗口) ──
    if window is None:
        raise ValueError(f"算子 {name} 需要窗口参数")

    if name == "delay":
        values = _array_arg(node.args[0], fields, groups, memo)
        out = np.full(values.shape, np.nan, dtype=np.float64)
        if window < values.shape[1]:
            out[:, window:] = values[:, : values.shape[1] - window]
        return out

    if name == "delta":
        values = _array_arg(node.args[0], fields, groups, memo)
        previous = _evaluate_call(
            Call("delay", node.args, window), fields, groups, memo
        )
        return values - previous

    if name == "ts_corr":
        left = _array_arg(node.args[0], fields, groups, memo)
        right = _array_arg(node.args[1], fields, groups, memo)
        return _corr_kernel(left, right, window)

    reducer = _SCALAR_REDUCERS.get(name)
    if reducer is not None:
        values = _array_arg(node.args[0], fields, groups, memo)
        return _rolling(values, window, reducer)

    raise NotImplementedError(f"NumPy 后端尚未实现算子: {name}")


_SCALAR_REDUCERS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "ts_sum": _reduce_sum,
    "ts_mean": _reduce_mean,
    "ts_std": _reduce_std,
    "ts_min": _reduce_min,
    "ts_max": _reduce_max,
    "ts_prod": _reduce_prod,
    "ts_rank": _reduce_ts_rank,
    "ts_argmax": _reduce_argmax,
    "ts_argmin": _reduce_argmin,
    "decay_linear": _reduce_decay_linear,
    "wma": _reduce_wma,
    "regbeta": _reduce_regbeta,
    "regresi": _reduce_regresi,
}
