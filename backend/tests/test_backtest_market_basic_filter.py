"""港美股矩阵基础过滤必须真实生效 (市值/换手率/价格/成交额)。

背景: 用户反馈「回测-基础过滤里没有港美股」。调查结论:
  1. 后端矩阵路径本就支持港美股基础过滤 —— instruments 维表带 total_shares /
     float_shares (hk 覆盖率 97%+ / us 93%+), 矩阵加载时注入, 换手率由
     float_shares 现算 (_write_turnover_rate_matrix)。真实数据验证:
     港股 2712 只中「市值≥200亿 + 换手率≥0.5%」精确筛出 344 只。
  2. 真正的问题是前端: 基础过滤 UI 无市场感知, 任何市场都渲染 A 股板块
     (沪主板/深主板/…) 与「排除 ST」, 而港美股既无板块概念也无 ST;
     后端 _symbol_in_boards 对非 A 股符号显式放行 (no-op), 造成「界面有
     板块选项却不生效」的误导。前端已改为港美股隐藏板块与 ST。

本测试用纯构造矩阵锁定后端行为 (不依赖本地数据仓, CI 可跑)。
"""
from __future__ import annotations

from datetime import date

import numpy as np

from app.backtest.matrix import MarketDataMatrix, build_basic_filter_mask

_TIME = 3  # 3 个交易日
_ASSETS = ["00700.HK", "09988.HK", "00005.HK", "01810.HK"]


def _matrix(fields: dict[str, np.ndarray]) -> MarketDataMatrix:
    """构造 (time × assets) 矩阵: 价格/成交量恒定, 其余字段按资产给一维值广播。"""
    n = len(_ASSETS)
    shape = (_TIME, n)
    timestamps = np.arange(_TIME)
    base = {
        "open": np.full(shape, 50.0),
        "high": np.full(shape, 51.0),
        "low": np.full(shape, 49.0),
        "close": np.full(shape, 50.0),
        "volume": np.full(shape, 1_000_000.0),
        "amount": np.full(shape, 50_000_000.0),
    }
    for name, values in fields.items():
        arr = np.asarray(values, dtype=np.float64)
        base[name] = np.broadcast_to(arr.reshape(1, -1), shape).copy()
    # 模拟矩阵加载路径: 有 float_shares 时 turnover_rate 由 _write_turnover_rate_matrix
    # 现算并写入 fields —— 测试构造同样补上, 否则过滤侧读到全 NaN 被 _apply_bound 跳过
    if "float_shares" in base and "turnover_rate" not in base:
        shares = base["float_shares"]
        base["turnover_rate"] = np.where(
            np.isfinite(shares) & (shares != 0),
            base["volume"] * 10_000.0 / shares,
            np.nan,
        )
    return MarketDataMatrix(
        timestamps=timestamps,
        timestamp_labels=tuple(d.isoformat() for d in (
            date(2026, 8, 1), date(2026, 8, 4), date(2026, 8, 5))),
        session_ids=np.full(_TIME, 0),
        symbols=tuple(_ASSETS),
        names=tuple(_ASSETS),
        open=base["open"],
        high=base["high"],
        low=base["low"],
        close=base["close"],
        volume=base["volume"],
        tradable=np.ones(shape, dtype=bool),
        limit_up_locked=np.zeros(shape, dtype=bool),
        limit_down_locked=np.zeros(shape, dtype=bool),
        fields=base,
    )


def test_hk_market_cap_filter_uses_close_times_total_shares() -> None:
    # 股本(股): 00700=200亿, 09988=100亿, 00005=50亿, 01810=10亿
    m = _matrix({
        "total_shares": [2.0e10, 1.0e10, 5.0e9, 1.0e9],
        "float_shares": [2.0e10, 1.0e10, 5.0e9, 1.0e9],
    })
    # 市值 = close(50) × total_shares → 10000/5000/2500/500 亿
    mask = build_basic_filter_mask(m, {"enabled": True, "market_cap_min": 5.0e11})
    kept = [symbol for symbol, ok in zip(_ASSETS, mask.all(axis=0)) if ok]
    assert kept == ["00700.HK", "09988.HK"]  # ≥5000 亿


def test_hk_turnover_filter_uses_float_shares_derived_rate() -> None:
    # 换手率 = volume×10000 / float_shares (百分数)。volume=1e5 手 → 100万股/日
    m = _matrix({
        "volume": [1.0e5, 1.0e5, 1.0e5, 1.0e5],
        "float_shares": [2.0e10, 1.0e10, 5.0e9, 1.0e9],
    })
    # 00700: 0.05% | 09988: 0.1% | 00005: 0.2% | 01810: 1.0% (百分数)
    mask = build_basic_filter_mask(m, {"enabled": True, "turnover_min": 0.15})
    kept = [symbol for symbol, ok in zip(_ASSETS, mask.all(axis=0)) if ok]
    assert kept == ["00005.HK", "01810.HK"]  # ≥0.15% 的两只


def test_hk_boards_and_st_are_noops_outside_cn() -> None:
    """港美股下板块过滤与排除 ST 不得误杀 —— 与前端隐藏它们的行为一致。"""
    m = _matrix({})
    mask = build_basic_filter_mask(m, {
        "enabled": True,
        "boards": ["创业板", "科创板"],   # A 股板块词, 对港符号无意义
        "exclude_st": True,
    })
    assert mask.all()  # 全部保留 (non-cn 符号放行)


def test_us_price_filter_still_applies() -> None:
    m = _matrix({"close": [10.0, 100.0, 250.0, 1000.0]})
    mask = build_basic_filter_mask(m, {"enabled": True, "price_min": 200.0})
    kept = [symbol for symbol, ok in zip(_ASSETS, mask.all(axis=0)) if ok]
    assert kept == ["00005.HK", "01810.HK"]  # 250 / 1000 (price 对所有市场通用)


def test_missing_share_fields_skips_bounds_without_crashing() -> None:
    """矩阵缺股本列时, 市值/换手率界应被安全跳过 (全 NaN 界 no-op), 不得抛错或全灭。"""
    m = _matrix({})
    mask = build_basic_filter_mask(m, {
        "enabled": True,
        "market_cap_min": 5.0e11,
        "turnover_min": 0.5,
    })
    assert mask.all()


def test_is_cn_symbol_classifies_digit_codes_by_suffix() -> None:
    """港股数字代码 (00700.HK) 不得被 isdigit 误判为 A 股 —— 回归 `_symbol_in_boards` 全灭 bug。"""
    from app.backtest.matrix import _is_cn_symbol

    assert _is_cn_symbol("600000.SH") is True
    assert _is_cn_symbol("300750.SZ") is True
    assert _is_cn_symbol("920344.BJ") is True
    assert _is_cn_symbol("00700.HK") is False   # 纯数字但港股
    assert _is_cn_symbol("AAPL.US") is False
    assert _is_cn_symbol("600000") is True      # 无后缀 6 位数字兼容路径


def test_strategy_engine_exclude_st_spares_hk_us_names() -> None:
    """策略引擎侧 exclude_st: 港美股放行 (名字含 ST 子串不误杀), A 股 *ST 仍过滤。"""
    import polars as pl

    from app.strategy.engine import StrategyEngine

    df = pl.DataFrame({
        "symbol": ["00150.HK", "600000.SH", "AAPL.US", "600123.SH"],
        "name": ["HYPEBEAST", "浦发银行", "APPLE", "*ST 兴业"],
        "close": [10.0, 10.0, 10.0, 10.0],
    })
    out = StrategyEngine._apply_basic_filter(df, {"enabled": True, "exclude_st": True})
    assert out["symbol"].to_list() == ["00150.HK", "600000.SH", "AAPL.US"]
