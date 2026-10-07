"""信号函数统一层 — 借鉴 czsc「信号-事件-交易」体系，纯 Python + NumPy 实现。

借鉴 czsc 的三点（不引入其 Rust 核心）：
1. 统一信号函数签名 + 注册表（:mod:`registry`）
2. 信号逻辑组合 all_of / any_of / not_of（:mod:`combine`）
3. 信号函数 → 事件（组合表达式）→ 策略 的层次（:mod:`backend`）

信号函数契约
------------
一个信号函数接收 ``MarketDataMatrix`` 与扁平参数字典，返回布尔矩阵（True=信号触发）。
注册后即可在策略里用声明式表达式引用，例如 ``all_of(ma_golden_cross, vol_ratio_ge)``。

与 czsc 的差异：czsc 信号函数面向单标的时序对象（CZSC）并多级别联立；
v3 信号函数面向全市场矩阵（MarketDataMatrix），保持 v3 既有的向量化回测口径。
"""
from __future__ import annotations

from app.strategy.signals.registry import (
    SignalDef,
    get_signal,
    list_signals,
    signal,
    signal_catalog,
)
from app.strategy.signals.combine import (
    compile_expr,
    resolve_expr_signals,
    SignalExprError,
)

# 触发 library 内所有信号函数的注册
from app.strategy.signals import library  # noqa: F401  (注册副作用)

__all__ = [
    "SignalDef",
    "signal",
    "get_signal",
    "list_signals",
    "signal_catalog",
    "compile_expr",
    "resolve_expr_signals",
    "SignalExprError",
    "library",
]
