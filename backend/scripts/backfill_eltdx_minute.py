"""用 eltdx 回补 1 分钟 K 历史分区。

背景
----
8 月之后的 1m 已通过通达信 vipdoc 导入; 09-29/09-30 通过 eltdx 实时同步写盘。
05-20 ~ 08-02 这一段只能靠 eltdx 的 bars.get(period='1m', anchor_date=...) 回补。

用法
----
    # 干跑一天, 只看规模与字段
    python backend/scripts/backfill_eltdx_minute.py --start 2026-05-20 --end 2026-05-20 --dry-run

    # 回补整个区间(跳过已存在分区, 默认安全)
    python backend/scripts/backfill_eltdx_minute.py --start 2026-05-20 --end 2026-08-02 --workers 4

    # 强制覆盖已存在分区(慎用)
    python backend/scripts/backfill_eltdx_minute.py --start 2026-05-20 --end 2026-08-02 --overwrite

注意
----
- 必须在**后端未运行**时执行, 否则与 _write_minute_partition 写竞争会覆盖掉分区。
- 本脚本用 Anaconda Python(3.13) 跑, 因为 backend/.venv(3.11) 未安装 eltdx。
- eltdx 要求代码带市场前缀(sh600519/sz000001/bj920xxx)。
"""
from __future__ import annotations

import argparse
import logging
import sys
import sysconfig
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic

import polars as pl

logger = logging.getLogger("backfill_eltdx_minute")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MINUTE_DIR = REPO_ROOT / "data" / "kline_minute"
DEFAULT_DAILY_DIR = REPO_ROOT / "data" / "kline_daily"

# 实测 500 只/批稳定, 0.5~0.7s; 再大性价比下降
_BATCH_SIZE = 500
# 单批失败重试
_MAX_RETRIES = 3


def _ensure_canonical_eltdx() -> None:
    """剔除 yanwen/GP/eltdx 兼容层, 并把用户 site-packages 置顶, 确保 import 到真包。"""
    # 1. 任何包含 yanwen 的路径都可能是兼容层, 全部剔除
    for p in list(sys.path):
        if "yanwen" in p.lower():
            sys.path.remove(p)
            logger.warning("从 sys.path 剔除兼容层: %s", p)
    # 2. 把 Python 3.13 用户 site-packages 置顶, 真包优先
    user_site = sysconfig.get_path("purelib")
    if user_site and Path(user_site).exists():
        if user_site in sys.path:
            sys.path.remove(user_site)
        sys.path.insert(0, user_site)
        logger.debug("置顶用户 site-packages: %s", user_site)


def _symbol_to_eltdx_code(symbol: str) -> str | None:
    """600519.SH -> sh600519; 000001.SZ -> sz000001; 920001.BJ -> bj920001."""
    if "." not in symbol:
        return None
    code, suffix = symbol.split(".", 1)
    mapping = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
    prefix = mapping.get(suffix.upper())
    return f"{prefix}{code}" if prefix else None


def _load_symbols(daily_dir: Path) -> list[str]:
    """从日线 parquet 取最新一个交易日的全部 symbol。"""
    dirs = sorted(daily_dir.glob("date=*"))
    if not dirs:
        raise RuntimeError("没有日线数据, 无法确定标的列表")
    latest = dirs[-1]
    logger.info("从 %s 读取标的列表", latest)
    df = pl.read_parquet(latest / "*.parquet")
    if "symbol" not in df.columns:
        raise RuntimeError("日线数据缺少 symbol 列")
    return df["symbol"].unique().sort().to_list()


def _eltdx_client():
    _ensure_canonical_eltdx()
    import eltdx
    return eltdx.TdxClient()


def _fetch_day(
    client,
    codes: list[str],
    anchor: date,
) -> pl.DataFrame:
    """取某一天全部标的的 1m bars。失败会重试。"""
    import eltdx  # noqa: F401  # 真包检查

    anchor_str = anchor.strftime("%Y-%m-%d")
    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            series = client.bars.get(codes, period="1m", count=240, anchor_date=anchor_str)
            rows: list[dict] = []
            for code, kline_series in series.items():
                symbol = f"{code[2:]}.{code[:2].upper()}"
                for bar in kline_series.bars:
                    # eltdx 返回 tz-aware 北京时间, 去时区后即为项目存的 naive 墙钟
                    dt = bar.time.replace(tzinfo=None)
                    rows.append({
                        "symbol": symbol,
                        "datetime": dt,
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": float(bar.volume_lots),  # 手
                        "amount": float(bar.amount),        # 元
                    })
            if not rows:
                return pl.DataFrame()
            df = pl.DataFrame(rows)
            # 停牌股会返回 anchor 之前更早交易日的 bars, 必须只保留 anchor 当天,
            # 否则一天的数据里混进多个日期, 写分区时会按 min(date) 写错目录
            return df.filter(pl.col("datetime").dt.date() == anchor)
        except Exception as e:
            last_err = e
            logger.warning("%s %d 只第 %d 次请求失败: %s", anchor, len(codes), attempt + 1, e)
    if last_err:
        raise last_err
    return pl.DataFrame()


def _trade_dates(start: date, end: date) -> list[date]:
    """简单交易日过滤: 跳过周六日。更精确过滤依赖后端日历, 这里先用工作日近似。"""
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _write_partition(df: pl.DataFrame, minute_dir: Path, overwrite: bool) -> int:
    """把一天的 DataFrame 写入 date=YYYY-MM-DD/part.parquet, 返回写入行数。"""
    if df.is_empty():
        return 0
    canonical = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
    df = df.select(canonical)
    # maintain_order=True 保证 keep="last" 是确定性的(默认 False 时是哈希序, 不可靠)
    df = df.unique(subset=["symbol", "datetime"], keep="last", maintain_order=True).sort("symbol", "datetime")
    day = str(df["datetime"].dt.date().min())
    out_dir = minute_dir / f"date={day}"
    out = out_dir / "part.parquet"
    if out.exists() and not overwrite:
        logger.info("  %s 分区已存在, 跳过 (use --overwrite to replace)", day)
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    df.write_parquet(tmp)
    tmp.replace(out)
    return df.height


def run(args: argparse.Namespace) -> int:
    minute_dir = Path(args.minute_dir)
    daily_dir = Path(args.daily_dir)
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    symbols = _load_symbols(daily_dir)
    codes = [_symbol_to_eltdx_code(s) for s in symbols]
    codes = [c for c in codes if c]
    if not codes:
        logger.error("没有可映射的 eltdx 代码")
        return 2
    logger.info("目标区间: %s ~ %s, 标的 %d 只", start, end, len(codes))

    with _eltdx_client() as client:
        logger.info("eltdx client ready")

        dates = _trade_dates(start, end)
        if args.dry_run:
            logger.info("DRY RUN: 将请求 %d 个交易日, 每天 %d 批, 不写盘", len(dates), -(-len(codes) // _BATCH_SIZE))
            return 0

        total_rows = 0
        total_symbols = 0
        t0 = monotonic()
        for d in dates:
            out_dir = minute_dir / f"date={d}"
            out = out_dir / "part.parquet"
            if out.exists() and not args.overwrite:
                logger.info("%s 分区已存在, 跳过", d)
                continue

            day_rows: list[pl.DataFrame] = []
            batches = [codes[i : i + _BATCH_SIZE] for i in range(0, len(codes), _BATCH_SIZE)]
            for idx, batch in enumerate(batches):
                try:
                    df = _fetch_day(client, batch, d)
                except Exception as e:
                    logger.error("%s 第 %d/%d 批失败: %s", d, idx + 1, len(batches), e)
                    continue
                if not df.is_empty():
                    day_rows.append(df)
                    total_symbols += df["symbol"].n_unique()
            if not day_rows:
                logger.warning("%s 无数据", d)
                continue
            merged = pl.concat(day_rows)
            written = _write_partition(merged, minute_dir, args.overwrite)
            logger.info("%s 写入 %d 行 (%.1fs)", d, written, monotonic() - t0)
            total_rows += written

        logger.info("=== 完成: %d 行写入 %s (耗时 %.1fs) ===", total_rows, minute_dir, monotonic() - t0)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="用 eltdx 回补 1m 历史分钟分区")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--daily-dir", default=str(DEFAULT_DAILY_DIR), help="日线 parquet 目录, 用于取 symbol 列表")
    parser.add_argument("--minute-dir", default=str(DEFAULT_MINUTE_DIR), help="输出分钟分区目录")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在分区(慎用)")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划不写盘")
    parser.add_argument("--workers", type=int, default=1, help="并发线程数(预留, 当前未启用)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
