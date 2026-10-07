"""信号组合策略后端 — 把声明式事件表达式编译成矩阵策略。

让策略作者无需手写 numpy 矩阵代码：声明 ``ENTRY_SIGNAL_EXPR`` / ``EXIT_SIGNAL_EXPR``
即可，加载期由 :class:`SignalComboStrategy` 编译成实现 :class:`MatrixStrategy` 协议的
实例，交由 engine 归一化为 ``matrix_native`` 后端 —— 回测 / SignalLab / 实时零改动接入。

单事件约束（首版）：一个信号组合策略只有一个 entry 事件和一个 exit 事件，因此
``entry_signal_ids`` / ``exit_signal_ids`` 各至多一个元素。多事件拆解归因留待后续。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
)
from app.strategy.signals.combine import compile_expr, resolve_expr_signals
from app.strategy.signals.registry import get_signal


class SignalComboStrategy:
    """声明式信号组合策略（实现 MatrixStrategy 协议）。"""

    def __init__(
        self,
        entry_expr: str | None,
        exit_expr: str | None,
        *,
        entry_signal_ids: tuple[str, ...] = (),
        exit_signal_ids: tuple[str, ...] = (),
        warmup_override: int | None = None,
    ) -> None:
        if not entry_expr and not exit_expr:
            raise ValueError(
                "signal_combo strategy must declare ENTRY_SIGNAL_EXPR or EXIT_SIGNAL_EXPR"
            )
        self.entry_expr = (entry_expr or "").strip()
        self.exit_expr = (exit_expr or "").strip()
        self.entry_signal_ids = tuple(entry_signal_ids)
        self.exit_signal_ids = tuple(exit_signal_ids)

        if self.entry_expr and len(self.entry_signal_ids) > 1:
            raise ValueError("signal_combo 首版单个 entry 事件至多一个信号名")
        if self.exit_expr and len(self.exit_signal_ids) > 1:
            raise ValueError("signal_combo 首版单个 exit 事件至多一个信号名")

        self._entry_signals = resolve_expr_signals(self.entry_expr)
        self._exit_signals = resolve_expr_signals(self.exit_expr)
        referenced = set(self._entry_signals) | set(self._exit_signals)
        if not referenced:
            raise ValueError("signal expr 未引用任何已注册信号函数")

        self._required_fields = frozenset(
            field
            for name in referenced
            for field in get_signal(name).required_fields
        )
        self._warmup = max(
            (get_signal(name).warmup for name in referenced),
            default=0,
        )
        if warmup_override is not None and warmup_override > self._warmup:
            self._warmup = int(warmup_override)

    def required_fields(self) -> frozenset[str]:
        return self._required_fields

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return self._warmup

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict[str, Any],
    ) -> SignalMatrix:
        entry = (
            compile_expr(self.entry_expr, market, params)
            if self.entry_expr
            else np.zeros(market.shape, dtype=bool)
        )
        exit_ = (
            compile_expr(self.exit_expr, market, params)
            if self.exit_expr
            else np.zeros(market.shape, dtype=bool)
        )
        # 单事件：命中信号码恒为 0（entry_signal_ids 的第一个下标）。
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=self.entry_signal_ids,
            exit_signal_ids=self.exit_signal_ids,
        )
