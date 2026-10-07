"""布林突破 — 突破布林上轨 + 放量（信号组合声明式）

信号函数统一层样板迁移：入场条件 ``all_of(boll_breakout_upper, vol_ratio_ge)``
声明式组合，布尔开关 require_boll_breakout / use_volume_filter 通过 enable_param 保留。
"""

META = {
    "id": "boll_breakout",
    "name": "布林突破",
    "description": "突破布林上轨 + 放量, 强势加速信号",
    "tags": ["布林", "突破"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "require_boll_breakout",
            "label": "要求突破布林上轨",
            "type": "bool",
            "default": True,
        },
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
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(boll_breakout_upper, vol_ratio_ge)"
EXIT_SIGNAL_EXPR = "boll_breakdown_lower"
ENTRY_SIGNALS = ["signal_boll_breakout_upper"]
EXIT_SIGNALS = ["signal_boll_breakdown_lower"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
LOOKBACK_DAYS = 60
