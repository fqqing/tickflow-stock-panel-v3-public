"""信号组合表达式编译器 — 借鉴 czsc 的 signals_all / signals_any / signals_not。

把一个声明式表达式字符串编译成布尔矩阵：

    "ma_golden_cross"                            -> 单信号
    "all_of(ma_golden_cross, vol_ratio_ge)"      -> AND 组合
    "any_of(a, b)"                               -> OR 组合
    "not_of(a)"                                  -> NOT
    "all_of(a, any_of(b, c))"                    -> 嵌套组合

使用 ``ast`` 解析（不执行任意代码），只接受 ``all_of/any_of/not_of`` 三个组合函数
与裸信号名，杜绝注入。与 czsc 的差异：czsc 在 Python 端直接调用 signals_all 等函数，
这里用字符串表达式让策略可以声明式地描述事件（无需写任何计算代码）。
"""
from __future__ import annotations

import ast
from typing import Any

import numpy as np

from app.backtest.matrix import MarketDataMatrix
from app.strategy.signals.registry import get_signal


class SignalExprError(ValueError):
    """组合表达式非法。"""


_COMBINERS = frozenset({"all_of", "any_of", "not_of"})


def compile_expr(
    expr: str,
    market: MarketDataMatrix,
    params: dict[str, Any],
) -> np.ndarray:
    """把组合表达式编译成布尔矩阵。

    Args:
        expr: 表达式字符串。
        market: 行情矩阵。
        params: 扁平参数字典（与策略 META["params"] 共享命名空间，透传给每个信号函数）。

    Returns:
        shape == market.shape 的布尔矩阵。
    """
    if not isinstance(expr, str) or not expr.strip():
        raise SignalExprError("signal expr must be a non-empty string")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise SignalExprError(f"invalid signal expr {expr!r}: {exc}") from exc
    result = _eval(tree.body, market, params)
    # 顶层单个信号被关闭（enable_param=False）→ 无条件（全 True）。
    if result is None:
        return np.ones(market.shape, dtype=bool)
    if not isinstance(result, np.ndarray):
        raise SignalExprError(f"signal expr {expr!r} did not produce a matrix")
    return result.astype(bool)


def compile_expr_with_hits(
    expr: str,
    market: MarketDataMatrix,
    params: dict[str, Any],
) -> tuple[np.ndarray | None, dict[str, np.ndarray]]:
    """编译表达式，并额外收集每个原子信号函数的真值矩阵。

    与 :func:`compile_expr` 的区别：返回 ``(组合结果, hits)``，其中 ``hits`` 是
    ``{信号函数名: 布尔矩阵}``，供信号组合策略生成「逐信号位掩码」做 SignalLab
    信号函数级归因。被开关关闭的信号（返回 None）不进入 hits。

    注意：``hits`` 里的键顺序由 ast.walk 的访问顺序决定，与 :func:`resolve_expr_signals`
    的「首次出现顺序」可能不一致 —— 位掩码的位序应统一用后者（静态、稳定）。
    """
    if not isinstance(expr, str) or not expr.strip():
        raise SignalExprError("signal expr must be a non-empty string")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise SignalExprError(f"invalid signal expr {expr!r}: {exc}") from exc
    hits: dict[str, np.ndarray] = {}
    result = _eval(tree.body, market, params, hits=hits)
    return result, hits


def resolve_expr_signals(expr: str) -> tuple[str, ...]:
    """静态解析表达式引用的全部信号函数名（不执行），按首次出现顺序返回。

    用于信号组合策略在加载期汇总 required_fields / warmup，无需真实行情矩阵。
    """
    if not isinstance(expr, str) or not expr.strip():
        return ()
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise SignalExprError(f"invalid signal expr {expr!r}: {exc}") from exc

    names: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _COMBINERS:
            if node.id not in seen:
                seen.add(node.id)
                names.append(node.id)
    return tuple(names)


def _eval(
    node: ast.AST,
    market: MarketDataMatrix,
    params: dict[str, Any],
    hits: dict[str, np.ndarray] | None = None,
) -> np.ndarray | None:
    if isinstance(node, ast.Name):
        result = _invoke_signal(node.id, market, params)
        if result is not None and hits is not None:
            hits[node.id] = result
        return result
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _COMBINERS:
            raise SignalExprError(
                f"only all_of/any_of/not_of are allowed, got {ast.unparse(node.func)!r}"
            )
        name = node.func.id
        args = [_eval(a, market, params, hits) for a in node.args]
        # 被关闭的信号（enable_param=False）返回 None，在组合里视为「移除」。
        active = [a for a in args if a is not None]
        if name == "all_of":
            if not args:
                raise SignalExprError("all_of requires at least one signal")
            if not active:
                return np.ones(market.shape, dtype=bool)  # 全部关闭 → 无条件
            return np.logical_and.reduce(active)
        if name == "any_of":
            if not args:
                raise SignalExprError("any_of requires at least one signal")
            if not active:
                return np.zeros(market.shape, dtype=bool)  # 全部关闭 → 永假
            return np.logical_or.reduce(active)
        if name == "not_of":
            if len(args) != 1:
                raise SignalExprError("not_of requires exactly one signal")
            if args[0] is None:
                raise SignalExprError("not_of 的信号被开关关闭，表达式无意义")
            return ~args[0]
    raise SignalExprError(f"unsupported expression node: {type(node).__name__}")


def _invoke_signal(
    name: str,
    market: MarketDataMatrix,
    params: dict[str, Any],
) -> np.ndarray | None:
    definition = get_signal(name)
    # 开关关闭 → 中性化（返回 None，由组合上下文决定移除方式）。
    if definition.enable_param is not None and params.get(definition.enable_param) is False:
        return None
    result = definition.fn(market, **params)
    if not isinstance(result, np.ndarray) or result.shape != market.shape:
        raise SignalExprError(
            f"signal {name!r} must return a matrix of shape {market.shape}, "
            f"got {getattr(result, 'shape', None)}"
        )
    return result.astype(bool)
