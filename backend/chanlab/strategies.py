"""v1 同源选股策略 —— 判链搬到 v2 自建日线库上重算。

为什么不转发 v1 的结果
======================

v1 的选股跑在自己的 ``enriched`` 表上 (17x/min 全市场 recompute, 内存峰值 6.98GB),
要使用就得把 3018 常驻, 这与「v2 必须能独立运行」冲突。而这些策略本身都是
**「OHLC -> bool」的 numpy 判定链**, 不依赖任何 v1 基础设施:

- 底部结构 ``bottom_structure``  —— MACD 底背离后首次形成结构 (源: AKL DBJGXG)
- 向上趋势并突破 ``upward_trend_breakout`` —— 定量结构 QSTPXG (26/89 高低轨)
- 趋势擒龙 ``trend_dragon`` —— 重心连续上移后的回踩假破 (源: qushiqinlong)
- 启动策略 ``startup_surge`` —— 涨停/跳空/连阳/温和放量四条共振

判链从 v1 ``app/strategy/builtin/*.py`` 直译, 算子走 :mod:`chanlab.ops` 的
有效 bar 口径 —— 与 v1 ``app.backtest.matrix.valid_*`` 同语义, 所以停牌日的处理、
EMA 的递推方式、``BARSLAST`` 的两义处理都能对齐。

⚠️ 刻意不复刻的部分
------------------

1. **基础过滤里的市值门槛** 用「流通股本 x 收盘价」近似 (v2 没有市值列)。
2. **score** 是 v2 口径 (三项动量/量比/涨幅的百分位加权); v1 的归一化绑在它的
   enriched 列上, 照搬只会得到一个看着一样、含义不同的数字。
3. **v1 自己的 ADMIN/service 侧弥补逻辑** (NOT limited to) 一律不带。

性能
====

全市场一次成型: (股票数 x 交易日数) 的 float32 矩阵, 判链在矩阵上向量化跑,
5500 只 x 480 根约 0.5~2s/策略。结果按 (策略, 参数签名, 截止日) 缓存, 同一天
反复查不重算。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from chanlab import index_source, ops, v1mirror
from chanlab.loader import resolve_own_dir
from chanlab.ops import EffIndex

F32 = np.float32

# ---------------------------------------------------------------------------
# 参数定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """一个可调开关。与 v1 META.params 一一对应 (id/label/default 都照抄)。"""

    id: str
    label: str
    kind: str  # bool | int | float
    default: Any
    low: float = 0.0
    high: float = 0.0
    step: float = 1.0


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    desc: str
    tags: tuple[str, ...]
    params: tuple[ParamSpec, ...]
    compute: Callable[..., np.ndarray]
    #: 有效 bar 预热长度 (与 v1 required_warmup_bars 一致)
    warmup: int
    #: v1 lark_screener.STRATEGY_TABLES 里的飞书目标表 (可能为空串 = 未配)
    lark_label: str = ""
    lark_base_token: str = ""
    lark_table_id: str = ""


def _bool(pid: str, label: str, default: bool) -> ParamSpec:
    return ParamSpec(pid, label, "bool", default)


def _int(pid: str, label: str, default: int, low: int, high: int) -> ParamSpec:
    return ParamSpec(pid, label, "int", default, low, high, 1.0)


def _float(pid: str, label: str, default: float, low: float, high: float, step: float) -> ParamSpec:
    return ParamSpec(pid, label, "float", default, low, high, step)


def _p(params: dict, spec: ParamSpec) -> Any:
    """取参数值: 缺失回落到 default, 类型错也回落 (前端只会传它关心的那几项)。"""
    raw = params.get(spec.id, spec.default)
    try:
        if spec.kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        value = int(raw) if spec.kind == "int" else float(raw)
    except (TypeError, ValueError):
        return spec.default
    if not np.isfinite(value):
        return spec.default
    clamped = min(max(value, spec.low), spec.high)
    return int(clamped) if spec.kind == "int" else clamped


# ---------------------------------------------------------------------------
# 面板: 全市场 OHLCV 矩阵
# ---------------------------------------------------------------------------

#: 默认回看日历日。要覆盖最长的 upward_trend_breakout 预热 261 根有效 bar,
#: 480 个交易日才够 —— 日历日要给到 700 才稳 (含春节这类长假)。
DEFAULT_DAYS = 760

_CACHE: dict[str, Any] = {}
#: 面板在进程内的存活秒数。同一天反复跑不同策略不该重扫 5500 个 parquet
PANEL_TTL = float(os.environ.get("TICKFLOW_PANEL_TTL", "1800"))

_PANEL: dict[str, Any] = {"frame": None, "days": 0, "end": None, "at": 0.0}
_BENCHMARK_BY_MARKET = {"SH": "000001.SH", "SZ": "399001.SZ", "BJ": "899050.BJ"}


@dataclass
class Panel:
    """全市场日线矩阵。所有数组形状 (股票数 x 交易日数)。"""

    symbols: list[str]
    names: list[str]
    markets: list[str]
    dates: list[str]
    floats: np.ndarray  # (rows,) 流通股本
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    index_close: np.ndarray
    ei: EffIndex
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return len(self.symbols)


def _wide(df: pl.DataFrame, col: str) -> tuple[np.ndarray, list[str], list[str]]:
    """长表 -> 宽表矩阵。列顺序按日期升序, 缺的行是 NaN。"""
    wide = (
        df.select(["symbol", "date", pl.col(col)])
        .sort(["symbol", "date"])
        .pivot(on="date", index="symbol", values=col)
        .sort("symbol")
    )
    symbols = [str(x) for x in wide["symbol"].to_list()]
    dates = [str(c) for c in wide.columns if c != "symbol"]
    matrix = wide.drop("symbol").to_numpy()
    return np.asarray(matrix, dtype=F32), symbols, dates


def _index_matrix(dates: list[str], symbols: list[str]) -> np.ndarray:
    """按交易所把基准指数收盘价广播成 (股票数 x 交易日数)。读不到就整列 NaN。"""
    out = np.full((len(symbols), len(dates)), np.nan, dtype=F32)
    needed = {_BENCHMARK_BY_MARKET.get(s.split(".")[-1], "") for s in symbols}
    needed.discard("")
    series: dict[str, np.ndarray] = {}
    for symbol in sorted(needed):
        try:
            rows = index_source.read_store(symbol, days=0)
        except Exception:  # 指数库没建 / 半截文件: 该交易所整体降级为不过滤
            continue
        by_date = {str(r.get("date"))[:10]: r.get("close") for r in rows}
        series[symbol] = np.array(
            [_as_float(by_date.get(d)) for d in dates], dtype=F32
        )
    for i, symbol in enumerate(symbols):
        series_row = series.get(_BENCHMARK_BY_MARKET.get(symbol.split(".")[-1], ""))
        if series_row is not None:
            out[i, :] = series_row
    return out


def _as_float(value: Any) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")
    return number if np.isfinite(number) else float("nan")


def load_panel(days: int = DEFAULT_DAYS, *, end: date | None = None, refresh: bool = False) -> Panel:
    """读自有日线库里最近 ``days`` 个日历日的全部标的, 组装成矩阵。

    进程内带 TTL 缓存 —— 同一天内反复跑不同策略不该重复扫 5500 个 parquet。
    手动刷新用 ``refresh=True``。
    """
    now = time.perf_counter()
    cached = _PANEL["frame"]
    if (
        cached is not None
        and not refresh
        and _PANEL["days"] == days
        and _PANEL["end"] == end
        and (now - _PANEL["at"]) < PANEL_TTL
    ):
        return cached

    cutoff = (end or date.today()) - timedelta(days=int(days))
    scan = pl.scan_parquet(str(resolve_own_dir() / "**" / "*.parquet"))
    df = (
        scan.filter(pl.col("date") >= cutoff)
        .select(["symbol", "date", "open", "high", "low", "close", "volume", "amount"])
        .sort(["symbol", "date"])
        .collect()
    )
    if df.is_empty():
        raise RuntimeError("自有日线库为空, 先跑 backend/scripts/build_daily_store.py")

    matrices: dict[str, np.ndarray] = {}
    symbols: list[str] = []
    dates: list[str] = []
    for col in ("close", "open", "high", "low", "volume", "amount"):
        matrix, symbols, dates = _wide(df, col)
        matrices[col] = matrix

    meta_df = v1mirror.load("instruments")
    names = [""] * len(symbols)
    floats = np.full(len(symbols), np.nan, dtype=F32)
    if not meta_df.is_empty():
        lookup = {str(r[0]): (str(r[1] or ""), _as_float(r[2])) for r in meta_df.select(
            ["symbol", "name", "float_shares"]
        ).iter_rows()}
        for i, symbol in enumerate(symbols):
            name, shares = lookup.get(symbol, ("", float("nan")))
            names[i] = name
            floats[i] = shares

    close = matrices["close"]
    valid = np.isfinite(close) & (close > 0)
    panel = Panel(
        symbols=symbols,
        names=names,
        markets=[s.split(".")[-1] if "." in s else "" for s in symbols],
        dates=dates,
        floats=floats,
        open=matrices["open"],
        high=matrices["high"],
        low=matrices["low"],
        close=close,
        volume=matrices["volume"],
        amount=matrices["amount"],
        index_close=_index_matrix(dates, symbols),
        ei=EffIndex(valid),
        meta={"as_of": dates[-1] if dates else "", "bars": len(dates), "source": "own"},
    )
    _PANEL.update(frame=panel, days=days, end=end, at=now, features=None)
    return panel


def invalidate() -> None:
    """下一次请求强制重建面板 (「手动刷新」与测试用)。"""
    _PANEL["at"] = 0.0
    _PANEL["features"] = None


# ---------------------------------------------------------------------------
# 派生特征
# ---------------------------------------------------------------------------


@dataclass
class Features:
    ma5: np.ndarray
    ma10: np.ndarray
    ma20: np.ndarray
    vol_ratio_5d: np.ndarray
    momentum_20d: np.ndarray
    change_pct: np.ndarray
    turnover_rate: np.ndarray
    dif: np.ndarray
    dea: np.ndarray
    capital_momentum: np.ndarray


def features(panel: Panel) -> Features:
    """全市场统一派生一遍 —— 各策略的输出列基本都是这些, 一次算完比分头算便宜。

    结果挂在面板缓存旁边: 同一份面板跑四个策略时, 这些 Moving Average 只算一次。
    """
    cached = _PANEL.get("features")
    if cached is not None and _PANEL["frame"] is panel:
        return cached
    computed = _compute_features(panel)
    _PANEL["features"] = computed
    return computed


def _compute_features(panel: Panel) -> Features:
    ei = panel.ei
    close = panel.close

    ma5 = ops.rolling_mean(close, ei, 5)
    ma10 = ops.rolling_mean(close, ei, 10)
    ma20 = ops.rolling_mean(close, ei, 20)

    previous_close = ops.shift(close, ei, 1)
    change_pct = np.where(previous_close > 0, close / previous_close - F32(1.0), np.nan).astype(F32)

    base5 = ops.shift(ops.rolling_mean(panel.volume, ei, 5), ei, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vol_ratio = np.where(base5 > 0, panel.volume / base5, np.nan).astype(F32)

    past_close = ops.shift(close, ei, 20)
    momentum = np.where(past_close > 0, close / past_close - F32(1.0), np.nan).astype(F32)

    # 换手率 = 成交量(手) x 100 / 流通股本 x 100 (%)。流通股本缺失时为 NaN,
    # 不填 0 —— 「不知道」和「没有成交」是两回事。
    with np.errstate(invalid="ignore", divide="ignore"):
        turnover = np.where(
            np.isfinite(panel.floats)[:, None] & (panel.floats[:, None] > 0),
            panel.volume * F32(100.0) / panel.floats[:, None] * F32(100.0),
            np.nan,
        ).astype(F32)

    dif, dea = _macd(panel)
    return Features(
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        vol_ratio_5d=vol_ratio,
        momentum_20d=momentum,
        change_pct=change_pct,
        turnover_rate=turnover,
        dif=dif,
        dea=dea,
        capital_momentum=_capital_momentum(panel),
    )


def _macd(panel: Panel) -> tuple[np.ndarray, np.ndarray]:
    """MACD 双线。EMA 用 adjust=False 递推 (通达信口径), 见 formula_signals 同款实现。"""
    ei = panel.ei
    fast = ops.ewm_adjust_false(panel.close, ei, 12)
    slow = ops.ewm_adjust_false(panel.close, ei, 26)
    dif = (fast - slow).astype(F32)
    dea = ops.ewm_adjust_false(dif, ei, 9)
    return dif, dea


def _capital_momentum(panel: Panel, window: int = 52) -> np.ndarray:
    """资金动能 = ``(A1 / MA(A1, 52) - 1) * 10``, ``A1 = CLOSE / INDEXC * 1e6``。

    与 v1 trend_dragon._capital_momentum 同口径。基准指数整列缺失时为 NaN
    (调用方决定「没有基准」是放行还是剔除)。
    """
    ei = panel.ei
    index_close = panel.index_close
    usable = np.isfinite(index_close) & (index_close > 0)
    if not usable.any():
        return np.full(panel.close.shape, np.nan, dtype=F32)

    with np.errstate(invalid="ignore", divide="ignore"):
        a1 = np.where(usable, panel.close / index_close * F32(1e6), np.nan).astype(F32)
    sub_ei = EffIndex(ei.valid & np.isfinite(a1))
    average = ops.rolling_mean(np.where(np.isfinite(a1), a1, np.nan).astype(F32), sub_ei, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.asarray((a1 / average - F32(1.0)) * F32(10.0), dtype=F32)


# ---------------------------------------------------------------------------
# 策略判链 (直译 v1 builtin)
# ---------------------------------------------------------------------------


def bottom_structure(panel: Panel, feats: Features, params: dict) -> np.ndarray:
    """底部结构: MACD 底背离形成后的首次结构形成。

    完整判链见 v1 ``builtin/_quant_structure.py::bottom_stale_chain``, 这里逐行直译。
    """
    ei = panel.ei
    close = panel.close
    valid = ei.valid

    dif, dea, macd_line = feats.dif, feats.dea, (feats.dif - feats.dea) * F32(2.0)

    dead = ops.cross(dea, dif, ei)
    golden = ops.cross(dif, dea, ei)
    # BARSLAST 未成立记 NaN —— 详见 ops.barslast 的说明
    n1 = ops.barslast(dead, ei)
    m1 = ops.barslast(golden, ei)

    cl1 = ops.llv_at(close, ei, n1 + F32(1.0))
    cl2 = ops.shift_at(cl1, ei, m1 + F32(1.0))
    cl3 = ops.shift_at(cl2, ei, m1 + F32(1.0))
    difl1 = ops.llv_at(dif, ei, n1 + F32(1.0))
    difl2 = ops.shift_at(difl1, ei, m1 + F32(1.0))
    difl3 = ops.shift_at(difl2, ei, m1 + F32(1.0))

    macd_prev = ops.shift(macd_line, ei, 1)
    dif_prev = ops.shift(dif, ei, 1)
    negative = np.isfinite(macd_prev) & (macd_prev < F32(0.0))

    with np.errstate(invalid="ignore"):
        direct = (cl1 < cl2) & (difl1 > difl2) & negative & (difl2 < F32(0.0))
        peak = (
            (cl1 < cl3)
            & (difl1 < difl2)
            & (difl1 > difl3)
            & (dif < dea)
            & negative
            & (difl3 < F32(0.0))
        )
        stale = (direct | peak) & negative & valid
        # 注意是 REF(底部钝化, 1) 而非 REF(底钝化, 1): v1 在这里踩过坑 —— 用错会让
        # 全市场信号数从 48 只塌到 9 只, 而且信号点整体后移。
        structure = (dif > dif_prev) & ops.previous_true(stale, ei) & (difl1 * F32(0.9884) < dif)
        formed = structure & ops.previous_false(structure, ei) & valid

    scan_days = _p(params, _int("scan_days", "近N个交易日内出现信号", 1, 1, 20))
    entry = _window(formed.astype(F32), ei, int(scan_days))
    return entry & valid


def upward_trend_breakout(panel: Panel, feats: Features, params: dict) -> np.ndarray:
    """定量结构 QSTPXG: 收盘上穿 26 日短上轨, 且回调后再度突破。"""
    del feats
    ei = panel.ei
    close = panel.close
    previous_close = ops.shift(close, ei, 1)

    short_upper = ops.ewm_adjust_false(panel.high, ei, 26)
    short_lower = ops.ewm_adjust_false(panel.low, ei, 26)
    long_upper = ops.ewm_adjust_false(panel.high, ei, 89)

    with np.errstate(invalid="ignore"):
        cross_up = (close > short_upper) & (previous_close <= ops.shift(short_upper, ei, 1))
        cross_down = (short_lower > close) & (ops.shift(short_lower, ei, 1) <= previous_close)

    since_up = ops.barslast(cross_up, ei)
    since_down = ops.barslast(cross_down, ei)
    bars_since_up = ops.shift_at(since_up, ei, np.ones_like(since_up)) + F32(1.0)
    bars_since_down = ops.shift_at(since_down, ei, np.ones_like(since_down)) + F32(1.0)

    entry = np.ones(panel.close.shape, dtype=bool)
    if _p(params, _bool("require_short_rail_cross", "要求上穿26日短上轨", True)):
        entry &= cross_up
    if _p(params, _bool("require_pullback_first", "要求先跌破再站上", True)):
        with np.errstate(invalid="ignore"):
            entry &= np.isfinite(bars_since_up) & np.isfinite(bars_since_down) & (
                bars_since_up > bars_since_down
            )
    if _p(params, _bool("require_above_short_rail", "要求站上26日前的短上轨", True)):
        with np.errstate(invalid="ignore"):
            entry &= close > ops.shift(short_upper, ei, 26)
    if _p(params, _bool("require_above_long_rail", "要求站上89日前的长上轨", True)):
        with np.errstate(invalid="ignore"):
            entry &= close > ops.shift(long_upper, ei, 89)
    return entry & ei.valid


def trend_dragon(panel: Panel, feats: Features, params: dict) -> np.ndarray:
    """趋势擒龙: 重心连续 9 根上移后的回踩再突破 (四条分支, 见 v1 源脚本)。

    A1 = C > REF(C, 4) 连续 9 根 -> 回踩 -> 距上次重心抬头的 5 根内突破。
    """
    ei = panel.ei
    close, open_, high = panel.close, panel.open, panel.high
    # v1 的 ``valid`` 在这里是 close/open/high 三者都有限, 比面板默认的
    # 「close 有限」更严 —— 少算一个字段会让有效秩错位, 历史不足的那一段
    # 信号数就对不上 (对拍时差异全部集中在序列前 20 根附近)。
    ei = EffIndex(ei.valid & np.isfinite(open_) & np.isfinite(high))

    a1 = close > ops.shift(close, ei, 4)
    a2 = ops.barslastcount(a1, ei) == F32(9.0)
    a3 = ops.barslast(a2, ei)

    ref_high = ops.shift_at(high, ei, a3)
    ref_close = ops.shift_at(close, ei, a3)
    ref_open = ops.shift_at(open_, ei, a3)
    ref_close_1 = ops.shift_at(close, ei, a3 - F32(1.0))
    ref_open_1 = ops.shift_at(open_, ei, a3 - F32(1.0))
    ref_close_2 = ops.shift_at(close, ei, a3 - F32(2.0))
    ref_open_2 = ops.shift_at(open_, ei, a3 - F32(2.0))

    in_range = np.isfinite(a3) & (a3 >= F32(1.0)) & (a3 <= F32(5.0))
    with np.errstate(invalid="ignore"):
        breakout = (
            (in_range & (a3 <= F32(3.0)) & (high > ref_high) & (ref_close < ref_open))
            | (
                (a3 >= F32(2.0))
                & (a3 <= F32(4.0))
                & (high > ref_high)
                & (ref_close_1 < ref_open_1)
            )
            | (
                (a3 >= F32(3.0))
                & (a3 <= F32(4.0))
                & (high > ref_high)
                & (ref_close_2 < ref_open_2)
            )
            | (
                (a3 == F32(5.0))
                & (high >= ops.rolling_max(high, ei, 5))
            )
        )

    dragon = breakout & ei.valid
    if _p(params, _bool("require_above_ma10", "要求站稳MA10", True)):
        with np.errstate(invalid="ignore"):
            dragon &= close > feats.ma10
    if _p(params, _bool("require_strong_close", "要求收盘不低于昨收", True)):
        with np.errstate(invalid="ignore"):
            dragon &= close >= ops.shift(close, ei, 1)

    scan_days = _p(params, _int("scan_days", "近N个交易日内出现信号", 3, 1, 20))
    entry = _window(dragon.astype(F32), ei, int(scan_days))

    bias_cap = _p(params, _float("bias20_cap_pct", "MA20乖离率上限%", 0.0, 0.0, 50.0, 0.1))
    if bias_cap > 0:
        with np.errstate(invalid="ignore", divide="ignore"):
            bias = (close / feats.ma20 - F32(1.0)) * F32(100.0)
        entry &= np.isfinite(bias) & (bias <= F32(bias_cap))

    if _p(params, _bool("use_momentum_filter", "启用资金动能上限", False)):
        cap = _p(params, _float("momentum_cap", "资金动能上限", 1.0, -50.0, 50.0, 0.1))
        momentum = feats.capital_momentum
        has_benchmark = np.isfinite(panel.index_close).any(axis=1)
        ok = ~has_benchmark[:, None] | (np.isfinite(momentum) & (momentum <= F32(cap)))
        entry &= ok
    return entry & ei.valid


def startup_surge(panel: Panel, feats: Features, params: dict) -> np.ndarray:
    """启动策略: 近 15 日有涨停 / 近 10 日有跳空 / 最近 3 根连阳 / 当日量比 1.5~3。"""
    del feats
    ei = panel.ei
    close, open_, high, low, volume = panel.close, panel.open, panel.high, panel.low, panel.volume
    valid = ei.valid & np.isfinite(open_) & np.isfinite(high) & np.isfinite(low) & np.isfinite(
        volume
    ) & (open_ > 0)

    pct = close / ops.shift(close, ei, 1) - F32(1.0)
    recent_limit_up = ops.rolling_max(pct, ei, 15) >= F32(0.098)

    gap_up = low > ops.shift(high, ei, 1)
    recent_gap = ops.rolling_max(gap_up.astype(F32), ei, 10) >= F32(0.5)

    body = (close - open_) / open_
    three_yang = (ops.rolling_min(body, ei, 3) > F32(0.0)) & (
        ops.rolling_mean(body, ei, 3) > F32(0.01)
    )

    vol_base = ops.shift(ops.rolling_mean(volume, ei, 5), ei, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vol_ratio = np.where(vol_base > 0, volume / vol_base, np.nan).astype(F32)
    volume_ok = np.isfinite(vol_ratio) & (vol_ratio >= F32(1.5)) & (vol_ratio <= F32(3.0))

    enough = np.isfinite(ops.shift(close, ei, 30))
    entry = valid & enough
    if _p(params, _bool("require_recent_limit_up", "近15日有涨停", True)):
        entry &= recent_limit_up
    if _p(params, _bool("require_recent_gap", "近10日有向上跳空", True)):
        entry &= recent_gap
    if _p(params, _bool("require_three_yang", "最近3日连阳且实体>1%", True)):
        entry &= three_yang
    if _p(params, _bool("require_volume_ratio", "当日量比前5日均量1.5~3倍", True)):
        entry &= volume_ok
    return entry & ei.valid


def _window(flags: np.ndarray, ei: EffIndex, days: int) -> np.ndarray:
    """「近 N 个有效交易日出现过信号」窗口 <=> ``HHV(X, N) >= 0.5``。"""
    if days <= 1:
        return np.asarray(flags, dtype=F32) >= F32(0.5)
    return ops.rolling_max(flags, ei, days) >= F32(0.5)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

STRATEGIES: dict[str, StrategySpec] = {
    spec.id: spec
    for spec in (
        StrategySpec(
            id="bottom_structure",
            name="底部结构",
            desc="价格创本轮新低而 DIF 未创新低形成底部钝化后, 首次出现底部结构",
            tags=("底部", "背离", "MACD"),
            warmup=120,
            compute=bottom_structure,
            lark_label="底部结构",
            lark_base_token="EEYSbsdLpa9QkZsmGyVc7vCCnbb",
            lark_table_id="tbl8TfrGiJYfzDd7",
            params=(
                _int("scan_days", "近N个交易日内出现信号", 1, 1, 20),
            ),
        ),
        StrategySpec(
            id="upward_trend_breakout",
            name="向上趋势并突破",
            desc="收盘上穿 26 日短上轨且回调后再度突破, 同时站上 26/89 日前的高低轨",
            tags=("趋势", "突破", "定量结构"),
            warmup=261,
            compute=upward_trend_breakout,
            lark_label="向上趋势并突破",
            lark_base_token="E1yLbkvgQaO28rssEbAca1twnxc",
            lark_table_id="tbl9qX3qJx0Wr5vf",
            params=(
                _bool("require_short_rail_cross", "要求上穿26日短上轨", True),
                _bool("require_pullback_first", "要求先跌破再站上", True),
                _bool("require_above_short_rail", "要求站上26日前的短上轨", True),
                _bool("require_above_long_rail", "要求站上89日前的长上轨", True),
            ),
        ),
        StrategySpec(
            id="trend_dragon",
            name="趋势擒龙",
            desc="重心连续 9 根上移后回踩, 再度放量突破且站稳 MA10, 可选资金动能过滤",
            tags=("趋势", "回踩", "擒龙"),
            warmup=120,
            compute=trend_dragon,
            lark_label="趋势擒龙",
            lark_base_token="S4lKbOf6TaQ7A2sFw4hcDbyanFE",
            lark_table_id="tbl8ZWQKMgGqyawK",
            params=(
                # v1 里这三个默认值对齐用户日常用法 (选股_趋势擒龙.py 的
                # --max-momentum 1 --max-bias20 10), 不是「不加限制」。
                _int("scan_days", "近N个交易日内出现信号", 5, 1, 20),
                _bool("require_above_ma10", "要求站稳MA10", True),
                _bool("require_strong_close", "要求收盘不低于昨收", True),
                _float("bias20_cap_pct", "MA20乖离率上限%(0=不过滤)", 10.0, 0.0, 50.0, 0.1),
                _bool("use_momentum_filter", "启用资金动能上限", True),
                _float("momentum_cap", "资金动能上限", 1.0, -50.0, 50.0, 0.1),
            ),
        ),
        StrategySpec(
            id="startup_surge",
            name="启动策略",
            desc="近 15 日有涨停 + 近 10 日有跳空 + 最近 3 日连阳 + 当日量比 1.5~3 倍",
            tags=("启动", "量价", "短线"),
            warmup=60,
            compute=startup_surge,
            lark_label="启动策略",
            lark_base_token="RSYubVkH8aJajys5sh2c6NGAn62",
            lark_table_id="tbl1VE8bY2Jos8RY",
            params=(
                _bool("require_recent_limit_up", "近15日有涨停", True),
                _bool("require_recent_gap", "近10日有向上跳空", True),
                _bool("require_three_yang", "最近3日连阳且实体>1%", True),
                _bool("require_volume_ratio", "当日量比前5日均量1.5~3倍", True),
            ),
        ),
    )
}

#: 输出 showcased 给前端表格的列 (与 v1 飞书表字段能对上的那部分)
ROW_FIELDS = (
    "symbol",
    "name",
    "market",
    "signal_date",
    "close",
    "change_pct",
    "amount",
    "turnover_rate",
    "vol_ratio_5d",
    "momentum_20d",
    "ma5",
    "ma20",
    "macd_dif",
    "macd_dea",
    "bias_ma20",
    "capital_momentum",
    "ago",
    "score",
)

#: 基础过滤 (v1 META.basic_filter 的等价物)
_PRICE_MIN, _PRICE_MAX = 3.0, 300.0
_AMOUNT_MIN = 0.5e8
_FLOAT_CAP_MIN = 10e8
_MIN_BARS = 60


def strategy_ids() -> list[str]:
    return list(STRATEGIES)


def describe(strategy_id: str) -> StrategySpec:
    if strategy_id not in STRATEGIES:
        raise KeyError(f"未知策略 {strategy_id}, 可选 {strategy_ids()}")
    return STRATEGIES[strategy_id]


def _default_params(spec: StrategySpec) -> dict:
    return {p.id: p.default for p in spec.params}


def clean_params(strategy_id: str, payload: Any) -> dict:
    """把前端传来的自由 JSON 夹成合法参数: 未知键丢弃, 越界值夹回默认。"""
    spec = describe(strategy_id)
    source = payload if isinstance(payload, dict) else {}
    merged = _default_params(spec)
    for p in spec.params:
        if p.id in source:
            merged[p.id] = _p(source, p)
    return merged


# ---------------------------------------------------------------------------
# 跑一遍全市场
# ---------------------------------------------------------------------------


def scan(
    strategy_id: str,
    params: dict | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
    refresh: bool = False,
) -> dict:
    """跑一次策略, 返回 ``{total, rows, signal_date, cost_sec, params}``。

    ``rows`` 里每只票都带 :data:`ROW_FIELDS` 那些列。只有**最后一根有信号**
    的标的才入选 (这与 v1 的「当日信号 + 近 N 日窗口」一致)。
    """
    spec = describe(strategy_id)
    cleaned = clean_params(strategy_id, params or {})
    started = time.perf_counter()

    panel = load_panel(days=DEFAULT_DAYS, refresh=refresh)
    ei = panel.ei
    feats = features(panel)

    entry = spec.compute(panel, feats, cleaned)
    entry &= ei.valid
    # 最后一根有信号才算「今天选中」: 信号布尔矩阵的最后列
    last_date = panel.dates[-1] if panel.dates else ""
    hits = np.zeros(panel.rows, dtype=bool)
    for r in range(panel.rows):
        total = int(ei.nv[r])
        if total <= 0:
            continue
        position = ei.lut[r, total - 1]
        if position >= 0 and bool(entry[r, position]):
            hits[r] = True

    keep = hits & _basic_filter(panel, ei)
    rows = [row_for(panel, feats, i, cleaned) for i in np.nonzero(keep)[0]]
    _score(rows)
    rows.sort(key=lambda r: (-(r["score"] or 0.0), r["symbol"]))
    total = len(rows)
    page = rows[offset : offset + limit] if limit > 0 else rows[offset:]
    return {
        "strategy": strategy_id,
        "strategy_name": spec.name,
        "params": cleaned,
        "total": total,
        "rows": page,
        "signal_date": last_date,
        "cost_sec": round(time.perf_counter() - started, 3),
        "meta": {"as_of": panel.meta.get("as_of", ""), "bars": panel.meta.get("bars", 0)},
    }


def _basic_filter(panel: Panel, ei: EffIndex) -> np.ndarray:
    """价格 / 成交额 / 流通市值 / ST / 新股一排前置过滤。"""
    last_close = ops.last_of(panel.close, ei)
    last_amount = ops.last_of(panel.amount, ei)
    ok = np.isfinite(last_close) & (last_close >= _PRICE_MIN) & (last_close <= _PRICE_MAX)
    ok &= np.isfinite(last_amount) & (last_amount >= _AMOUNT_MIN)
    ok &= ei.nv >= _MIN_BARS
    with np.errstate(invalid="ignore"):
        cap = np.where(np.isfinite(panel.floats), panel.floats * last_close, np.nan)
    ok &= ~np.isfinite(cap) | (cap >= _FLOAT_CAP_MIN)
    for i, name in enumerate(panel.names):
        upper = str(name or "").upper().replace(" ", "")
        if "ST" in upper or "退" in str(name or ""):
            ok[i] = False
    return ok


def row_for(panel: Panel, feats: Features, index: int, params: dict) -> dict[str, Any]:
    """取一只票在最后一根上的全部输出列。

    ``ago`` 恒为 0 —— 只有最后一根带信号的标的才入选, 这也保证了信号列当作
    「距今」看不会错位。
    """
    del params
    ei = panel.ei
    total = int(ei.nv[index])
    position = int(ei.lut[index, total - 1]) if total > 0 else -1

    def last(matrix: np.ndarray) -> float | None:
        if position < 0:
            return None
        value = float(matrix[index, position])
        return value if np.isfinite(value) else None

    close = last(panel.close)
    amount = last(panel.amount)
    ma20 = last(feats.ma20)
    bias = None
    if close is not None and ma20 not in (None, 0):
        bias = round((close / ma20 - 1.0) * 100.0, 2)
    return {
        "symbol": panel.symbols[index],
        "name": panel.names[index],
        "market": panel.markets[index],
        "signal_date": panel.dates[position] if position >= 0 else "",
        "close": _round(close, 2),
        "change_pct": last(feats.change_pct),
        "amount": amount,
        "turnover_rate": last(feats.turnover_rate),
        "vol_ratio_5d": last(feats.vol_ratio_5d),
        "momentum_20d": last(feats.momentum_20d),
        "ma5": last(feats.ma5),
        "ma20": _round(ma20, 2),
        "macd_dif": _round(last(feats.dif), 4),
        "macd_dea": _round(last(feats.dea), 4),
        "bias_ma20": bias,
        "capital_momentum": last(feats.capital_momentum),
        "ago": 0,
        "score": 0.0,
    }


def _round(value: Any, digits: int) -> float | None:
    number = _as_float(value)
    return None if not np.isfinite(number) else round(number, digits)


def _score(rows: list[dict]) -> None:
    """给每条结果打分 (0~100): 20 日动量 / 量比 / 当日涨幅的百分位加权。

    ⚠️ 这是 v2 口径 —— v1 的 score 归一化绑在它的 enriched 列上, 照搬会得到
    一个看着一样但含义不同的数字, 不如换一个能解释清楚的。
    """
    if not rows:
        return

    def ranks(key: str) -> dict[int, float]:
        values = [(i, r[key]) for i, r in enumerate(rows) if isinstance(r.get(key), int | float)]
        ordered = sorted(values, key=lambda kv: kv[1])
        out: dict[int, float] = {}
        count = max(len(ordered) - 1, 1)
        for rank, (i, _) in enumerate(ordered):
            out[i] = rank / count * 100.0
        return out

    momentum = ranks("momentum_20d")
    volume = ranks("vol_ratio_5d")
    change = ranks("change_pct")
    for i, row in enumerate(rows):
        row["score"] = round(
            0.4 * momentum.get(i, 0.0) + 0.3 * volume.get(i, 0.0) + 0.3 * change.get(i, 0.0), 2
        )
