"""基准指数序列 —— 把大盘指数收盘价按交易所对齐到 (交易日 x 标的) 矩阵。

用途: 需要「个股相对大盘」口径的策略/因子。典型是趋势擒龙源脚本里的资金动能:

    资金动能 = (C / INDEXC / MA52(C / INDEXC) - 1) * 10
    INDEXC    = 该股所属市场的大盘指数收盘价

源脚本 (qushiqinlong/选股_趋势擒龙.py::compute_capital_momentum) 按
``market == 1`` (沪) 取上证指数、否则取深证成指, 并在个股与指数的日期交集上
做 52 日均值。矩阵是 (交易日 x 标的) 的密集结构, 没有这条序列, 所以这里把它
读出来、按标的后缀广播成同形状矩阵, 再由 :mod:`app.backtest.matrix` 作为计算
特征 ``index_close`` 暴露给策略。

为什么不复用 :mod:`app.indicators.pipeline` 里的 ``_BENCHMARK_PREFERENCE``:
那份是「交易所异常波动偏离值的对应指数」(上证A指 000002.SH / 深证A指 399107.SZ),
与策略公式要的「大盘指数」(上证指数 000001.SH / 深证成指 399001.SZ) 口径不同,
硬合并会让移植后的数值与用户原脚本漂移。**真正同口径的先例是
:mod:`app.api.kline` 的个股 K 线副图** (``_BENCHMARK_INDEX_BY_EXCHANGE``),
本模块与它取同一份指数映射, 差异只在取数方式:

- K 线副图按单只股票取数, 并且用**实时指数快照**补今天 (``_inject_live_index_close``);
- 矩阵侧是 (交易日 x 标的) 的批量结构, 取不到当天指数行时**沿用最近一根收盘**
  (盘中当日指数行一旦落盘就会被自动用上)。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from app.parquet import scan_daily_parquet

EXCHANGE_CODE: dict[str, int] = {"SH": 0, "SZ": 1, "BJ": 2}

# 后缀 -> 基准指数候选 (按优先级, 取第一个可用的)。
# 口径必须与个股 K 线副图一致 (``app.api.kline._BENCHMARK_INDEX_BY_EXCHANGE``):
# 沪用上证指数、深用深证成指、北用北证50; 这里是矩阵侧的同一份映射, 多一层回退
# (北证50 缺数据时退上证指数), 单测里有一条防漂移断言绑定两者的取值。
BENCHMARK_INDEX_BY_EXCHANGE: dict[str, tuple[str, ...]] = {
    "SH": ("000001.SH",),
    "SZ": ("399001.SZ",),
    "BJ": ("899050.BJ", "000001.SH"),
}

BENCHMARK_CLOSE_FIELD = "index_close"

_CACHE_TTL_SECONDS = 600.0
_cache: dict[str, tuple[float, pl.DataFrame | None]] = {}
_cache_lock = threading.Lock()


def exchange_of(symbol: str) -> str | None:
    """标的代码后缀 -> 交易所 (SH/SZ/BJ), 无法识别返回 None。"""
    suffix = str(symbol).rsplit(".", 1)[-1].upper()
    return suffix if suffix in BENCHMARK_INDEX_BY_EXCHANGE else None


def load_benchmark_closes(data_dir: Path | str | None = None) -> pl.DataFrame | None:
    """读取基准指数日线, 返回长表 ``exchange, date, close`` (每个交易日每所一行)。

    每个交易所取 :data:`BENCHMARK_INDEX_BY_EXCHANGE` 里第一个有数据的候选。
    读不到数据时返回 None (调用方置 NaN, 不阻塞策略)。进程内按 data_dir 缓存,
    TTL 见 ``_CACHE_TTL_SECONDS`` —— 指数日线每天只增一行, 不必每次重读。
    """
    directory = _resolve_data_dir(data_dir)
    key = str(directory)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    frame = _read_benchmark_closes(directory)
    with _cache_lock:
        _cache[key] = (now, frame)
    return frame


def _read_benchmark_closes(directory: Path) -> pl.DataFrame | None:
    index_glob = str(directory / "kline_index_daily" / "**" / "*.parquet")
    wanted = sorted(
        {symbol for candidates in BENCHMARK_INDEX_BY_EXCHANGE.values() for symbol in candidates}
    )
    frame: pl.DataFrame | None = None
    try:
        source = scan_daily_parquet(index_glob).filter(pl.col("symbol").is_in(wanted))
        available = source.select(pl.col("symbol").unique()).collect()["symbol"].to_list()
        if not available:
            return None
        available_set = set(available)
        picked: dict[str, str] = {}
        for exchange, candidates in BENCHMARK_INDEX_BY_EXCHANGE.items():
            hit = next((s for s in candidates if s in available_set), None)
            if hit is not None:
                picked[exchange] = hit
        if not picked:
            return None
        rows = (
            source.filter(pl.col("symbol").is_in(sorted(set(picked.values()))))
            .select(["symbol", "date", pl.col("close").cast(pl.Float64, strict=False)])
            .collect()
        )
        if rows.is_empty():
            return None
        mapping = pl.DataFrame(
            {
                "symbol": list(picked.values()),
                "exchange": list(picked.keys()),
            }
        )
        frame = (
            rows.join(mapping, on="symbol", how="inner")
            .select(["exchange", "date", "close"])
            .unique(subset=["exchange", "date"])
            .sort(["exchange", "date"])
        )
    except Exception:  # 基准数据缺失不应让策略失败
        return None
    return frame if not frame.is_empty() else None


def benchmark_close_matrix(
    trading_dates: Sequence[str],
    symbols: Sequence[str],
    *,
    data_dir: Path | str | None = None,
) -> np.ndarray:
    """把基准指数收盘价广播成 ``(len(trading_dates), len(symbols))`` 的 float32 矩阵。

    - 标的按后缀选指数 (SH -> 上证指数 / SZ -> 深证成指 / BJ -> 北证50);
      后缀无法识别 (港美股、指数自身) 的位置为 NaN。
    - ``trading_dates`` 是 ``YYYY-MM-DD`` 字符串, 应对应矩阵的日期轴。
    - **指数当天没有数据时向前沿用最近一根** (盘中当日行可能还没落盘, 严格丢空会让
      整个横截面被过滤光); 位于指数数据起点之前的位置仍为 NaN。
    """
    dates = _normalize_dates(trading_dates)
    assets = tuple(str(symbol) for symbol in symbols)
    out = np.full((dates.size, len(assets)), np.nan, dtype=np.float32)
    if dates.size == 0 or not assets:
        return out

    by_exchange: dict[int, list[int]] = {}
    for asset_id, symbol in enumerate(assets):
        exchange = exchange_of(symbol)
        if exchange is not None:
            by_exchange.setdefault(EXCHANGE_CODE[exchange], []).append(asset_id)
    if not by_exchange:
        return out

    frame = load_benchmark_closes(data_dir)
    if frame is None:
        return out

    for code, asset_ids in by_exchange.items():
        exchange = _EXCHANGE_BY_CODE.get(code)
        closes = _close_by_date(frame, exchange)
        if not closes:
            continue
        column = np.full(dates.size, np.nan, dtype=np.float32)
        last = np.nan
        for position, day in enumerate(dates):
            value = closes.get(day)
            if value is None:
                value = last
            else:
                last = value
            column[position] = value
        out[:, np.asarray(asset_ids, dtype=np.int64)] = column[:, None]
    return out


_EXCHANGE_BY_CODE: dict[int, str] = {code: name for name, code in EXCHANGE_CODE.items()}


def _close_by_date(frame: pl.DataFrame, exchange: str) -> dict[str, float]:
    rows = frame.filter(pl.col("exchange") == exchange)
    if rows.is_empty():
        return {}
    return {
        str(day): float(value)
        for day, value in zip(
            rows["date"].cast(pl.Utf8).to_list(),
            rows["close"].to_list(),
            strict=True,
        )
        if value is not None and np.isfinite(value)
    }


def _normalize_dates(trading_dates: Sequence[str]) -> np.ndarray:
    normalized: list[str] = []
    for value in trading_dates:
        if isinstance(value, date):
            normalized.append(value.isoformat())
        else:
            normalized.append(str(value)[:10])
    return np.asarray(normalized, dtype=object)


def reset_benchmark_cache() -> None:
    """清空进程内缓存 (测试用)。"""
    with _cache_lock:
        _cache.clear()


def _resolve_data_dir(data_dir: Path | str | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings

    return Path(settings.data_dir)
