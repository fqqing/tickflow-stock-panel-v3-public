"""信号组合策略后端 — 把声明式事件表达式编译成矩阵策略。

让策略作者无需手写 numpy 矩阵代码：声明 ``ENTRY_SIGNAL_EXPR`` / ``EXIT_SIGNAL_EXPR``
即可，加载期由 :class:`SignalComboStrategy` 编译成实现 :class:`MatrixStrategy` 协议的
实例，交由 engine 归一化为 ``matrix_native`` 后端 —— 回测 / SignalLab / 实时零改动接入。

信号码（位掩码）：entry/exit 各用「每个信号函数占一位」的位掩码，bit i 对应
``entry_signal_ids[i]``（= 表达式引用的信号函数名，按首次出现顺序）。命中即置位，
SignalLab 据此反解「这个事件命中了哪几个信号函数」做信号函数级归因。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
)
from app.strategy.signals.combine import compile_expr_with_hits, resolve_expr_signals
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
        # entry_signal_ids / exit_signal_ids 仅保留签名兼容（engine 传入策略声明的
        # ENTRY_SIGNALS 策略级信号列）。SignalLab 归因已细化为「信号函数级」，位掩码
        # 位序统一用表达式引用的信号函数名（resolve_expr_signals 的首次出现顺序）。
        del entry_signal_ids, exit_signal_ids

        self._entry_signals = resolve_expr_signals(self.entry_expr)
        self._exit_signals = resolve_expr_signals(self.exit_expr)
        self.entry_signal_ids = self._entry_signals
        self.exit_signal_ids = self._exit_signals
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
        shape = market.shape
        if self.entry_expr:
            entry_raw, entry_hits = compile_expr_with_hits(self.entry_expr, market, params)
            entry = (
                np.ones(shape, dtype=bool)
                if entry_raw is None
                else entry_raw.astype(bool)
            )
        else:
            entry = np.zeros(shape, dtype=bool)
            entry_hits: dict[str, np.ndarray] = {}
        if self.exit_expr:
            exit_raw, exit_hits = compile_expr_with_hits(self.exit_expr, market, params)
            exit_ = (
                np.ones(shape, dtype=bool)
                if exit_raw is None
                else exit_raw.astype(bool)
            )
        else:
            exit_ = np.zeros(shape, dtype=bool)
            exit_hits: dict[str, np.ndarray] = {}
        # 位掩码：每个信号函数一位，命中置位。未命中事件处保持 -1（无信号）。
        return make_signal_matrix(
            shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=_hits_to_mask(entry_hits, self._entry_signals, entry, shape),
            exit_signal_code=_hits_to_mask(exit_hits, self._exit_signals, exit_, shape),
            entry_signal_ids=self.entry_signal_ids,
            exit_signal_ids=self.exit_signal_ids,
        )


def _hits_to_mask(
    hits: dict[str, np.ndarray],
    names: tuple[str, ...],
    combined: np.ndarray,
    shape: tuple[int, int],
) -> np.ndarray:
    """把各信号函数真值矩阵折叠成位掩码矩阵。

    ``names`` 是位序（resolve_expr_signals 的首次出现顺序，含被关闭的信号），
    bit i 对应 ``names[i]``。被开关关闭的信号（hits 里没有）位恒 0。仅在事件命中处
    写位掩码，其余保持 -1。
    """
    code = np.full(shape, -1, dtype=np.int64)
    acc = np.zeros(shape, dtype=np.int64)
    for i, name in enumerate(names):
        mask = hits.get(name)
        if mask is None:
            continue
        acc = acc | np.where(mask, np.int64(1) << i, np.int64(0))
    code[combined] = acc[combined]
    return code
