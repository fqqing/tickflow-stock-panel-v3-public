"""向上趋势并突破 — 收盘上穿短上轨 + 回调后再度突破 (定量结构 QSTPXG)

源公式 (通达信条件选股 / 徐小明定量结构 8.2 定制版):

    DSG:=EMA(HIGH,26);  DXG:=EMA(LOW,26);
    CSG:=EMA(HIGH,89);  CXG:=EMA(LOW,89);
    BBB:=CROSS(CLOSE,DSG);  SSS:=CROSS(DXG,CLOSE);
    BBB_0:=BARSLAST(REF(BBB,1))+1;
    SSS_0:=BARSLAST(REF(SSS,1))+1;
    XG: BBB AND (BBB_0>SSS_0 AND (CLOSE>REF(CSG,89) AND CLOSE>REF(DSG,26)));

即: 现价上穿 26 日短上轨, 且「距上次上穿」比「距上次下穿」更久 (先跌破、再重新站上
= 回调后突破), 同时现价高于 26 日前的短上轨与 89 日前的长上轨。

矩阵原生实现 —— 全部算子走 app.backtest.matrix 的 valid_* 族, 与面板
「有效 bar (自动跳停牌日) + 前复权 OHLC」口径一致。CXG 在源公式中未参与输出, 故略去。
"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_barslast,
)
from app.backtest.matrix import (
    valid_ewm_adjust_false as ewm_adjust_false,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

_SPAN_SHORT = 26
_SPAN_LONG = 89
# EMA89 自身收敛 + REF(CSG,89) 回望 89 根 + EMA26 短轨收敛余量。
# 原值 200 (2*89+22) 是「按依赖链线性叠加」的估算, 实测 EMA 的收敛比这个估算慢:
#   9/18 探针扫 120~300 根, 信号数在 262 根后才不再变化 (261 起仍差 2 只),
#   即 200 根时有标的的 EMA89 尚未收敛 —— 表现为「同一标的今日选出、历史不选出」
#   的欠预热假阳性。261 是覆盖实测收敛点且留 1 根余量的最小取值。
# 代价: 单标的计算量 +29%。预计算窗口已同步抬到 _REFRESH_HISTORY_DAYS=480
#   日历日 (约 330 交易日), 仍覆盖 261 + warmup(60), 不触发额外重建。
_WARMUP_BARS = 261

META = {
    "id": "upward_trend_breakout",
    "name": "向上趋势并突破",
    "description": "收盘上穿26日短上轨EMA(HIGH,26)、距上次上穿比距上次下穿更久(回调后再度突破)、且站上89日长上轨与26日前的短上轨",
    "tags": ["趋势", "突破", "定量结构", "均线"],
    "asset_types": ["stock", "etf"],
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
            "id": "require_short_rail_cross",
            "label": "要求上穿26日短上轨(BBB)",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_pullback_first",
            "label": "要求先跌破再站上(BBB_0>SSS_0)",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_above_short_rail",
            "label": "要求站上26日前的短上轨",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_above_long_rail",
            "label": "要求站上89日前的长上轨",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_retest_breakout"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20


class UpwardTrendBreakoutMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return _WARMUP_BARS

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        valid = np.isfinite(market.close)
        close = market.close
        previous_close = shift(close, 1, valid)

        # DSG / DXG / CSG —— 26 日与 89 日高低轨 EMA
        short_upper = ewm_adjust_false(market.high, valid, span=_SPAN_SHORT)
        short_lower = ewm_adjust_false(market.low, valid, span=_SPAN_SHORT)
        long_upper = ewm_adjust_false(market.high, valid, span=_SPAN_LONG)

        # BBB := CROSS(CLOSE, DSG) / SSS := CROSS(DXG, CLOSE)
        cross_up = (close > short_upper) & (previous_close <= shift(short_upper, 1, valid))
        cross_down = (short_lower > close) & (shift(short_lower, 1, valid) <= previous_close)

        # BBB_0 := BARSLAST(REF(BBB,1)) + 1, SSS_0 同理 —— 距上一次上穿/下穿的有效 bar 数
        bars_since_up = valid_barslast(
            shift(cross_up.astype(np.float32), 1, valid), valid
        ) + np.float32(1.0)
        bars_since_down = valid_barslast(
            shift(cross_down.astype(np.float32), 1, valid), valid
        ) + np.float32(1.0)

        entry = np.ones(market.shape, dtype=bool)
        if params.get("require_short_rail_cross", True):
            entry &= cross_up
        if params.get("require_pullback_first", True):
            entry &= bars_since_up > bars_since_down
        if params.get("require_above_short_rail", True):
            entry &= close > shift(short_upper, _SPAN_SHORT, valid)
        if params.get("require_above_long_rail", True):
            entry &= close > shift(long_upper, _SPAN_LONG, valid)
        entry &= valid

        ma20 = matrix_feature(market, "ma20")
        exit_ = valid & (close < ma20) & (previous_close >= shift(ma20, 1, valid))

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_retest_breakout",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = UpwardTrendBreakoutMatrixStrategy()
