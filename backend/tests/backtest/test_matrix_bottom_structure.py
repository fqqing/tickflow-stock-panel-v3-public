"""内置「底部结构」/「钝化加低九」—— 矩阵原生实现 vs 源公式直译对拍。

参考实现写在下面: 按 ``AKL公式解析/底部结构选股_源码.txt`` (DBJGXG) 与
``钝化加低九选股_源码.txt`` (DHJDJXG) 逐行直译的纯 numpy 版本, 与生产代码
(共享模块 ``_quant_structure``) **不共享任何算子**, 因此对拍才有意义。

⚠️ 「底部结构」取的是 ``REF(底部钝化, 1)`` 而**不是** ``REF(底钝化, 1)`` —— 源码原文
``底部结构:=DIFF>REF(DIFF,1) AND (REF(底部钝化,1) AND DIFL1*0.9884<DIFF);``。
``底钝化`` 只被 ``M4:=BARSLAST(底钝化 OR 底再次钝化)`` 引用, 进而只服务于
``底结构消失`` 那段指标图文字标注, 不在选股链上。用户脚本
``qushiqinlong/底部结构选股.py::dbjg_signals`` 用的也是 ``底部钝化`` —— 两边本来
一致, 是面板早期实现偏了一格 (多加了"首次成立 + DIFF<DEA"两道约束, 会让信号
整体后移)。``test_bottom_structure_uses_raw_stale_not_first_stale`` 专门盯这条。

对拍在**有效 bar 压缩序列**上做: 矩阵实现自动跳停牌行, 参考实现则在剔除
停牌行后的连续序列上计算, 两者应当逐位一致。
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    MarketDataMatrix,
    build_market_data_matrix,
)

_FIELDS = ("open", "high", "low", "close", "volume")
_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin"
_SHARED_MODULE_PATH = _BUILTIN_DIR / "_quant_structure.py"


def _load_shared_module() -> ModuleType:
    """按引擎的加载方式 (spec_from_file_location) 载入共享依赖模块。"""
    spec = importlib.util.spec_from_file_location(
        "_quant_structure_under_test", _SHARED_MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_builtin_module(name: str) -> ModuleType:
    """加载内置策略模块。

    策略文件里的 ``from _quant_structure import ...`` 依赖 builtin/ 在 sys.path 上,
    而那只在 ``StrategyEngine._load_file`` 内部做 —— 直接 ``import`` 会
    ``ModuleNotFoundError``。这里先走一次引擎加载铺好环境, 再按模块名取回来,
    保证测试与生产走的是同一条加载链。
    """
    from app.strategy.engine import StrategyEngine

    StrategyEngine._load_file(_BUILTIN_DIR / f"{name}.py")
    return importlib.import_module(f"app.strategy.builtin.{name}")


# ===== 源公式的逐 bar 直译参考实现 =====


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty(values.size, dtype=np.float64)
    out[0] = values[0]
    for index in range(1, values.size):
        out[index] = alpha * values[index] + (1.0 - alpha) * out[index - 1]
    return out


def _cross(fast: np.ndarray, slow: np.ndarray) -> np.ndarray:
    out = np.zeros(fast.size, dtype=bool)
    out[1:] = (fast[1:] > slow[1:]) & (fast[:-1] <= slow[:-1])
    return out


def _barslast(condition: np.ndarray) -> np.ndarray:
    """BARSLAST: 当前成立记 0, 从未成立记 NaN (源脚本口径)。"""
    out = np.full(condition.size, np.nan, dtype=np.float64)
    last = -1
    for index in range(condition.size):
        if condition[index]:
            last = index
        if last >= 0:
            out[index] = index - last
    return out


def _ref_at(values: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    out = np.full(values.size, np.nan, dtype=np.float64)
    for index in range(values.size):
        step = offsets[index]
        if np.isfinite(step) and step >= 0 and int(step) == step and int(step) <= index:
            out[index] = values[index - int(step)]
    return out


def _llv_at(values: np.ndarray, windows: np.ndarray) -> np.ndarray:
    out = np.full(values.size, np.nan, dtype=np.float64)
    for index in range(values.size):
        width = windows[index]
        if not np.isfinite(width) or width < 1 or int(width) != width:
            continue
        span = int(width)
        if span > index + 1:
            continue
        segment = values[index - span + 1 : index + 1]
        if np.isnan(segment).all():
            continue
        out[index] = float(np.nanmin(segment))
    return out


def _lt(left: np.ndarray, right: np.ndarray | float) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        result = np.asarray(left) < right
    return np.where(np.isfinite(left) & np.isfinite(right), result, False)


def _gt(left: np.ndarray, right: np.ndarray | float) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        result = np.asarray(left) > right
    return np.where(np.isfinite(left) & np.isfinite(right), result, False)


def _previous_true(condition: np.ndarray) -> np.ndarray:
    out = np.zeros(condition.size, dtype=bool)
    out[1:] = condition[:-1]
    return out


def _previous_zero(condition: np.ndarray) -> np.ndarray:
    """``REF(条件, 1) = 0``: 上一根不成立; 无历史时为 False。"""
    out = np.zeros(condition.size, dtype=bool)
    out[1:] = ~condition[:-1]
    return out


def _nine_turn_down(close: np.ndarray, level: int = 9) -> np.ndarray:
    a1 = np.zeros(close.size, dtype=bool)
    a2 = np.zeros(close.size, dtype=bool)
    a1[4:] = close[4:] > close[:-4]
    a2[4:] = close[4:] < close[:-4]
    step = a2 & _previous_true(a1)
    for _ in range(2, level + 1):
        step = a2 & _previous_true(step)
    return step


def reference_bottom_chain(close: np.ndarray, *, macd_prev_bars: int = 1) -> dict[str, np.ndarray]:
    """DBJGXG 的逐 bar 直译: 返回整条底部钝化链。"""
    c = np.asarray(close, dtype=np.float64)
    size = c.size
    diff = _ema(c, 12) - _ema(c, 26)
    dea = _ema(diff, 9)
    macd = (diff - dea) * 2.0

    n1 = _barslast(_cross(dea, diff))
    m1 = _barslast(_cross(diff, dea))

    cl1 = _llv_at(c, n1 + 1)
    cl2 = _ref_at(cl1, m1 + 1)
    cl3 = _ref_at(cl2, m1 + 1)
    difl1 = _llv_at(diff, n1 + 1)
    difl2 = _ref_at(difl1, m1 + 1)
    difl3 = _ref_at(difl2, m1 + 1)

    macd_prev = _ref_at(macd, np.full(size, float(macd_prev_bars)))
    diff_prev = _ref_at(diff, np.ones(size))
    negative = macd_prev < 0.0

    direct = (cl1 < cl2) & (difl1 > difl2) & negative & (difl2 < 0.0)
    peak = (cl1 < cl3) & (difl1 < difl2) & (difl1 > difl3) & (diff < dea) & negative & (difl3 < 0.0)
    stale = (direct | peak) & negative
    first_stale = stale & _previous_zero(stale) & (diff < dea)
    # 底部结构引用 REF(底部钝化,1) —— 不是 REF(底钝化,1)
    structure = (diff > diff_prev) & _previous_true(stale) & (difl1 * 0.9884 < diff)
    formed = structure & _previous_zero(structure)
    return {
        "diff": diff,
        "dea": dea,
        "macd": macd,
        "direct": direct,
        "peak": peak,
        "stale": stale,
        "first_stale": first_stale,
        "structure": structure,
        "formed": formed,
    }


# ===== 行情构造工具 =====


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


def _random_series(seed: int, bars: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.022, bars)))
    open_ = close * (1.0 + rng.normal(0.0, 0.010, bars))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.006, bars)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.006, bars)))
    volume = 1000.0 * np.exp(rng.normal(0.0, 0.3, bars))
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def _valid_rows(series: dict[str, np.ndarray], suspended_rows: set[int]) -> np.ndarray:
    keep = np.ones(series["close"].size, dtype=bool)
    keep[list(suspended_rows)] = False
    return keep


# ===== 对拍 =====


def _panel_specs() -> list[tuple[str, int, set[int], int]]:
    """(symbol, bars, 停牌行, 随机种子) —— 种子写死, 不用 hash (会被 PYTHONHASHSEED 打乱)。"""
    return [
        ("000001.SZ", 500, set(), 1001),
        ("300750.SZ", 420, set(range(140, 148)), 1002),  # 中段停牌 8 根
        ("688981.SH", 360, set(range(0, 18)), 1003),  # 上市晚 18 根
    ]


def test_bottom_chain_matches_source_formula_on_random_panel():
    shared = _load_shared_module()

    frames, expectations, hits = [], {}, 0
    for symbol, bars, suspended, seed in _panel_specs():
        series = _random_series(seed=seed, bars=bars)
        frames.append(_panel_from_series(series, symbol, suspended_rows=suspended))
        keep = _valid_rows(series, suspended)
        reference = reference_bottom_chain(series["close"][keep])
        expectations[symbol] = reference
        hits += int(reference["formed"].sum())

    market = _market(frames)
    chain = shared.bottom_stale_chain(market)

    for asset_id, symbol in enumerate(market.symbols):
        reference = expectations[symbol]
        rows = np.flatnonzero(np.isfinite(market.close[:, asset_id]))
        assert rows.size == reference["formed"].size
        for name in ("direct", "peak", "stale", "first_stale", "structure", "formed"):
            actual = chain[name][rows, asset_id].astype(bool)
            np.testing.assert_array_equal(
                actual,
                reference[name],
                err_msg=f"{symbol} 的 {name} 与源公式直译不一致",
            )

    assert hits >= 1, "对拍是空跑: 随机序列没有产生任何底结构形成"


def test_bottom_structure_entry_matches_formed_signal():
    """scan_days=1 时 entry 就是「当日底结构形成」, 与纯函数参考逐位一致。"""
    shared = _load_shared_module()
    strategy = _load_builtin_module("bottom_structure").MATRIX_STRATEGY

    series = _random_series(seed=20260916, bars=520)
    market = _market([_panel_from_series(series, "000001.SZ")])

    signals = strategy.compute_signals(market, {"scan_days": 1})
    chain = shared.bottom_stale_chain(market)
    np.testing.assert_array_equal(signals.entry, chain["formed"].astype(np.uint8))

    reference = reference_bottom_chain(series["close"])
    np.testing.assert_array_equal(signals.entry[:, 0].astype(bool), reference["formed"])


def test_stale_nine_turn_matches_source_formula():
    """钝化加低九 = 底钝化 AND T9, 且隔峰底钝化用 REF(MACD,2)。"""
    shared = _load_shared_module()
    strategy = _load_builtin_module("stale_nine_turn").MATRIX_STRATEGY

    series = _random_series(seed=314159, bars=700)
    market = _market([_panel_from_series(series, "000001.SZ")])

    signals = strategy.compute_signals(market, {"scan_days": 1})
    chain = shared.bottom_stale_chain(market, macd_prev_bars=2)
    expected = chain["first_stale"] & shared.nine_turn_down(market.close, chain["valid"])
    np.testing.assert_array_equal(signals.entry[:, 0].astype(bool), expected[:, 0])

    reference = reference_bottom_chain(series["close"], macd_prev_bars=2)
    expected_source = reference["first_stale"] & _nine_turn_down(series["close"])
    np.testing.assert_array_equal(signals.entry[:, 0].astype(bool), expected_source)


def test_macd_prev_bars_switch_changes_peak_condition():
    """REF(MACD,1) / REF(MACD,2) 是两个公式的真实差异, 不能互相顶替。

    两者只在 MACD 换号的那一两天分叉, 而且要同时满足隔峰底钝化的其余条件,
    所以随机序列里比较罕见 —— 扫描若干种子直到找出体现差异的那条。
    """
    shared = _load_shared_module()

    for seed in range(1, 60):
        series = _random_series(seed=seed, bars=700)
        market = _market([_panel_from_series(series, "000001.SZ")])
        one = shared.bottom_stale_chain(market, macd_prev_bars=1)
        two = shared.bottom_stale_chain(market, macd_prev_bars=2)
        if not np.array_equal(one["peak"], two["peak"]):
            break
    else:
        pytest.fail("扫描 60 条序列都没能体现 REF(MACD,1) / REF(MACD,2) 的差异")

    reference_one = reference_bottom_chain(series["close"], macd_prev_bars=1)
    reference_two = reference_bottom_chain(series["close"], macd_prev_bars=2)
    np.testing.assert_array_equal(one["peak"][:, 0], reference_one["peak"])
    np.testing.assert_array_equal(two["peak"][:, 0], reference_two["peak"])


def test_formed_signal_is_first_occurrence_of_structure():
    """底结构形成必须是底部结构的首次成立 (前一根必不成立)。"""
    shared = _load_shared_module()

    series = _random_series(seed=161803, bars=520)
    market = _market([_panel_from_series(series, "000001.SZ")])
    chain = shared.bottom_stale_chain(market)

    structure = chain["structure"][:, 0].astype(bool)
    formed = chain["formed"][:, 0].astype(bool)
    assert not (formed & ~structure).any(), "形成信号必须落在底部结构上"

    rows = np.flatnonzero(formed)
    assert rows.size >= 1, "构造序列没有底结构形成信号"
    assert not structure[rows - 1].any(), "首次成立的前一根不应仍在结构内"


def test_bottom_structure_uses_raw_stale_not_first_stale():
    """回归: 「底部结构」用的是 ``REF(底部钝化, 1)``, 不是 ``REF(底钝化, 1)``。

    两者不是同一个变量 —— ``first_stale``(底钝化) 在 ``stale``(底部钝化) 之上还多了
    「上一根尚未钝化」与 ``DIFF < DEA`` 两道约束, 窄得多。2026-09-17 用 2026-09-15
    的全市场真实数据实测: 误用 ``first_stale`` 时面板只选出 9 只, 用 ``stale`` 选出
    48 只 (用户工具当日 43 只) —— 这就是「面板和工具选出来不一致」的根因。
    """
    shared = _load_shared_module()

    series = _random_series(seed=161803, bars=520)
    market = _market([_panel_from_series(series, "000001.SZ")])
    chain = shared.bottom_stale_chain(market)

    stale = chain["stale"][:, 0].astype(bool)
    first_stale = chain["first_stale"][:, 0].astype(bool)
    structure = chain["structure"][:, 0].astype(bool)

    # first_stale 必须是 stale 的真子集, 且严格更少 —— 否则这条回归是空跑
    assert not (first_stale & ~stale).any()
    assert first_stale.sum() < stale.sum()

    prev_stale = np.concatenate(([False], stale[:-1]))
    prev_first = np.concatenate(([False], first_stale[:-1]))

    assert (structure & ~prev_stale).sum() == 0, "底部结构必须紧跟在「底部钝化」之后"
    assert (structure & ~prev_first).sum() > 0, (
        "构造序列没能体现两种口径的差异: 换成 REF(底钝化,1) 也选得出同样的票, "
        "这条回归失去意义"
    )


def test_bottom_structure_scan_window_marks_subsequent_valid_bars():
    strategy = _load_builtin_module("bottom_structure").MATRIX_STRATEGY

    series = _random_series(seed=161803, bars=520)
    market = _market([_panel_from_series(series, "000001.SZ")])

    single = strategy.compute_signals(market, {"scan_days": 1}).entry[:, 0].astype(bool)
    window = strategy.compute_signals(market, {"scan_days": 6}).entry[:, 0].astype(bool)

    rows = np.flatnonzero(single)
    assert rows.size >= 1, "构造序列没有信号, 窗口断言无意义"

    expected = np.zeros_like(single)
    for row in rows:
        expected[row : row + 6] = True
    np.testing.assert_array_equal(window, expected)
    assert window.sum() > single.sum()


def test_bottom_structure_scan_days_falls_back_and_clamps():
    strategy = _load_builtin_module("bottom_structure").MATRIX_STRATEGY

    series = _random_series(seed=5, bars=300)
    market = _market([_panel_from_series(series, "000001.SZ")])

    def entry(days: object) -> np.ndarray:
        return strategy.compute_signals(market, {"scan_days": days}).entry

    default = entry(1)
    for invalid in (None, "abc"):
        np.testing.assert_array_equal(entry(invalid), default)
    np.testing.assert_array_equal(entry(0), entry(1))
    np.testing.assert_array_equal(entry(-3), entry(1))
    np.testing.assert_array_equal(entry(999), entry(20))


def test_both_strategies_declare_expected_meta():
    bottom_structure = _load_builtin_module("bottom_structure")
    stale_nine_turn = _load_builtin_module("stale_nine_turn")

    assert bottom_structure.META["id"] == "bottom_structure"
    assert bottom_structure.META["asset_types"] == ["stock"]
    assert bottom_structure.ENTRY_SIGNALS == ["signal_bottom_structure"]
    assert bottom_structure.MATRIX_STRATEGY.required_fields() == frozenset({"close"})
    assert bottom_structure.MATRIX_STRATEGY.required_warmup_bars({}) > 0

    assert stale_nine_turn.META["id"] == "stale_nine_turn"
    assert stale_nine_turn.ENTRY_SIGNALS == ["signal_stale_nine_turn"]
    assert stale_nine_turn.MATRIX_STRATEGY.required_fields() == frozenset({"close"})


def test_nine_turn_down_matches_source_chain():
    shared = _load_shared_module()

    series = _random_series(seed=4242, bars=520)
    market = _market([_panel_from_series(series, "000001.SZ")])
    actual = shared.nine_turn_down(market.close, np.isfinite(market.close))
    np.testing.assert_array_equal(actual[:, 0].astype(bool), _nine_turn_down(series["close"]))
    assert actual[:, 0].sum() >= 1, "构造序列没有出现下跌九转"
