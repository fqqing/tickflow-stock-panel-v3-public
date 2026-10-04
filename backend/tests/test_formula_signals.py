"""解密公式派生指标的单元测试。

对拍对象是「逐行直译源公式」的参考实现 (纯 Python + 显式下标运算), 用于保证
numpy 实现与源语义逐位一致:

- 趋势擒龙 / 资金动能 → ``qushiqinlong/选股_趋势擒龙.py``
- 主图定量结构 / MACD 定量结构 → ``AKL公式解析/`` 的 AKL 反编译源码
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.indicators.formula_signals import (
    barslast,
    barslastcount,
    capital_momentum,
    cross,
    ema,
    every,
    every_at,
    hhv_at,
    llv_at,
    macd_quant_structure,
    quant_structure_main,
    ref,
    ref_at,
    trend_dragon,
)

# ===== 参考实现 (直译源脚本, 故意不复用被测代码) =====


def _ref_barslastcount(cond: list[bool]) -> list[int]:
    run, out = 0, []
    for flag in cond:
        run = run + 1 if flag else 0
        out.append(run)
    return out


def _ref_barslast(cond: list[bool]) -> list[int]:
    last, out = -1, []
    for i, flag in enumerate(cond):
        if flag:
            last = i
        out.append(i - last if last >= 0 else -1)
    return out


def _ref_trend_dragon(
    open_: list[float], high: list[float], close: list[float]
) -> tuple[list[bool], list[int]]:
    n = len(close)
    signal = [False] * n
    a3 = [-1] * n
    if n < 20:
        return signal, a3

    a1 = [False] * n
    for i in range(4, n):
        a1[i] = close[i] > close[i - 4]

    a2 = [count == 9 for count in _ref_barslastcount(a1)]
    a3 = _ref_barslast(a2)

    ma10 = [float("nan")] * n
    for i in range(9, n):
        window = close[i - 9 : i + 1]
        ma10[i] = sum(window) / 10

    for i in range(n):
        v = a3[i]
        if v < 1:
            continue
        c, h, m10 = close[i], high[i], ma10[i]
        if np.isnan(m10):
            continue

        hit = False
        if 1 <= v <= 3:
            j = i - v
            if j >= 0 and c > m10 and h > high[j] and close[j] < open_[j]:
                hit = True
        if not hit and 2 <= v <= 4:
            j, j2 = i - v, i - (v - 1)
            if j >= 0 and j2 >= 0 and c > m10 and h > high[j] and close[j2] < open_[j2]:
                hit = True
        if not hit and 3 <= v <= 4:
            j, j3 = i - v, i - (v - 2)
            if j >= 0 and j3 >= 0 and c > m10 and h > high[j] and close[j3] < open_[j3]:
                hit = True
        if not hit and v == 5 and i >= 4 and h >= max(high[i - 4 : i + 1]) and c > m10:
            hit = True

        if hit and i > 0 and close[i] >= close[i - 1]:
            signal[i] = True

    return signal, a3


def _random_ohlc(n: int, seed: int):
    rng = np.random.default_rng(seed)
    close = np.cumprod(1 + rng.normal(0.0008, 0.02, n)) * 10.0
    open_ = close * (1 + rng.normal(0, 0.006, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    return open_, high, low, close


# ===== BARSLAST / BARSLASTCOUNT =====


def test_barslast_semantics():
    cond = np.array([False, True, False, False, True, False])
    np.testing.assert_array_equal(barslast(cond), [-1, 0, 1, 2, 0, 1])


def test_barslastcount_semantics():
    cond = np.array([True, True, False, True, True, True])
    np.testing.assert_array_equal(barslastcount(cond), [1, 2, 0, 1, 2, 3])


# ===== 趋势擒龙 =====


def test_trend_dragon_short_series_never_signals():
    n = 12
    signal, a3 = trend_dragon(np.ones(n), np.ones(n), np.ones(n), np.ones(n))
    assert not signal.any()
    assert (a3 == -1).all()


@pytest.mark.parametrize("seed", [1, 7, 20260916, 42, 99])
def test_trend_dragon_matches_reference(seed: int):
    open_, high, low, close = _random_ohlc(400, seed)
    signal, a3 = trend_dragon(open_, high, low, close)
    ref_signal, ref_a3 = _ref_trend_dragon(open_.tolist(), high.tolist(), close.tolist())
    np.testing.assert_array_equal(a3, np.array(ref_a3))
    np.testing.assert_array_equal(signal, np.array(ref_signal))


def test_trend_dragon_actually_produces_hits():
    """随机数据里必须真的出现过信号, 否则上面的一致性测试是空跑。"""
    total = 0
    for seed in range(30):
        open_, high, low, close = _random_ohlc(500, seed)
        signal, _ = trend_dragon(open_, high, low, close)
        total += int(signal.sum())
    assert total > 0


# ===== 资金动能 =====


def _ref_capital_momentum(close: list[float], index_close: list[float]) -> list[float]:
    out = [float("nan")] * len(close)
    rs = [c / ic * 1_000_000.0 for c, ic in zip(close, index_close, strict=True)]
    for i in range(51, len(rs)):
        window = rs[i - 51 : i + 1]
        if any(np.isnan(x) for x in window):
            continue
        mean = sum(window) / 52
        if mean == 0:
            continue
        out[i] = (rs[i] / mean - 1) * 10
    return out


def test_capital_momentum_insufficient_window_is_nan():
    close = np.linspace(10, 11, 51)
    index_close = np.linspace(3000, 3100, 51)
    momentum = capital_momentum(close, index_close)
    assert np.isnan(momentum).all()


def test_capital_momentum_matches_reference():
    _, _, _, close = _random_ohlc(300, 2026)
    rng = np.random.default_rng(7)
    index_close = np.cumprod(1 + rng.normal(0.0005, 0.01, close.shape[0])) * 3000.0
    momentum = capital_momentum(close, index_close)
    ref = np.array(_ref_capital_momentum(close.tolist(), index_close.tolist()))
    np.testing.assert_allclose(momentum, ref, rtol=1e-9, atol=1e-9, equal_nan=True)
    assert np.isfinite(momentum[51:]).all()


def test_capital_momentum_nan_index_window_is_nan():
    """指数缺数据的那一段窗口整体作废, 不得用残缺窗口偷算。"""
    close = np.linspace(10, 12, 120)
    index_close = np.linspace(3000, 3200, 120)
    index_close[70] = np.nan
    momentum = capital_momentum(close, index_close)
    # 早于该缺口收口的窗口仍可计算
    assert np.isfinite(momentum[51:70]).all()
    # 覆盖到 index 70 的窗口 (i>=70 且 i-51<=70) 全部作废
    assert np.all(np.isnan(momentum[70:]))


# ===== 通用逐 bar 算子 =====


def test_ema_seeds_from_first_sample():
    assert ema(np.full(5, 3.0), 12).tolist() == [3.0] * 5

    values = [1.0, 2.0, 3.0]
    alpha = 2.0 / 4.0
    expected = [1.0, alpha * 2 + (1 - alpha) * 1.0]
    expected.append(alpha * 3 + (1 - alpha) * expected[1])
    np.testing.assert_allclose(ema(np.array(values), 3), expected, rtol=1e-12)


def test_ema_gap_keeps_previous_value():
    values = np.array([1.0, np.nan, 1.0, 1.0])
    assert ema(values, 3).tolist() == [1.0, 1.0, 1.0, 1.0]


def test_ema_matches_polars_ewm_mean():
    polars = pytest.importorskip("polars")
    _, _, _, close = _random_ohlc(200, 5)
    frame = polars.DataFrame({"close": close})
    expected = frame.select(
        polars.col("close").ewm_mean(alpha=2.0 / 27.0, adjust=False).alias("v")
    )["v"].to_numpy()
    np.testing.assert_allclose(ema(close, 26), expected, rtol=1e-12)


def test_cross_semantics_and_scalar_broadcast():
    a = np.array([1.0, 2.0, 3.0, 2.0, 5.0])
    b = np.array([2.0, 2.0, 2.0, 2.0, 2.0])
    np.testing.assert_array_equal(cross(a, b), [False, False, True, False, True])
    # 与标量比较: CROSS(DIFF, 0) / CROSS(0, DIFF) 用于顶部结构链
    around_zero = np.array([1.0, -1.0, -2.0, 1.0])
    np.testing.assert_array_equal(cross(around_zero, 0.0), [False, False, False, True])
    np.testing.assert_array_equal(cross(0.0, around_zero), [False, True, False, False])
    np.testing.assert_array_equal(cross(a, 0.0), [False] * 5)


def test_cross_ignores_nan_legs():
    a = np.array([np.nan, 1.0, 3.0])
    b = np.array([np.nan, 2.0, 2.0])
    np.testing.assert_array_equal(cross(a, b), [False, False, True])


def test_ref_and_ref_at_out_of_range_are_nan():
    values = np.array([10.0, 11.0, 12.0])
    np.testing.assert_allclose(ref(values, 0), values)
    assert np.isnan(ref(values, -1)).all(), "负偏移不得算成未来数据"
    assert np.isnan(ref(values, 3)).all()
    np.testing.assert_allclose(ref(values, 1)[1:], [10.0, 11.0])

    offsets = np.array([-1, 0, 1])
    result = ref_at(values, offsets)
    assert np.isnan(result[0])
    np.testing.assert_allclose(result[1:], [11.0, 11.0])


def test_every_requires_full_window():
    cond = np.array([True, True, True, False, True])
    np.testing.assert_array_equal(every(cond, 3), [False, False, True, False, False])
    # 历史不足窗口长度时不成立; 窗口 <= 0 同样不成立 (源公式用 BARSLAST-1 做窗口)
    np.testing.assert_array_equal(every(cond, 9), [False] * 5)
    np.testing.assert_array_equal(every(cond, 0), [False] * 5)
    np.testing.assert_array_equal(
        every_at(cond, np.array([3, 2, 1, 0, -2])), [False, True, True, False, False]
    )


def test_variable_window_extremes():
    values = np.array([5.0, 1.0, 4.0, 2.0])
    np.testing.assert_allclose(llv_at(values, np.array([1, 2, 3, 0])), [5.0, 1.0, 1.0, np.nan])
    # 窗口超过已有历史 → NaN; 等长窗口则取全段极值
    np.testing.assert_allclose(hhv_at(values, np.array([1, 2, 3, 9])), [5.0, 5.0, 5.0, np.nan])
    np.testing.assert_allclose(hhv_at(values, np.array([1, 2, 3, 4])), [5.0, 5.0, 5.0, 5.0])
    # 窗口内全 NaN → NaN, 不得被当成极值; 标量窗口按整段广播
    nan_values = np.array([np.nan, np.nan, 3.0])
    assert np.isnan(llv_at(nan_values, 2)[0])
    np.testing.assert_allclose(llv_at(nan_values, 3), [np.nan, np.nan, 3.0])


# ===== 主图定量结构 =====


def _ref_structure(high: list[float], low: list[float], close: list[float]) -> dict:
    n = len(close)
    dsg, dxg = _ref_ema(high, 25), _ref_ema(low, 25)
    csg, cxg = _ref_ema(high, 89), _ref_ema(low, 89)

    bbb, sss = _ref_cross(close, dsg), _ref_cross(dxg, close)
    ages = _ref_barslast([False, *bbb[:-1]])
    bbb0 = [v + 1 for v in ages]
    sss0 = [v + 1 for v in _ref_barslast([False, *sss[:-1]])]

    icon = [0] * n
    for i in range(n):
        if bbb[i] and bbb0[i] > sss0[i]:
            icon[i] = 4
        elif sss[i] and bbb0[i] < sss0[i]:
            icon[i] = 5

    rising = [close[i] > close[i - 4] if i >= 4 else False for i in range(n)]
    falling = [close[i] < close[i - 4] if i >= 4 else False for i in range(n)]

    def run(driver: list[bool], starter: list[bool]) -> list[int]:
        digits = [0] * n
        seq = [driver[i] and (starter[i - 1] if i >= 1 else False) for i in range(n)]
        for step in range(1, 11):
            if 6 <= step <= 9:
                for i in range(n):
                    if seq[i]:
                        digits[i] = step
            seq = [driver[i] and (seq[i - 1] if i >= 1 else False) for i in range(n)]
        return digits

    return {
        "dsg": dsg,
        "dxg": dxg,
        "csg": csg,
        "cxg": cxg,
        "icon": icon,
        "dn_digit": run(falling, rising),
        "up_digit": run(rising, falling),
    }


def _ref_ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1)
    out = [values[0]] * len(values)
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _ref_cross(a: list[float], b: list[float]) -> list[bool]:
    return [False] + [a[i] > b[i] and a[i - 1] <= b[i - 1] for i in range(1, len(a))]


@pytest.mark.parametrize("seed", [3, 17, 20260916, 202])
def test_quant_structure_matches_reference(seed: int):
    _, high, low, close = _random_ohlc(400, seed)
    actual = quant_structure_main(high, low, close)
    expected = _ref_structure(high.tolist(), low.tolist(), close.tolist())
    for key, want in expected.items():
        np.testing.assert_allclose(actual[key], np.array(want), rtol=1e-9, atol=1e-9)


def test_quant_structure_reference_not_empty():
    """随机数据里必须真的出现过图标与九转标注, 否则对拍是空跑。"""
    icons = digits = 0
    for seed in range(12):
        _, high, low, close = _random_ohlc(500, seed)
        result = quant_structure_main(high, low, close)
        icons += int((result["icon"] > 0).sum())
        digits += int((result["dn_digit"] > 0).sum()) + int((result["up_digit"] > 0).sum())
    assert icons > 0
    assert digits > 0


def test_quant_structure_nine_turn_digits_form_a_chain():
    """数字 9 出现时, 前三根必須依次是 6/7/8 (T6~T9 是同一段连续序列的尾四根)。"""
    _, high, low, close = _random_ohlc(600, 11)
    result = quant_structure_main(high, low, close)
    checked = 0
    for digits in (result["dn_digit"], result["up_digit"]):
        for i in range(3, close.shape[0]):
            if digits[i] == 9:
                assert digits[i - 3 : i + 1].tolist() == [6, 7, 8, 9]
                checked += 1
    assert checked > 0


def test_quant_structure_short_series_is_empty_but_shaped():
    result = quant_structure_main(np.ones(3), np.ones(3), np.ones(3))
    assert result["icon"].tolist() == [0, 0, 0]
    assert result["dn_digit"].tolist() == [0, 0, 0]
    np.testing.assert_allclose(result["dsg"], np.ones(3))


# ===== MACD 定量结构 =====


def _ref_macd_structure(close: list[float]) -> dict:
    n = len(close)
    fast, slow = _ref_ema(close, 12), _ref_ema(close, 26)
    diff = [fast[i] - slow[i] for i in range(n)]
    dea = _ref_ema(diff, 9)
    macd = [2 * (diff[i] - dea[i]) for i in range(n)]

    def lt(a: float, b: float) -> bool:
        return a < b if not (math.isnan(a) or math.isnan(b)) else False

    def gt(a: float, b: float) -> bool:
        return a > b if not (math.isnan(a) or math.isnan(b)) else False

    def ge(a: float, b: float) -> bool:
        return a >= b if not (math.isnan(a) or math.isnan(b)) else False

    def val(seq: list[float], i: int) -> float:
        return seq[i] if i >= 0 else math.nan

    def flag(seq: list[bool], i: int) -> bool:
        return seq[i] if i >= 0 else False

    def refv(seq: list[float], i: int, k: int) -> float:
        return seq[i - k] if 0 <= k <= i else math.nan

    def extreme(seq: list[float], i: int, k: int, want_min: bool) -> float:
        if k <= 0 or k > i + 1:
            return math.nan
        kept = [x for x in seq[i - k + 1 : i + 1] if not math.isnan(x)]
        if not kept:
            return math.nan
        return min(kept) if want_min else max(kept)

    def everyv(cond: list[bool], i: int, k: int) -> bool:
        return all(cond[i - k + 1 : i + 1]) if 0 < k <= i + 1 else False

    dead, golden = _ref_cross(dea, diff), _ref_cross(diff, dea)
    n1, m1 = _ref_barslast(dead), _ref_barslast(golden)

    lows1 = [extreme(close, i, n1[i] + 1, True) for i in range(n)]
    lows2 = [refv(lows1, i, m1[i] + 1) for i in range(n)]
    lows3 = [refv(lows2, i, m1[i] + 1) for i in range(n)]
    dl1 = [extreme(diff, i, n1[i] + 1, True) for i in range(n)]
    dl2 = [refv(dl1, i, m1[i] + 1) for i in range(n)]
    dl3 = [refv(dl2, i, m1[i] + 1) for i in range(n)]
    highs1 = [extreme(close, i, m1[i] + 1, False) for i in range(n)]
    highs2 = [refv(highs1, i, n1[i] + 1) for i in range(n)]
    highs3 = [refv(highs2, i, n1[i] + 1) for i in range(n)]
    dh1 = [extreme(diff, i, m1[i] + 1, False) for i in range(n)]
    dh2 = [refv(dh1, i, n1[i] + 1) for i in range(n)]
    dh3 = [refv(dh2, i, n1[i] + 1) for i in range(n)]

    bottom_direct = [
        lt(lows1[i], lows2[i]) and gt(dl1[i], dl2[i]) and lt(val(macd, i - 1), 0) and lt(dl2[i], 0)
        for i in range(n)
    ]
    bottom_peak = [
        lt(lows1[i], lows3[i])
        and lt(dl1[i], dl2[i])
        and gt(dl1[i], dl3[i])
        and lt(diff[i], dea[i])
        and lt(val(macd, i - 1), 0)
        and lt(dl3[i], 0)
        for i in range(n)
    ]
    b_stale_raw = [
        (bottom_direct[i] or bottom_peak[i]) and lt(val(macd, i - 1), 0) for i in range(n)
    ]
    b_stale = [
        b_stale_raw[i] and not flag(b_stale_raw, i - 1) and lt(diff[i], dea[i]) for i in range(n)
    ]
    b_gone = [
        (flag(bottom_direct, i - 1) and lt(dl1[i], dl2[i]) and lt(diff[i], dea[i]))
        or (flag(bottom_peak, i - 1) and lt(dl1[i], dl3[i]) and lt(diff[i], dea[i]))
        for i in range(n)
    ]
    b_struct = [
        gt(diff[i], val(diff, i - 1)) and flag(b_stale, i - 1) and lt(dl1[i] * 0.9884, diff[i])
        for i in range(n)
    ]
    b_formed = [b_struct[i] and not flag(b_struct, i - 1) for i in range(n)]
    b_again_raw = [
        everyv(b_struct, i, 5) and b_stale_raw[i] and lt(diff[i], val(diff, i - 1) * 1.01)
        for i in range(n)
    ]
    b_again = [b_again_raw[i] and not flag(b_again_raw, i - 1) for i in range(n)]

    top_direct = [
        gt(highs1[i], highs2[i])
        and lt(dh1[i], dh2[i])
        and gt(val(macd, i - 1), 0)
        and gt(dh2[i], 0)
        for i in range(n)
    ]
    top_peak = [
        gt(highs1[i], highs2[i])
        and gt(highs1[i], highs3[i])
        and gt(dh1[i], dh2[i])
        and lt(dh1[i], dh3[i])
        and gt(val(macd, i - 1), 0)
        and gt(dh3[i], 0)
        for i in range(n)
    ]
    t_stale_raw = [(top_direct[i] or top_peak[i]) and gt(val(macd, i - 1), 0) for i in range(n)]
    t_stale = [
        t_stale_raw[i] and not flag(t_stale_raw, i - 1) and gt(diff[i], dea[i]) for i in range(n)
    ]
    t_gone = [
        (flag(top_direct, i - 1) and ge(dh1[i], dh2[i]) and gt(diff[i], dea[i]))
        or (flag(top_peak, i - 1) and ge(dh1[i], dh3[i]) and gt(diff[i], dea[i]))
        for i in range(n)
    ]
    t_struct = [
        flag(t_stale, i - 1) and lt(diff[i], val(diff, i - 1)) and gt(dh1[i] * 0.99, diff[i])
        for i in range(n)
    ]
    t_formed = [t_struct[i] and not flag(t_struct, i - 1) for i in range(n)]
    t_again_raw = [
        everyv(t_struct, i, 5) and t_stale_raw[i] and gt(diff[i] * 0.99, val(diff, i - 1))
        for i in range(n)
    ]
    t_again = [t_again_raw[i] and not flag(t_again_raw, i - 1) for i in range(n)]

    def stamp(formed_marks, stale_marks, gone_marks, scales, tops):
        text, position = [0] * n, [math.nan] * n
        for i in range(n):
            if formed_marks[i]:
                text[i], position[i] = 1, diff[i] * scales[0]
            if stale_marks[i]:
                text[i], position[i] = 2, diff[i] * scales[1]
            if gone_marks[i]:
                text[i], position[i] = 3, diff[i] * scales[2]
        return text, position

    bottom_text, bottom_y = stamp(
        b_formed,
        [b_stale[i] or b_again[i] for i in range(n)],
        [b_gone[i] and not b_stale_raw[i] and not b_again[i] for i in range(n)],
        (1 / 0.88, 1 / 0.8, 1 / 0.75),
        False,
    )
    top_text, top_y = stamp(
        t_formed,
        [t_stale[i] or t_again[i] for i in range(n)],
        [t_gone[i] and not t_stale_raw[i] and not t_again[i] for i in range(n)],
        (1.15, 1.2, 1.25),
        True,
    )
    return {
        "diff": diff,
        "dea": dea,
        "macd": macd,
        "bottom_text": bottom_text,
        "bottom_y": bottom_y,
        "top_text": top_text,
        "top_y": top_y,
    }


@pytest.mark.parametrize("seed", [3, 17, 20260916, 202])
def test_macd_quant_structure_matches_reference(seed: int):
    _, _, _, close = _random_ohlc(400, seed)
    actual = macd_quant_structure(close)
    expected = _ref_macd_structure(close.tolist())
    for key, want in expected.items():
        np.testing.assert_allclose(actual[key], np.array(want), rtol=1e-9, atol=1e-9)


def test_macd_quant_structure_series_identity():
    _, _, _, close = _random_ohlc(300, 8)
    result = macd_quant_structure(close)
    np.testing.assert_allclose(result["diff"], ema(close, 12) - ema(close, 26), rtol=1e-12)
    np.testing.assert_allclose(result["dea"], ema(result["diff"], 9), rtol=1e-12)
    np.testing.assert_allclose(result["macd"], 2 * (result["diff"] - result["dea"]), rtol=1e-12)


def test_macd_quant_structure_marks_are_exclusive_and_positioned():
    """结构形成与钝化互斥 (前者要求上一根已钝化, 后者要求上一根未钝化), 位置按比例缩放。"""
    seen = 0
    for seed in range(10):
        _, _, _, close = _random_ohlc(500, seed)
        result = macd_quant_structure(close)
        diff = result["diff"]
        for text, position, scales in (
            (result["bottom_text"], result["bottom_y"], (1 / 0.88, 1 / 0.8, 1 / 0.75)),
            (result["top_text"], result["top_y"], (1.15, 1.2, 1.25)),
        ):
            assert set(np.unique(text)).issubset({0, 1, 2, 3})
            for index in np.where(text == 0)[0]:
                assert math.isnan(position[index])
            for label, scale in zip((1, 2, 3), scales, strict=True):
                for index in np.where(text == label)[0]:
                    assert position[index] == pytest.approx(diff[index] * scale, rel=1e-12)
                    seen += 1
    assert seen > 0, "随机数据里必须真的出现过结构标注"


def test_macd_quant_structure_short_series_is_flat():
    result = macd_quant_structure(np.array([10.0]))
    assert result["bottom_text"].tolist() == [0]
    assert result["top_text"].tolist() == [0]
    assert math.isnan(result["bottom_y"][0])
