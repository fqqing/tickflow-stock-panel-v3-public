"""MA金叉 — MA5上穿MA20 + 量能配合 + MA60上方（信号组合声明式）

本策略是「信号函数统一层」的样板迁移：入场条件由三个原子信号组合
``all_of(ma_golden_cross, vol_ratio_ge, close_above_ma60)`` 声明，
由 :class:`app.strategy.signals.backend.SignalComboStrategy` 编译执行。
布尔开关参数（require_ma_golden / use_volume_filter / require_above_ma60）
通过信号的 enable_param 机制保留，关掉即等价于把对应信号从组合移除。
"""

META = {
    "id": "ma_golden_cross",
    "name": "MA 金叉",
    "description": "MA5上穿MA20当日触发, 量能配合",
    "tags": ["均线", "金叉"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_ma_golden", "label": "要求MA5上穿MA20", "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 1.2,
            "min": 0.5,
            "max": 5.0,
            "step": 0.1,
        },
        {
            "id": "require_above_ma60",
            "label": "要求收盘价在MA60上方",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"momentum_20d": 0.5, "vol_ratio_5d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(ma_golden_cross, vol_ratio_ge, close_above_ma60)"
EXIT_SIGNAL_EXPR = "ma_dead_cross"
ENTRY_SIGNALS = ["signal_ma_golden_5_20"]
EXIT_SIGNALS = ["signal_ma_dead_5_20"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
LOOKBACK_DAYS = 60
