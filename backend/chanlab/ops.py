"""有效 bar 口径的 numpy 算子 —— v1 ``app.backtest.matrix`` 的 ``valid_*`` 族的本地实现。

为什么不用简单的 ``np.roll``: 全市场矩阵里有大量停牌日 (缺行), 直接按列位移会把
停牌日算进窗口, 导致「30 日均线」实际取到 27 个交易日的数据。v1 的矩阵原生策略
全部走有效 bar 口径, 这里必须一致, 否则两边选出来的票不一样。

为什么不为 ``app.backtest.matrix`` 加依赖: 它在 v1 仓库里且绑定了一堆 FastAPI
上下文。真正需要的只是「Try 也许有效秩 ->  Gather」这一招, 一百行就能说清楚。

核心结构 :class:`EffIndex`
=========================

给定 ``valid`` 掩码, 一次算出两张表:

- ``rank[r, c]``: 位置的有效秩 (该位置是这只票第几个有效 bar, 0 起/无效为 -1)
- ``lut[r, k]``: 该行第 k 个有效 bar 在哪一列 (不存在为 -1)

有了它们, 「往前第 n 个有效 bar」就是一次 gather, 不需要 python 循环,
变长窗口 (通达信的 ``LLV(C, N1+1)``) 也只是 rank 减去一个数组。
"""

from __future__ import annotations

from typing import Any

import numpy as np

F32 = np.float32


class EffIndex:
    """有效 bar 索引。构造一次, 之后所有算子共享。"""

    __slots__ = ("_rows_idx", "cols", "count", "lut", "nv", "rank", "rows", "valid")

    def __init__(self, valid: np.ndarray) -> None:
        if valid.ndim != 2:
            raise ValueError(f"valid 必须是二维, 收到 {valid.ndim} 维")
        self.valid = valid.astype(bool, copy=False)
        self.rows, self.cols = valid.shape

        rows_idx = np.repeat(np.arange(self.rows)[:, None], self.cols, axis=1)
        cols_idx = np.broadcast_to(np.arange(self.cols), (self.rows, self.cols))

        # cumsum 对 bool 得到「截至该列的有效个数」, 无效位置的值等于同行左邻居 —— 无所谓,
        # 因为下游一律先乘 valid / 用 where 过滤。
        self.count = np.cumsum(self.valid, axis=1)
        self.rank = np.where(self.valid, self.count - 1, -1).astype(np.int64)

        # 一次 scatter 建成 lut: 第 k 个有效 bar 的列号
        self.lut = np.full((self.rows, self.cols), -1, dtype=np.int64)
        rows_flat = rows_idx[self.valid]
        cols_flat = cols_idx[self.valid]
        ranks_flat = self.rank[self.valid]
        self.lut[rows_flat, ranks_flat] = cols_flat
        self._rows_idx = rows_idx
        self.nv = self.valid.sum(axis=1)

    def to_rank(self, values: np.ndarray, fill: float = float("nan")) -> np.ndarray:
        """列空间 -> **有效秩空间**: 第 k 列变成「该行第 k 个有效 bar 的值」。

        换过来之后, 「往前 n 个有效 bar」就是纯粹的列偏移, 没有空洞。
        滚动平均可以用前缀和、滚动极值可以用稀疏表, 都是 O(列数) 而不是
        O(列数 x 窗口)。
        """
        grid = np.broadcast_to(np.arange(self.cols), (self.rows, self.cols))
        ok = grid < self.nv[:, None]
        picked = np.take_along_axis(np.asarray(values, dtype=F32), np.clip(self.lut, 0, self.cols - 1), axis=1)
        return np.where(ok, picked, F32(fill))

    def from_rank(self, matrix: np.ndarray) -> np.ndarray:
        """有效秩空间 -> 列空间 (无效列填 NaN)。"""
        picked = np.take_along_axis(np.asarray(matrix, dtype=F32), np.clip(self.rank, 0, self.cols - 1), axis=1)
        return np.where(self.valid, picked, np.nan).astype(F32)

    def gather_rank(self, ranks: np.ndarray, mask: np.ndarray | None = None) -> tuple[Any, Any]:
        """把「有效秩」换成列号。

        返回 ``(cols, ok)``: ``cols`` 已裁剪到合法范围 (可直接用于 fancy index),
        ``ok`` 标记哪些位置真的能取到值。
        """
        clipped = np.clip(ranks, 0, self.cols - 1)
        cols = self.lut[self._rows_idx, clipped]
        ok = (ranks >= 0) & (cols >= 0)
        if mask is not None:
            ok &= mask
        return cols, ok

    def take(self, values: np.ndarray, ranks: np.ndarray, mask: np.ndarray | None = None) -> Any:
        """按有效秩取值, 取不到的位置是 NaN。"""
        cols, ok = self.gather_rank(ranks, mask)
        picked = np.where(ok, values[self._rows_idx, cols], np.nan)
        return np.asarray(picked, dtype=F32)


def shift(values: np.ndarray, ei: EffIndex, n: int) -> np.ndarray:
    """``REF(X, n)`` —— 往前第 n 个**有效 bar** 的值, 取不到为 NaN。"""
    return ei.take(values, ei.rank - int(n))


def shift_at(values: np.ndarray, ei: EffIndex, offsets: np.ndarray) -> np.ndarray:
    """``REF(X, A)`` —— A 逐 bar 变化的变长取数 (通达信里的 ``REF(HIGH, A3)``)。

    ``offsets`` 单位是**有效 bar 数**, 非有限值或负数视为取不到。
    """
    clean = np.where(np.isfinite(offsets), offsets, np.nan)
    rounded = np.rint(np.nan_to_num(clean, nan=-1.0)).astype(np.int64)
    return ei.take(values, ei.rank - rounded, np.isfinite(clean) & (rounded >= 0))


def previous_true(condition: np.ndarray, ei: EffIndex) -> np.ndarray:
    """``REF(条件, 1)`` 是否成立。没有上一根时算不成立 (而不是 True)。"""
    return previous_value(condition.astype(F32), ei) > np.float32(0.5)


def previous_false(condition: np.ndarray, ei: EffIndex) -> np.ndarray:
    """``REF(条件, 1) = 0`` 是否成立。没有上一根时**不成立** (见下方说明)。

    「上一根不存在」时返回 False 是有意的: 序列开头的 bar 不该因为取不到历史
    就被当成「首次成立」。v1 的 :func:`previous_false` 同口径。
    """
    previous = previous_value(condition.astype(F32), ei)
    return np.isfinite(previous) & (previous < np.float32(0.5))


def previous_value(values: np.ndarray, ei: EffIndex) -> np.ndarray:
    """上一根有效 bar 的值 (NaN 表示没有上一根)。"""
    return shift(values, ei, 1)


def _rank(matrix: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """在有效秩空间里按「第 k 个有效 bar」取值 (越界处取最后一列, 由调用方兜底)。"""
    return np.take_along_axis(matrix, np.clip(indices, 0, matrix.shape[1] - 1), axis=1)


def _window_extreme(values: np.ndarray, ei: EffIndex, windows: np.ndarray, *, use_min: bool) -> np.ndarray:
    """``LLV(X, N)`` / ``HHV(X, N)`` —— 窗口含当前 bar, N 可以是标量或逐 bar 变化的数组。

    实现是**稀疏表**: 第 j 层保存「长度 2^j 的窗口极值」, 查询时把长度 L 拆成两段
    重叠的 2^j 窗口。代价从 O(L) 降到 O(log L) —— 这里的 L 最大能到几百
    (``LLV(C, N1+1)``, N1 是距上次死叉的 bar 数), 朴素逐层循环会让底部结构策略
    从 1 秒变成 45 秒。

    ⚠️ 层号必须按**实际能取到的长度** ``min(N, 有效秩+1)`` 来选: 序列左端窗口被
    截断时, 若按名义长度 N 选层, 2^j 窗口会越过 0 号有效 bar, 把同一行里本不该
    参与的值带进来 (实测表现为上市初期的 LLV 偏大)。
    """
    combine = np.fmin if use_min else np.fmax
    pad = np.float32(np.inf if use_min else -np.inf)
    table = ei.to_rank(values, fill=pad)

    raw = np.nan_to_num(np.where(np.isfinite(windows), windows, 0.0))
    lengths = np.clip(np.rint(raw).astype(np.int64), 1, ei.cols)
    usable = np.clip(np.minimum(lengths, ei.rank + 1), 1, ei.cols)
    logs = np.floor(np.log2(usable.astype(np.float64))).astype(np.int64)
    max_log = int(logs.max()) if logs.size else 0

    out = np.full(ei.valid.shape, np.nan, dtype=F32)
    now = ei.rank
    for level in range(max_log + 1):
        span = 1 << level
        want = (logs == level) & ei.valid
        if want.any():
            start = np.clip(now - lengths + 1, 0, ei.cols - 1)
            tail = np.minimum(start + span - 1, now)
            segment = combine(_rank(table, now), _rank(table, tail))
            out = np.where(want, segment, out)
        if level < max_log:
            moved = np.full_like(table, pad)
            moved[:, span:] = table[:, :-span]
            table = combine(table, moved)
    return out


def rolling_extreme_at(values: np.ndarray, ei: EffIndex, windows: np.ndarray, *, use_min: bool) -> np.ndarray:
    return _window_extreme(values, ei, windows, use_min=use_min)


def llv_at(values: np.ndarray, ei: EffIndex, windows: np.ndarray) -> np.ndarray:
    return _window_extreme(values, ei, windows, use_min=True)


def hhv_at(values: np.ndarray, ei: EffIndex, windows: np.ndarray) -> np.ndarray:
    return _window_extreme(values, ei, windows, use_min=False)


def _rolling_extreme(values: np.ndarray, ei: EffIndex, n: int, *, use_min: bool) -> np.ndarray:
    """固定窗口版本 —— 广播成常量窗口后走同一条路径, 保证两者结果一致。"""
    windows = np.full(ei.valid.shape, float(max(int(n), 1)), dtype=np.float32)
    return _window_extreme(values, ei, windows, use_min=use_min)


def rolling_max(values: np.ndarray, ei: EffIndex, n: int) -> np.ndarray:
    return _rolling_extreme(values, ei, n, use_min=False)


def rolling_min(values: np.ndarray, ei: EffIndex, n: int) -> np.ndarray:
    return _rolling_extreme(values, ei, n, use_min=True)


def rolling_sum(values: np.ndarray, ei: EffIndex, n: int) -> np.ndarray:
    """滚动求和 —— 在有效秩空间做前缀和, 一次 cumsum 出全部结果。"""
    field = ei.to_rank(values, fill=0.0)
    cumulative = np.concatenate(
        [np.zeros((ei.rows, 1), dtype=F32), np.cumsum(field, axis=1)], axis=1
    )
    own = np.clip(ei.rank + 1, 0, ei.cols)
    past = np.clip(ei.rank - int(n) + 1, 0, ei.cols)
    rows_idx = np.arange(ei.rows)[:, None]
    sums = cumulative[rows_idx, own] - cumulative[rows_idx, past]
    return np.where(ei.valid, sums, np.nan).astype(F32)


def rolling_mean(values: np.ndarray, ei: EffIndex, n: int) -> np.ndarray:
    """滚动均值: 分母是**实际取到的有效 bar 数**, 不是窗口名义长度。

    停牌会让名义窗口里只有 3 根数据, 一律除 5 会把均线算成系统性偏低。
    """
    total = rolling_sum(values, ei, n)
    counts = rolling_sum(np.ones_like(values, dtype=F32), ei, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.asarray(total / counts, dtype=F32)


def barslast(condition: np.ndarray, ei: EffIndex) -> np.ndarray:
    """``BARSLAST(条件)`` —— 距上次成立的**有效 bar 数**, 当天成立为 0。

    ⚠️ 「从未成立过」返回 **NaN** 而不是 0。v1 的 ``valid_barslast`` 在这里返回 0,
    再用 ``_observed_only`` 把未成立区间抹掉 —— 两义合一最容易出错
    (把从未金叉当成「距离 0」会让 ``LLV(C, N1+1)`` 塌成一根, 凭空造出假背离),
    所以这里直接融合成一步。
    """
    hit = condition & ei.valid
    mark = np.where(hit, ei.rank, -1)
    last_hit = np.maximum.accumulate(mark, axis=1)
    seen = last_hit >= 0
    ages = ei.rank - last_hit
    out = np.where(seen & ei.valid, ages, np.nan)
    return np.asarray(out, dtype=F32)


def barslastcount(condition: np.ndarray, ei: EffIndex) -> np.ndarray:
    """``BARSLASTCOUNT(条件)`` —— 到目前为止连续成立的有效 bar 数 (不成立为 0)。"""
    missed = ei.valid & ~condition
    mark = np.where(missed, ei.rank, -1)
    last_miss = np.maximum.accumulate(mark, axis=1)
    counts = ei.rank - last_miss
    counts = np.where(ei.valid, counts, -1)
    return np.asarray(counts, dtype=F32)


def ewm_adjust_false(values: np.ndarray, ei: EffIndex, span: int) -> np.ndarray:
    """``EMA(X, span)`` —— adjust=False 的递推式, **只沿有效 bar 推进**。

    通达信 / AKL 公式里的 EMA 就是递推式 (首值取自身), 不是 pandas 默认的
    adjust=True。两者在序列前几十根差得很明显 —— 而这正好落在策略信号的判定区。
    """
    alpha = F32(2.0 / (int(span) + 1.0))
    out = np.full(ei.valid.shape, np.nan, dtype=F32)
    carried = np.full(ei.rows, np.nan, dtype=F32)
    for c in range(ei.cols):
        column = np.asarray(values[:, c], dtype=F32)
        hit = ei.valid[:, c]
        updated = np.where(np.isfinite(carried), alpha * column + (F32(1.0) - alpha) * carried, column)
        carried = np.where(hit, np.where(np.isfinite(column), updated, carried), carried)
        out[:, c] = np.where(hit, carried, np.nan)
    return out


def cross(a: np.ndarray, b: np.ndarray, ei: EffIndex) -> np.ndarray:
    """``CROSS(A, B)`` —— A 上穿 B (今日 A>B 且上一有效 bar A<=B)。"""
    return (a > b) & (previous_value(a, ei) <= previous_value(b, ei)) & ei.valid


def last_of(values: np.ndarray, ei: EffIndex) -> np.ndarray:
    """每行的最后一个有效值 (全行无效则为 NaN)。"""
    out = np.full(ei.rows, np.nan, dtype=F32)
    for r in range(ei.rows):
        total = int(ei.nv[r])
        if total <= 0:
            continue
        position = ei.lut[r, total - 1]
        if position >= 0:
            out[r] = values[r, position]
    return out
