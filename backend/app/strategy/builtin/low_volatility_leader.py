"""低波动龙头 — 正动量 + 低波动 + MA20上方（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "low_volatility_leader",
    "name": "低波动龙头",
    "description": "20日动量为正 + 年化波动 < 30% + MA20上方",
    "tags": ["低波动", "龙头"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "require_positive_momentum",
            "label": "要求20日动量为正",
            "type": "bool",
            "default": True,
        },
        {"id": "use_volatility_filter", "label": "启用波动率过滤", "type": "bool", "default": True},
        {
            "id": "vol_max",
            "label": "最大年化波动",
            "type": "float",
            "default": 0.30,
            "min": 0.05,
            "max": 1.0,
            "step": 0.01,
        },
        {
            "id": "require_above_ma20",
            "label": "要求收盘价在MA20上方",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(momentum_positive, annual_vol_below, close_above_ma20)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = ["signal_ma20_breakout"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 30
LOOKBACK_DAYS = 60
