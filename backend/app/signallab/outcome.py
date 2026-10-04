"""信号前瞻路径台账(signal outcome ledger).

把形状为 ``(交易日, 标的)`` 的入场信号布尔矩阵, 展开成「每条信号一条记录」的事件台账,
记录其成交价, 多个持有期的收益, 窗口内的最大浮盈(MFE)/最大浮亏(MAE)以及是否触及止损止盈。

设计动因
--------
app/backtest 的 ``TradeRecord`` 只覆盖**资金实际买到的**那部分信号, 且不带持有路径信息,
因此无法回答「同批信号里, 没被资金选中的那些后来走势如何」, 也无法计算 MFE/MAE。
本模块不加资金约束地把**全部**信号摊平, 于是同一份台账可以用来反复回答:

- 胜率, 盈亏比, 期望收益, 中位数收益(需求 1)
- 最大浮盈/浮亏分布, 从而反推「止盈止损设在哪一档更合理」
- 按形态分组后的差异, 供归因(需求 2)与参数剔除(需求 3)使用

口径约定(必须与 CONTRIBUTING.md 第 3 节一致)
--------------------------------------------
- 收益一律是**小数制**, 0.0366 表示 +3.66%。
- 入场: 信号出现在第 t 根(只用截至 t 的数据算出), 默认在 ``t + entry_delay`` 根以
  **开盘价**成交(``entry_delay=1`` 即 T+1 开盘), 严禁前视。
- 成交日必须是可成交日: 停牌, 无数据, 一字涨停封死都不能成交, 向后顺延最多
  ``max_delay_days`` 根; 始终找不到则 ``filled=False``, 且不进入任何统计分母。
- ``ret_{h}d = close[fill + h - 1] / entry_price - 1``, 即 h=1 是成交当日收盘相对成交价,
  持有窗口包含成交日当天。
- MFE/MAE 默认取 window 内 ``high``/``low`` 的极值(乐观口径, 假设极端价一定成交);
  ``intraday_extremes=False`` 时只用收盘价。两者不可混用, 由 ``intraday_extremes`` 显式声明。
- 前瞻窗口不足(数据尾部, 退市, 长期停牌)的样本记 ``truncated=True``, 其超出部分的
  horizon 收益为 null, MFE/MAE 只在可得窗口内计算。**不得把截断样本按 0 收益填列**,
  否则尾部样本会系统性拉低统计结果。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

# 默认观察持有期(交易日)。MEMORY 里缠论事件研究用 5/10/20/60, 这里补 1/3 以覆盖短线。
DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20, 60)

# 成交延迟上限(交易日)。超过即判为不可成交样本。
_MAX_DELAY_CEILING = 20


@dataclass(frozen=True)
class OutcomeConfig:
    """信号台账的计算口径。所有字段都是「会改变结果」的维度, 必须显式声明。"""

    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    entry_delay: int = 1
    max_delay_days: int = 3
    stop_loss: float | None = None       # 负小数, 如 -0.08 表示 -8%
    take_profit: float | None = None     # 正小数, 如 0.20 表示 +20%
    intraday_extremes: bool = True
    include_unfilled: bool = True        # 是否保留未能成交的信号行

    def __post_init__(self) -> None:
        horizons = tuple(int(h) for h in self.horizons)
        if not horizons:
            raise ValueError("horizons 不能为空")
        if any(h <= 0 for h in horizons):
            raise ValueError(f"horizons 必须全为正整数, 收到 {horizons}")
        object.__setattr__(self, "horizons", tuple(sorted(set(horizons))))

        if self.entry_delay < 0:
            raise ValueError("entry_delay 不能为负")
        if not 0 <= self.max_delay_days <= _MAX_DELAY_CEILING:
            raise ValueError(f"max_delay_days 必须在 0~{_MAX_DELAY_CEILING} 之间")
        if self.stop_loss is not None and self.stop_loss >= 0:
            raise ValueError(f"stop_loss 须为负小数(如 -0.08), 收到 {self.stop_loss}")
        if self.take_profit is not None and self.take_profit <= 0:
            raise ValueError(f"take_profit 须为正小数(如 0.20), 收到 {self.take_profit}")

    @property
    def window(self) -> int:
        """MFE/MAE 与止损止盈的观察窗口长度(交易日)。"""
        return int(max(self.horizons))


def ret_column(horizon: int) -> str:
    """horizon 收益列名。"""
    return f"ret_{horizon}d"


def excess_column(horizon: int) -> str:
    """相对全市场基准的超额收益列名。"""
    return f"exc_{horizon}d"


def _buyable_mask(market: Any) -> np.ndarray:
    """可买入掩码: 当日有行情且非一字涨停封死。

    ``tradable`` 由 backtest/matrix 生成, 已排除停牌(零成交且振幅为 0)与缺失行情;
    ``limit_up_locked`` 表示涨停封死, 此时盘中买不进。
    """
    open_ = np.asarray(market.open, dtype=np.float64)
    tradable = np.asarray(market.tradable).astype(bool)
    locked = np.asarray(market.limit_up_locked).astype(bool)
    return tradable & ~locked & np.isfinite(open_) & (open_ > 0)


def _col_extreme(values: np.ndarray, *, want_max: bool) -> np.ndarray:
    """按列取极值, 忽略 NaN; 整列 NaN 时返回 NaN(而不是 ±inf)。

    停牌日的价格是 NaN, 直接 amax/amin 会污染整列结果, 这里是唯一正确的取法。
    """
    finite = np.isfinite(values)
    sentinel = -np.inf if want_max else np.inf
    safe = np.where(finite, values, sentinel)
    out = safe.max(axis=0) if want_max else safe.min(axis=0)
    return np.where(finite.any(axis=0), out, np.nan)


def _arg_extreme(values: np.ndarray, *, want_max: bool) -> np.ndarray:
    """按列取极值出现的行偏移(0=窗口第一根); 整列 NaN 或无结果时返回 -1。"""
    finite = np.isfinite(values)
    sentinel = -np.inf if want_max else np.inf
    safe = np.where(finite, values, sentinel)
    idx = safe.argmax(axis=0) if want_max else safe.argmin(axis=0)
    return np.where(finite.any(axis=0), idx, -1).astype(np.int16)


def _first_true(mask: np.ndarray) -> np.ndarray:
    """每列第一个 True 的行偏移; 整列无 True 返回 -1。

    ``argmax`` 在整列 False 时返回 0, 必须先用 any 校正, 否则会误判成「第 0 根就触发」。
    """
    return np.where(mask.any(axis=0), mask.argmax(axis=0), -1).astype(np.int16)


def _resolve_fills(
    buyable: np.ndarray,
    signal_rows: np.ndarray,
    signal_cols: np.ndarray,
    *,
    config: OutcomeConfig,
    n_dates: int,
) -> np.ndarray:
    """求每条信号的实际成交延迟, 找不到可成交日时返回 -1。

    返回值是相对信号日的偏移(``entry_delay + 顺延天数``)。
    """
    start = int(config.entry_delay)
    delay_used = np.full(signal_rows.size, -1, dtype=np.int32)
    pending = np.ones(signal_rows.size, dtype=bool)
    for extra in range(int(config.max_delay_days) + 1):
        if not pending.any():
            break
        offset = start + extra
        candidate = signal_rows + offset
        reachable = candidate < n_dates
        probe = np.where(reachable, candidate, n_dates - 1)
        hit = pending & reachable & buyable[probe, signal_cols]
        if not hit.any():
            continue
        delay_used[hit] = offset
        pending &= ~hit
    return delay_used


def build_signal_outcomes(
    market: Any,
    entry: np.ndarray,
    *,
    exit_signals: np.ndarray | None = None,
    entry_signal_code: np.ndarray | None = None,
    baseline: Mapping[int, np.ndarray] | None = None,
    config: OutcomeConfig | None = None,
) -> pl.DataFrame:
    """把入场信号矩阵展开成信号事件台账。

    Args:
        market: ``backtest/matrix.MarketDataMatrix``, 提供 idx -> date/symbol 的标签与 OHLC。
        entry: ``(交易日, 标的)`` 布尔矩阵, True 表示该标的当日出现入场信号。
        exit_signals: 可选的出场信号布尔矩阵, 用于记录最早一次离场信号出现在第几根。
        entry_signal_code: 可选的整型矩阵, 标记每个信号来自哪个 signal id(下标).
        baseline: ``{horizon: np.ndarray}``, 每个交易日全市场同口径收益基准, 由
            ``compute_market_baseline`` 生成; 提供时会额外产出 ``exc_{h}d`` 超额收益列。
        config: 计算口径。

    Returns:
        polars.DataFrame, 每行一条信号事件, 列定义见本文件顶部「口径约定」。
    """
    cfg = config or OutcomeConfig()
    labels = tuple(market.timestamp_labels)
    symbols = tuple(market.symbols)
    entry_mask = np.asarray(entry).astype(bool)
    if entry_mask.ndim != 2:
        raise ValueError("entry 必须是二维布尔矩阵")
    n_dates, n_symbols = entry_mask.shape
    if n_dates != len(labels) or n_symbols != len(symbols):
        raise ValueError("entry 形状与 market 的轴不一致")

    open_ = np.asarray(market.open, dtype=np.float64)
    high = np.asarray(market.high, dtype=np.float64)
    low = np.asarray(market.low, dtype=np.float64)
    close = np.asarray(market.close, dtype=np.float64)

    signal_rows, signal_cols = np.nonzero(entry_mask)
    total = int(signal_rows.size)
    if total == 0:
        return _empty_frame(cfg)

    buyable = _buyable_mask(market)
    delay_used = _resolve_fills(
        buyable, signal_rows, signal_cols, config=cfg, n_dates=n_dates
    )
    filled = delay_used >= 0

    fill_rows = np.full(total, -1, dtype=np.int64)
    fill_rows[filled] = signal_rows[filled] + delay_used[filled]
    window = cfg.window
    available = np.zeros(total, dtype=np.int16)
    available[filled] = np.minimum(window, n_dates - fill_rows[filled]).astype(np.int16)
    truncated = filled & (available < window)

    entry_price = np.full(total, np.nan, dtype=np.float64)
    ret_values = {h: np.full(total, np.nan, dtype=np.float64) for h in cfg.horizons}
    exc_values = (
        {h: np.full(total, np.nan, dtype=np.float64) for h in cfg.horizons}
        if baseline is not None
        else None
    )
    mfe = np.full(total, np.nan, dtype=np.float64)
    mae = np.full(total, np.nan, dtype=np.float64)
    mfe_bar = np.full(total, -1, dtype=np.int16)
    mae_bar = np.full(total, -1, dtype=np.int16)
    stop_bar = np.full(total, -1, dtype=np.int16)
    target_bar = np.full(total, -1, dtype=np.int16)
    touch_stop = np.zeros(total, dtype=bool)
    touch_target = np.zeros(total, dtype=bool)
    exit_offset = np.full(total, -1, dtype=np.int16)
    ret_at_exit = np.full(total, np.nan, dtype=np.float64)
    window_bars = np.zeros(total, dtype=np.int16)

    # 按成交日分组处理: 同一天的所有信号共用一次矩阵切片, 循环次数只有交易日数量级,
    # 而不是信号数量级(全市场三年可达数十万条信号)。
    for row in np.unique(fill_rows[filled]):
        pointer = np.flatnonzero(filled & (fill_rows == row))
        length = int(available[pointer[0]])
        if length <= 0:
            continue
        start = int(row)
        stop = start + length
        cols = signal_cols[pointer]
        price = open_[start, cols]
        tradable_now = np.isfinite(price) & (price > 0)
        if not tradable_now.all():
            pointer = pointer[tradable_now]
            cols = cols[tradable_now]
            price = price[tradable_now]
        if pointer.size == 0:
            continue

        window_bars[pointer] = np.int16(length)
        entry_price[pointer] = price

        r_close = (close[start:stop, :][:, cols] / price) - 1.0
        if cfg.intraday_extremes:
            up_source = (high[start:stop, :][:, cols] / price) - 1.0
            down_source = (low[start:stop, :][:, cols] / price) - 1.0
        else:
            up_source = down_source = r_close

        mfe[pointer] = _col_extreme(up_source, want_max=True)
        mae[pointer] = _col_extreme(down_source, want_max=False)
        mfe_bar[pointer] = _arg_extreme(up_source, want_max=True)
        mae_bar[pointer] = _arg_extreme(down_source, want_max=False)

        for horizon in cfg.horizons:
            if horizon > length:
                continue
            value = r_close[horizon - 1]
            ret_values[horizon][pointer] = value
            if exc_values is not None and baseline is not None:
                base_row = baseline.get(horizon)
                if base_row is not None:
                    exc_values[horizon][pointer] = value - float(base_row[start])

        if cfg.stop_loss is not None:
            hit = down_source <= cfg.stop_loss
            stop_idx = _first_true(hit)
            touch_stop[pointer] = stop_idx >= 0
            stop_bar[pointer] = stop_idx
        if cfg.take_profit is not None:
            hit = up_source >= cfg.take_profit
            target_idx = _first_true(hit)
            touch_target[pointer] = target_idx >= 0
            target_bar[pointer] = target_idx

        if exit_signals is not None:
            exits = np.asarray(exit_signals)[start:stop, :][:, cols].astype(bool)
            first = _first_true(exits)
            exit_offset[pointer] = first
            hits = first >= 0
            if hits.any():
                ret_at_exit[pointer[hits]] = r_close[first[hits], np.flatnonzero(hits)]

    decided = touch_stop & touch_target
    stop_before_target = np.zeros(total, dtype=bool)
    stop_before_target[decided] = stop_bar[decided] <= target_bar[decided]

    data: dict[str, Any] = {
        "symbol": [symbols[c] for c in signal_cols],
        "signal_date": [labels[r] for r in signal_rows],
        "fill_date": [labels[r] if r >= 0 else None for r in fill_rows],
        "entry_price": entry_price,
        "delay_days": np.where(filled, delay_used, -1).astype(np.int16),
        "filled": filled,
        "window_bars": window_bars,
        "truncated": truncated,
        "mfe": mfe,
        "mae": mae,
        "mfe_bar": mfe_bar,
        "mae_bar": mae_bar,
        "touch_stop": touch_stop,
        "touch_target": touch_target,
        "stop_bar": stop_bar,
        "target_bar": target_bar,
        "exit_signal_offset": exit_offset,
        "ret_at_exit_close": ret_at_exit,
    }
    for horizon in cfg.horizons:
        data[ret_column(horizon)] = ret_values[horizon]
        if exc_values is not None:
            data[excess_column(horizon)] = exc_values[horizon]
    if entry_signal_code is not None:
        codes = np.asarray(entry_signal_code)
        data["entry_signal_code"] = codes[signal_rows, signal_cols].astype(np.int16)

    frame = pl.DataFrame(data)
    # 「先止损还是先止盈」只有在两者都触及的样本上有意义, 其余保持 null 而不是 False,
    # 否则后续统计会把「没触及」误算成「先止盈」。
    frame = frame.with_columns(
        pl.when(pl.col("touch_stop") & pl.col("touch_target"))
        .then(pl.col("stop_bar") <= pl.col("target_bar"))
        .otherwise(None)
        .alias("stop_before_target")
    )
    frame = _finite_or_null(frame)
    if not cfg.include_unfilled:
        frame = frame.filter(pl.col("filled"))
    # _finite_or_null 与 with_columns 都不改变行序, filter 也不重新排序。
    return frame


def compute_market_baseline(
    market: Any,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict[int, np.ndarray]:
    """全市场同期基准收益: 在第 j 个交易日以开盘价买入持有 h 根后的收益中位数。

    与 ``build_signal_outcomes`` 的收益口径完全一致(同一天, 同为开盘价成交, 同为第 h 根收盘),
    因此相减得到的超额收益是可比的。没有基准时「+6% 的 60 日收益」无法判断好坏 ——
    大盘同期上涨 8% 的话那实际是跑输的。
    """
    labels_count = len(market.timestamp_labels)
    open_ = np.asarray(market.open, dtype=np.float64)
    close = np.asarray(market.close, dtype=np.float64)
    buyable = _buyable_mask(market)
    baseline: dict[int, np.ndarray] = {}
    for horizon in sorted(set(int(h) for h in horizons)):
        series = np.full(labels_count, np.nan, dtype=np.float64)
        tail = labels_count - horizon
        if tail > 0:
            entry_price = open_[:tail, :]
            outcome = (close[horizon - 1 : horizon - 1 + tail, :] / entry_price) - 1.0
            valid = buyable[:tail, :] & np.isfinite(outcome)
            for row in range(tail):
                picked = outcome[row][valid[row]]
                if picked.size:
                    series[row] = float(np.median(picked))
        baseline[horizon] = series
    return baseline


def _finite_or_null(frame: pl.DataFrame) -> pl.DataFrame:
    """把浮点列里的 NaN 统一转成 null。

    「没有观测」必须是 null 而不是 NaN: NaN 在 polars 的 mean/median 里不同实现可能参与
    也可能不参与运算, 而 null 一定被跳过, 语义唯一。调用方拿到 NaN 也很难判断是
    「算不出来」还是「真算了个数」。
    """
    targets = [
        name for name, dtype in frame.schema.items()
        if dtype.is_float() or dtype.is_decimal()
    ]
    if not targets:
        return frame
    return frame.with_columns(
        pl.when(pl.col(name).is_finite()).then(pl.col(name)).otherwise(None).alias(name)
        for name in targets
    )


def _empty_frame(config: OutcomeConfig) -> pl.DataFrame:
    """空台账: 保持与真实结果完全一致的列和类型, 避免调用方分支。"""
    data: dict[str, Any] = {
        "symbol": pl.Series("symbol", [], dtype=pl.Utf8),
        "signal_date": pl.Series("signal_date", [], dtype=pl.Utf8),
        "fill_date": pl.Series("fill_date", [], dtype=pl.Utf8),
        "entry_price": pl.Series("entry_price", [], dtype=pl.Float64),
        "delay_days": pl.Series("delay_days", [], dtype=pl.Int16),
        "filled": pl.Series("filled", [], dtype=pl.Boolean),
        "window_bars": pl.Series("window_bars", [], dtype=pl.Int16),
        "truncated": pl.Series("truncated", [], dtype=pl.Boolean),
        "mfe": pl.Series("mfe", [], dtype=pl.Float64),
        "mae": pl.Series("mae", [], dtype=pl.Float64),
        "mfe_bar": pl.Series("mfe_bar", [], dtype=pl.Int16),
        "mae_bar": pl.Series("mae_bar", [], dtype=pl.Int16),
        "touch_stop": pl.Series("touch_stop", [], dtype=pl.Boolean),
        "touch_target": pl.Series("touch_target", [], dtype=pl.Boolean),
        "stop_bar": pl.Series("stop_bar", [], dtype=pl.Int16),
        "target_bar": pl.Series("target_bar", [], dtype=pl.Int16),
        "stop_before_target": pl.Series("stop_before_target", [], dtype=pl.Boolean),
        "exit_signal_offset": pl.Series("exit_signal_offset", [], dtype=pl.Int16),
        "ret_at_exit_close": pl.Series("ret_at_exit_close", [], dtype=pl.Float64),
    }
    for horizon in config.horizons:
        data[ret_column(horizon)] = pl.Series(ret_column(horizon), [], dtype=pl.Float64)
    return pl.DataFrame(data)
