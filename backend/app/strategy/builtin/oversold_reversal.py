"""超跌反转 — RSI14 < 30 + 涨幅 > 1% + 站上 MA5（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "oversold_reversal",
    "name": "超跌反转",
    "description": "RSI14 < 30超卖 + 涨幅 > 1% + 站上MA5, 超卖反转信号",
    "tags": ["超跌", "反弹", "RSI"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_rsi_filter", "label": "启用RSI过滤", "type": "bool", "default": True},
        {
            "id": "rsi_max",
            "label": "RSI上限",
            "type": "float",
            "default": 30.0,
            "min": 10.0,
            "max": 50.0,
            "step": 1.0,
        },
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 5.0,
            "step": 0.5,
        },
        {
            "id": "require_above_ma5",
            "label": "要求收盘价在MA5上方",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"change_pct": 0.4, "rsi_14": 0.3, "vol_ratio_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(rsi_below, change_pct_ge, close_above_ma5)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 15
LOOKBACK_DAYS = 60
