"""「启动策略」(公司版 is_valid_stock 四条件) —— 矩阵原生实现 vs 源 pandas 逻辑对拍。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.matrix import (
    MarketDataMatrix,
    build_market_data_matrix,
)

REFERENCE_MIN_HISTORY = 31  # 源脚本 i 从 30 起步 => 至少 31 根有效 bar


def _reference_startup(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    """逐 bar 直译 filter_stock.is_valid_stock (在有效 bar 压缩序列上评估)。"""
    n = close.size
    out = np.zeros(n, dtype=bool)
    for t in range(REFERENCE_MIN_HISTORY - 1, n):
        # 1) 近 15 根内单日涨幅 >= 9.8% (pct_change().tail(15).max())
        pct_window = close[t - 14 : t + 1] / close[t - 15 : t] - 1.0
        cond_limit = bool(pct_window.max() >= 0.098)
        # 2) 近 10 根内存在向上跳空 (low > high.shift(1)).tail(10).any()
        cond_gap = bool(np.any(low[t - 9 : t + 1] > high[t - 10 : t]))
        # 3) 最近 3 根全部收阳且实体均值 > 1%
        body = (close[t - 2 : t + 1] - open_[t - 2 : t + 1]) / open_[t - 2 : t + 1]
        cond_yang = bool((body > 0).all()) and float(body.mean()) > 0.01
        # 4) 当日量 / 前 5 根均量 in [1.5, 3]
        ratio = float(volume[t] / volume[t - 5 : t].mean())
        cond_volume = 1.5 <= ratio <= 3.0
        out[t] = cond_limit and cond_gap and cond_yang and cond_volume
    return out


def _startup_series() -> dict[str, np.ndarray]:
    """构造仅在最后一根 bar 四条件同时成立的序列。

    bar 32: 涨停日 (+10%); bar 36: 向上跳空; bar 42-44: 三连阳; bar 44: 量比 2.0。
    其余 bar 全部平盘 (body=0, pct~0), 保证三连阳只在尾部成立 => 信号唯一。
    """
    n = 45
    open_ = np.full(n, 10.0)
    high = np.full(n, 10.05)
    low = np.full(n, 9.95)
    close = np.full(n, 10.0)
    volume = np.full(n, 1000.0)

    open_[32], high[32], low[32], close[32] = 10.0, 11.3, 9.95, 11.0  # +10% 涨停
    open_[36], high[36], low[36], close[36] = 10.3, 10.9, 10.2, 10.6  # 跳空低点 > 昨高
    for index in range(37, 42):
        open_[index] = high[index] = close[index] = 10.6
        high[index] = 10.65
        low[index] = 10.55
    open_[42], close[42], high[42], low[42] = 10.6, 10.85, 10.9, 10.55  # 阳线 1
    open_[43], close[43], high[43], low[43] = 10.85, 11.1, 11.15, 10.8  # 阳线 2
    open_[44], close[44], high[44], low[44] = 11.1, 11.35, 11.4, 11.05  # 阳线 3
    volume[44] = 2000.0  # 量比 = 2000 / 1000 = 2.0
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def _panel_from_series(
    series: dict[str, np.ndarray],
    symbol: str,
    suspended_rows: set[int] | None = None,
) -> pl.DataFrame:
    suspended_rows = suspended_rows or set()
    start = date(2024, 1, 1)
    rows = []
    for index in range(series["close"].size):
        if index in suspended_rows:
            values = {key: float("nan") for key in ("open", "high", "low", "close", "volume")}
        else:
            values = {key: float(series[key][index]) for key in series}
        rows.append(
            {
                "symbol": symbol,
                "name": "测试股票",
                "date": start + timedelta(days=index),
                **values,
            }
        )
    return pl.DataFrame(rows)


def _market(frames: list[pl.DataFrame]) -> MarketDataMatrix:
    panel = pl.concat(frames).sort(["date", "symbol"])
    return build_market_data_matrix(panel)


def test_startup_surge_hits_only_the_engineered_bar():
    from app.strategy.builtin.startup_surge import MATRIX_STRATEGY

    market = _market([_panel_from_series(_startup_series(), "000001.SZ")])
    signals = MATRIX_STRATEGY.compute_signals(market, {})
    entry = signals.entry[:, 0].astype(bool)

    assert entry.sum() == 1
    assert entry[-1], "构造序列的最后一根应命中启动信号"


def test_startup_surge_requires_recent_limit_up():
    """去掉 +10% 涨停日后, 其余条件不变, 尾部不应再有信号。"""
    from app.strategy.builtin.startup_surge import MATRIX_STRATEGY

    series = _startup_series()
    series["open"][32], series["high"][32] = 10.0, 10.05  # 抹平涨停日
    series["low"][32], series["close"][32] = 9.95, 10.0
    market = _market([_panel_from_series(series, "000001.SZ")])

    entry = MATRIX_STRATEGY.compute_signals(market, {}).entry[:, 0].astype(bool)
    assert entry.sum() == 0


def test_startup_surge_param_switches_relax_conditions():
    from app.strategy.builtin.startup_surge import MATRIX_STRATEGY

    market = _market([_panel_from_series(_startup_series(), "000001.SZ")])
    relaxed = (
        MATRIX_STRATEGY.compute_signals(
            market,
            {
                "require_recent_limit_up": False,
                "require_recent_gap": False,
                "require_three_yang": False,
                "require_volume_ratio": False,
            },
        )
        .entry[:, 0]
        .astype(bool)
    )

    # 全部条件关闭后, 只剩「有效 + 至少 31 根历史」
    assert relaxed.sum() == 45 - 30
    assert not relaxed[:30].any()


def test_startup_surge_matches_reference_on_random_panel():
    from app.strategy.builtin.startup_surge import MATRIX_STRATEGY

    rng = np.random.default_rng(20260916)
    series0 = _startup_series()
    frames = [_panel_from_series(series0, "000001.SZ")]
    expectations: dict[str, np.ndarray] = {
        "000001.SZ": _reference_startup(
            series0["open"], series0["high"], series0["low"], series0["close"], series0["volume"]
        )
    }

    random_specs = [
        ("600000.SH", 120, set()),  # 连续序列
        ("300750.SZ", 140, set(range(60, 66))),  # 中段停牌 6 根
        ("688981.SH", 130, set(range(0, 12))),  # 上市晚 12 根
    ]
    for symbol, n_bars, suspended in random_specs:
        close = 10.0 * np.exp(np.cumsum(rng.normal(0.0, 0.025, n_bars)))
        open_ = close * (1.0 + rng.normal(0.0, 0.012, n_bars))
        high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.008, n_bars)))
        low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.008, n_bars)))
        volume = 1000.0 * np.exp(rng.normal(0.0, 0.35, n_bars))
        series = {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
        frames.append(_panel_from_series(series, symbol, suspended_rows=suspended))
        # 参考实现只看有效 bar 压缩序列
        keep = np.ones(n_bars, dtype=bool)
        keep[list(suspended)] = False
        expectations[symbol] = _reference_startup(
            open_[keep], high[keep], low[keep], close[keep], volume[keep]
        )

    market = _market(frames)
    signals = MATRIX_STRATEGY.compute_signals(market, {})
    total_hits = 0
    for asset_id, symbol in enumerate(market.symbols):
        expected = expectations[symbol]
        entry = signals.entry[:, asset_id].astype(bool)
        valid_rows = np.flatnonzero(np.isfinite(market.close[:, asset_id]))
        # 把参考结果 (压缩序列下标) 映射回矩阵行号
        expected_rows = np.zeros(entry.shape, dtype=bool)
        expected_rows[valid_rows] = expected
        np.testing.assert_array_equal(entry, expected_rows)
        total_hits += int(expected.sum())
    assert total_hits >= 1, "对拍是空跑: 随机+构造序列均无命中"
