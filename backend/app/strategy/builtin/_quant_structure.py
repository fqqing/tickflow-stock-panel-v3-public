"""定量结构选股公式 (底部结构 / 钝化加低九) 的矩阵原生直译。

这是一个**共享依赖模块** —— 文件名以 ``_`` 开头, 引擎扫描内置策略时会跳过它,
只在加载同目录策略时把它作为依赖一起做 AST 安全校验 (见
``StrategyEngine._load_file`` 的 ``_*.py`` 约定)。

源公式 (``AKL公式解析/底部结构选股_源码.txt``, 缩写 DBJGXG):

    DIFF = EMA(C, 12) - EMA(C, 26); DEA = EMA(DIFF, 9); MACD = (DIFF-DEA)*2
    N1 = BARSLAST(CROSS(DEA, DIFF)); M1 = BARSLAST(CROSS(DIFF, DEA))
    CL1 = LLV(C, N1+1);      CL2 = REF(CL1, M1+1);      CL3 = REF(CL2, M1+1)
    DIFL1 = LLV(DIFF, N1+1); DIFL2 = REF(DIFL1, M1+1);  DIFL3 = REF(DIFL2, M1+1)
    直接底钝化 = CL1 < CL2 AND DIFL1 > DIFL2 AND REF(MACD,1) < 0 AND DIFL2 < 0
    隔峰底钝化 = CL1 < CL3 AND DIFL1 < DIFL2 AND DIFL1 > DIFL3
                 AND DIFF < DEA AND REF(MACD,1) < 0 AND DIFL3 < 0
    底部钝化   = (直接底钝化 OR 隔峰底钝化) AND REF(MACD,1) < 0
    底钝化     = 底部钝化 AND REF(底部钝化,1) = 0 AND DIFF < DEA
    底部结构   = DIFF > REF(DIFF,1) AND REF(底部钝化,1) AND DIFL1*0.9884 < DIFF
    底结构形成 = 底部结构 AND REF(底部结构,1) = 0
    输出       = 底结构形成

⚠️ ``底部结构`` 引用的确实是 **``底部钝化``** 而不是 ``底钝化`` (源码原文:
``底部结构:=DIFF>REF(DIFF,1) AND (REF(底部钝化,1) AND DIFL1*0.9884<DIFF);``)。
``底钝化`` 只被 ``M4:=BARSLAST(底钝化 OR 底再次钝化)`` 引用, 进而只服务于
``底结构消失`` 那段指标图文字标注, **不在选股链上**。两者差别很大: ``底钝化``
比 ``底部钝化`` 多了"上一根尚未钝化"与 ``DIFF < DEA`` 两道约束, 会让信号点整体
后移。2026-09-16 用 2026-09-15 的真实数据实测: 误用 ``REF(底钝化,1)`` 时全市场只
选中 9 只, 用 ``REF(底部钝化,1)`` 选中 48 只 (用户工具当日 43 只)。

语义要点 (N1 / M1 的角色): 死叉之后的 ``N1+1`` 根就是"本轮下跌", ``LLV(C, N1+1)``
取本轮下跌的收盘最低; 再往前的上一轮 ``M1+1`` 根用 ``REF`` 取到上一轮的同类低点。
两轮低点比较就得到"底钝化" —— 价格创新低而 DIF 没有创新低, 即价格与动能背离。

「钝化加低九选股」共用同一条链, 只有两处差异 (原公式如此):

- 隔峰底钝化的 ``REF(MACD, 1)`` 改用 ``REF(MACD, 2)`` -> ``macd_prev_bars=2``
- 输出换成 ``底钝化 AND T9`` (底钝化首次信号与下跌九转第 9 天共振)

口径说明:

- MACD 三线在本模块用 :func:`~app.backtest.matrix.valid_ewm_adjust_false` 自算,
  而不是取面板落盘的 ``macd_*`` 列 —— 这样停牌行的处理与其它算子一致: 只在
  有效 bar 上推进 EMA, 与 :func:`~app.app.indicators.formula_signals.ema` 同口径。
- 所有窗口 / 取数算子都走 ``valid_*`` 族, 计数单位是**有效 bar** (自动跳停牌)。
- 源公式里的 ``N2`` / ``M2`` 定义后未被引用; ``底钝化消失`` / ``底再次钝化`` /
  ``底结构消失`` 只服务于通达信指标图的文字标注, 与选股输出无关 —— 均不复刻。
"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    valid_barslast,
    valid_ewm_adjust_false,
    valid_rolling_min_at,
    valid_shift,
    valid_shift_at,
)

MACD_FAST_SPAN = 12
MACD_SLOW_SPAN = 26
MACD_SIGNAL_SPAN = 9
MACD_SCALE = 2.0
BOTTOM_STRUCTURE_RATIO = 0.9884
NINE_TURN_LOOKBACK = 4
NINE_TURN_LEVEL = 9

_ONE = np.float32(1.0)
_ZERO = np.float32(0.0)
_HALF = np.float32(0.5)
_BOTTOM_RATIO = np.float32(BOTTOM_STRUCTURE_RATIO)


def macd_triplet(market: MarketDataMatrix, valid: np.ndarray) -> tuple[np.ndarray, ...]:
    """MACD 三线 ``(DIFF, DEA, MACD)``, EMA 只沿有效 bar 推进。"""
    close = market.close
    fast = valid_ewm_adjust_false(close, valid, span=MACD_FAST_SPAN)
    slow = valid_ewm_adjust_false(close, valid, span=MACD_SLOW_SPAN)
    diff = fast - slow
    dea = valid_ewm_adjust_false(diff, valid, span=MACD_SIGNAL_SPAN)
    return diff, dea, (diff - dea) * np.float32(MACD_SCALE)


def golden_cross(diff: np.ndarray, dea: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """``CROSS(DIFF, DEA)``: 今日 DIFF > DEA 且上一有效 bar DIFF <= DEA。"""
    return (diff > dea) & (valid_shift(diff, 1, valid) <= valid_shift(dea, 1, valid))


def dead_cross(diff: np.ndarray, dea: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """``CROSS(DEA, DIFF)``: 今日 DEA > DIFF 且上一有效 bar DEA <= DIFF。"""
    return (dea > diff) & (valid_shift(dea, 1, valid) <= valid_shift(diff, 1, valid))


def previous_true(condition: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """``REF(条件, 1)``: 上一有效 bar 是否成立 (无历史时视为不成立)。"""
    previous = valid_shift(condition.astype(np.float32), 1, valid)
    return np.isfinite(previous) & (previous > _HALF)


def previous_false(condition: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """``REF(条件, 1) = 0``: 上一有效 bar 是否不成立 (无历史时视为不成立)。"""
    previous = valid_shift(condition.astype(np.float32), 1, valid)
    return np.isfinite(previous) & (previous < _HALF)


def nine_turn_down(close: np.ndarray, valid: np.ndarray, *, level: int = NINE_TURN_LEVEL):
    """下跌九转第 ``level`` 天 (T1..T9 链, ``level=9`` 即公式里的 T9)。

        A1 = C > REF(C, 4);  A2 = C < REF(C, 4)
        T1 = A2 AND REF(A1, 1);  T2 = A2 AND REF(T1, 1);  ...  T9 = A2 AND REF(T8, 1)

    即"连续 ``level`` 根收在 4 日前收盘之下, 且首次转入时前一根还在上方"。
    """
    steps = int(level)
    if steps < 1:
        raise ValueError("nine turn level must be positive")
    previous_close = valid_shift(close, NINE_TURN_LOOKBACK, valid)
    comparable = np.isfinite(previous_close)
    rising = comparable & (close > previous_close)
    falling = comparable & (close < previous_close)
    step = falling & previous_true(rising, valid)
    for _ in range(2, steps + 1):
        step = falling & previous_true(step, valid)
    return step & valid


def _observed_only(ages: np.ndarray, condition: np.ndarray) -> np.ndarray:
    """把 ``BARSLAST`` 里"从未成立"的那一段置为无效。

    ``valid_barslast`` 的 ``0`` 有两义 —— 当天成立, 或者从未成立过。公式里的
    ``LLV(CLOSE, N1+1)`` / ``REF(CL1, M1+1)`` 只在"死叉/金叉至少出现过一次"
    之后才有意义: 若把从未成立当成 0, 窗口会塌成 1 根, 等于拿当天收盘冒充
    "本轮下跌的最低点", 凭空制造出 ``CL1 < CL2`` 的假底背离。

    这里用"曾经成立过"的时间掩码把未成立区间整体置为 NaN, 让下游的变长
    窗口 / 取数算子直接判无效 (与源脚本把 BARSLAST 的未成立值记成 NaN 同口径)。
    """
    seen = np.logical_or.accumulate(np.asarray(condition, dtype=bool), axis=0)
    return np.where(seen, np.asarray(ages, dtype=np.float32), np.float32("nan")).astype(np.float32)


def bottom_stale_chain(
    market: MarketDataMatrix,
    *,
    macd_prev_bars: int = 1,
) -> dict[str, np.ndarray]:
    """底部结构选股公式的完整判定链 (矩阵原生)。

    ``macd_prev_bars`` 对应源公式里隔峰底钝化的 ``REF(MACD, N)``: 底部结构选股用
    1, 钝化加低九选股用 2。

    返回 (全部与 market 同形状):

    - ``valid``: 有效 bar 掩码
    - ``diff`` / ``dea`` / ``macd``: MACD 三线
    - ``direct`` / ``peak``: 直接底钝化 / 隔峰底钝化
    - ``stale``: 底部钝化 (含价格与 DIF 双新低的背离判定) —— 「底部结构选股」用它
    - ``first_stale``: 底钝化 —— 底部钝化的**首次**成立且 DIFF 仍在 DEA 下方,
      只有「钝化加低九选股」(``底钝化 AND T9``) 会用到
    - ``structure``: 底部结构 = ``DIFF>REF(DIFF,1) AND REF(底部钝化,1) AND DIFL1*0.9884<DIFF``
    - ``formed``: 底结构形成 —— 底部结构的首次成立, 即「底部结构选股」的选股输出
    """
    close = market.close
    valid = np.isfinite(close)

    with np.errstate(invalid="ignore", divide="ignore"):
        diff, dea, macd = macd_triplet(market, valid)

        dead_seen = dead_cross(diff, dea, valid)
        gold_seen = golden_cross(diff, dea, valid)
        n1 = _observed_only(valid_barslast(dead_seen, valid), dead_seen)
        m1 = _observed_only(valid_barslast(gold_seen, valid), gold_seen)

        # 本轮 (死叉以来) 与上一轮 (再往前) 的收盘低点 / DIF 低点
        cl1 = valid_rolling_min_at(close, n1 + _ONE, valid)
        cl2 = valid_shift_at(cl1, m1 + _ONE, valid)
        cl3 = valid_shift_at(cl2, m1 + _ONE, valid)
        difl1 = valid_rolling_min_at(diff, n1 + _ONE, valid)
        difl2 = valid_shift_at(difl1, m1 + _ONE, valid)
        difl3 = valid_shift_at(difl2, m1 + _ONE, valid)

        macd_prev = valid_shift(macd, int(macd_prev_bars), valid)
        diff_prev = valid_shift(diff, 1, valid)
        negative = macd_prev < _ZERO

        # 价格创新低 + DIF 不创新低 = 底背离; "直接"看上一轮, "隔峰"看上上轮
        direct = (cl1 < cl2) & (difl1 > difl2) & negative & (difl2 < _ZERO)
        peak = (
            (cl1 < cl3)
            & (difl1 < difl2)
            & (difl1 > difl3)
            & (diff < dea)
            & negative
            & (difl3 < _ZERO)
        )

        stale = (direct | peak) & negative & valid
        first_stale = stale & previous_false(stale, valid) & (diff < dea) & valid
        # 注意取 REF(底部钝化, 1) 而非 REF(底钝化, 1) —— 见模块 docstring 的口径警示
        structure = (
            (diff > diff_prev)
            & previous_true(stale, valid)
            & (difl1 * _BOTTOM_RATIO < diff)
            & valid
        )
        formed = structure & previous_false(structure, valid) & valid

    return {
        "valid": valid,
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
