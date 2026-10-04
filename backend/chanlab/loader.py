"""读取单标的 K 线: 日线走 v1 的 parquet, 分钟线走 free-stockdb.

v2 不复制 v1 的数据, 通过 TICKFLOW_DATA_DIR 共享同一份 data 目录,
避免重复同步与双份磁盘占用.

分钟线来自 free-stockdb 本地库 (leveldb, 通过 127.0.0.1:7899 访问,
需先启动 stockdb.exe)。它覆盖了 2025-01-02 起的 1m/5m/30m/60m, 比 v1
的 kline_minute 长得多, 是多级别联立能真正跑起来的前提.

⚠️ 量纲: 项目内部约定 volume=手 / amount=元。free-stockdb 的 volume 是
**股**, 实测与 v1 差整 100 倍且跨板块一致 (没有腾讯系那种分板块的坑),
所以这里统一 /100 换算成手。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import polars as pl

ENRICHED_SUBDIR = "kline_daily_enriched"
REQUIRED_COLUMNS = ("open", "high", "low", "close")
OPTIONAL_COLUMNS = ("volume", "amount", "turnover_rate")
DEFAULT_DATA_DIR = Path("D:/project/GP/tickflow-stock-panel/data")
DEFAULT_STOCKDB_SDK = Path("D:/MyDownload/free-stockdb-windows-v0.3.5-more-power/stockdb/pybao")

# v2 自有的全历史日线库 (见 backend/scripts/build_daily_store.py)
# 按 symbol 分区 —— 单标的只读一个文件, 比扫 v1 的上千个 date 分区快一到两个数量级
DEFAULT_OWN_DIR = Path(__file__).resolve().parents[2] / "data" / "kline_daily"

# free-stockdb 的周期名 -> 是否属于分钟线
MINUTE_FREQUENCIES = ("1m", "5m", "15m", "30m", "60m")
SHARES_PER_LOT = 100  # 股 -> 手

PathLike = str | Path

# 同一份 frame 在同一进程里可能被重复读很多次: 调参脚本要对一只票换十几套配置,
# 而每次 CChan 都会重新实例化数据源并重扫上千个分区文件. 加一层可选缓存把这块
# 从「每次几秒」降到「整轮一次」. 默认关闭, 只有显式开启才生效.
_FRAME_CACHE: dict[tuple, pl.DataFrame] = {}
_CACHE_ENABLED = os.environ.get("TICKFLOW_CACHE_FRAMES", "") == "1"


def enable_frame_cache(enabled: bool = True) -> None:
    """开关 frame 缓存. 批量任务开启后务必在结束时 clear_frame_cache() 释放."""
    global _CACHE_ENABLED
    _CACHE_ENABLED = enabled


def clear_frame_cache() -> None:
    """清空 frame 缓存并回收内存."""
    _FRAME_CACHE.clear()


def resolve_data_dir(data_dir: PathLike | None = None) -> Path:
    """数据目录优先级: 显式入参 > TICKFLOW_DATA_DIR > 本机默认路径."""
    if data_dir is not None:
        return Path(data_dir)
    env = os.environ.get("TICKFLOW_DATA_DIR")
    if env:
        return Path(env)
    return DEFAULT_DATA_DIR


def enriched_glob(data_dir: PathLike | None = None) -> str:
    """enriched 日线的 parquet glob (Hive 分区: date=YYYY-MM-DD)."""
    root = resolve_data_dir(data_dir)
    return str(root / ENRICHED_SUBDIR / "**" / "*.parquet")


def resolve_own_dir(own_dir: PathLike | None = None) -> Path:
    """自有日线库目录: 显式入参 > TICKFLOW_OWN_DAILY_DIR > 仓库默认位置."""
    if own_dir is not None:
        return Path(own_dir)
    env = os.environ.get("TICKFLOW_OWN_DAILY_DIR")
    if env:
        return Path(env)
    return DEFAULT_OWN_DIR


def own_store_path(symbol: str, own_dir: PathLike | None = None) -> Path:
    """自有库里单标的的 parquet 路径."""
    return resolve_own_dir(own_dir) / f"symbol={symbol}" / "data.parquet"


def _as_date(value: str | date) -> date:
    """把 YYYY-MM-DD 字符串或已有 date/datetime 归一成 date."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def load_symbol_daily_own(
    symbol: str,
    start: str | date | None = None,
    end: str | date | None = None,
    own_dir: PathLike | None = None,
    lookback: int | None = None,
) -> pl.DataFrame | None:
    """从自有全历史库读日线. 标的不在库里时返回 ``None`` (交给调用方兜底).

    列: ``date`` / ``open``..``close``(前复权, 基准=最新交易日) / ``volume``(手) /
    ``amount``(元) / ``raw_*``(不复权)。另有 ``quote_ts``。
    """
    path = own_store_path(symbol, own_dir)
    if not path.is_file():
        return None

    scan = pl.scan_parquet(path)
    if start is not None:
        scan = scan.filter(pl.col("date") >= _as_date(start))
    if end is not None:
        scan = scan.filter(pl.col("date") <= _as_date(end))
    df = scan.collect()
    if lookback is not None and df.height > lookback:
        df = df.tail(lookback)
    return df


def load_symbol_daily(
    symbol: str,
    start: str | date | None = None,
    end: str | date | None = None,
    data_dir: PathLike | None = None,
    lookback: int | None = None,
    source: str = "auto",
) -> pl.DataFrame:
    """读取单标的日线, 按日期升序, 已剔除空值与重复交易日.

    ``source``:
        ``auto``  先找自有全历史库, 没有再回落到 v1 的 enriched (推荐)
        ``own``   只用自有库, 没有就抛错 —— 用于「必须长历史」的场景
        ``v1``    只读 v1 enriched —— 用于对拍双方的旧口径
    """
    if source not in ("auto", "own", "v1"):
        raise ValueError(f"未知数据源 {source!r}, 可选 auto/own/v1")

    if _CACHE_ENABLED:
        key = (str(resolve_data_dir(data_dir)), symbol, str(start), str(end), lookback, source)
        cached = _FRAME_CACHE.get(key)
        if cached is not None:
            return cached

    if source != "v1":
        own = load_symbol_daily_own(symbol, start=start, end=end, lookback=lookback)
        if own is not None:
            own = own.drop_nulls(subset=list(REQUIRED_COLUMNS)).sort("date")
            if _CACHE_ENABLED:
                _FRAME_CACHE[key] = own
            return own
        if source == "own":
            raise FileNotFoundError(
                f"{symbol} 不在自有日线库: {own_store_path(symbol)}. "
                "先跑 backend/scripts/build_daily_store.py"
            )

    if _CACHE_ENABLED:
        key = (str(resolve_data_dir(data_dir)), symbol, str(start), str(end), lookback)

    scan = pl.scan_parquet(enriched_glob(data_dir)).filter(pl.col("symbol") == symbol)
    if start is not None:
        scan = scan.filter(pl.col("date") >= _as_date(start))
    if end is not None:
        scan = scan.filter(pl.col("date") <= _as_date(end))

    columns = ["date", *REQUIRED_COLUMNS, *OPTIONAL_COLUMNS]
    df = scan.select([pl.col(c) for c in columns if c in scan.collect_schema().names()]).collect()

    # 停牌或未完成的行 OHLC 可能为空, 直接丢弃而不是填 0
    df = df.drop_nulls(subset=list(REQUIRED_COLUMNS)).sort("date")

    # 同一交易日保留最后写入的那行. 不用 unique(keep="last"): maintain_order=False 时
    # "last" 取的是内部哈希顺序, 不保证是时间上靠后的那行.
    df = df.group_by("date", maintain_order=True).last()

    if lookback is not None and df.height > lookback:
        df = df.tail(lookback)

    if _CACHE_ENABLED:
        _FRAME_CACHE[key] = df
    return df


def load_symbol_turnover_rate(
    symbol: str,
    data_dir: PathLike | None = None,
) -> dict[date, float]:
    """读取单标的换手率序列 (百分数), 用于笔 silencing 之外的动力学判据."""
    df = load_symbol_daily(symbol, data_dir=data_dir)
    if "turnover_rate" not in df.columns:
        return {}
    return dict(zip(df["date"].to_list(), df["turnover_rate"].to_list(), strict=False))


# --------------------------------------------------------------------------
# free-stockdb (分钟线 / 长历史日线)
# --------------------------------------------------------------------------

_sdk_rd = None


def stockdb_code(symbol: str) -> str:
    """v1 的 ``600519.SH`` -> free-stockdb 的 ``600519``."""
    return symbol.split(".")[0]


def _stockdb():
    """惰性连上本地 stockdb 服务. 连不上时给出明确指引而不是抛裸异常."""
    global _sdk_rd
    if _sdk_rd is not None:
        return _sdk_rd

    sdk_dir = Path(os.environ.get("TICKFLOW_STOCKDB_SDK", DEFAULT_STOCKDB_SDK))
    if not sdk_dir.is_dir():
        raise RuntimeError(
            f"free-stockdb SDK 目录不存在: {sdk_dir}. "
            "可用 TICKFLOW_STOCKDB_SDK 指向 stockdb/pybao"
        )
    import sys

    if str(sdk_dir) not in sys.path:
        sys.path.insert(0, str(sdk_dir))

    from stock_sdk import init, rd

    init("127.0.0.1", 7899, warm=False)
    _sdk_rd = rd
    return rd


def _fmt_date(value: str | date | datetime | None) -> str | None:
    """转成 free-stockdb 的 ``YYYYMMDD``/``YYYYMMDDHHMMSS`` 整数字符串."""
    if value is None:
        return None
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits or None
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d%H%M%S")
    return value.strftime("%Y%m%d")


def _stockdb_bounds(
    start: str | date | datetime | None,
    end: str | date | datetime | None,
) -> tuple[str | None, str | None]:
    """free-stockdb 的 start/end 必须成对传.

    ⚠️ 实测: **只给 start 不给 end 时, 日线只返回 1 条、分钟线返回 0 条**。
    这不是超时也不是空数据, 很容易被误判成「这个标的没数据」。所以只要调用方
    给了任意一端, 另一端就用极值补齐; 两端都没给才走「全量」。
    """
    s = _fmt_date(start)
    e = _fmt_date(end)
    if s is None and e is None:
        return None, None
    if s is None:
        return "19700101", e
    if e is None:
        return s, "21000101"
    return s, e


def load_symbol_minute(
    symbol: str,
    freq: str = "30m",
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    lookback: int | None = None,
) -> pl.DataFrame:
    """读取分钟 K 线, 按时间升序.

    返回列: ``datetime`` / ``open`` / ``high`` / ``low`` / ``close`` /
    ``volume``(手) / ``amount``(元).
    """
    if freq not in MINUTE_FREQUENCIES:
        raise ValueError(f"不支持的分钟周期 {freq!r}, 可选 {MINUTE_FREQUENCIES}")

    if _CACHE_ENABLED:
        key = ("minute", symbol, freq, str(start), str(end), lookback)
        cached = _FRAME_CACHE.get(key)
        if cached is not None:
            return cached

    rd = _stockdb()
    s, e = _stockdb_bounds(start, end)
    try:
        rows = rd.get_data(
            stockdb_code(symbol),
            start=s,
            end=e,
            frequency=freq,
        ) or []
    except TypeError:
        # free-stockdb 的 _merge_minutes_to_period 里写的是
        # ``max(x['high'] for x in items if 'high' in x)`` —— 只判断键在不在,
        # 不判断值是不是 None, 所以原始分钟里只要有一根 OHLC 为 null 就会
        # 「TypeError: '>' not supported between NoneType and float」。
        # 这是上游 SDK 的坑且在进程内无法拦截(它在我们拿到数据之前就炸了),
        # 只能整只标的降级: 这类票的分钟数据本身就有洞, 拿它算缠论也没有意义。
        return pl.DataFrame()

    df = pl.DataFrame(
        {
            "ts": [int(r["date"]) for r in rows],
            "open": [float(r["open"]) for r in rows],
            "high": [float(r["high"]) for r in rows],
            "low": [float(r["low"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [float(r.get("volume") or 0) / SHARES_PER_LOT for r in rows],
            "amount": [float(r.get("amount") or 0) for r in rows],
        }
    )
    if not df.is_empty():
        df = df.with_columns(
            pl.col("ts").cast(pl.String).str.to_datetime("%Y%m%d%H%M%S").alias("datetime")
        ).drop("ts").select(
            ["datetime", "open", "high", "low", "close", "volume", "amount"]
        )
        df = df.drop_nulls(subset=list(REQUIRED_COLUMNS)).sort("datetime")

    if lookback is not None and df.height > lookback:
        df = df.tail(lookback)

    if _CACHE_ENABLED:
        _FRAME_CACHE[key] = df
    return df


def load_symbol_daily_stockdb(
    symbol: str,
    start: str | date | None = None,
    end: str | date | None = None,
    lookback: int | None = None,
) -> pl.DataFrame:
    """从 free-stockdb 读日线 (前复权).

    与 :func:`load_symbol_daily` 的区别是起点早得多 (多数标的到 2001 年),
    可用来补 v1 enriched 缺失的历史。实测两者收盘价偏差 < 0.1%,
    差异来自前复权基准日不同。
    """
    if _CACHE_ENABLED:
        key = ("daily_sd", symbol, str(start), str(end), lookback)
        cached = _FRAME_CACHE.get(key)
        if cached is not None:
            return cached

    rd = _stockdb()
    s, e = _stockdb_bounds(start, end)
    rows = rd.get_data(
        stockdb_code(symbol),
        start=s,
        end=e,
        frequency="1d",
    ) or []

    df = pl.DataFrame(
        {
            "date": [
                date(int(str(r["date"])[:4]), int(str(r["date"])[4:6]), int(str(r["date"])[6:8]))
                for r in rows
            ],
            "open": [float(r["open"]) for r in rows],
            "high": [float(r["high"]) for r in rows],
            "low": [float(r["low"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [float(r.get("volume") or 0) / SHARES_PER_LOT for r in rows],
            "amount": [float(r.get("amount") or 0) for r in rows],
        }
    )
    df = df.drop_nulls(subset=list(REQUIRED_COLUMNS)).sort("date")
    if lookback is not None and df.height > lookback:
        df = df.tail(lookback)

    if _CACHE_ENABLED:
        _FRAME_CACHE[key] = df
    return df
