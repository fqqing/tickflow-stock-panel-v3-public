"""高换手拉升 — 换手率 > 5% 且涨幅 > 3%（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "high_turnover_surge",
    "name": "高换手拉升",
    "description": "换手率 > 5% 且涨幅 > 3%, 资金活跃",
    "tags": ["换手率", "放量", "资金"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_turnover_filter", "label": "启用换手率过滤", "type": "bool", "default": True},
        {
            "id": "min_turnover",
            "label": "最低换手率%",
            "type": "float",
            "default": 5.0,
            "min": 1.0,
            "max": 20.0,
            "step": 0.5,
        },
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
    ],
    "scoring": {"turnover_rate": 0.4, "change_pct": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(turnover_ge, change_pct_ge)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = ["signal_volume_surge"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
LOOKBACK_DAYS = 60
