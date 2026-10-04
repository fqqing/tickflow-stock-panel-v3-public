"""缠论结构买卖点矩阵策略的对拍测试。

被测层是**策略层**: 它把 ``app.indicators.chan.analyze`` 的顺序结构结果映射到
``(时间 x 标的)`` 的 entry / exit 矩阵。所以这里独立复现的是**映射规则**,
而不是重写缠论引擎 (引擎本身的不变量由 ``tf_chan_verify.py`` 那一套单独验过):

- 参照实现完全绕开策略类, 自己按「valid bar CSR -> analyze -> 索引 + 1」重算一遍;
- 额外断言几个结构性不变量 (笔方向交替 / 中枢 zg > zd / 信号类型合法),
  防止合成序列刚好构不出结构、让「两边都空」的假通过。

⚠️ 长度必须够: 严格笔要求两分型间隔 >= 4 根合并 K 线, 一买还要 MACD 背驰,
太短的随机序列会零信号。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

from app.backtest.matrix import build_market_data_matrix
from app.indicators.chan import analyze

_STRATEGY_PATH = (
    Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin" / "chan_structure.py"
)


@lru_cache(maxsize=1)
def _load_strategy():
    from app.strategy.engine import StrategyEngine

    return StrategyEngine._load_file(_STRATEGY_PATH)


def _panel(assets: dict[str, np.ndarray], n_rows: int):
    """把 {symbol: (close, high, low)} 拼成长表 (symbol, date, open, high, low, close, volume)。"""
    import datetime as dt

    import polars as pl

    base = dt.date(2024, 1, 1)
    rows = []
    for symbol, parts in assets.items():
        close, high, low = parts
        for i in range(n_rows):
            c = close[i]
            if not np.isfinite(c):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "date": base + dt.timedelta(days=i),
                    "open": float(c),
                    "high": float(high[i]),
                    "low": float(low[i]),
                    "close": float(c),
                    "volume": 1_000.0,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def _wave(n: int, seed: int, amplitude: float = 0.25) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构造带趋势与波动的价格序列, 让缠论能构出笔 / 中枢 / 背离。"""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.012, n) + np.sin(np.arange(n) / 9.0) * 0.004
    close = 20.0 * np.exp(np.cumsum(steps))
    span = close * amplitude * 0.08
    high = close + span * 0.6
    low = close - span * 0.6
    return close, high, low


def _reference(high, low, close, valid_positions, asset_id, shape, params):
    """独立参照实现: 只做「analyze -> 确认位 +1 -> 落矩阵」的映射。"""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    entry = np.zeros(shape, dtype=np.uint8)
    exit_ = np.zeros(shape, dtype=np.uint8)
    buy_flags = {
        "1buy": bool(params.get("use_1buy", True)),
        "2buy": bool(params.get("use_2buy", False)),
        "3buy": bool(params.get("use_3buy", True)),
    }
    sell_flags = {
        "1sell": bool(params.get("use_1sell", True)),
        "2sell": bool(params.get("use_2sell", True)),
        "3sell": bool(params.get("use_3sell", True)),
    }
    use_sell = bool(params.get("use_sell_exit", True))
    analysis = analyze(high, low, close, strict=bool(params.get("strict", True)))
    for signal in analysis.signals:
        confirm = signal.index + 1
        if confirm >= len(valid_positions):
            continue
        row = int(valid_positions[confirm])
        if signal.is_buy:
            if buy_flags.get(signal.kind):
                entry[row, asset_id] = 1
        elif use_sell and sell_flags.get(signal.kind):
            exit_[row, asset_id] = 1
    return entry, exit_, analysis


def _f32(values: np.ndarray) -> np.ndarray:
    """矩阵里的 OHLC 是 float32 —— 参照实现必须过同一道量化, 否则均线末位差会让
    严格比较的分型判定翻转 (仓库已知坑, 见 verify_trend_dragon 的 _reference_hit_f32)。"""
    return np.asarray(values, dtype=np.float32).astype(np.float64)


def _run(assets: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]], params: dict | None = None):
    n_rows = max(len(v[0]) for v in assets.values())
    panel = _panel(assets, n_rows)
    market = build_market_data_matrix(panel)
    strategy = _load_strategy().matrix_strategy
    resolved = {
        "use_1buy": True,
        "use_2buy": False,
        "use_3buy": True,
        "use_sell_exit": True,
        "use_1sell": True,
        "use_2sell": True,
        "use_3sell": True,
        "strict": True,
        "min_bars": 20,
    }
    resolved.update(params or {})
    signals = strategy.compute_signals(market, resolved)
    offsets = market.valid_bars.offsets
    rows = market.valid_bars.rows
    expect_entry = np.zeros(market.shape, dtype=np.uint8)
    expect_exit = np.zeros(market.shape, dtype=np.uint8)
    per_asset_analysis = []
    # ⚠️ 矩阵的列顺序是 symbol 字典序, 不是构造顺序 —— 必须按 market.symbols 对齐 asset_id
    for asset_id, symbol in enumerate(market.symbols):
        whole = assets[symbol]
        positions = rows[int(offsets[asset_id]) : int(offsets[asset_id + 1])]
        assert len(positions) == n_rows, f"{symbol} 有效 bar 数应与面板行数一致"
        entry, exit_, analysis = _reference(
            _f32(whole[1][positions]), _f32(whole[2][positions]), _f32(whole[0][positions]),
            positions, asset_id, market.shape, resolved,
        )
        expect_entry |= entry
        expect_exit |= exit_
        per_asset_analysis.append((symbol, analysis))
    return signals, expect_entry, expect_exit, per_asset_analysis, market


@pytest.fixture(scope="module")
def _assets():
    # 种子是按「必须六类信号齐全」挑出来的: 16 出全 1/2/3 买卖, 19 与 30 补三买/三卖。
    # 若换成随机种子, 合成序列很可能一类三买都不出, 切换类测试就退化成 0 == 0 的假通过。
    return {
        "600000.SH": _wave(260, seed=16),
        "000001.SZ": _wave(260, seed=19),
        "300750.SZ": _wave(260, seed=30, amplitude=0.4),
    }


def test_engine_loads_strategy():
    strategy = _load_strategy()
    assert strategy.execution_backend == "matrix_native"
    assert strategy.matrix_strategy is not None
    assert strategy.filter_fn is None and strategy.filter_history_fn is None
    assert strategy.stop_loss == -0.08
    assert strategy.max_hold_days == 30


def test_compute_signals_matches_reference_mapping(_assets):
    signals, expect_entry, expect_exit, analyses, _ = _run(_assets)

    assert signals.entry.shape == expect_entry.shape
    assert np.array_equal(signals.entry.astype(bool), expect_entry.astype(bool))
    assert np.array_equal(signals.exit.astype(bool), expect_exit.astype(bool))
    # 信号码必须能索引回 entry_signal_ids / exit_signal_ids —— 关掉中间某一类后,
    # 下标会整体左移, 用固定的全量映射表会把成交记录标错信号名。
    for name, matrix, ids in (
        ("entry", signals.entry_signal_code, signals.entry_signal_ids),
        ("exit", signals.exit_signal_code, signals.exit_signal_ids),
    ):
        active = matrix[matrix >= 0]
        assert active.size > 0, name
        assert active.min() >= 0 and active.max() < len(ids), (name, ids)
    # 合成序列必须真的构出信号, 否则这条对拍是「两边都空」的假通过
    assert int(expect_entry.sum()) > 0, "合成序列没有买点, 对拍无意义"
    assert int(expect_exit.sum()) > 0, "合成序列没有卖点, 对拍无意义"
    kinds = {s.kind for _symbol, a in analyses for s in a.signals}
    assert {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"} <= kinds, kinds
    for _symbol, analysis in analyses:
        assert len(analysis.strokes) > 0


def test_structure_invariants(_assets):
    """独立复算结构本身的硬约束, 防合成序列退化。"""
    _signals, _e, _x, analyses, _market = _run(_assets)
    for symbol, analysis in analyses:
        for stroke in analysis.strokes:
            assert stroke.direction in (1, -1), symbol
            assert stroke.end_index > stroke.start_index, symbol
        for i in range(1, len(analysis.strokes)):
            assert analysis.strokes[i].direction != analysis.strokes[i - 1].direction, symbol
        for center in analysis.centers:
            assert center.zg > center.zd, symbol
            assert center.stroke_count >= 3, symbol
        for signal in analysis.signals:
            assert signal.kind in {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}, symbol


def test_entry_never_precedes_signal_bar(_assets):
    """确认延迟: 任何启用类型的 entry 都必须严格晚于它的缠论信号索引 (无前视)。"""
    params = {"use_1buy": True, "use_2buy": False, "use_3buy": True}
    signals, _e, _x, analyses, _market = _run(_assets, params)
    enabled = {"1buy", "3buy"}
    rows = _market.valid_bars.rows
    offsets = _market.valid_bars.offsets
    checked = 0
    for asset_id, (_symbol, analysis) in enumerate(analyses):
        positions = rows[int(offsets[asset_id]) : int(offsets[asset_id + 1])]
        position_of_row = {int(r): p for p, r in enumerate(positions)}
        for signal in analysis.signals:
            if not signal.is_buy or signal.kind not in enabled:
                continue
            confirm = signal.index + 1
            if confirm >= len(positions):
                continue
            row = int(positions[confirm])
            assert signals.entry[row, asset_id] == 1
            assert position_of_row[row] > signal.index
            checked += 1
    assert checked > 0, "没有可核对的买点"


def test_sell_exit_switch(_assets):
    """关掉缠论卖点离场后 exit 必须全零, entry 不受影响。"""
    on = _run(_assets, {"use_sell_exit": True})
    off = _run(_assets, {"use_sell_exit": False})
    assert np.array_equal(on[0].entry, off[0].entry)
    assert int(on[0].exit.sum()) > 0
    assert int(off[0].exit.sum()) == 0
    assert off[0].exit_signal_ids == ()


def test_buy_kind_switch(_assets):
    """只留三买时, entry 数必须少于「一买 + 三买」且仍非零; 信号码要跟着左移。"""
    both = _run(_assets, {"use_1buy": True, "use_3buy": True})
    only3 = _run(_assets, {"use_1buy": False, "use_3buy": True})
    assert int(only3[0].entry.sum()) < int(both[0].entry.sum())
    assert list(only3[0].entry_signal_ids) == ["signal_chan_3buy"]
    # 只声明一个类型时, 所有 entry 的信号码只能是 0
    codes = only3[0].entry_signal_code[only3[0].entry_signal_code >= 0]
    assert codes.size > 0
    assert set(codes.tolist()) == {0}


def test_short_history_produces_no_signal():
    """历史不足 min_bars 时不得产出任何信号 (防止新股被隐式选中)。"""
    short = {"600000.SH": _wave(40, seed=21)}
    signals, expect_entry, expect_exit, _analyses, _market = _run(short, {"min_bars": 120})
    assert int(signals.entry.sum()) == 0
    assert int(signals.exit.sum()) == 0
    assert int(expect_entry.sum()) == 0
    assert int(expect_exit.sum()) == 0


def test_no_buy_kind_selected_is_empty(_assets):
    signals, _e, _x, _analyses, _market = _run(
        _assets, {"use_1buy": False, "use_2buy": False, "use_3buy": False}
    )
    assert int(signals.entry.sum()) == 0
    assert int(signals.exit.sum()) == 0
