"""内置「趋势擒龙」—— 矩阵原生实现 vs 源 pandas 公式直译对拍。

参考实现用 ``app.indicators.formula_signals.trend_dragon``: 它是
``qushiqinlong/选股_趋势擒龙.py::compute_trend_dragon`` 的纯 numpy 逐 bar 直译
(已在 ``tests/test_formula_signals.py`` 里与纯 Python 参考逐位对拍过)。这里用
它在**有效 bar 压缩序列**上重算一遍, 再映射回矩阵行号, 用来验证 numba +
有效 bar 语义的矩阵实现与源脚本完全一致。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.matrix import (
    MarketDataMatrix,
    build_market_data_matrix,
    matrix_feature,
)
from app.indicators.formula_signals import trend_dragon

_FIELDS = ("open", "high", "low", "close", "volume")

# 策略 META 的默认值现在对齐源脚本的日常用法 (`--max-momentum 1 --max-bias20 10`)。
# 下面这些用例想验证的是**信号本身**, 所以一律显式关掉两条事件研究过滤, 免得
# 「默认又改了一档」把断言带偏。
_NO_FILTERS = {"use_momentum_filter": False, "bias20_cap_pct": 0.0}


def _params(**overrides) -> dict:
    return {**_NO_FILTERS, **overrides}


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
            values = {key: float("nan") for key in _FIELDS}
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
    return build_market_data_matrix(pl.concat(frames).sort(["date", "symbol"]))


def _random_series(seed: int, bars: int, drift: float = 0.0005) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = 10.0 * np.exp(np.cumsum(rng.normal(drift, 0.02, bars)))
    open_ = close * (1.0 + rng.normal(0.0, 0.010, bars))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.006, bars)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.006, bars)))
    volume = 1000.0 * np.exp(rng.normal(0.0, 0.3, bars))
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def _reference_on_valid_rows(
    series: dict[str, np.ndarray],
    suspended_rows: set[int],
) -> np.ndarray:
    """把序列压缩掉停牌行后跑源公式直译, 返回逐 bar 命中 (压缩序列下标)。"""
    keep = np.ones(series["close"].size, dtype=bool)
    keep[list(suspended_rows)] = False
    signal, _ = trend_dragon(
        series["open"][keep],
        series["high"][keep],
        series["low"][keep],
        series["close"][keep],
    )
    return signal


def _map_to_rows(
    expected: np.ndarray, valid_rows: np.ndarray, shape: tuple[int, int]
) -> np.ndarray:
    out = np.zeros(shape, dtype=bool)
    out[valid_rows] = expected
    return out


def test_trend_dragon_matches_source_formula_on_random_panel():
    from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY

    rng = np.random.default_rng(20260916)
    specs = [
        ("000001.SZ", 400, set()),
        ("300750.SZ", 320, set(range(120, 127))),  # 中段停牌 7 根
        ("688981.SH", 260, set(range(0, 15))),  # 上市晚 15 根
    ]
    frames, expectations = [], {}
    total_hits = 0
    for symbol, bars, suspended in specs:
        series = _random_series(seed=int(rng.integers(1, 10**6)), bars=bars)
        frames.append(_panel_from_series(series, symbol, suspended_rows=suspended))
        expectations[symbol] = _reference_on_valid_rows(series, suspended)
        total_hits += int(expectations[symbol].sum())

    market = _market(frames)
    # scan_days=1 -> entry 就是「当日出现信号」, 可与源公式逐 bar 对拍
    signals = MATRIX_STRATEGY.compute_signals(market, _params(scan_days=1))

    for asset_id, symbol in enumerate(market.symbols):
        entry = signals.entry[:, asset_id].astype(bool)
        valid_rows = np.flatnonzero(np.isfinite(market.close[:, asset_id]))
        expected = _map_to_rows(expectations[symbol], valid_rows, entry.shape)
        np.testing.assert_array_equal(entry, expected)

    assert total_hits >= 1, "对拍是空跑: 随机序列没有产生任何信号"


def test_trend_dragon_scan_window_marks_subsequent_valid_bars():
    """`近 N 日出现过信号` = 命中点及其后 N-1 根有效 bar。"""
    from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY

    series = _random_series(seed=99, bars=400)
    market = _market([_panel_from_series(series, "000001.SZ")])

    single = MATRIX_STRATEGY.compute_signals(market, _params(scan_days=1)).entry[:, 0].astype(bool)
    window = MATRIX_STRATEGY.compute_signals(market, _params(scan_days=5)).entry[:, 0].astype(bool)

    hits = np.flatnonzero(single)
    assert hits.size >= 1, "构造序列没有信号, 窗口断言无意义"

    expected = np.zeros_like(single)
    for row in hits:
        expected[row : row + 5] = True
    np.testing.assert_array_equal(window, expected)
    assert window.sum() > single.sum()


def test_trend_dragon_param_switches_only_relax_conditions():
    """关掉 MA10 / 收盘强度两个开关后, 命中集合只能变大。"""
    from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY

    series = _random_series(seed=25, bars=400)
    market = _market([_panel_from_series(series, "000001.SZ")])

    strict = MATRIX_STRATEGY.compute_signals(market, _params(scan_days=1)).entry[:, 0].astype(bool)
    relaxed = (
        MATRIX_STRATEGY.compute_signals(
            market,
            _params(scan_days=1, require_above_ma10=False, require_strong_close=False),
        )
        .entry[:, 0]
        .astype(bool)
    )

    assert relaxed.sum() > strict.sum()
    # 严格版必须是放宽版的子集 (两个开关只做 AND, 不引入新命中)
    assert not (strict & ~relaxed).any()


def test_trend_dragon_bias_cap_filters_high_bias_entries():
    from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY

    series = _random_series(seed=31, bars=400, drift=0.004)
    market = _market([_panel_from_series(series, "000001.SZ")])

    uncapped = MATRIX_STRATEGY.compute_signals(market, _params(scan_days=1)).entry[:, 0].astype(bool)
    capped = (
        MATRIX_STRATEGY.compute_signals(market, _params(scan_days=1, bias20_cap_pct=3.0))
        .entry[:, 0]
        .astype(bool)
    )

    assert not (capped & ~uncapped).any(), "乖离率过滤只能砍命中, 不能新增"
    bias_pct = matrix_feature(market, "ma20_bias")[:, 0] * 100.0
    assert np.isfinite(bias_pct[capped]).all()
    assert (bias_pct[capped] <= 3.0 + 1e-4).all()
    assert capped.sum() < uncapped.sum(), "构造序列没能体现乖离率过滤的效果"


def test_trend_dragon_scan_days_falls_back_and_clamps():
    """非法 scan_days 回落到源脚本默认窗口; 越界值夹到 [1, 20], 不应抛错。"""
    from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY

    series = _random_series(seed=5, bars=260)
    market = _market([_panel_from_series(series, "000001.SZ")])

    def entry(days: object) -> np.ndarray:
        return MATRIX_STRATEGY.compute_signals(market, {"scan_days": days}).entry

    default = entry(5)
    for invalid in (None, "abc"):
        np.testing.assert_array_equal(entry(invalid), default)
    np.testing.assert_array_equal(entry(0), entry(1))
    np.testing.assert_array_equal(entry(-3), entry(1))
    np.testing.assert_array_equal(entry(999), entry(20))


def test_trend_dragon_meta_and_signal_ids():
    from app.strategy.builtin import trend_dragon as module

    assert module.META["id"] == "trend_dragon"
    assert module.META["asset_types"] == ["stock"]
    assert module.ENTRY_SIGNALS == ["signal_trend_dragon"]
    assert module.MATRIX_STRATEGY.required_fields() == frozenset({"open", "high", "close"})
    assert module.MATRIX_STRATEGY.required_warmup_bars({}) > 0


def test_trend_dragon_defaults_match_tool_usage():
    """默认参数对齐源脚本的日常调用 ``--max-momentum 1 --max-bias20 10``。

    源脚本 docstring 第 30 行就是 ``python 选股_趋势擒龙.py --max-momentum 1
    --max-bias20 10 --feishu`` —— 用户是**带着限制**跑的。面板默认若不带同样的
    限制, 两边选出来的票天然对不上 (2026-09-17 实测: 面板无限制当日 7097 只 vs
    工具 578 只)。
    """
    from app.strategy.builtin import trend_dragon as module

    params = {item["id"]: item for item in module.META["params"]}
    assert params["use_momentum_filter"]["default"] is True
    assert params["momentum_cap"]["default"] == 1.0
    assert params["bias20_cap_pct"]["default"] == 10.0

    # 步长要允许 0.5 这类非整数上限 (源脚本 --max-momentum 0.5)
    for key in ("momentum_cap", "bias20_cap_pct"):
        assert params[key]["step"] <= 0.5, f"{key} 的步长太粗, 调不出 0.5"
    assert params["momentum_cap"]["min"] <= 0.5 <= params["momentum_cap"]["max"]

    # 面板只回传用户改过的键, 所以缺省路径必须与 META 的默认一致 ——
    # 否则会出现「UI 显示开着、实际没生效」。
    strategy = module.MATRIX_STRATEGY
    assert strategy.required_warmup_bars({}) == strategy.required_warmup_bars(
        {"use_momentum_filter": True}
    )
    assert strategy.required_warmup_bars({"use_momentum_filter": False}) < strategy.required_warmup_bars({})
