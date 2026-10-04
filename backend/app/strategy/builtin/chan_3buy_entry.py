"""缠论三买回踩 — 只做三买, 并用「距买点价上限」做**不追高**的风控闸门。

与 ``chan_structure`` 的区别: 那个把缠论所有买卖点当信号全量铺开; 这里**只在三买上做
短线**, 并且多一道价格位置闸门 (见下)。

设计依据 (2026-09-18, 超声电子 000823.SZ 复盘 + 全市场事件研究):
- 一买是**左侧**信号 (背驰抄底), 60 日 +6.15% / 胜率 51.8% —— 适合中线埋伏, 短线太慢;
- 三买是**右侧**信号 (离开中枢后回踩不破), 60 日仅 +1.39%, 但**5 日平均最大涨幅 +6.31%**
  —— 收益集中在确认后的第一周, 这正是短线要的窗口;
- 同一段强趋势里卖点会**反复触发** (超声电子 05-14 一卖之后还涨 69%), 所以离场用
  「缠论卖点 + 硬止损」两条腿。

⚠️ **闸门的定位 (实测校正, 别再当收益因子用)**: 三买信号落在回踩笔的终点
(``signal.index``), 该笔终点价 ``signal.price`` 就是「买点价」; 信号确认 (``index + 1``)
当天收盘相对它的溢价超过 ``max_premium`` 就不入场。
最初的设计假设是「越贴近买点价越好」, **但全市场实测否掉了这个假设** ——
(2026-09-18, 620 根 x 20301 只 A 股, 13,996 个三买信号, 进场 = 确认日次日开盘):
- 溢价与后续 5 日收益的 Spearman 相关只有 **+0.06** (20 日 **-0.016**), 且**逐季翻转**
  (-0.184 ~ +0.257) ⇒ 溢价**不是**可靠的收益预测因子;
- 溢价与「确认日前 20 日年化波动」相关 **+0.68** —— 溢价高本质上就是**这只票波动大**,
  而 ``corr(vol20, ret5) = +0.24``; 按波动分层后溢价效应**变号**
  (低波动层: 高溢价更好 +2.38% vs +1.39%; 中/高波动层: 低溢价更好);
- 中位收益在各溢价档位几乎走平甚至下滑 (>20% 档 5 日均值 +11.53% 但**中位仅 +1.87%**,
  20 日中位 **-2.25%**) ⇒ 高溢价那点均值优势是**少数右尾彩票**撑起来的, 不是可复制的胜率。

**所以闸门只当「不追高」的风控用**, 价值在左尾而不是均值: 上限从「无」收紧到 5%,
20 日收益的 p5 从 **-15.13% 改善到 -9.30%**、跌超 8% 的比例从 **12.6% 降到 6.6%**,
而 5 日/20 日**均值几乎不变** (-0.07pp ~ -1.07pp)。默认取 **10%**: 保留 86% 的样本
(不至于把策略砍得太稀疏), 同时把最差那一档左尾砍掉约一半。
要做「追求收益」而不是「控回撤」, 把上限调大甚至设 100 (=不过滤) 即可。

**确认延迟 (无前视)**: 与 ``chan_structure`` 同口径 —— 分型要等右邻 K 线, 信号在序列
位置 ``p`` 上最早 ``p + 1`` 根收盘才可见, 故 entry 标在 ``signal.index + 1``, 配合框架
``entry_fill = "open_t+1"`` 实际成交在 ``signal.index + 2`` 的开盘。
⚠️ 闸门比较用的也是 ``signal.index + 1`` 那根**已收盘**的价格, 不能提前拿到 ``p + 2``。

**为什么没有「信号新鲜度」参数**: 本策略只把 entry 标在 ``signal.index + 1`` 这一个位置,
所以「信号确认距今天数」恒等于 0 (永远是最新鲜的那根), 不需要也不应该再开一个新鲜度参数
—— 加了个旋钮却永远不生效, 只会误导设置面板。要做「N 根内出现过三买就持续在榜」,
得改 entry 的落点语义 (把 confirm 到 confirm+N 全部标 1), 那是另一种策略行为。
实测也印证: 加新鲜度参数时 1/3/30 三档的 entry 数量完全相同 (9422), 已确认无区分度。
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
    "id": "chan_3buy_entry",
    "name": "缠论三买回踩",
    "description": "缠论三买(离开中枢后回踩不破)确认, 且确认日收盘距买点价不超过阈值(默认10%, 用于不追高控回撤)才入场; 短线博确认后首周主升, 缠论卖点+硬止损离场",
    "tags": ["缠论", "三买", "短线", "回踩"],
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
            "id": "max_premium",
            "label": "距买点价上限% (不追高)",
            "type": "float",
            "default": 10.0,
            "min": -20.0,
            "max": 120.0,
            "step": 1.0,
        },
        {"id": "use_sell_exit", "label": "缠论卖点离场", "type": "bool", "default": True},
        {"id": "use_1sell", "label": "一卖离场", "type": "bool", "default": False},
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
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

# 只做三买 —— 一买/二买是左侧或中枢震荡里的点, 与本策略「右侧回踩」的预设相悖。
# 复用 chan_structure 已注册的 signal_chan_3buy, 无需新增信号中文名映射。
ENTRY_SIGNALS = ["signal_chan_3buy"]
EXIT_SIGNALS = ["signal_chan_2sell", "signal_chan_3sell"]
EXECUTION_BACKEND = "matrix_native"

# 默认给一卖留个「减仓参考」位: 一卖灵敏度高 (强趋势里反复触发), 默认不拿它清仓,
# 但用户可以打开。默认离场 = 二卖 + 三卖 + 硬止损。
STOP_LOSS = -0.06
TAKE_PROFIT = None

# 短线口径: 三买的超额收益集中在确认后 5 个交易日内, 持有上限压到 15 个交易日。
# ⚠️ 与 chan_structure 同样刻意停在 30 以内 —— 该值参与 matrix 磁盘缓存 profile 的
# forward_bars 取值, 一旦超过现网最大值 (30) 会迫使所有策略的缓存档位重建。
MAX_HOLD_DAYS = 15

_MIN_BARS = MIN_BARS_FOR_STRUCTURE

_SELL_FLAGS: tuple[tuple[str, str], ...] = (
    ("use_1sell", "1sell"),
    ("use_2sell", "2sell"),
    ("use_3sell", "3sell"),
)


class Chan3BuyEntryMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        # 与 chan_structure 同因: 一卖要拿 MACD 柱面积做背驰比较, span=26 时 120 根
        # 序列首值播种残留约 1%, 面板太短会让背驰判定系统性偏移。
        return max(int(params.get("min_bars", 120) or 120), 60)

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        sell_kinds = tuple(
            kind for flag, kind in _SELL_FLAGS if params.get(flag, False)
        )
        use_sell_exit = bool(params.get("use_sell_exit", True)) and bool(sell_kinds)
        strict = bool(params.get("strict", True))
        min_bars = max(int(params.get("min_bars", 120) or 120), _MIN_BARS)

        # 闸门参数: 上限按百分数收, 转成小数比较。100+ 等价于不过滤 (不追高风险自担)。
        max_premium = float(params.get("max_premium", 10.0) or 0.0) / 100.0

        shape = market.shape
        entry = np.zeros(shape, dtype=np.uint8)
        exit_ = np.zeros(shape, dtype=np.uint8)
        entry_code = np.full(shape, -1, dtype=np.int16)
        exit_code = np.full(shape, -1, dtype=np.int16)

        exit_ids = tuple(f"signal_chan_{kind}" for kind in sell_kinds) if use_sell_exit else ()
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
            close_seq = np.asarray(close_all[positions, asset_id], dtype=np.float64)
            for signal in analysis.signals:
                # 信号落在序列位置 signal.index, 后一根有效 K 线才确认 => 标在 index + 1。
                confirm = signal.index + 1
                if confirm >= limit:
                    continue
                if signal.kind == "3buy":
                    # 不追高闸门 —— 买点价 = 回踩笔终点价; 确认日收盘相对它的溢价超限
                    # 就放弃这一笔 (控回撤, 不是选收益, 依据见模块 docstring)。
                    close_at_confirm = close_seq[confirm]
                    price_at_signal = float(signal.price)
                    if not np.isfinite(close_at_confirm) or price_at_signal <= 0:
                        continue
                    if close_at_confirm > price_at_signal * (1.0 + max_premium):
                        continue
                    row = int(positions[confirm])
                    entry[row, asset_id] = 1
                    entry_code[row, asset_id] = 0
                elif use_sell_exit and signal.kind in exit_index:
                    row = int(positions[confirm])
                    exit_[row, asset_id] = 1
                    exit_code[row, asset_id] = exit_index[signal.kind]

        return make_signal_matrix(
            shape,
            entry=entry,
            exit=exit_,
            entry_signal_code=entry_code,
            exit_signal_code=exit_code,
            entry_signal_ids=("signal_chan_3buy",),
            exit_signal_ids=exit_ids,
        )


MATRIX_STRATEGY = Chan3BuyEntryMatrixStrategy()
