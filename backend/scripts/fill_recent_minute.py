"""用现役腾讯 mkline 插件补齐最近交易日的分钟数据.

用途: 当天同步中途被打断(服务崩溃 / 关电脑)后, 当天分区只有半截数据,
而腾讯 mkline 的滚动窗口有约 2 个交易日, 当天数据还在窗口内, 可以直接补满.

与 import_tdx_minute.py 的区别:
  - import_tdx_minute : 从通达信本地二进制补**更早**的历史(源自己会过期)
  - 本脚本            : 从腾讯网络源补**最近**的几天(受滚动窗口限制)

用法:
    python backend/scripts/fill_recent_minute.py --dry-run
    python backend/scripts/fill_recent_minute.py --dates 2026-09-28
    python backend/scripts/fill_recent_minute.py          # 自动取最近交易日
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

logger = logging.getLogger("fill_recent_minute")

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
_CN_SUFFIXES = (".SH", ".SZ", ".BJ")

sys.path.insert(0, str(REPO_ROOT / "backend"))


def load_symbols(limit: int = 0, include_bj: bool = False) -> list[str]:
    """取全 A 标的列表(按 instruments 里的 A 股后缀过滤)。

    默认**排除北交所**: 腾讯 mkline 不支持北交所, 而 tencent/provider.py 的
    ``_bj_fallback`` 会用已失效的东财 stock-sdk 去补, 每批必然打满 180s 超时
    (347 只 / 40 一批 ≈ 9 批 ≈ 27 分钟), 白白拖垮主链路。
    """
    con = duckdb.connect()
    rows = con.execute(
        "SELECT DISTINCT symbol FROM read_parquet(?) WHERE symbol IS NOT NULL",
        [(DATA_DIR / "instruments" / "**" / "*.parquet").as_posix()],
    ).fetchall()
    con.close()
    syms = sorted({r[0] for r in rows if str(r[0]).endswith(_CN_SUFFIXES)})
    if not include_bj:
        syms = [s for s in syms if not s.upper().endswith(".BJ")]
    return syms[:limit] if limit else syms


def sanitize_day(day: str, minute_dir: Path) -> tuple[int, int]:
    """清理指定分区里违反 OHLC 关系的脏行(保留前 / 清理后行数)。

    脏数据来源: stock-sdk(东财)失效期的产物 —— 典型症状是 open 被填成某个
    固定值、以及 688/300 板块 volume 被放大 100 倍(对日 K 偏差恰好 9900%)。
    这类行会让分时图与回测失真, 宁缺勿脏。
    """
    out = minute_dir / f"date={day}" / "part.parquet"
    if not out.exists():
        return (0, 0)
    df = pl.read_parquet(out)
    clean = df.filter(
        (pl.col("high") >= pl.max_horizontal("open", "close"))
        & (pl.col("low") <= pl.min_horizontal("open", "close"))
        & (pl.col("low") > 0)
        & (pl.col("volume") >= 0)
        & (pl.col("amount") >= 0)
    )
    if clean.height == df.height:
        return (df.height, df.height)
    _atomic_write(clean, out)
    return (df.height, clean.height)


def _atomic_write(df: pl.DataFrame, out: Path) -> None:
    tmp = out.with_name(out.name + ".tmp")
    df.write_parquet(tmp)
    tmp.replace(out)


def write_days(df: pl.DataFrame, dates: set[str], minute_dir: Path, overwrite: bool) -> int:
    """按日期写分区: 读旧 -> concat -> unique(symbol,datetime,keep=last) -> 原子写。"""
    if df.is_empty():
        return 0
    df = df.with_columns(pl.col("datetime").dt.date().cast(pl.String).alias("_d"))
    written = 0
    for day_df in df.partition_by("_d"):
        key = str(day_df["_d"][0])
        if key not in dates:
            continue
        out = minute_dir / f"date={key}" / "part.parquet"
        day_df = day_df.drop("_d")
        if out.exists():
            exist = pl.read_parquet(out)
            if overwrite:
                logger.info("  覆盖 %s", key)
            else:
                # ⚠️ 不能写 unique(subset=[symbol,datetime], keep="last"):
                # polars 在 maintain_order=False 时 "last" 取的是内部哈希顺序下的最后一行,
                # 不保证是 concat 后靠后的新数据 —— 实测会把旧的脏行留下来、新数据被丢掉。
                # 用 anti-join 显式剔除被新数据覆盖的旧行。
                key_cols = ["symbol", "datetime"]
                exist = exist.join(
                    day_df.select(key_cols).unique(), on=key_cols, how="anti",
                )
                day_df = pl.concat([exist, day_df])
        day_df = day_df.sort("symbol", "datetime")
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(day_df, out)
        written += day_df.height
        logger.info("  写入 date=%s rows=%d symbols=%d", key, day_df.height,
                    day_df["symbol"].n_unique())
    return written


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default="", help="逗号分隔的目标日期, 留空自动取最近交易日")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只标的(调试用)")
    ap.add_argument("--include-bj", action="store_true", help="含北交所(会走已失效的东财回落)")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--sanitize", default="",
                    help="只清理指定日期分区的脏行(逗号分隔), 不拉取不补数据")
    args = ap.parse_args()

    minute_dir = Path(args.data_dir) / "kline_minute"
    if args.sanitize:
        for day in args.sanitize.split(","):
            before, after = sanitize_day(day.strip(), minute_dir)
            logger.info("  清理 %s: %d -> %d (删除 %d 行脏数据)",
                        day.strip(), before, after, before - after)
        return 0

    from app.services.kline_sync import _try_custom_minute  # 项目内导入

    syms = load_symbols(args.limit, args.include_bj)
    if not syms:
        logger.error("取不到标的列表")
        return 2
    logger.info("标的数: %d", len(syms))

    end = datetime.now()
    start = end - timedelta(days=7)
    t0 = datetime.now()
    df, fallback = _try_custom_minute(syms, start, end, "stock", freq="1m")
    if df.is_empty():
        logger.error("拉取为空 (fallback=%s)", fallback)
        return 2
    logger.info("拉取 %d 行, %.1fs (fallback=%s)", df.height,
                (datetime.now() - t0).total_seconds(), fallback)

    got_days = sorted({str(d) for d in df["datetime"].dt.date().unique().to_list()})
    logger.info("源返回日期: %s", got_days)
    target = set(args.dates.split(",")) if args.dates else {got_days[-1]}
    logger.info("目标日期: %s", sorted(target))

    if args.dry_run:
        logger.info("腾讯窗口实际可得(每日):")
        stats = (
            df.with_columns(pl.col("datetime").dt.date().cast(pl.String).alias("day"))
            .group_by("day")
            .agg(pl.len().alias("rows"), pl.col("symbol").n_unique().alias("syms"))
            .sort("day")
        )
        for row in stats.iter_rows(named=True):
            mark = " <= 目标" if row["day"] in target else ""
            logger.info("  %s  rows=%-8d symbols=%d%s",
                        row["day"], row["rows"], row["syms"], mark)
        logger.info("DRY RUN 结束, 未写盘")
        return 0

    minute_dir = Path(args.data_dir) / "kline_minute"
    written = write_days(df, target, minute_dir, args.overwrite)
    logger.info("完成, 共写入 %d 行", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
