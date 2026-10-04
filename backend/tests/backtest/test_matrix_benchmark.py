"""基准指数列 (``index_close``) 与趋势擒龙的资金动能过滤 —— 对拍与边界。

两条对拍线:

1. 矩阵特征 ``index_close`` vs 直接按标的广播的期望值 (交易所映射 / 缺日前向沿用 /
   指数历史之前置 NaN / 后缀无法识别置 NaN)。
2. 资金动能 vs :func:`app.indicators.formula_signals.capital_momentum` ——
   面板个股 K 线副图那一侧的同口径实现 (纯 numpy, 与矩阵代码无共享), 在有效 bar
   压缩序列上重算再比对。

指数数据全部写在 ``tmp_path`` 里, 不读项目真实 ``data/``。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest import benchmark as benchmark_module
from app.backtest.benchmark import (
    BENCHMARK_INDEX_BY_EXCHANGE,
    benchmark_close_matrix,
    exchange_of,
    reset_benchmark_cache,
)
from app.backtest.matrix import (
    MarketDataMatrix,
    build_market_data_matrix,
    matrix_feature,
    supports_matrix_feature,
)
from app.indicators.formula_signals import capital_momentum

_FIELDS = ("open", "high", "low", "close", "volume")
_START = date(2024, 1, 1)
_MOMENTUM_CAP = 0.0


@pytest.fixture(autouse=True)
def _clear_benchmark_cache():
    reset_benchmark_cache()
    yield
    reset_benchmark_cache()


@pytest.fixture
def fake_data_dir(tmp_path, monkeypatch) -> Path:
    """把基准指数取数目录指向 tmp_path (引擎外部注入点)。"""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(benchmark_module, "_resolve_data_dir", lambda _=None: root)
    return root


# ===== 构造工具 =====


def _write_index(root: Path, symbol: str, closes: dict[date, float]) -> None:
    """按 kline_index_daily 的 date= 分区写指数日线。

    文件名带 symbol: 同一个 date= 目录下可以并存多只指数 (真实数据也是一个分区
    里放当天全部指数), 否则后写的会覆盖先写的。
    """
    filename = f"part_{symbol.replace('.', '_')}.parquet"
    for day, close in closes.items():
        directory = root / "kline_index_daily" / f"date={day.isoformat()}"
        directory.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": [symbol],
                "date": [day],
                "open": [close],
                "high": [close],
                "low": [close],
                "close": [close],
                "volume": [0.0],
                "amount": [0.0],
                "quote_ts": [0],
            }
        ).write_parquet(directory / filename)


def _panel_from_series(
    series: dict[str, np.ndarray],
    symbol: str,
    suspended_rows: set[int] | None = None,
) -> pl.DataFrame:
    suspended_rows = suspended_rows or set()
    rows = []
    for index in range(series["close"].size):
        if index in suspended_rows:
            values = {key: float("nan") for key in _FIELDS}
        else:
            values = {key: float(series[key][index]) for key in series}
        rows.append(
            {
                "symbol": symbol,
                "name": "测试股票",
                "date": _START + timedelta(days=index),
                **values,
            }
        )
    return pl.DataFrame(rows)


def _market(frames: list[pl.DataFrame]) -> MarketDataMatrix:
    return build_market_data_matrix(pl.concat(frames).sort(["date", "symbol"]))


def _random_series(seed: int, bars: int, drift: float = 0.0005) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = 10.0 * np.exp(np.cumsum(rng.normal(drift, 0.02, bars)))
    open_ = close * (1.0 + rng.normal(0.0, 0.010, bars))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.006, bars)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.006, bars)))
    volume = 1000.0 * np.exp(rng.normal(0.0, 0.3, bars))
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def _days(count: int) -> list[date]:
    return [_START + timedelta(days=index) for index in range(count)]


def _labels(count: int) -> list[str]:
    return [day.isoformat() for day in _days(count)]


def _flat_index(count: int, level: float) -> dict[date, float]:
    return {day: level for day in _days(count)}


def _strategy():
    from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY

    return MATRIX_STRATEGY


# ===== 交易所映射与注册 =====


def test_exchange_of_reads_symbol_suffix():
    assert exchange_of("600000.SH") == "SH"
    assert exchange_of("000001.sz") == "SZ"
    assert exchange_of("830799.BJ") == "BJ"
    assert exchange_of("00700.HK") is None
    assert exchange_of("sh600000") is None
    assert exchange_of("AAPL") is None


def test_benchmark_mapping_matches_kline_endpoint_convention():
    """矩阵侧映射必须与个股 K 线副图 (api/kline.py) 的「对应指数」取值一致。"""
    from app.api.kline import (
        _BENCHMARK_INDEX_BY_EXCHANGE,
        _DEFAULT_BENCHMARK_INDEX,
    )

    for exchange, symbol in _BENCHMARK_INDEX_BY_EXCHANGE.items():
        assert symbol in BENCHMARK_INDEX_BY_EXCHANGE[exchange], exchange
    assert _DEFAULT_BENCHMARK_INDEX in BENCHMARK_INDEX_BY_EXCHANGE["SH"]


# ===== index_close 矩阵 =====


def test_index_close_matrix_broadcasts_by_exchange(fake_data_dir):
    symbols = ["600000.SH", "000001.SZ", "830799.BJ", "00700.HK"]
    _write_index(fake_data_dir, "000001.SH", _flat_index(3, 3000.0))
    _write_index(fake_data_dir, "399001.SZ", _flat_index(3, 9000.0))
    _write_index(fake_data_dir, "899050.BJ", _flat_index(3, 1000.0))

    matrix = benchmark_close_matrix(_labels(3), symbols)

    assert matrix.shape == (3, 4)
    assert matrix.dtype == np.float32
    np.testing.assert_allclose(matrix[:, 0], 3000.0)
    np.testing.assert_allclose(matrix[:, 1], 9000.0)
    np.testing.assert_allclose(matrix[:, 2], 1000.0)
    assert np.isnan(matrix[:, 3]).all()


def test_index_close_matrix_forward_fills_missing_index_day(fake_data_dir):
    """指数 parquet 通常落后一个交易日: 缺的那天沿用最近一根收盘。"""
    _write_index(
        fake_data_dir,
        "000001.SH",
        {_days(2)[0]: 3000.0, _days(2)[1]: 3100.0},
    )

    matrix = benchmark_close_matrix(_labels(4), ["600000.SH"])

    np.testing.assert_allclose(matrix[:, 0], [3000.0, 3100.0, 3100.0, 3100.0])


def test_index_close_matrix_is_nan_before_index_history(fake_data_dir):
    """指数历史起点之前不编造数值 (前向沿用只在有值之后生效)。"""
    days = _days(4)
    _write_index(fake_data_dir, "000001.SH", {days[2]: 3000.0, days[3]: 3050.0})

    matrix = benchmark_close_matrix(_labels(4), ["600000.SH"])

    assert np.isnan(matrix[0, 0]) and np.isnan(matrix[1, 0])
    np.testing.assert_allclose(matrix[2:, 0], [3000.0, 3050.0])


def test_index_close_matrix_falls_back_when_preferred_index_missing(fake_data_dir):
    """北证50 缺数据时退到上证指数 (候选列表次选)。"""
    _write_index(fake_data_dir, "000001.SH", _flat_index(2, 3000.0))

    matrix = benchmark_close_matrix(_labels(2), ["830799.BJ", "600000.SH"])

    np.testing.assert_allclose(matrix[:, 0], 3000.0)
    np.testing.assert_allclose(matrix[:, 1], 3000.0)


def test_index_close_matrix_without_data_is_nan(fake_data_dir):
    matrix = benchmark_close_matrix(_labels(3), ["600000.SH", "00700.HK"])

    assert matrix.shape == (3, 2)
    assert np.isnan(matrix).all()


def test_matrix_feature_exposes_index_close(fake_data_dir):
    _write_index(fake_data_dir, "000001.SH", _flat_index(60, 3000.0))
    series = _random_series(seed=11, bars=60)
    market = _market([_panel_from_series(series, "600000.SH")])

    assert supports_matrix_feature(market, "index_close")
    values = matrix_feature(market, "index_close")

    assert values.shape == market.shape
    np.testing.assert_allclose(values[:, 0], 3000.0)


# ===== 资金动能 (趋势擒龙过滤) =====


def _specs() -> list[tuple[str, int, set[int], int]]:
    """(symbol, bars, 停牌行, 种子) —— 种子写死, 不用 hash (PYTHONHASHSEED 会打乱)。"""
    return [
        ("600000.SH", 200, set(), 4101),
        ("300750.SZ", 180, set(range(60, 66)), 4102),
    ]


def _prepare_momentum_panel(fake_data_dir, bars: int = 200):
    """两只票 (沪/深) + 各自交易所的指数序列, 返回 (market, valid, series_by_symbol, index)。"""
    _write_index(fake_data_dir, "000001.SH", _flat_index(bars, 3000.0))
    _write_index(fake_data_dir, "399001.SZ", _flat_index(bars, 9000.0))

    frames, series_by_symbol = [], {}
    for symbol, count, suspended, seed in _specs():
        series = _random_series(seed=seed, bars=count, drift=0.002)
        frames.append(_panel_from_series(series, symbol, suspended_rows=suspended))
        series_by_symbol[symbol] = (series, suspended)
    market = _market(frames)
    valid = np.isfinite(market.close) & np.isfinite(market.open) & np.isfinite(market.high)
    return market, valid, series_by_symbol


def test_capital_momentum_matches_panel_implementation(fake_data_dir):
    """矩阵资金动能 vs 面板个股 K 线副图那侧的 capital_momentum (有效 bar 压缩序列)。"""
    from app.strategy.builtin.trend_dragon import _capital_momentum

    market, valid, series_by_symbol = _prepare_momentum_panel(fake_data_dir)
    momentum = _capital_momentum(market, valid)
    index_close = matrix_feature(market, "index_close")

    compared = 0
    for asset_id, symbol in enumerate(market.symbols):
        series, suspended = series_by_symbol[symbol]
        keep = np.ones(series["close"].size, dtype=bool)
        keep[list(suspended)] = False
        rows = np.flatnonzero(valid[:, asset_id])
        assert rows.size == int(keep.sum())
        expected = capital_momentum(series["close"][keep], index_close[rows, asset_id])
        actual = momentum[rows, asset_id]
        np.testing.assert_allclose(actual, expected, rtol=1e-4, atol=1e-3, equal_nan=True)
        compared += int(np.isfinite(expected).sum())

    assert compared > 0, "对拍是空跑: 没有任何一根攒够 52 个有效 bar"


def test_trend_dragon_momentum_filter_only_removes_entries(fake_data_dir):
    from app.strategy.builtin.trend_dragon import _capital_momentum

    market, valid, _ = _prepare_momentum_panel(fake_data_dir)
    strategy = _strategy()
    # 过滤默认是开的 -> 基准那一侧要显式关掉, 否则两边都带过滤, 断言退化成恒真
    base = strategy.compute_signals(
        market, {"scan_days": 5, "use_momentum_filter": False, "bias20_cap_pct": 0.0}
    ).entry.astype(bool)
    filtered = strategy.compute_signals(
        market,
        {
            "scan_days": 5,
            "use_momentum_filter": True,
            "momentum_cap": _MOMENTUM_CAP,
            "bias20_cap_pct": 0.0,
        },
    ).entry.astype(bool)

    momentum = _capital_momentum(market, valid)
    assert not (filtered & ~base).any(), "资金动能过滤只能砍命中, 不能新增"
    known = base & np.isfinite(momentum)
    assert known.any(), "构造序列没有可用的动能, 断言会退化成恒真"
    assert (momentum[filtered & known] <= _MOMENTUM_CAP + 1e-3).all()
    assert filtered.sum() < base.sum(), "构造序列没能体现资金动能过滤的效果"


def test_trend_dragon_momentum_filter_keeps_entries_without_benchmark(fake_data_dir):
    """基准指数整体缺失 -> 动能为 NaN -> 不参与过滤, 命中集合保持不变。"""
    assert not (fake_data_dir / "kline_index_daily").exists()
    series = _random_series(seed=4501, bars=200, drift=0.002)
    market = _market([_panel_from_series(series, "600000.SH")])
    strategy = _strategy()

    base = strategy.compute_signals(
        market, {"scan_days": 5, "use_momentum_filter": False, "bias20_cap_pct": 0.0}
    ).entry.astype(bool)
    filtered = strategy.compute_signals(
        market,
        {
            "scan_days": 5,
            "use_momentum_filter": True,
            "momentum_cap": _MOMENTUM_CAP,
            "bias20_cap_pct": 0.0,
        },
    ).entry.astype(bool)

    np.testing.assert_array_equal(filtered, base)
    assert base.any(), "构造序列没有命中, 断言会退化成恒真"


def test_trend_dragon_momentum_filter_drops_short_history(fake_data_dir):
    """基准可用但该票攒不出动能 (交集交易日不足 52) -> 与源脚本一致地剔除。"""
    days = _days(200)
    _write_index(fake_data_dir, "000001.SH", {day: 3000.0 for day in days[160:]})
    series = _random_series(seed=4601, bars=200, drift=0.002)
    market = _market([_panel_from_series(series, "600000.SH")])
    strategy = _strategy()

    base = strategy.compute_signals(
        market, {"scan_days": 5, "use_momentum_filter": False, "bias20_cap_pct": 0.0}
    ).entry.astype(bool)
    filtered = strategy.compute_signals(
        market,
        {
            "scan_days": 5,
            "use_momentum_filter": True,
            "momentum_cap": _MOMENTUM_CAP,
            "bias20_cap_pct": 0.0,
        },
    ).entry.astype(bool)

    assert base.any(), "构造序列没有命中, 断言会退化成恒真"
    assert not filtered.any(), "基准可用时, 攒不出动能的票必须被剔除"


def test_trend_dragon_momentum_warmup_bars():
    strategy = _strategy()
    without = strategy.required_warmup_bars({"use_momentum_filter": False})
    with_filter = strategy.required_warmup_bars({"use_momentum_filter": True})

    assert with_filter > without
    assert with_filter >= 52 * 2 - 4  # 动能先攒 52 个有效 bar, 其均值再要 52 个

    # 资金动能过滤默认开启 (对齐源脚本日常用法), 空 params 必须按开启算窗口
    assert strategy.required_warmup_bars({}) == with_filter
