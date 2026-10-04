"""从通达信本地 vipdoc 导入历史 1 分钟 K, 回填 data/kline_minute 缺失的日期分区.

背景
----
免费公开分钟接口(腾讯 mkline / 东财 / 新浪)只保留最近 2~5 个交易日, 无法回补
更早的历史. 通达信客户端会把最近约 23 个交易日(每文件封顶 5520 根)的 1 分钟
数据缓存在本地 vipdoc/{sh,sz,bj}/minline/*.lc1, 直接解码二进制即可, 不依赖
2026-09 起已被服务端改坏的那套网络协议.

用法
----
    # 干跑: 只解析 3 只票, 打印结果, 不写盘
    python backend/scripts/import_tdx_minute.py --dry-run --symbols 600519.SH,000001.SZ

    # 全量回填(只写不存在的日期分区)
    python backend/scripts/import_tdx_minute.py --start 2026-08-03 --end 2026-08-27

    # 覆盖已存在分区(慎用)
    python backend/scripts/import_tdx_minute.py --start 2026-08-03 --overwrite

二进制格式
----------
每条 32 字节, numpy dtype 见 _BAR_DTYPE:
    date(u2)  -> 年 = d // 2048 + 2004, 月 = (d % 2048) // 100, 日 = (d % 2048) % 100
    time(u2)  -> 当天分钟数(571 = 09:31, 900 = 15:00), 不是 HHMM
    o/h/l/c    float32 不复权原始价
    amount    float32 元
    volume    uint32  股 -> 除以 100 转手(对齐项目口径)
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import polars as pl

logger = logging.getLogger("import_tdx_minute")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TDX_ROOT = Path(r"D:\software\txd\vipdoc")
DEFAULT_MINUTE_DIR = REPO_ROOT / "data" / "kline_minute"

# 32 字节/条: date, time, open, high, low, close, amount, volume, reserved
_BAR_DTYPE = np.dtype([
    ("date", "<u2"),
    ("time", "<u2"),
    ("open", "<f4"),
    ("high", "<f4"),
    ("low", "<f4"),
    ("close", "<f4"),
    ("amount", "<f4"),
    ("volume", "<u4"),
    ("reserved", "<u4"),
])

# 市场目录 -> (符号后缀, A 股代码前缀白名单).
# vipdoc 里还混着指数(sh000001)/基金(sh5xxxxx)/债券(sh1xxxxx)/B股(sh900xxx),
# 用前缀白名单过滤, 避免把无关标的写进分钟库.
_MARKETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "sh": ("SH", ("60", "68")),
    "sz": ("SZ", ("00", "30")),
    "bj": ("BJ", ("43", "83", "87", "88", "92")),
}

_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
_FLUSH_EVERY = 200  # 每解析多少只票把缓冲刷到临时目录(控制内存峰值)


def decode_bars(raw: bytes, symbol: str) -> pl.DataFrame:
    """把单个 .lc1 文件解码成 canonical 列的 DataFrame."""
    usable = (len(raw) // 32) * 32
    if usable == 0:
        return pl.DataFrame()
    arr = np.frombuffer(raw[:usable], dtype=_BAR_DTYPE)
    if arr.size == 0:
        return pl.DataFrame()

    d = arr["date"].astype(np.int64)
    year = d // 2048 + 2004
    rest = d % 2048
    month = rest // 100
    day = rest % 100

    # 环形缓冲未写满时尾部是全 0 填充, 用合法日期过滤掉
    valid = (year >= 1990) & (year <= 2100) & (month >= 1) & (month <= 12) & (day >= 1) & (day <= 31)
    if not valid.all():
        arr = arr[valid]
        year = year[valid]
        month = month[valid]
        day = day[valid]
        if arr.size == 0:
            return pl.DataFrame()

    minute_of_day = arr["time"].astype(np.int64)
    # 自 1970-01-01 的天数(向量化, 无需逐个 datetime 构造)
    days = _days_from_civil(year, month, day)
    ts = days * 86400 + minute_of_day * 60

    return pl.DataFrame({
        "symbol": pl.Series([symbol] * arr.size, dtype=pl.String),
        "datetime": pl.from_numpy((ts * 1_000_000).astype("datetime64[us]")),
        "open": pl.from_numpy(arr["open"].astype(np.float64)),
        "high": pl.from_numpy(arr["high"].astype(np.float64)),
        "low": pl.from_numpy(arr["low"].astype(np.float64)),
        "close": pl.from_numpy(arr["close"].astype(np.float64)),
        "volume": pl.from_numpy(arr["volume"].astype(np.float64) / 100.0),
        "amount": pl.from_numpy(arr["amount"].astype(np.float64)),
    })


def _days_from_civil(year: np.ndarray, month: np.ndarray, day: np.ndarray) -> np.ndarray:
    """公历年月日 -> 自 1970-01-01 的天数(Howard Hinnant 算法, 向量化)."""
    y = year.astype(np.int64)
    m = month.astype(np.int64)
    d = day.astype(np.int64)
    y -= np.where(m <= 2, 1, 0)
    era = np.where(y >= 0, y, y - 399) // 400
    yoe = y - era * 400
    doy = (153 * np.where(m > 2, m - 3, m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def iter_lc1_files(tdx_root: Path, symbols: set[str] | None) -> list[tuple[str, Path]]:
    """枚举需要解析的 (symbol, 文件路径). symbols 非空时按其过滤."""
    out: list[tuple[str, Path]] = []
    for market, (suffix, prefixes) in _MARKETS.items():
        base = tdx_root / market / "minline"
        if not base.is_dir():
            logger.warning("目录不存在, 跳过: %s", base)
            continue
        for path in sorted(base.glob("*.lc1")):
            # 文件名形如 sh600519.lc1, 前 2 位是市场前缀
            code = path.stem[2:]
            if not code.startswith(prefixes):
                continue
            symbol = f"{code}.{suffix}"
            if symbols is not None and symbol not in symbols:
                continue
            out.append((symbol, path))
    return out


def _flush(buffer: dict[str, list[pl.DataFrame]], tmp_dir: Path) -> None:
    """把按日期累积的缓冲追加写入临时目录(每日期一个分片文件)."""
    for date_key, frames in buffer.items():
        if not frames:
            continue
        day_dir = tmp_dir / date_key
        day_dir.mkdir(parents=True, exist_ok=True)
        merged = pl.concat(frames) if len(frames) > 1 else frames[0]
        idx = len(list(day_dir.glob("*.parquet")))
        merged.write_parquet(day_dir / f"chunk_{idx:05d}.parquet")
    buffer.clear()


def run(args: argparse.Namespace) -> int:
    tdx_root = Path(args.tdx_root)
    minute_dir = Path(args.minute_dir)
    if not tdx_root.is_dir():
        logger.error("通达信 vipdoc 目录不存在: %s", tdx_root)
        return 2

    wanted = set(args.symbols.split(",")) if args.symbols else None
    files = iter_lc1_files(tdx_root, wanted)
    if not files:
        logger.error("没有匹配的 .lc1 文件")
        return 2
    logger.info("待解析文件: %d 个 (源目录 %s)", len(files), tdx_root)

    start_day = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end_day = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    if args.dry_run:
        return _dry_run(files, start_day, end_day)

    if args.stats:
        return _stats(files, start_day, end_day)

    return _import(files, minute_dir, start_day, end_day, args.overwrite)


def _stats(files: list[tuple[str, Path]], start_day, end_day) -> int:
    """只汇总不写盘: 区间内标的数量 / 总行数 / 每天标的数, 用于评估导入规模."""
    per_day: dict[str, int] = {}
    total_rows = 0
    symbols: set[str] = set()
    for symbol, path in files:
        df = decode_bars(path.read_bytes(), symbol)
        if df.is_empty():
            continue
        df = _clip(df, start_day, end_day)
        if df.is_empty():
            continue
        symbols.add(symbol)
        total_rows += df.height
        for d in df["datetime"].dt.date().unique().to_list():
            key = str(d)
            per_day[key] = per_day.get(key, 0) + 1
    logger.info("=== STATS ===")
    logger.info("区间内标的数量: %d, 总行数: %d", len(symbols), total_rows)
    if total_rows:
        logger.info("预估落盘体积: %.0f MB (按 10.3 B/row)", total_rows * 10.3 / 1e6)
    for day in sorted(per_day):
        logger.info("  %s  标的 %d", day, per_day[day])
    return 0


def _dry_run(files: list[tuple[str, Path]], start_day, end_day) -> int:
    logger.info("=== DRY RUN (不写盘) ===")
    total = 0
    for symbol, path in files:
        raw = path.read_bytes()
        df = decode_bars(raw, symbol)
        if df.is_empty():
            logger.warning("  %s 解析为空", symbol)
            continue
        df = _clip(df, start_day, end_day)
        if df.is_empty():
            logger.info("  %s 在指定区间内无数据", symbol)
            continue
        dates = df["datetime"].dt.date().unique().sort()
        total += df.height
        logger.info(
            "  %s rows=%d 日期 %s ~ %s (%d 天) 首根=%s 末根=%s",
            symbol, df.height, dates.min(), dates.max(), len(dates),
            df["datetime"].min(), df["datetime"].max(),
        )
        logger.info("     样本: %s", df.head(2).to_dicts())
    logger.info("=== DRY RUN 合计 %d 行 ===", total)
    return 0


def _clip(df: pl.DataFrame, start_day, end_day) -> pl.DataFrame:
    day = df["datetime"].dt.date()
    if start_day is not None:
        df = df.filter(day >= start_day)
    if end_day is not None:
        df = df.filter(day <= end_day)
    return df


def _import(
    files: list[tuple[str, Path]],
    minute_dir: Path,
    start_day,
    end_day,
    overwrite: bool,
) -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="tdx_minute_"))
    buffer: dict[str, list[pl.DataFrame]] = {}
    parsed = 0
    try:
        for symbol, path in files:
            df = decode_bars(path.read_bytes(), symbol)
            if not df.is_empty():
                df = _clip(df, start_day, end_day)
            if df.is_empty():
                continue
            df = df.with_columns(pl.col("datetime").dt.date().alias("_d"))
            for day_df in df.partition_by("_d"):
                key = str(day_df["_d"][0])
                buffer.setdefault(key, []).append(day_df.drop("_d"))
            parsed += 1
            if parsed % _FLUSH_EVERY == 0:
                _flush(buffer, tmp_root)
                logger.info("  已解析 %d/%d 只票", parsed, len(files))
        _flush(buffer, tmp_root)
        logger.info("解析完成: %d 只票有数据, 临时目录 %s", parsed, tmp_root)

        total_written = 0
        for day_dir in sorted(tmp_root.iterdir()):
            date_key = day_dir.name
            out = minute_dir / f"date={date_key}" / "part.parquet"
            if out.exists() and not overwrite:
                logger.info("  跳过 %s (分区已存在)", date_key)
                continue
            chunks = [pl.read_parquet(p) for p in sorted(day_dir.glob("*.parquet"))]
            merged = pl.concat(chunks) if len(chunks) > 1 else chunks[0]
            merged = merged.select(_CANONICAL).unique(
                subset=["symbol", "datetime"], keep="last",
            ).sort("symbol", "datetime")
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".tmp")
            merged.write_parquet(tmp)
            tmp.replace(out)
            total_written += merged.height
            logger.info("  写入 date=%s rows=%d symbols=%d", date_key, merged.height,
                        merged["symbol"].n_unique())
        logger.info("=== 导入完成, 共写入 %d 行 ===", total_written)
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="从通达信本地 vipdoc 导入历史 1 分钟 K")
    parser.add_argument("--tdx-root", default=str(DEFAULT_TDX_ROOT))
    parser.add_argument("--minute-dir", default=str(DEFAULT_MINUTE_DIR))
    parser.add_argument("--symbols", default="", help="逗号分隔, 如 600519.SH,000001.SZ")
    parser.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true", help="只汇总规模, 不写盘")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在分区")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
