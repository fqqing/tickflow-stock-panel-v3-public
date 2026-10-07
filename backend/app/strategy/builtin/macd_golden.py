"""MACD金叉放量 — MACD金叉当日 + 量能放大（信号组合声明式）

信号函数统一层样板迁移：入场条件 ``all_of(macd_golden, vol_ratio_ge)`` 声明式组合，
布尔开关 require_macd_golden / use_volume_filter 通过 enable_param 机制保留。
"""

META = {
    "id": "macd_golden",
    "name": "MACD 金叉放量",
    "description": "MACD金叉当日 + 量能放大",
    "tags": ["MACD", "金叉", "放量"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_macd_golden", "label": "要求MACD金叉", "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 1.5,
            "min": 0.5,
            "max": 5.0,
            "step": 0.1,
        },
    ],
    "scoring": {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(macd_golden, vol_ratio_ge)"
EXIT_SIGNAL_EXPR = "macd_dead"
ENTRY_SIGNALS = ["signal_macd_golden"]
EXIT_SIGNALS = ["signal_macd_dead"]
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 20
LOOKBACK_DAYS = 60
