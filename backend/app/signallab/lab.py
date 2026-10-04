"""Signal Lab 复盘运行器与台账仓储。

把 ``scripts/run_signal_lab.py`` 里的编排逻辑收进 app 层, 让 API 与命令行脚本共用同一份
实现 —— 口径只有一处定义, 不会两边漂移。

三段职责
--------
1. :func:`run_lab` — 跑一次复盘: 策略矩阵信号 -> 事件台账(含形态特征)。
2. 台账仓储 — 落盘到 ``data/signal_lab/<strategy>/events_<start>_<end>.parquet``,
   供 API 反复查询(复盘很贵, 查询必须便宜)。
3. :func:`attach_context_features` — 给台账补「信号当时已知」的形态特征,
   归因分析必须依赖这些列; 严禁把 mfe/mae/ret_* 当特征(那是未来信息)。

形态特征的口径
--------------
全部只取信号日 t 及之前的数据: 距 60 日高点的回撤、距 60 日低点的涨幅、20 日量比、
14 日 ATR%、相对 MA20 的乖离。它们描述「信号出现时这只票处于什么状态」,
正是「哪些形态的信号更好」这个问题需要的自变量。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine
from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    slice_market_data_matrix,
    slice_signal_matrix,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.signallab.outcome import (
    DEFAULT_HORIZONS,
    OutcomeConfig,
    build_signal_outcomes,
    compute_market_baseline,
)
from app.strategy.engine import StrategyEngine
from app.strategy.monitor import _SIGNAL_CN as SIGNAL_CN
from app.strategy.scoring import effective_scoring, effective_scoring_directions

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], None]

# 台账文件名: events_<start>_<end>.parquet
_DATASET_RE = re.compile(
    r"^events_(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.parquet$"
)

# 形态特征窗口(交易日)。改动会影响已落盘台账的列含义, 必须同步 bump 版本号。
_FEAT_HIGH_LOW_WINDOW = 60
_FEAT_VOL_WINDOW = 20
_FEAT_ATR_WINDOW = 14
_FEAT_MA_WINDOW = 20

# 信号时点可知的形态特征列(与 attach_context_features 的产出一一对应)
CONTEXT_FEATURES: tuple[str, ...] = (
    "ctx_drawdown_from_high",   # 距 60 日高点的回撤(负数=回调中)
    "ctx_rally_from_low",       # 距 60 日低点的涨幅(正数=已反弹)
    "ctx_vol_ratio",            # 成交量 / 20 日均量
    "ctx_atr_pct",              # 14 日 ATR / 收盘价(波动率)
    "ctx_ma_bias",              # 收盘价 / MA20 - 1(趋势乖离)
)

# 归因默认特征: 信号分支 + 形态。entry_signal_name 是字符串, 按取值分组而非分位。
DEFAULT_ATTRIBUTION_FEATURES: tuple[str, ...] = ("entry_signal_name", *CONTEXT_FEATURES)


@dataclass(frozen=True)
class LabRunConfig:
    """一次复盘的全部入参。所有会改变结果的维度都在这里显式声明。"""

    strategy_id: str
    start: date
    end: date
    symbols: tuple[str, ...] | None = None
    limit: int = 300                       # 未指定 symbols 时按字典序取前 N 只
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    entry_delay: int = 1
    max_delay_days: int = 3
    stop_loss: float | None = None
    take_profit: float | None = None
    drop_warmup: int | None = None         # None = 用策略声明的 warmup
    matrix_cache_mb: int = 768
    with_features: bool = True
    write: bool = True                     # False = 只在内存里算(脚本 --no-write)

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ValueError("strategy_id 不能为空")
        if self.end < self.start:
            raise ValueError(f"end({self.end}) 不能早于 start({self.start})")
        horizons = tuple(sorted({int(h) for h in self.horizons}))
        if not horizons or any(h <= 0 for h in horizons):
            raise ValueError(f"horizons 必须为正整数, 收到 {self.horizons}")
        object.__setattr__(self, "horizons", horizons)

    @property
    def dataset_name(self) -> str:
        return f"events_{self.start.isoformat()}_{self.end.isoformat()}.parquet"


# ===== 形态特征 =====


def _rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    """沿 axis=0 的滚动最大值, 忽略 NaN。

    窗口循环而不是 sliding_window_view: 后者会物化 (rows, window, cols) 的临时数组,
    全市场(400 交易日 x 5000 标的)下 60 窗口就是 960MB, 必炸。循环只多持有一份同形数组。
    """
    out = np.where(np.isfinite(values), values, -np.inf)
    for shift in range(1, window):
        padded = np.empty_like(out)
        padded[:shift] = -np.inf
        padded[shift:] = out[:-shift]
        np.maximum(out, padded, out=out)
    return np.where(np.isfinite(out), out, np.nan)


def _rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    out = np.where(np.isfinite(values), values, np.inf)
    for shift in range(1, window):
        padded = np.empty_like(out)
        padded[:shift] = np.inf
        padded[shift:] = out[:-shift]
        np.minimum(out, padded, out=out)
    return np.where(np.isfinite(out), out, np.nan)


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """沿 axis=0 的滚动均值, NaN 不参与分子分母(停牌日既不加和也不计数)。"""
    finite = np.isfinite(values)
    filled = np.where(finite, values, 0.0)
    counts = finite.astype(np.float64)
    cum_sum = np.vstack([np.zeros((1, values.shape[1])), np.cumsum(filled, axis=0)])
    cum_cnt = np.vstack([np.zeros((1, values.shape[1])), np.cumsum(counts, axis=0)])
    window_sum = cum_sum[window:] - cum_sum[:-window]
    window_cnt = cum_cnt[window:] - cum_cnt[:-window]
    out = np.full(values.shape, np.nan, dtype=np.float64)
    enough = window_cnt > 0
    out[window - 1:] = np.where(
        enough, window_sum / np.where(enough, window_cnt, 1.0), np.nan
    )
    return out


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.vstack([np.full((1, close.shape[1]), np.nan), close[:-1]])
    return np.fmax(
        np.fmax(high - low, np.abs(high - prev_close)),
        np.abs(low - prev_close),
    )


def attach_context_features(
    frame: pl.DataFrame,
    market: Any,
    entry: np.ndarray,
) -> pl.DataFrame:
    """给台账补形态特征列。

    Args:
        frame: ``build_signal_outcomes`` 的产出, 行序必须等于 ``np.nonzero(entry)``。
        market: ``MarketDataMatrix``, 提供 OHLCV。
        entry: 与生成台账时**同一个**布尔矩阵(预热置零后的版本)。

    Returns:
        补了 :data:`CONTEXT_FEATURES` 列的新 DataFrame。形状对不上时原样返回
        (宁可缺特征也不要错行 —— 特征错位会让归因结论完全失真)。
    """
    mask = np.asarray(entry).astype(bool)
    rows, cols = np.nonzero(mask)
    if rows.size == 0 or frame.height != rows.size:
        return frame

    close = np.asarray(market.close, dtype=np.float64)
    high = np.asarray(market.high, dtype=np.float64)
    low = np.asarray(market.low, dtype=np.float64)
    volume = np.asarray(market.volume, dtype=np.float64)

    window_high = _rolling_max(high, _FEAT_HIGH_LOW_WINDOW)
    window_low = _rolling_min(low, _FEAT_HIGH_LOW_WINDOW)
    mean_volume = _rolling_mean(volume, _FEAT_VOL_WINDOW)
    atr = _rolling_mean(_true_range(high, low, close), _FEAT_ATR_WINDOW)
    ma20 = _rolling_mean(close, _FEAT_MA_WINDOW)

    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = close / window_high - 1.0
        rally = close / window_low - 1.0
        vol_ratio = volume / mean_volume
        atr_pct = atr / close
        ma_bias = close / ma20 - 1.0

    data = {
        "ctx_drawdown_from_high": drawdown[rows, cols],
        "ctx_rally_from_low": rally[rows, cols],
        "ctx_vol_ratio": vol_ratio[rows, cols],
        "ctx_atr_pct": atr_pct[rows, cols],
        "ctx_ma_bias": ma_bias[rows, cols],
    }
    return frame.with_columns(
        [
            pl.Series(name, np.where(np.isfinite(values), values, np.nan), dtype=pl.Float64)
            for name, values in data.items()
        ]
    )


def signal_name_column(entry_signal_code: pl.Series, signal_ids: Sequence[str]) -> pl.Series:
    """把信号下标列翻成中文名列(找不到映射时退回 ``signal#i``)。"""
    if not signal_ids:
        return pl.Series("entry_signal_name", [None] * entry_signal_code.len(), dtype=pl.Utf8)
    names = [SIGNAL_CN.get(str(sid), str(sid)) for sid in signal_ids]
    codes = entry_signal_code.to_list()
    out: list[str | None] = []
    for code in codes:
        if code is None:
            out.append(None)
            continue
        index = int(code)
        out.append(names[index] if 0 <= index < len(names) else f"signal#{index}")
    return pl.Series("entry_signal_name", out, dtype=pl.Utf8)


# ===== 复盘运行 =====


def _resolve_warmup(strategy_engine: StrategyEngine, strategy_id: str, strategy: Any,
                    params: Mapping[str, Any]) -> int:
    """策略声明的预热根数(拿不到就退回 0, 由调用方按矩阵前置历史补差额)。"""
    try:
        value = strategy_engine.required_history_bars(
            [strategy_id], params_map={strategy_id: dict(params)}
        )
    except Exception:
        # 依赖链较深(复合策略/自定义), 拿不到就退回策略自报的 warmup, 再不行 0。
        logger.debug("required_history_bars 不可用, 退回策略声明值", exc_info=True)
        value = None
    if not value:
        value = int(getattr(strategy, "warmup_bars", 0) or 0)
    return max(0, int(value))


def run_lab(
    repo: Any,
    strategy_engine: StrategyEngine,
    config: LabRunConfig,
    *,
    data_dir: Path | None = None,
    on_progress: ProgressFn | None = None,
) -> pl.DataFrame:
    """跑一次信号复盘, 返回事件台账(每行一条信号)。

    口径与 ``scripts/run_signal_lab.py`` 完全一致(信号矩阵 -> T+1 开盘成交 -> 多持有期收益)。

    预热(重要)
    ----------
    ``prepare_matrix_optimization`` 已把数据起点提前到 ``max(120, warmup*1.6)`` 个自然日,
    信号是在**含这段前置历史**的完整矩阵上算的, 所以正式窗口首行的指标通常已经收敛。
    早期版本无条件再丢 ``warmup`` 行, 等于把评估窗口吃掉大半(实测 145 个交易日只留 24 个,
    信号数 65 -> 11)。现在改为**只在前置历史不足时补丢差额行**:
    ``cut = max(0, warmup - 矩阵中窗口之前的行数)``, ``drop_warmup`` 可再强制加码。
    """
    def progress(stage: str, pct: int, msg: str) -> None:
        if on_progress is not None:
            on_progress(stage, pct, msg)

    strategy = strategy_engine.get(config.strategy_id)
    if getattr(strategy, "execution_backend", None) != "matrix_native":
        raise ValueError(
            f"策略 {config.strategy_id} 不是 matrix_native, Signal Lab 只支持矩阵原生策略"
        )

    progress("init", 5, f"解析策略 {config.strategy_id}")
    params = StrategyEngine.resolve_params(strategy)
    symbols = list(config.symbols) if config.symbols else None

    backtest_config = StrategyBacktestConfig(
        strategy_id=config.strategy_id,
        symbols=symbols,
        start=config.start,
        end=config.end,
        params=params,
    )
    service = StrategyBacktestService(BacktestEngine(repo), strategy_engine)

    progress("matrix", 15, "装载行情矩阵")
    prepared = service.prepare_matrix_optimization(
        [backtest_config],
        matrix_cache_max_bytes=int(config.matrix_cache_mb) * 1024 * 1024,
    )
    try:
        pipeline_config = MatrixPipelineConfig(
            basic_filter=StrategyBacktestService._effective_basic_filter(strategy, {}),
            scoring=effective_scoring(strategy.meta.get("scoring"), {}),
            scoring_directions=effective_scoring_directions({}),
            order_by=strategy.meta.get("order_by"),
            descending=bool(strategy.meta.get("descending", True)),
        )
        progress("signals", 35, "计算全区间信号")
        with prepared.compute_cache.activate(prepared.market_data):
            signals = MatrixStrategyPipeline().run(
                strategy.matrix_strategy,
                prepared.market_data,
                params,
                pipeline_config,
            )
        market = slice_market_data_matrix(prepared.market_data, prepared.start_id, prepared.stop_id)
        window = slice_signal_matrix(signals, prepared.start_id, prepared.stop_id)

        warmup = _resolve_warmup(strategy_engine, config.strategy_id, strategy, params)
        entry = np.asarray(window.entry).astype(bool).copy()
        # 窗口之前已有多少行历史(信号就是在含它的完整矩阵上算的)
        pre_warm = int(getattr(prepared, "start_id", 0) or 0)
        cut = max(warmup - pre_warm, int(config.drop_warmup or 0), 0)
        if cut:
            progress("warmup", 60, f"丢弃预热不足的 {cut} 行(已有前置 {pre_warm} 行/需 {warmup} 行)")
            if cut < entry.shape[0]:
                entry[:cut, :] = False
            else:
                entry[:, :] = False

        signal_ids = tuple(getattr(window, "entry_signal_ids", ()) or ())

        progress("outcomes", 65, "展开信号台账")
        baseline = compute_market_baseline(market, horizons=config.horizons)
        frame = build_signal_outcomes(
            market,
            entry,
            exit_signals=window.exit,
            entry_signal_code=window.entry_signal_code,
            baseline=baseline,
            config=OutcomeConfig(
                horizons=config.horizons,
                entry_delay=config.entry_delay,
                max_delay_days=config.max_delay_days,
                stop_loss=config.stop_loss,
                take_profit=config.take_profit,
            ),
        )
        if config.with_features and not frame.is_empty():
            progress("features", 85, "计算形态特征")
            frame = attach_context_features(frame, market, entry)
        if not frame.is_empty() and "entry_signal_code" in frame.columns:
            frame = frame.with_columns(
                signal_name_column(frame["entry_signal_code"], signal_ids)
            )

        if config.write and data_dir is not None and not frame.is_empty():
            progress("write", 95, "落盘")
            save_ledger(frame, data_dir, config.strategy_id, config.start, config.end)
        progress("done", 100, f"完成, 共 {frame.height} 条信号")
        return frame
    finally:
        prepared.compute_cache.close()


# ===== 台账仓储 =====


def dataset_dir(data_dir: Path, strategy_id: str) -> Path:
    return Path(data_dir) / "signal_lab" / strategy_id


def dataset_path(data_dir: Path, strategy_id: str, start: date, end: date) -> Path:
    return dataset_dir(data_dir, strategy_id) / f"events_{start}_{end}.parquet"


def save_ledger(frame: pl.DataFrame, data_dir: Path, strategy_id: str,
                start: date, end: date) -> Path:
    path = dataset_path(data_dir, strategy_id, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return path


def _dataset_meta(path: Path) -> dict[str, Any]:
    match = _DATASET_RE.match(path.name)
    start = end = None
    if match:
        start = match.group("start")
        end = match.group("end")
    meta: dict[str, Any] = {
        "strategy_id": path.parent.name,
        "start": start,
        "end": end,
        "path": str(path),
        "rows": None,
        "mtime": None,
        "size_bytes": None,
    }
    try:
        stat = path.stat()
        meta["mtime"] = stat.st_mtime
        meta["size_bytes"] = stat.st_size
    except OSError:
        return meta
    try:
        import pyarrow.parquet as pq

        meta["rows"] = int(pq.ParquetFile(path).metadata.num_rows)
    except Exception:
        logger.debug("读取台账行数失败: %s", path, exc_info=True)
    return meta


def list_datasets(data_dir: Path, strategy_id: str | None = None) -> list[dict[str, Any]]:
    """列出已落盘的台账数据集, 按修改时间从新到旧。"""
    root = Path(data_dir) / "signal_lab"
    if not root.exists():
        return []
    targets = [root / strategy_id] if strategy_id else [p for p in root.iterdir() if p.is_dir()]
    items: list[dict[str, Any]] = []
    for folder in targets:
        if not folder.is_dir():
            continue
        for path in folder.glob("events_*.parquet"):
            items.append(_dataset_meta(path))
    items.sort(key=lambda item: (item.get("mtime") or 0.0), reverse=True)
    return items


def resolve_dataset(data_dir: Path, strategy_id: str,
                    start: date | None = None, end: date | None = None) -> Path | None:
    """定位台账文件: 给了 start/end 就精确匹配, 否则取该策略最新的一份。"""
    if start is not None and end is not None:
        path = dataset_path(data_dir, strategy_id, start, end)
        return path if path.exists() else None
    candidates = [
        item for item in list_datasets(data_dir, strategy_id=strategy_id)
        if (start is None or item.get("start") == start.isoformat())
        and (end is None or item.get("end") == end.isoformat())
    ]
    if not candidates:
        return None
    return Path(candidates[0]["path"])


def load_ledger(data_dir: Path, strategy_id: str,
                start: date | None = None, end: date | None = None) -> pl.DataFrame | None:
    path = resolve_dataset(data_dir, strategy_id, start, end)
    if path is None:
        return None
    return pl.read_parquet(path)
