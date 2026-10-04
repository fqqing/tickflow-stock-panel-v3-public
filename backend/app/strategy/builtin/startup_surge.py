"""启动策略 — 涨停基因 + 跳空缺口 + 三连阳 + 温和放量 (公司版选股)

源脚本 (yanwen/GP/pro_online/services/filter_stock.py, is_valid_stock):

    has_limit_up:  close.pct_change().tail(15).max() >= 9.8      近15日有单日涨幅>=9.8% (近似涨停)
    has_up_gap:    (low > high.shift(1)).tail(10).any()           近10日存在向上跳空缺口
    pre_rise_strong_and_today: 近3日(含当日)全部收阳且实体均值 > 1%
    volume_expand: vol / vol.shift(1).rolling(5).mean() in [1.5, 3]  当日量比前5日均量1.5~3倍
    四条 AND; 且至少 31 根历史 (源脚本 i 从 30 起步)。

矩阵原生实现 —— 全部算子走 app.backtest.matrix 的 valid_* 族, 与面板
「有效 bar (自动跳停牌日) + 前复权 OHLC」口径一致:
- pct_change / shift(1) / rolling 均在有效 bar 序列上进行, 停牌日不计入窗口;
- 「至少 31 根」用 shift(close, 30) 非空表达 (有 30 根更早的有效 bar);
- 源脚本的异动/预警列 (3/5/10/30日涨跌幅) 为展示富化, 不参与选股, 故略去。
"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_rolling_max as rolling_max,
)
from app.backtest.matrix import (
    valid_rolling_mean as rolling_mean,
)
from app.backtest.matrix import (
    valid_rolling_min as rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

_LIMIT_UP_WINDOW = 15  # 近 15 日内出现过涨停 (单日涨幅 >= 9.8%)
_LIMIT_UP_PCT = 0.098
_GAP_WINDOW = 10  # 近 10 日内出现过向上跳空
_YANG_DAYS = 3  # 最近 3 日连阳
_YANG_BODY_MIN = 0.01  # 实体均值下限
_VOL_BASE_DAYS = 5  # 量比基准: 前 5 日均量
_VOL_RATIO_MIN = 1.5
_VOL_RATIO_MAX = 3.0
_MIN_HISTORY = 31  # 源脚本 i 从 30 起步 => 至少 31 根有效 bar
_WARMUP_BARS = _MIN_HISTORY + 5


META = {
    "id": "startup_surge",
    "name": "启动策略",
    "description": "近15日有单日涨幅>=9.8%(涨停基因)且近10日存在向上跳空缺口, 最近3日连阳且实体均值>1%, 当日量比前5日均量1.5~3倍(温和放量启动)",
    "tags": ["异动", "量价", "短线", "启动"],
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
            "id": "require_recent_limit_up",
            "label": "近15日有涨停(涨幅>=9.8%)",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_recent_gap",
            "label": "近10日有向上跳空缺口",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_three_yang",
            "label": "最近3日连阳且实体均值>1%",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_volume_ratio",
            "label": "当日量比前5日均量1.5~3倍",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_startup_surge"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20


class StartupSurgeMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return _WARMUP_BARS

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        close = market.close
        open_ = market.open
        high = market.high
        low = market.low
        volume = market.volume
        valid = (
            np.isfinite(close)
            & np.isfinite(open_)
            & np.isfinite(high)
            & np.isfinite(low)
            & np.isfinite(volume)
            & (open_ > 0)
        )

        # 1) 近 15 日内出现过单日涨幅 >= 9.8% (含当日)
        pct = close / shift(close, 1, valid) - 1.0
        recent_limit_up = rolling_max(pct, valid, _LIMIT_UP_WINDOW) >= _LIMIT_UP_PCT

        # 2) 近 10 日内存在向上跳空 (当日最低 > 上一有效 bar 最高)
        gap_up = low > shift(high, 1, valid)
        recent_gap = rolling_max(gap_up.astype(np.float32), valid, _GAP_WINDOW) >= 0.5

        # 3) 最近 3 根有效 bar 全部收阳且实体均值 > 1%
        body = (close - open_) / open_
        three_yang = (rolling_min(body, valid, _YANG_DAYS) > 0) & (
            rolling_mean(body, valid, _YANG_DAYS) > _YANG_BODY_MIN
        )

        # 4) 当日量 / 前 5 根有效 bar 均量 在 [1.5, 3] (基准为 0 时结果 inf/NaN -> False)
        vol_base = shift(rolling_mean(volume, valid, _VOL_BASE_DAYS), 1, valid)
        with np.errstate(divide="ignore", invalid="ignore"):
            vol_ratio = volume / vol_base
        volume_ok = (vol_ratio >= _VOL_RATIO_MIN) & (vol_ratio <= _VOL_RATIO_MAX)

        # 源脚本: 至少 31 根有效 bar 才开始评估 (shift(close, 30) 非空 <=> 之前有 30 根有效 bar)
        enough_history = np.isfinite(shift(close, _MIN_HISTORY - 1, valid))

        entry = valid & enough_history
        if params.get("require_recent_limit_up", True):
            entry &= recent_limit_up
        if params.get("require_recent_gap", True):
            entry &= recent_gap
        if params.get("require_three_yang", True):
            entry &= three_yang
        if params.get("require_volume_ratio", True):
            entry &= volume_ok

        ma20 = matrix_feature(market, "ma20")
        previous_close = shift(close, 1, valid)
        exit_ = valid & (close < ma20) & (previous_close >= shift(ma20, 1, valid))

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_startup_surge",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = StartupSurgeMatrixStrategy()
