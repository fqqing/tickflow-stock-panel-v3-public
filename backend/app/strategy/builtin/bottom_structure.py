"""底部结构 — 价格创新低而 DIF 不创新低的底背离结构首次形成

源公式 (``AKL公式解析/底部结构选股_源码.txt``, 缩写 DBJGXG):

    DIFF = EMA(C,12) - EMA(C,26); DEA = EMA(DIFF,9); MACD = (DIFF-DEA)*2
    N1 = BARSLAST(CROSS(DEA,DIFF)); M1 = BARSLAST(CROSS(DIFF,DEA))
    CL1 = LLV(C, N1+1); DIFL1 = LLV(DIFF, N1+1)      # 本轮下跌的收盘/DIF 低点
    CL2/DIFL2 = REF(..., M1+1)                        # 上一轮同类低点
    直接底钝化 = CL1<CL2 AND DIFL1>DIFL2 AND REF(MACD,1)<0 AND DIFL2<0
    隔峰底钝化 = CL1<CL3 AND DIFL1<DIFL2 AND DIFL1>DIFL3 AND DIFF<DEA ...
    底部结构 = DIFF>REF(DIFF,1) AND REF(底部钝化,1) AND DIFL1*0.9884 < DIFF
    输出 = 底结构形成 = 底部结构首次成立

矩阵原生实现 —— 判定链在共享模块 :mod:`_quant_structure` 里, 与「钝化加低九」
策略复用同一份逻辑; 算子全部走 app.backtest.matrix 的 ``valid_*`` 族
(有效 bar 口径: 停牌日不占窗口位置)。

⚠️ 与 ``qushiqinlong/底部结构选股.py`` 的差异只有一处, 且已对齐: 早期版本误把
``底部结构`` 写成 ``REF(底钝化,1)``。AKL 源码原文用的是 ``REF(底部钝化,1)``,
用户脚本用的也是 ``底部钝化`` —— 两边本来就一致, 是面板实现偏了 (2026-09-17 修)。
``底钝化`` 在源码里只被 ``M4`` 引用, 服务于 ``底结构消失`` 的指标图文字标注,
不在选股链上。
"""

import numpy as np
from _quant_structure import bottom_stale_chain

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_max,
    valid_shift,
)

_DEFAULT_SCAN_DAYS = 1  # 源公式就是当日信号, 默认不放大扫描窗口
_MAX_SCAN_DAYS = 20

# EMA26/EMA9 的收敛 + BARSLAST 链至少要走完两轮死叉/金叉, 120 根足够稳
_WARMUP_BARS = 120

META = {
    "id": "bottom_structure",
    "name": "底部结构",
    "description": "价格创出本轮新低而 DIF 未创新低(底背离)形成底部钝化后, 首次出现底部结构 (近N个交易日内出现即入选)",
    "tags": ["底部", "背离", "结构", "MACD"],
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

ENTRY_SIGNALS = ["signal_bottom_structure"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 30


class BottomStructureMatrixStrategy:
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
        chain = bottom_stale_chain(market)
        valid = chain["valid"]
        formed = chain["formed"]

        scan_days = _resolve_scan_days(params)
        if scan_days <= 1:
            entry = formed.copy()
        else:
            window = valid_rolling_max(formed.astype(np.float32), valid, scan_days)
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
            entry_signal_ids=("signal_bottom_structure",),
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


MATRIX_STRATEGY = BottomStructureMatrixStrategy()
