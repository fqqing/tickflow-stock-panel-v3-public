"""解密公式派生指标 (逐 bar 精确复刻, 纯函数)。

这些指标不随 enriched parquet 落盘, 而是由 ``/api/kline/daily`` 的
``indicators`` 参数按需计算 (个股弹窗按需点亮), 避免给全市场重建增加负担。

实现口径与源脚本保持一致:
- 趋势擒龙 / 蛟龙出海: ``qushiqinlong/选股_趋势擒龙.py::compute_trend_dragon``
- 资金动能: ``qushiqinlong/选股_趋势擒龙.py::compute_capital_momentum``
  (等价于 ``yanwen/.../tdx_zb_qsql.py`` 中 RS / RS_MA52 / 资金动能 三行)

约定:
- 输入为一维 numpy 数组, 按时间升序, 与 K 线一一对应。
- 输出与输入等长; 语义缺口用 ``np.nan`` / ``-1`` 表示, 由调用方决定如何展示。
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "barslast",
    "barslastcount",
    "capital_momentum",
    "cross",
    "ema",
    "every",
    "every_at",
    "hhv_at",
    "llv_at",
    "macd_quant_structure",
    "quant_structure_main",
    "ref",
    "ref_at",
    "trend_dragon",
]

TREND_CONTINUOUS_BARS = 9
CAPITAL_MOMENTUM_WINDOW = 52
CAPITAL_MOMENTUM_SCALE = 10.0
RS_SCALE = 1_000_000.0

# ===== 主图定量结构 (通达信指标 D'D) =====
STRUCTURE_SHORT_SPAN = 25
STRUCTURE_LONG_SPAN = 89
STRUCTURE_ICON_BREAKOUT = 4
STRUCTURE_ICON_BREAKDOWN = 5
STRUCTURE_NINE_TURN_LOOKBACK = 4
STRUCTURE_NINE_TURN_LABELS = (6, 7, 8, 9)
STRUCTURE_DIGIT_NONE = 0

# ===== MACD 定量结构 (通达信指标 DDD) =====
MACD_FAST_SPAN = 12
MACD_SLOW_SPAN = 26
MACD_SIGNAL_SPAN = 9
MACD_BOTTOM_RATIO = 0.9884
MACD_TOP_RATIO = 0.99
MACD_STOP_DRIFT = 1.01
STRUCTURE_REPEAT_WINDOW = 5
STRUCTURE_TEXT_NONE = 0
STRUCTURE_TEXT_FORMED = 1
STRUCTURE_TEXT_STALE = 2
STRUCTURE_TEXT_GONE = 3
_BOTTOM_TEXT_SCALE = (1.0 / 0.88, 1.0 / 0.8, 1.0 / 0.75)
_TOP_TEXT_SCALE = (1.15, 1.2, 1.25)


def barslastcount(condition: np.ndarray) -> np.ndarray:
    """BARSLASTCOUNT: 条件连续成立的有效 bar 数 (不成立则归零)。

    与通达信一致: 当前成立计入计数, 因此首次成立为 1。
    """
    cond = np.asarray(condition, dtype=bool)
    out = np.zeros(cond.shape, dtype=np.int64)
    run = 0
    for i in range(cond.shape[0]):
        run = run + 1 if cond[i] else 0
        out[i] = run
    return out


def barslast(condition: np.ndarray) -> np.ndarray:
    """BARSLAST: 距上一次条件成立的 bar 数; 当前成立记 0, 从未成立记 -1。"""
    cond = np.asarray(condition, dtype=bool)
    out = np.full(cond.shape, -1, dtype=np.int64)
    last_true = -1
    for i in range(cond.shape[0]):
        if cond[i]:
            last_true = i
        if last_true >= 0:
            out[i] = i - last_true
    return out


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """滚动均值; 不足 window 根输出 NaN (对应 pandas ``min_periods=window``)。"""
    arr = np.asarray(values, dtype=np.float64)
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    if arr.shape[0] < window:
        return out
    cumsum = np.concatenate(([0.0], np.nancumsum(arr)))
    sums = cumsum[window:] - cumsum[:-window]
    out[window - 1 :] = sums / window
    # 窗口内含 NaN 时整体作废, 避免把停牌缺口当成真实价格
    for i in range(window - 1, arr.shape[0]):
        if np.isnan(arr[i - window + 1 : i + 1]).any():
            out[i] = np.nan
    return out


def trend_dragon(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """趋势擒龙 (主图「蛟龙出海」信号)。

    公式:
        A1 = C > REF(C, 4)
        A2 = BARSLASTCOUNT(A1) == 9          # 连续 9 根满足 A1
        A3 = BARSLAST(A2)                    # 上次 A2 距今的 bar 数
        XGY = 四组回调突破条件取 OR (依赖 A3 与 MA10)
        信号 = XGY AND C >= REF(C, 1)

    返回 ``(signal, a3)``: signal 为 bool 数组; a3 为 A3 原始值 (-1 表示从未成立)。
    ``low`` 参数暂不参与判断, 保留以对齐 K 线入参顺序。
    """
    close = np.asarray(close, dtype=np.float64)
    open_ = np.asarray(open_, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    n = close.shape[0]
    signal = np.zeros(n, dtype=bool)
    a3 = np.full(n, -1, dtype=np.int64)
    if n < 20:
        return signal, a3

    # A1: C > REF(C, 4)
    a1 = np.zeros(n, dtype=bool)
    a1[4:] = close[4:] > close[:-4]

    # A2: 连续 9 根 A1
    a2 = barslastcount(a1) == TREND_CONTINUOUS_BARS

    # A3: 上次 A2 距今 bar 数
    a3 = barslast(a2)

    ma10 = _rolling_mean(close, 10)

    for i in range(n):
        v = int(a3[i])
        if v < 1:
            continue
        c = close[i]
        h = high[i]
        m10 = ma10[i]
        if np.isnan(m10):
            continue

        hit = False

        # 条件1: A3∈[1,3] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3)<REF(O,A3)
        if 1 <= v <= 3:
            j = i - v
            if j >= 0 and c > m10 and h > high[j] and close[j] < open_[j]:
                hit = True

        # 条件2: A3∈[2,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-1)<REF(O,A3-1)
        if not hit and 2 <= v <= 4:
            j = i - v
            j2 = i - (v - 1)
            if j >= 0 and j2 >= 0 and c > m10 and h > high[j] and close[j2] < open_[j2]:
                hit = True

        # 条件3: A3∈[3,4] AND C>MA10 AND H>REF(H,A3) AND REF(C,A3-2)<REF(O,A3-2)
        if not hit and 3 <= v <= 4:
            j = i - v
            j3 = i - (v - 2)
            if j >= 0 and j3 >= 0 and c > m10 and h > high[j] and close[j3] < open_[j3]:
                hit = True

        # 条件4: A3=5 AND H>=HHV(H,5) AND C>MA10
        if not hit and v == 5 and i >= 4 and h >= high[i - 4 : i + 1].max() and c > m10:
            hit = True

        if hit and i > 0 and close[i] >= close[i - 1]:
            signal[i] = True

    return signal, a3


def capital_momentum(close: np.ndarray, index_close: np.ndarray) -> np.ndarray:
    """资金动能 = (RS / RS_MA52 - 1) * 10, 其中 RS = C / INDEXC * 1e6。

    ``close`` 与 ``index_close`` 需已按同一交易日对齐 (缺失日传 NaN)。
    不足 52 根或指数缺失的 bar 输出 NaN。
    """
    close = np.asarray(close, dtype=np.float64)
    index_close = np.asarray(index_close, dtype=np.float64)
    if close.shape != index_close.shape:
        raise ValueError("close 与 index_close 长度不一致")

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = close / index_close * RS_SCALE
    rs_ma = _rolling_mean(rs, CAPITAL_MOMENTUM_WINDOW)
    with np.errstate(divide="ignore", invalid="ignore"):
        momentum = (rs / rs_ma - 1.0) * CAPITAL_MOMENTUM_SCALE
    momentum[~np.isfinite(momentum)] = np.nan
    return momentum


# ===== 通用逐 bar 算子 (口径对齐通达信, 供定量结构系列公式复用) =====


def _broadcast_pair(left, right) -> tuple[np.ndarray, np.ndarray]:
    """把标量/数组混用的两个入参统一成同长度 float64 数组。"""
    left_arr = np.asarray(left, dtype=np.float64)
    right_arr = np.asarray(right, dtype=np.float64)
    if left_arr.ndim == 0 and right_arr.ndim != 0:
        left_arr = np.full(right_arr.shape, float(left_arr), dtype=np.float64)
    if right_arr.ndim == 0 and left_arr.ndim != 0:
        right_arr = np.full(left_arr.shape, float(right_arr), dtype=np.float64)
    return left_arr, right_arr


def _shift_bool(condition: np.ndarray, n: int = 1) -> np.ndarray:
    """``REF(condition, n)`` 的布尔版本; 越界补 False (不引入未来数据)。"""
    cond = np.asarray(condition, dtype=bool)
    out = np.zeros(cond.shape, dtype=bool)
    if n <= 0 or n >= cond.shape[0]:
        return out
    out[n:] = cond[:-n]
    return out


def ema(values: np.ndarray, span: int) -> np.ndarray:
    """``EMA(X, N)``: alpha = 2/(N+1), 以首根样本播种。

    等价于 polars ``ewm_mean(alpha=2/(N+1), adjust=False)``, 与 enriched
    落盘的 macd_*/ema* 列同一口径。序列中的 NaN 视为停牌缺口, 保持前值。
    """
    arr = np.asarray(values, dtype=np.float64)
    out = np.empty(arr.shape, dtype=np.float64)
    if arr.size == 0:
        return out
    alpha = 2.0 / (span + 1.0)
    prev = arr[0]
    out[0] = prev
    for i in range(1, arr.size):
        value = arr[i]
        if not np.isnan(value):
            prev = alpha * value + (1.0 - alpha) * prev
        out[i] = prev
    return out


def cross(a, b) -> np.ndarray:
    """``CROSS(A, B)``: A 当日上穿 B (昨日 A <= 昨日 B 且今日 A > 今日 B)。

    任一操作数为 NaN 时不计交叉, 避免停牌缺口被误判成信号。
    """
    a_arr, b_arr = _broadcast_pair(a, b)
    out = np.zeros(a_arr.shape, dtype=bool)
    if a_arr.shape[0] < 2:
        return out
    out[1:] = (a_arr[1:] > b_arr[1:]) & (a_arr[:-1] <= b_arr[:-1])
    return out


def ref(values: np.ndarray, n: int) -> np.ndarray:
    """``REF(X, N)``: 取 N 根之前的值; N < 0 或越界补 NaN (不引入未来数据)。"""
    arr = np.asarray(values, dtype=np.float64)
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    if n < 0 or n >= arr.shape[0]:
        return out
    if n == 0:
        return arr.copy()
    out[n:] = arr[:-n]
    return out


def _align_int_series(shape: tuple, values) -> np.ndarray:
    """把标量/数组统一成 int64 序列, 供逐 bar 变长窗口复用。"""
    arr = np.asarray(values, dtype=np.int64)
    if arr.ndim == 0:
        return np.full(shape, int(arr), dtype=np.int64)
    return arr


def ref_at(values: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """``REF(X, N)`` 的变长版本 (N 为逐 bar 数组), 供 BARSLAST 链式取数使用。"""
    arr = np.asarray(values, dtype=np.float64)
    offs = _align_int_series(arr.shape, offsets)
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    for i in range(arr.shape[0]):
        step = int(offs[i])
        if 0 <= step <= i:
            out[i] = arr[i - step]
    return out


def every(condition: np.ndarray, window: int) -> np.ndarray:
    """``EVERY(X, N)``: 最近 N 根 (含当根) 全部成立; 历史不足 N 根时不成立。"""
    cond = np.asarray(condition, dtype=bool)
    out = np.zeros(cond.shape, dtype=bool)
    width = int(window)
    if width <= 0:
        return out
    for i in range(cond.shape[0]):
        if width > i + 1:
            continue
        out[i] = bool(cond[i - width + 1 : i + 1].all())
    return out


def every_at(condition: np.ndarray, windows: np.ndarray) -> np.ndarray:
    """``EVERY(X, N)`` 的变长版本; N <= 0 或历史不足时视为不成立。"""
    cond = np.asarray(condition, dtype=bool)
    windows_arr = _align_int_series(cond.shape, windows)
    out = np.zeros(cond.shape, dtype=bool)
    for i in range(cond.shape[0]):
        width = int(windows_arr[i])
        if width <= 0 or width > i + 1:
            continue
        out[i] = bool(cond[i - width + 1 : i + 1].all())
    return out


def _rolling_extreme(values: np.ndarray, windows: np.ndarray, *, use_min: bool) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    windows_arr = _align_int_series(arr.shape, windows)
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    for i in range(arr.shape[0]):
        width = int(windows_arr[i])
        if width <= 0 or width > i + 1:
            continue
        segment = arr[i - width + 1 : i + 1]
        if np.isnan(segment).all():
            continue
        out[i] = float(np.nanmin(segment) if use_min else np.nanmax(segment))
    return out


def llv_at(values: np.ndarray, windows: np.ndarray) -> np.ndarray:
    """变长窗口的 ``LLV(X, N)``; 窗口非法或全为 NaN 时输出 NaN。"""
    return _rolling_extreme(values, windows, use_min=True)


def hhv_at(values: np.ndarray, windows: np.ndarray) -> np.ndarray:
    """变长窗口的 ``HHV(X, N)``; 窗口非法或全为 NaN 时输出 NaN。"""
    return _rolling_extreme(values, windows, use_min=False)


def quant_structure_main(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> dict[str, np.ndarray]:
    """主图定量结构 (通达信指标 D'D)。

    公式逐行直译 (来源: ``AKL公式解析/主图定量结构_源码.txt``):

        DSG = EMA(HIGH, 25); DXG = EMA(LOW, 25)      # 短轨道
        CSG = EMA(HIGH, 89); CXG = EMA(LOW, 89)      # 长轨道
        BBB = CROSS(CLOSE, DSG); SSS = CROSS(DXG, CLOSE)
        BBB_0 = BARSLAST(REF(BBB, 1)) + 1
        SSS_0 = BARSLAST(REF(SSS, 1)) + 1
        DRAWICON(BBB AND BBB_0 > SSS_0, LOW, 4)
        DRAWICON(SSS AND BBB_0 < SSS_0, HIGH, 5)
        A1 = C > REF(C, 4); A2 = C < REF(C, 4)
        T1 = A2 AND REF(A1, 1); ... T10 = A2 AND REF(T9, 1)   # 下跌九转
        B1 = C < REF(C, 4); B2 = C > REF(C, 4)
        D1 = B2 AND REF(B1, 1); ... D10 = B2 AND REF(D9, 1)   # 上涨九转
        DRAWTEXT(T6..T9, LOW, 6..9); DRAWTEXT(D6..D9, HIGH * 1.001, 6..9)

    返回值 (全部与输入等长):

    - ``dsg``/``dxg``/``csg``/``cxg``: 四条轨道线
    - ``icon``: 0 无 / 4 收盘上穿短上轨 / 5 收盘跌破短下轨
    - ``dn_digit``: 下跌九转标注数字 (0 表示无标注, 否则 6~9)
    - ``up_digit``: 上涨九转标注数字 (0 表示无标注, 否则 6~9)

    源公式里的 ``AAE~EEE`` 与 ``AE1`` 未被任何输出引用 (8.2 定制版周期函数),
    属死代码, 此处不复刻。
    """
    close = np.asarray(close, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    n = close.shape[0]

    result = {
        "dsg": np.full(n, np.nan, dtype=np.float64),
        "dxg": np.full(n, np.nan, dtype=np.float64),
        "csg": np.full(n, np.nan, dtype=np.float64),
        "cxg": np.full(n, np.nan, dtype=np.float64),
        "icon": np.zeros(n, dtype=np.int64),
        "dn_digit": np.zeros(n, dtype=np.int64),
        "up_digit": np.zeros(n, dtype=np.int64),
    }
    if n == 0:
        return result

    dsg = ema(high, STRUCTURE_SHORT_SPAN)
    dxg = ema(low, STRUCTURE_SHORT_SPAN)
    csg = ema(high, STRUCTURE_LONG_SPAN)
    cxg = ema(low, STRUCTURE_LONG_SPAN)
    result["dsg"], result["dxg"] = dsg, dxg
    result["csg"], result["cxg"] = csg, cxg

    with np.errstate(invalid="ignore", divide="ignore"):
        breakout = cross(close, dsg)
        breakdown = cross(dxg, close)
        breakout_age = barslast(_shift_bool(breakout, 1)) + 1
        breakdown_age = barslast(_shift_bool(breakdown, 1)) + 1

        icon = result["icon"]
        icon[breakout & (breakout_age > breakdown_age)] = STRUCTURE_ICON_BREAKOUT
        icon[breakdown & (breakout_age < breakdown_age)] = STRUCTURE_ICON_BREAKDOWN

        lookback = STRUCTURE_NINE_TURN_LOOKBACK
        rising = np.zeros(n, dtype=bool)
        falling = np.zeros(n, dtype=bool)
        if n > lookback:
            rising[lookback:] = close[lookback:] > close[:-lookback]
            falling[lookback:] = close[lookback:] < close[:-lookback]

        dn_digit = result["dn_digit"]
        step_down = falling & _shift_bool(rising, 1)
        for step in range(1, 11):
            if step in STRUCTURE_NINE_TURN_LABELS:
                dn_digit[step_down] = step
            step_down = falling & _shift_bool(step_down, 1)

        up_digit = result["up_digit"]
        step_up = rising & _shift_bool(falling, 1)
        for step in range(1, 11):
            if step in STRUCTURE_NINE_TURN_LABELS:
                up_digit[step_up] = step
            step_up = rising & _shift_bool(step_up, 1)

    return result


def _macd_bottom_texts(
    close: np.ndarray,
    diff: np.ndarray,
    dea: np.ndarray,
    diff_prev: np.ndarray,
    macd_prev: np.ndarray,
    n1: np.ndarray,
    m1: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """底部结构链: 直接/隔峰底钝化 -> 底部结构 -> 再次钝化 -> 结构消失。"""
    cl1 = llv_at(close, n1 + 1)
    cl2 = ref_at(cl1, m1 + 1)
    cl3 = ref_at(cl2, m1 + 1)
    difl1 = llv_at(diff, n1 + 1)
    difl2 = ref_at(difl1, m1 + 1)
    difl3 = ref_at(difl2, m1 + 1)

    direct = (cl1 < cl2) & (difl1 > difl2) & (macd_prev < 0) & (difl2 < 0)
    peak = (
        (cl1 < cl3)
        & (difl1 < difl2)
        & (difl1 > difl3)
        & (diff < dea)
        & (macd_prev < 0)
        & (difl3 < 0)
    )
    stale_raw = (direct | peak) & (macd_prev < 0)
    stale = stale_raw & ~_shift_bool(stale_raw, 1) & (diff < dea)
    stale_gone = (_shift_bool(direct, 1) & (difl1 < difl2) & (diff < dea)) | (
        _shift_bool(peak, 1) & (difl1 < difl3) & (diff < dea)
    )

    structure = (diff > diff_prev) & _shift_bool(stale, 1) & (difl1 * MACD_BOTTOM_RATIO < diff)
    formed = structure & ~_shift_bool(structure, 1)
    again_raw = (
        every(structure, STRUCTURE_REPEAT_WINDOW) & stale_raw & (diff < diff_prev * MACD_STOP_DRIFT)
    )
    again = again_raw & ~_shift_bool(again_raw, 1)

    n = close.shape[0]
    text = np.zeros(n, dtype=np.int64)
    position = np.full(n, np.nan, dtype=np.float64)
    # 与通达信 DRAWTEXT 顺序一致: 先"结构形成"再"钝化"再"消失", 后写的覆盖先写的
    stamp = (
        (formed, STRUCTURE_TEXT_FORMED, _BOTTOM_TEXT_SCALE[0]),
        (stale | again, STRUCTURE_TEXT_STALE, _BOTTOM_TEXT_SCALE[1]),
        (stale_gone & ~stale_raw & ~again, STRUCTURE_TEXT_GONE, _BOTTOM_TEXT_SCALE[2]),
    )
    for marks, label, scale in stamp:
        text[marks] = label
        position[marks] = diff[marks] * scale
    return text, position


def _macd_top_texts(
    close: np.ndarray,
    diff: np.ndarray,
    dea: np.ndarray,
    diff_prev: np.ndarray,
    macd_prev: np.ndarray,
    n1: np.ndarray,
    m1: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """顶部结构链: 直接/隔峰顶钝化 -> 顶部结构 -> 再次钝化 -> 结构消失。"""
    ch1 = hhv_at(close, m1 + 1)
    ch2 = ref_at(ch1, n1 + 1)
    ch3 = ref_at(ch2, n1 + 1)
    difh1 = hhv_at(diff, m1 + 1)
    difh2 = ref_at(difh1, n1 + 1)
    difh3 = ref_at(difh2, n1 + 1)

    direct = (ch1 > ch2) & (difh1 < difh2) & (macd_prev > 0) & (difh2 > 0)
    peak = (
        (ch1 > ch2)
        & (ch1 > ch3)
        & (difh1 > difh2)
        & (difh1 < difh3)
        & (macd_prev > 0)
        & (difh3 > 0)
    )
    stale_raw = (direct | peak) & (macd_prev > 0)
    stale = stale_raw & ~_shift_bool(stale_raw, 1) & (diff > dea)
    stale_gone = (_shift_bool(direct, 1) & (difh1 >= difh2) & (diff > dea)) | (
        _shift_bool(peak, 1) & (difh1 >= difh3) & (diff > dea)
    )

    structure = _shift_bool(stale, 1) & (diff < diff_prev) & (difh1 * MACD_TOP_RATIO > diff)
    formed = structure & ~_shift_bool(structure, 1)
    again_raw = (
        every(structure, STRUCTURE_REPEAT_WINDOW) & stale_raw & (diff * MACD_TOP_RATIO > diff_prev)
    )
    again = again_raw & ~_shift_bool(again_raw, 1)

    n = close.shape[0]
    text = np.zeros(n, dtype=np.int64)
    position = np.full(n, np.nan, dtype=np.float64)
    stamp = (
        (formed, STRUCTURE_TEXT_FORMED, _TOP_TEXT_SCALE[0]),
        (stale | again, STRUCTURE_TEXT_STALE, _TOP_TEXT_SCALE[1]),
        (stale_gone & ~stale_raw & ~again, STRUCTURE_TEXT_GONE, _TOP_TEXT_SCALE[2]),
    )
    for marks, label, scale in stamp:
        text[marks] = label
        position[marks] = diff[marks] * scale
    return text, position


def macd_quant_structure(close: np.ndarray) -> dict[str, np.ndarray]:
    """MACD 定量结构 (通达信指标 DDD): 徐小明定量结构的底部/顶部结构判定。

    公式逐行直译 (来源: ``AKL公式解析/MACD定量结构_源码.txt``):

        DIFF = EMA(C, 12) - EMA(C, 26); DEA = EMA(DIFF, 9); MACD = (DIFF-DEA)*2
        N1 = BARSLAST(CROSS(DEA, DIFF)); M1 = BARSLAST(CROSS(DIFF, DEA))
        CL1 = LLV(C, N1+1);   CL2 = REF(CL1, M1+1);   CL3 = REF(CL2, M1+1)
        DIFL1..3 同理取 DIF 的区间低点
        直接底钝化 = CL1 < CL2 AND DIFL1 > DIFL2 AND REF(MACD,1) < 0 AND DIFL2 < 0
        隔峰底钝化 = CL1 < CL3 AND DIFL1 < DIFL2 AND DIFL1 > DIFL3
                     AND DIFF < DEA AND REF(MACD,1) < 0 AND DIFL3 < 0
        底钝化     = (直接 OR 隔峰) AND REF(MACD,1) < 0, 且为首次成立
        底钝化消失 = REF(直接/隔峰,1) 且 DIFF 再创新低
        底部结构   = DIFF > REF(DIFF,1) AND REF(底钝化,1) AND DIFL1*0.9884 < DIFF
        底再次钝化 = EVERY(底部结构,5) AND 底部钝化 AND DIFF < REF(DIFF,1)*1.01 (首次)
        顶部四条链同理 (系数 0.99, 无 DIFF/DEA 关系项)

        标注位置: 底 结构形成/钝化/消失 画在 DIFF/0.88, /0.8, /0.75;
                  顶 结构形成/钝化/消失 画在 DIFF*1.15, *1.2, *1.25

    返回值 (全部与输入等长):

    - ``diff``/``dea``/``macd``: 三条 MACD 序列
    - ``bottom_text``/``top_text``: 0 无 / 1 结构形成 / 2 钝化 / 3 消失
    - ``bottom_y``/``top_y``: 对应标注的纵坐标 (无标注时为 NaN)

    源公式里的死代码一律不复刻: ``N2``/``M2`` 定义后未被引用;
    ``底结构消失``/``顶结构消失`` 虽由 M6/N6 + EVERY 链算出, 但没有任何
    ``DRAWTEXT`` 引用 (图上画的"消失"是 ``底钝化消失``/``顶钝化消失``)。
    负偏移的 ``REF`` (例如 ``REF(CL1, M3)`` 在结构形成当日 M3 = -1) 一律按无效
    处理, 因此当天不可能同时出现"结构形成"与"钝化消失"。
    """
    close = np.asarray(close, dtype=np.float64)
    n = close.shape[0]
    diff = ema(close, MACD_FAST_SPAN) - ema(close, MACD_SLOW_SPAN)
    dea = ema(diff, MACD_SIGNAL_SPAN)
    macd = (diff - dea) * 2.0

    result = {
        "diff": diff,
        "dea": dea,
        "macd": macd,
        "bottom_text": np.zeros(n, dtype=np.int64),
        "bottom_y": np.full(n, np.nan, dtype=np.float64),
        "top_text": np.zeros(n, dtype=np.int64),
        "top_y": np.full(n, np.nan, dtype=np.float64),
    }
    if n < 2:
        return result

    with np.errstate(invalid="ignore", divide="ignore"):
        diff_prev = ref(diff, 1)
        macd_prev = ref(macd, 1)
        n1 = barslast(cross(dea, diff))
        m1 = barslast(cross(diff, dea))
        bottom_text, bottom_y = _macd_bottom_texts(close, diff, dea, diff_prev, macd_prev, n1, m1)
        top_text, top_y = _macd_top_texts(close, diff, dea, diff_prev, macd_prev, n1, m1)

    result["bottom_text"], result["bottom_y"] = bottom_text, bottom_y
    result["top_text"], result["top_y"] = top_text, top_y
    return result
