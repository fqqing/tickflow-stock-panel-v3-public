"""缠论 (缠中说禅) 结构分析引擎 (逐 bar 精确实现, 纯函数)。

与 ``formula_signals`` 一样按需计算, 不落盘。完整链路:

1. **K 线包含处理** — 合并存在包含关系的相邻 K 线, 消除噪音
2. **分型识别** — 顶分型 / 底分型 (合并后三根 K 线的极值结构)
3. **笔** — 相邻顶底分型连线, K 线间隔可配置 (严格笔 / 宽松笔)
4. **中枢** — 连续三笔的重叠区间 [ZD, ZG], 支持延伸
5. **买卖点** — 一买 (趋势背驰) / 二买 (回抽不破前低) / 三买 (突破中枢回抽不回),
   以及对应的一卖 / 二卖 / 三卖

约定:
- 输入 OHLC 为一维 numpy 数组, 时间升序, 与 K 线一一对应; 缺口用 ``np.nan``。
- 输出中所有 **索引均为原始 K 线索引**, 便于前端直接对齐 K 线绘制。
- 缠论细节存在流派差异, 本模块把可争议处全部做成参数, 默认取最通行口径。

口径说明 (关键):
- ``strict=True`` (严格笔, 默认): 顶底分型的中间 K 线之间至少间隔 4 根合并 K 线。
- ``strict=False`` (宽松笔 / 新笔): 间隔降为 3 根, 信号更早更多, 但噪音显著上升。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import numpy as np

__all__ = [
    "SIGNAL_LABELS",
    "Center",
    "ChanAnalysis",
    "ChanSignal",
    "ChanSnapshot",
    "Fractal",
    "MergedBar",
    "Stroke",
    "analyze",
    "build_strokes",
    "detect_centers",
    "detect_fractals",
    "latest_signal",
    "merge_inclusions",
]

# ===== 参数默认值 =====

# 笔的最小间隔: 两个分型「中间 K 线」之间允许的最小合并 K 线跨度
STROKE_MIN_GAP_STRICT = 4
STROKE_MIN_GAP_LOOSE = 3

# 背驰判定: 后一段 MACD 同向柱面积 / 前一段 < 该比例, 视为力度衰减
DIVERGENCE_AREA_RATIO = 0.9

# 中枢延伸的最大笔数, 防止一根横盘走势无限吞并后续走势
CENTER_MAX_EXTEND_STROKES = 60

# MACD 参数 (与通达信默认一致)
MACD_FAST_SPAN = 12
MACD_SLOW_SPAN = 26
MACD_SIGNAL_SPAN = 9

# 形成有效结构所需的最少 K 线数 (分型 3 + 笔间隔 + 中枢 3 笔的经验下限)
MIN_BARS_FOR_STRUCTURE = 20

SIGNAL_LABELS: dict[str, str] = {
    "1buy": "一买",
    "2buy": "二买",
    "3buy": "三买",
    "1sell": "一卖",
    "2sell": "二卖",
    "3sell": "三卖",
}

_BUY_KINDS = ("1buy", "2buy", "3buy")
_SELL_KINDS = ("1sell", "2sell", "3sell")


# ===== 数据结构 =====


@dataclass(frozen=True, slots=True)
class MergedBar:
    """合并后的 K 线 (消除包含关系)。

    ``index`` 为该合并 K 线所覆盖的**最后一根**原始 K 线索引, 即它在图上占据的位置。
    ``high`` / ``low`` 为合并后的区间。
    """

    index: int
    high: float
    low: float


@dataclass(frozen=True, slots=True)
class Fractal:
    """分型。``kind``: +1 顶分型, -1 底分型。"""

    pos: int
    """在合并 K 线序列中的位置。"""

    index: int
    """原始 K 线索引 (分型中间那根)。"""

    kind: int
    price: float
    high: float
    low: float


@dataclass(frozen=True, slots=True)
class Stroke:
    """笔。连接相邻的顶分型与底分型。"""

    start_pos: int
    """起点分型在 ``fractals`` 中的下标。"""

    end_pos: int
    start_index: int
    end_index: int
    start_price: float
    end_price: float
    direction: int
    """+1 向上笔, -1 向下笔。"""

    high: float
    low: float


@dataclass(frozen=True, slots=True)
class Center:
    """中枢。``[zd, zg]`` 为中枢区间, ``zg > zd``。"""

    start_stroke: int
    end_stroke: int
    start_index: int
    end_index: int
    zg: float
    """中枢上沿 = 构成中枢的三笔高点的最小值。"""

    zd: float
    """中枢下沿 = 构成中枢的三笔低点的最大值。"""

    stroke_count: int


@dataclass(frozen=True, slots=True)
class ChanSignal:
    """缠论买卖点。``index`` 为信号确认的原始 K 线索引 (通常落在笔的终点)。"""

    kind: str
    index: int
    price: float
    stroke_pos: int
    center_pos: int | None = None
    divergence: bool = False

    @property
    def label(self) -> str:
        return SIGNAL_LABELS.get(self.kind, self.kind)

    @property
    def is_buy(self) -> bool:
        return self.kind in _BUY_KINDS


@dataclass(frozen=True, slots=True)
class ChanSnapshot:
    """面向「当下点位提示」的结论摘要。"""

    kind: str | None
    """最新买点类型; 无买点时为 None。"""

    bars_since: int | None
    """该买点距今多少根 K 线。"""

    price: float | None
    center_zg: float | None
    center_zd: float | None
    trend: str
    """up 上涨 / down 下跌 / range 盘整。"""

    label: str
    text: str


@dataclass(frozen=True, slots=True)
class ChanAnalysis:
    merged: tuple[MergedBar, ...]
    fractals: tuple[Fractal, ...]
    strokes: tuple[Stroke, ...]
    centers: tuple[Center, ...]
    signals: tuple[ChanSignal, ...]
    trend: str
    snapshot: ChanSnapshot
    """最近**买点**快照。"""

    sell_snapshot: ChanSnapshot
    """最近**卖点**快照 (与 ``snapshot`` 对称, 供卖点提示复用同一套渲染)。"""


# ===== 基础工具 =====


def _macd(close: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (DIF, DEA, MACD 柱)。柱高按通达信惯例乘 2。

    递推口径与 EMA 一致 (首值取序列起点), 但把 DIF 快慢两条 EMA 合到一个循环里,
    避免对 close 遍历两次 —— 全市场扫描时这是第 3 热的点。
    """
    values = close.tolist()
    n = len(values)
    if n == 0:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty, empty

    alpha_fast = 2.0 / (MACD_FAST_SPAN + 1.0)
    alpha_slow = 2.0 / (MACD_SLOW_SPAN + 1.0)
    alpha_signal = 2.0 / (MACD_SIGNAL_SPAN + 1.0)

    dif = [0.0] * n
    fast = values[0]
    slow = values[0]
    for i in range(1, n):
        value = values[i]
        fast = alpha_fast * value + (1.0 - alpha_fast) * fast
        slow = alpha_slow * value + (1.0 - alpha_slow) * slow
        dif[i] = fast - slow

    dea = [0.0] * n
    dea[0] = dif[0]
    for i in range(1, n):
        dea[i] = alpha_signal * dif[i] + (1.0 - alpha_signal) * dea[i - 1]

    hist = [(dif[i] - dea[i]) * 2.0 for i in range(n)]
    return (
        np.array(dif, dtype=np.float64),
        np.array(dea, dtype=np.float64),
        np.array(hist, dtype=np.float64),
    )


def _segment_area(hist: np.ndarray, start: int, end: int, sign: int) -> float:
    """区间 ``[start, end]`` 内同向 MACD 柱的面积 (绝对值累加)。

    用布尔掩码取和而不是 ``nansum(where(...))`` —— 后者每次都要建临时数组,
    在背驰判定里被调用上千次, 是全链路第二热的点。
    """
    lo = max(0, min(start, end))
    hi = min(hist.shape[0] - 1, max(start, end))
    if hi < lo:
        return 0.0
    seg = hist[lo : hi + 1]
    if sign > 0:
        positive = seg[seg > 0]
        return float(positive.sum()) if positive.size else 0.0
    negative = seg[seg < 0]
    return float(-negative.sum()) if negative.size else 0.0


# ===== 1. K 线包含处理 =====


def merge_inclusions(high: np.ndarray, low: np.ndarray) -> list[MergedBar]:
    """缠论包含处理。

    相邻两根 K 线若一根完全覆盖另一根 (含端点相等), 则合并为一根。合并方向由
    **再前一根**已合并 K 线决定:
    - 向上处理 (再前一根更高): 取 ``max(high)`` / ``max(low)``
    - 向下处理 (再前一根更低): 取 ``min(high)`` / ``min(low)``

    输出按时间升序, ``index`` 指向该合并 K 线覆盖的最后一根原始 K 线。
    含 ``NaN`` 的行直接跳过 (停牌 / 缺口不参与结构)。

    性能: 这是全链路最热的一段 (全市场扫描时占比过半), 故先用 ``tolist()`` 转成
    Python 原生 float 再迭代, 并用平行的三条 list 代替嵌套 list —— 避免每次迭代
    都对 numpy 标量装箱、也避免内层 list 的反复索引。
    """
    highs = high.tolist()
    lows = low.tolist()
    merged_high: list[float] = []
    merged_low: list[float] = []
    merged_index: list[int] = []

    for i in range(len(highs)):
        h = highs[i]
        row_low = lows[i]
        if not (isfinite(h) and isfinite(row_low)):
            continue
        if not merged_high:
            merged_high.append(h)
            merged_low.append(row_low)
            merged_index.append(i)
            continue

        prev_high = merged_high[-1]
        prev_low = merged_low[-1]
        contained = (h >= prev_high and row_low <= prev_low) or (
            h <= prev_high and row_low >= prev_low
        )
        if not contained:
            merged_high.append(h)
            merged_low.append(row_low)
            merged_index.append(i)
            continue

        if len(merged_high) >= 2:
            direction = 1 if prev_high > merged_high[-2] else -1
        else:
            direction = 1 if h >= prev_high else -1

        if direction > 0:
            merged_high[-1] = prev_high if prev_high > h else h
            merged_low[-1] = prev_low if prev_low > row_low else row_low
        else:
            merged_high[-1] = prev_high if prev_high < h else h
            merged_low[-1] = prev_low if prev_low < row_low else row_low
        merged_index[-1] = i

    return [
        MergedBar(index=merged_index[k], high=merged_high[k], low=merged_low[k])
        for k in range(len(merged_high))
    ]


# ===== 2. 分型识别 =====


def detect_fractals(merged: list[MergedBar]) -> list[Fractal]:
    """在无包含关系的合并 K 线上识别顶 / 底分型。

    去包含后, 三根连续 K 线的高点若中间最高, 其低点必然也最高, 故只需比较高点。
    """
    fractals: list[Fractal] = []
    for i in range(1, len(merged) - 1):
        prev_bar, cur, next_bar = merged[i - 1], merged[i], merged[i + 1]
        if cur.high > prev_bar.high and cur.high > next_bar.high:
            fractals.append(
                Fractal(
                    pos=i,
                    index=cur.index,
                    kind=1,
                    price=cur.high,
                    high=cur.high,
                    low=cur.low,
                )
            )
        elif cur.low < prev_bar.low and cur.low < next_bar.low:
            fractals.append(
                Fractal(
                    pos=i,
                    index=cur.index,
                    kind=-1,
                    price=cur.low,
                    high=cur.high,
                    low=cur.low,
                )
            )
    return fractals


# ===== 3. 笔 =====


def _more_extreme(candidate: Fractal, current: Fractal) -> bool:
    """同向分型取更极端者: 顶取更高, 底取更低 (相等也替换, 保持靠近末端)。"""
    if candidate.kind > 0:
        return candidate.price >= current.price
    return candidate.price <= current.price


def build_strokes(fractals: list[Fractal], *, strict: bool = True) -> list[Stroke]:
    """由分型序列构造笔。

    规则:
    1. 相邻笔方向必须相反 (顶底交替)。
    2. 顶底分型的**中间 K 线**之间至少间隔 ``min_gap`` 根合并 K 线, 否则该分型被忽略。
    3. 出现同向分型时取更极端者; 若它已是上一笔的终点, 则回写修正上一笔终点。
    """
    min_gap = STROKE_MIN_GAP_STRICT if strict else STROKE_MIN_GAP_LOOSE
    if len(fractals) < 2:
        return []

    confirmed: list[tuple[int, int]] = []
    start_pos = 0
    i = 1
    while i < len(fractals):
        cur = fractals[i]
        head = fractals[start_pos]

        if cur.kind == head.kind:
            if _more_extreme(cur, head):
                start_pos = i
                if confirmed:
                    tail_start, _ = confirmed[-1]
                    confirmed[-1] = (tail_start, i)
            i += 1
            continue

        if cur.pos - head.pos >= min_gap:
            confirmed.append((start_pos, i))
            start_pos = i
        i += 1

    strokes: list[Stroke] = []
    for s_pos, e_pos in confirmed:
        s_fx = fractals[s_pos]
        e_fx = fractals[e_pos]
        strokes.append(
            Stroke(
                start_pos=s_pos,
                end_pos=e_pos,
                start_index=s_fx.index,
                end_index=e_fx.index,
                start_price=s_fx.price,
                end_price=e_fx.price,
                direction=1 if e_fx.kind > 0 else -1,
                high=max(s_fx.price, e_fx.price),
                low=min(s_fx.price, e_fx.price),
            )
        )
    return strokes


# ===== 4. 中枢 =====


def detect_centers(strokes: list[Stroke]) -> list[Center]:
    """识别中枢。

    中枢 = 连续三笔的重叠区间:
    - ``zg = min(三笔高点)``, ``zd = max(三笔低点)``, 要求 ``zg > zd``
    - **延伸**: 后续笔的**终点**仍落在 ``[zd, zg]`` 内则并入中枢;
      一旦某笔终点跑出区间, 该笔即为「离开笔」, 中枢在此结束。

    采用「以终点是否仍在区间内」作为延伸判据, 是为了让突破笔能干净地终止中枢,
    从而让第三类买卖点 (突破后回抽不回) 的定位与主流软件一致。
    """
    centers: list[Center] = []
    n = len(strokes)
    i = 0
    while i + 2 < n:
        group = strokes[i : i + 3]
        zg = min(s.high for s in group)
        zd = max(s.low for s in group)
        if zg <= zd:
            i += 1
            continue

        end = i + 2
        j = i + 3
        extended = 0
        while j < n and extended < CENTER_MAX_EXTEND_STROKES:
            end_price = strokes[j].end_price
            if zd <= end_price <= zg:
                end = j
                j += 1
                extended += 1
                continue
            break

        centers.append(
            Center(
                start_stroke=i,
                end_stroke=end,
                start_index=strokes[i].start_index,
                end_index=strokes[end].end_index,
                zg=zg,
                zd=zd,
                stroke_count=end - i + 1,
            )
        )
        i = end + 1
    return centers


# ===== 5. 买卖点 =====


def _detect_divergence_1buy(strokes: list[Stroke], hist: np.ndarray) -> list[ChanSignal]:
    """第一类买点: 向下笔创新低, 但 MACD 绿柱面积显著衰减 (底背驰)。"""
    out: list[ChanSignal] = []
    prev_down: Stroke | None = None
    for pos, s in enumerate(strokes):
        if s.direction > 0:
            continue
        if prev_down is not None and s.low < prev_down.low:
            cur_area = _segment_area(hist, s.start_index, s.end_index, -1)
            prev_area = _segment_area(hist, prev_down.start_index, prev_down.end_index, -1)
            if prev_area > 0 and cur_area < prev_area * DIVERGENCE_AREA_RATIO:
                out.append(
                    ChanSignal(
                        kind="1buy",
                        index=s.end_index,
                        price=s.end_price,
                        stroke_pos=pos,
                        divergence=True,
                    )
                )
        prev_down = s
    return out


def _detect_divergence_1sell(strokes: list[Stroke], hist: np.ndarray) -> list[ChanSignal]:
    """第一类卖点: 向上笔创新高, 但 MACD 红柱面积显著衰减 (顶背驰)。"""
    out: list[ChanSignal] = []
    prev_up: Stroke | None = None
    for pos, s in enumerate(strokes):
        if s.direction < 0:
            continue
        if prev_up is not None and s.high > prev_up.high:
            cur_area = _segment_area(hist, s.start_index, s.end_index, 1)
            prev_area = _segment_area(hist, prev_up.start_index, prev_up.end_index, 1)
            if prev_area > 0 and cur_area < prev_area * DIVERGENCE_AREA_RATIO:
                out.append(
                    ChanSignal(
                        kind="1sell",
                        index=s.end_index,
                        price=s.end_price,
                        stroke_pos=pos,
                        divergence=True,
                    )
                )
        prev_up = s
    return out


def _detect_second_points(
    strokes: list[Stroke], firsts: list[ChanSignal], *, buy: bool
) -> list[ChanSignal]:
    """第二类买 / 卖点。

    - 二买: 一买之后先向上再向下, 该向下笔的低点**不破**一买的低点。
    - 二卖: 一卖之后先向下再向上, 该向上笔的高点**不破**一卖的高点。
    """
    out: list[ChanSignal] = []
    n = len(strokes)
    for first in firsts:
        k = first.stroke_pos
        if k + 2 >= n:
            continue
        rebound = strokes[k + 1]
        pull = strokes[k + 2]
        if buy:
            if rebound.direction > 0 and pull.direction < 0 and pull.low > strokes[k].low:
                out.append(
                    ChanSignal(
                        kind="2buy",
                        index=pull.end_index,
                        price=pull.end_price,
                        stroke_pos=k + 2,
                    )
                )
        elif rebound.direction < 0 and pull.direction > 0 and pull.high < strokes[k].high:
            out.append(
                ChanSignal(
                    kind="2sell",
                    index=pull.end_index,
                    price=pull.end_price,
                    stroke_pos=k + 2,
                )
            )
    return out


def _detect_third_points(strokes: list[Stroke], centers: list[Center]) -> list[ChanSignal]:
    """第三类买 / 卖点。

    - 三买: 向上笔 (离开笔) 的终点升破 ``zg``, 紧接着的向下笔回抽低点**仍 > zg**。
    - 三卖: 向下笔 (离开笔) 的终点跌破 ``zd``, 紧接着的向上笔反抽高点**仍 < zd**。
    """
    out: list[ChanSignal] = []
    n = len(strokes)
    for c_pos, center in enumerate(centers):
        leave = center.end_stroke + 1
        if leave + 1 >= n:
            continue
        leave_stroke = strokes[leave]
        back = strokes[leave + 1]

        if leave_stroke.direction > 0 and leave_stroke.end_price > center.zg:
            if back.direction < 0 and back.low > center.zg:
                out.append(
                    ChanSignal(
                        kind="3buy",
                        index=back.end_index,
                        price=back.end_price,
                        stroke_pos=leave + 1,
                        center_pos=c_pos,
                    )
                )
        elif (
            leave_stroke.direction < 0
            and leave_stroke.end_price < center.zd
            and back.direction > 0
            and back.high < center.zd
        ):
            out.append(
                ChanSignal(
                    kind="3sell",
                    index=back.end_index,
                    price=back.end_price,
                    stroke_pos=leave + 1,
                    center_pos=c_pos,
                )
            )
    return out


def _classify_trend(strokes: list[Stroke], centers: list[Center]) -> str:
    """粗粒度趋势判定。

    - ``down``: 最近两个中枢依次下移 (后中枢完全在前中枢之下)
    - ``up``: 最近两个中枢依次上移
    - ``range``: 其余 (含仅一个中枢或中枢重叠)
    """
    if len(centers) >= 2:
        prev_c, cur_c = centers[-2], centers[-1]
        if cur_c.zg < prev_c.zd:
            return "down"
        if cur_c.zd > prev_c.zg:
            return "up"

    tail = [s for s in strokes[-6:]]
    if len(tail) >= 2:
        if tail[-1].direction < 0 and tail[-1].low < min(s.low for s in tail[:-1]):
            return "down"
        if tail[-1].direction > 0 and tail[-1].high > max(s.high for s in tail[:-1]):
            return "up"
    return "range"


def _build_snapshot(
    signals: list[ChanSignal],
    centers: list[Center],
    trend: str,
    n_bars: int,
    *,
    is_buy: bool = True,
) -> ChanSnapshot:
    """产出「当下点位」提示: 取该方向上最后一个信号, 并给出可读结论。

    买卖两侧共用这段文案逻辑 (``is_buy`` 只切换取哪个方向与措辞),
    这样选股列 / 扫描页能对卖点复用完全相同的渲染路径。
    """
    kinds = _BUY_KINDS if is_buy else _SELL_KINDS
    side_cn = "买点" if is_buy else "卖点"
    last_center = centers[-1] if centers else None
    picked = [s for s in signals if s.kind in kinds]
    if not picked:
        label = f"无{side_cn}"
        detail = f"当前无缠论{side_cn}信号"
        if last_center is not None:
            detail += f", 处于中枢 {last_center.zd:.2f} ~ {last_center.zg:.2f} 区间运行"
        return ChanSnapshot(
            kind=None,
            bars_since=None,
            price=None,
            center_zg=last_center.zg if last_center else None,
            center_zd=last_center.zd if last_center else None,
            trend=trend,
            label=label,
            text=detail,
        )

    latest = picked[-1]
    bars_since = max(n_bars - 1 - latest.index, 0)
    label = latest.label
    parts = [f"{label} @ {latest.price:.2f}", f"距今 {bars_since} 根"]
    if latest.divergence:
        parts.append("伴随背驰")
    # 快照中的中枢取该信号自身关联的中枢, 与文案保持一致;
    # 信号无关联中枢时退回最后一个中枢。
    signal_center = None
    if latest.center_pos is not None and latest.center_pos < len(centers):
        signal_center = centers[latest.center_pos]
    else:
        signal_center = last_center
    if signal_center is not None:
        parts.append(f"中枢 {signal_center.zd:.2f} ~ {signal_center.zg:.2f}")
    return ChanSnapshot(
        kind=latest.kind,
        bars_since=bars_since,
        price=latest.price,
        center_zg=signal_center.zg if signal_center else None,
        center_zd=signal_center.zd if signal_center else None,
        trend=trend,
        label=label,
        text=", ".join(parts),
    )


# ===== 对外入口 =====


def latest_signal(
    signals: tuple[ChanSignal, ...] | list[ChanSignal],
    kinds: set[str] | frozenset[str] | tuple[str, ...],
) -> ChanSignal | None:
    """取指定类型里索引最大的信号; 没有则 None。

    扫描端点用它判「某类信号是否新鲜」—— 直接读 ``snapshot`` 只覆盖买点,
    扫卖点会恒定返回空 (2026-09-18 修正)。
    """
    best: ChanSignal | None = None
    for signal in signals:
        if signal.kind in kinds and (best is None or signal.index > best.index):
            best = signal
    return best


def analyze(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    strict: bool = True,
) -> ChanAnalysis:
    """完整跑一遍缠论结构分析。

    参数:
        high / low / close: 一维数组, 时间升序, 等长。
        strict: True 用严格笔 (默认), False 用宽松笔。
    """
    high_arr = np.asarray(high, dtype=np.float64)
    low_arr = np.asarray(low, dtype=np.float64)
    close_arr = np.asarray(close, dtype=np.float64)
    n_bars = int(close_arr.shape[0])

    merged = merge_inclusions(high_arr, low_arr)
    fractals = detect_fractals(merged)
    strokes = build_strokes(fractals, strict=strict)
    centers = detect_centers(strokes)

    if n_bars > 0 and np.isfinite(close_arr).any():
        _, _, hist = _macd(np.nan_to_num(close_arr, nan=0.0))
    else:
        hist = np.zeros(n_bars, dtype=np.float64)

    signals: list[ChanSignal] = []
    first_buys = _detect_divergence_1buy(strokes, hist)
    first_sells = _detect_divergence_1sell(strokes, hist)
    signals += first_buys
    signals += first_sells
    signals += _detect_second_points(strokes, first_buys, buy=True)
    signals += _detect_second_points(strokes, first_sells, buy=False)
    signals += _detect_third_points(strokes, centers)
    signals.sort(key=lambda s: (s.index, s.kind))

    trend = _classify_trend(strokes, centers)
    snapshot = _build_snapshot(signals, centers, trend, n_bars, is_buy=True)
    sell_snapshot = _build_snapshot(signals, centers, trend, n_bars, is_buy=False)

    return ChanAnalysis(
        merged=tuple(merged),
        fractals=tuple(fractals),
        strokes=tuple(strokes),
        centers=tuple(centers),
        signals=tuple(signals),
        trend=trend,
        snapshot=snapshot,
        sell_snapshot=sell_snapshot,
    )
