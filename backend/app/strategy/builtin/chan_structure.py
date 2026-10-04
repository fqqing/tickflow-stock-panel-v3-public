"""缠论结构买卖点 — 一/二/三买进场, 缠论卖点离场。

与面板里其它矩阵策略的根本区别: 缠论的笔/中枢是**顺序状态机** (笔的延伸、
中枢扩展要看「之前所有笔」), 矩阵算子没有累积语义 (无 cum_max / expanding),
所以这里在 ``compute_signals`` 里按标的顺序跑 ``app.indicators.chan.analyze``,
再把信号落到 ``(时间 x 标的)`` 的 entry / exit 矩阵上。

**确认延迟 (无前视关键)**: ``analyze`` 给出的信号索引是笔终点那根 K 线, 而该笔
要等**后一根 K 线**出现才能确认 (分型需要右邻 K 线)。所以信号在序列位置 ``p`` 上,
最早只能在 ``p + 1`` 根收盘后才可见 —— 本策略把 entry / exit 标在 ``p + 1`` 上,
配合框架的 ``entry_fill = "open_t+1"``, 实际成交在 ``p + 2`` 开盘, 与事件研究的
「信号确认日收盘 + 次日成交」口径一致。
实测验证 (2026-09-18, 全市场 5466 只 800 根 walk-forward 事件研究): 188323 个信号里
166134 个 (88.2%) 的首次可见日恰好是「批算信号索引 + 1」, 其余 12.2% 全部集中在
滑动窗口左边界被截断的那批 (窗口起点仍在移动时结构会重新形成), 与真实盘面无关。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
)
from app.indicators.chan import MIN_BARS_FOR_STRUCTURE, analyze

META = {
    "id": "chan_structure",
    "name": "缠论结构买卖点",
    "description": "一/二/三买进场, 缠论卖点离场 (可退回纯止损), 顺序结构算法",
    "tags": ["缠论", "结构", "买卖点"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_1buy", "label": "启用一买", "type": "bool", "default": True},
        {"id": "use_2buy", "label": "启用二买", "type": "bool", "default": False},
        {"id": "use_3buy", "label": "启用三买", "type": "bool", "default": True},
        {"id": "use_sell_exit", "label": "缠论卖点离场", "type": "bool", "default": True},
        {"id": "use_1sell", "label": "一卖离场", "type": "bool", "default": True},
        {"id": "use_2sell", "label": "二卖离场", "type": "bool", "default": True},
        {"id": "use_3sell", "label": "三卖离场", "type": "bool", "default": True},
        {"id": "strict", "label": "严格笔", "type": "bool", "default": True},
        {
            "id": "min_bars",
            "label": "最少K线",
            "type": "int",
            "default": 120,
            "min": 20,
            "max": 600,
            "step": 10,
        },
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_chan_1buy", "signal_chan_2buy", "signal_chan_3buy"]
EXIT_SIGNALS = ["signal_chan_1sell", "signal_chan_2sell", "signal_chan_3sell"]

# 默认止损/最长持有: 与面板其它策略同量级, 让「缠论卖点」和「止损」是两个可比的口子。
# MAX_HOLD_DAYS 刻意停在 30 —— 它参与 matrix 磁盘缓存 profile 的 forward_bars 取值
# (现网最大就是 30), 设成 60 会让所有策略的缓存档位一起变、被迫全量重建。
# 需要更长持有期时用回测的 overrides={"max_hold_days": N} 覆盖, 那条路不碰缓存 profile。
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 30

_BUY_KINDS = ("1buy", "2buy", "3buy")
_SELL_KINDS = ("1sell", "2sell", "3sell")

# 面板至少要给这么多根才动手: 20 根是缠论能构出结构的下限, 再少分型都凑不齐。
_MIN_BARS = MIN_BARS_FOR_STRUCTURE


def _picked(params: dict, flags: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """把一组 bool 参数收成启用的信号类型元组。"""
    return tuple(kind for flag, kind in flags if params.get(flag, False))


class ChanStructureMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        # MACD 用序列首值播种 (residual (1-2/(span+1))^n), span=26 时 120 根残留约 1%,
        # 一买要拿 MACD 柱面积做背驰比较, 面板太短会让背驰判定系统性偏移。
        return max(int(params.get("min_bars", 120) or 120), 60)

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        buy_kinds = _picked(
            params,
            (("use_1buy", "1buy"), ("use_2buy", "2buy"), ("use_3buy", "3buy")),
        )
        sell_kinds = _picked(
            params,
            (("use_1sell", "1sell"), ("use_2sell", "2sell"), ("use_3sell", "3sell")),
        )
        use_sell_exit = bool(params.get("use_sell_exit", True)) and bool(sell_kinds)
        strict = bool(params.get("strict", True))
        min_bars = max(int(params.get("min_bars", 120) or 120), _MIN_BARS)

        shape = market.shape
        if not buy_kinds:
            return make_signal_matrix(shape, entry_signal_ids=(), exit_signal_ids=())

        entry = np.zeros(shape, dtype=np.uint8)
        exit_ = np.zeros(shape, dtype=np.uint8)
        entry_code = np.full(shape, -1, dtype=np.int16)
        exit_code = np.full(shape, -1, dtype=np.int16)

        # ⚠️ 信号码必须是**声明列表里的下标**, 不能按全量 kind 顺序固定映射 ——
        # 关掉二买后 3buy 的下标会从 2 变成 1, 用固定表会让成交记录标错信号名。
        entry_ids = tuple(f"signal_chan_{kind}" for kind in buy_kinds)
        exit_ids = tuple(f"signal_chan_{kind}" for kind in sell_kinds) if use_sell_exit else ()
        entry_index = {kind: code for code, kind in enumerate(buy_kinds)}
        exit_index = {kind: code for code, kind in enumerate(sell_kinds)}

        index = market.valid_bars
        offsets = index.offsets
        rows = index.rows
        high_all = market.high
        low_all = market.low
        close_all = market.close

        for asset_id in range(shape[1]):
            start = int(offsets[asset_id])
            stop = int(offsets[asset_id + 1])
            if stop - start < min_bars:
                continue
            positions = rows[start:stop]
            analysis = analyze(
                np.asarray(high_all[positions, asset_id], dtype=np.float64),
                np.asarray(low_all[positions, asset_id], dtype=np.float64),
                np.asarray(close_all[positions, asset_id], dtype=np.float64),
                strict=strict,
            )
            limit = int(positions.shape[0])
            for signal in analysis.signals:
                # 信号落在序列位置 signal.index, 后一根有效 K 线才确认 => 标在 index + 1。
                confirm = signal.index + 1
                if confirm >= limit:
                    continue
                row = int(positions[confirm])
                if signal.kind in entry_index:
                    entry[row, asset_id] = 1
                    entry_code[row, asset_id] = entry_index[signal.kind]
                elif use_sell_exit and signal.kind in exit_index:
                    exit_[row, asset_id] = 1
                    exit_code[row, asset_id] = exit_index[signal.kind]

        return make_signal_matrix(
            shape,
            entry=entry,
            exit=exit_,
            entry_signal_code=entry_code,
            exit_signal_code=exit_code,
            entry_signal_ids=entry_ids,
            exit_signal_ids=exit_ids,
        )


MATRIX_STRATEGY = ChanStructureMatrixStrategy()
