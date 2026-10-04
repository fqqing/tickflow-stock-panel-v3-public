"""钝化加低九 — 底部钝化的首次信号与下跌九转第 9 天共振

源公式 (``AKL公式解析/钝化加低九选股_源码.txt``, 缩写 DHJDJXG):

    A1 = C > REF(C,4);  A2 = C < REF(C,4)
    T1 = A2 AND REF(A1,1);  T2 = A2 AND REF(T1,1);  ...  T9 = A2 AND REF(T8,1)
    ... 底钝化链与「底部结构选股」完全相同 ...
    输出 = 底钝化 AND T9

与「底部结构」共用同一条判定链 (:mod:`_quant_structure`), 只有两处差异, 且都是
原公式本身的差异, 不是实现取舍:

- **隔峰底钝化**用 ``REF(MACD, 2)`` 而不是 ``REF(MACD, 1)`` -> ``macd_prev_bars=2``
- 输出换成 ``底钝化 AND T9`` —— 底钝化的"首次信号"当日恰好也是下跌九转第 9 天

时间上它比「底部结构」更早: 底钝化是底部结构的前置条件, 而 T9 又是下跌末段的
择时确认, 两者共振时点通常落在底部结构形成之前几天。
"""

import numpy as np
from _quant_structure import bottom_stale_chain, nine_turn_down

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_shift,
)

_DEFAULT_SCAN_DAYS = 1  # 源公式就是当日信号
_MAX_SCAN_DAYS = 20
_WARMUP_BARS = 120

META = {
    "id": "stale_nine_turn",
    "name": "钝化加低九",
    "description": "底部钝化首次成立(价格创新低而DIF不创新低)当日, 同时是下跌九转第9天 (近N个交易日内出现即入选)",
    "tags": ["底部", "背离", "九转", "择时"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 300,
        "market_cap_min": 10e8,
        "amount_min": 0.5e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "scan_days",
            "label": "近N个交易日内出现信号",
            "type": "int",
            "default": _DEFAULT_SCAN_DAYS,
            "min": 1,
            "max": _MAX_SCAN_DAYS,
            "step": 1,
        },
    ],
    "scoring": {"momentum_20d": 0.3, "vol_ratio_5d": 0.3, "change_pct": 0.4},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_stale_nine_turn"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 30


class StaleNineTurnMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return _WARMUP_BARS

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        chain = bottom_stale_chain(market, macd_prev_bars=2)
        valid = chain["valid"]
        resonance = chain["first_stale"] & nine_turn_down(market.close, valid)

        scan_days = _resolve_scan_days(params)
        if scan_days <= 1:
            entry = resonance.copy()
        else:
            window = valid_rolling_max(resonance.astype(np.float32), valid, scan_days)
            entry = window >= np.float32(0.5)
        entry &= valid

        close = market.close
        ma20 = matrix_feature(market, "ma20")
        previous_close = valid_shift(close, 1, valid)
        exit_ = valid & (close < ma20) & (previous_close >= valid_shift(ma20, 1, valid))

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_stale_nine_turn",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


def _resolve_scan_days(params: dict) -> int:
    """扫描窗口: 非法值回落到源公式默认值 (1), 并夹到 [1, 20]。"""
    raw = params.get("scan_days", _DEFAULT_SCAN_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SCAN_DAYS
    return min(max(days, 1), _MAX_SCAN_DAYS)


MATRIX_STRATEGY = StaleNineTurnMatrixStrategy()
