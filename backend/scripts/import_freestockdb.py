#!/usr/bin/env python3
"""从 free-stockdb (本地 stockdb.exe) 导入历史数据。

前置: 数据更新.exe 同步完成 + stockdb.exe 已启动 (127.0.0.1:7899)。
只连本地, 不碰远程体验服务 (批量拉远程会被永久封禁设备)。

导入范围 (用户 2026-09-30 拍板)
-------------------------------
- 日K:  **只补本地缺口** 2018-01-01 ~ 2022-09-18
        (本地 kline_daily 最早分区是 2022-09-19, 之前那 4 年是真的没有)
- 分钟: 5m / 30m / 60m 全量 (源只有 2025-01-02 起, 约 424 个交易日)
        1m 不导入 (全量 46GB 过大, 保持腾讯 mkline 每日增量)

量纲转换 (重要)
--------------
源端 volume 单位是**股**, 本项目内部口径是**手**:
    volume_local = volume_src / 100
判据: amount / volume_src / close 约等于 1.0 (若已是手该比值会是 0.01)。
amount 单位是元, 与本地一致, 不转换。

分块写入 (踩过的坑)
------------------
不能每只标的都写 ``date=YYYY-MM-DD/part.parquet`` —— 后者会覆盖前者,
跑完全市场只剩最后一只股票。所以按 chunk (默认 250 只) 聚合成
``part-{chunk序号}.parquet``, 同一天多个文件由 duckdb 的
``read_parquet('**/*.parquet')`` 自动合并。

写入位置
--------
- 日K   -> data/kline_daily/date=YYYY-MM-DD/
- 分钟  -> data/kline_minute_{freq}/date=YYYY-MM-DD/
           注意: 项目原生的 kline_minute 是 1m 表且无 freq 列,
           多周期必须落到独立目录, 否则会和 1m 数据混在一起。
           后端需在 app/tickflow/repository.py 补对应 VIEW (见文件尾部说明)。

用法
----
    # 先小规模试跑 (不写盘)
    python scripts/import_freestockdb.py daily --limit 20 --dry-run
    python scripts/import_freestockdb.py minute --freq 30m --limit 20 --dry-run

    # 正式导入 (耗时: 日K 约 20min, 30m/60m 各约 40min, 5m 约 50min)
    python scripts/import_freestockdb.py daily
    python scripts/import_freestockdb.py minute --freq 30m
    python scripts/import_freestockdb.py minute --freq 60m
    python scripts/import_freestockdb.py minute --freq 5m
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT.parent / "data"
_INSTRUMENTS = DATA_DIR / "instruments" / "instruments.parquet"
_DEFAULT_PYBAO = Path(r"D:\MyDownload\free-stockdb-windows-v0.3.5-more-power\stockdb\pybao")

#: 日K 本地缺口区间 (本地最早分区 2022-09-19)
_DAILY_DEFAULT_START = "20180101"
_DAILY_DEFAULT_END = "20220918"

#: 分钟周期 -> 目标目录
_MINUTE_TABLE = {
    "1m": "kline_minute",
    "5m": "kline_minute_5m",
    "15m": "kline_minute_15m",
    "30m": "kline_minute_30m",
    "60m": "kline_minute_60m",
}

logger = logging.getLogger("import_freestockdb")


def load_sdk(pybao: Path):
    """加载 stock_sdk.rd (不要把 pybao 装进 venv, 会污染环境)。"""
    p = str(pybao)
    if p not in sys.path:
        sys.path.insert(0, p)
    import stock_sdk

    return stock_sdk.rd


def _plain(symbol: str) -> str:
    """600519.SH -> 600519"""
    return symbol.split(".")[0]


def _d(s: str) -> str:
    """20260921 -> 2026-09-21"""
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def load_symbols(limit: int = 0, only: str = "") -> list[str]:
    """从本地 instruments 读 A 股标的 (SH/SZ/BJ), 保证与项目口径一致。

    刻意不用源端的「股票代码」表 —— 那个含 ETF/指数/B股,
    写进 kline_daily 会污染股票池 (见 daily_pipeline 的注释)。
    """
    import polars as pl

    df = pl.read_parquet(_INSTRUMENTS, columns=["symbol"])
    syms = (
        df.filter(
            pl.col("symbol").str.ends_with(".SH")
            | pl.col("symbol").str.ends_with(".SZ")
            | pl.col("symbol").str.ends_with(".BJ")
        )["symbol"]
        .sort()
        .to_list()
    )
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        syms = [s for s in syms if s in wanted or _plain(s) in wanted]
    if limit:
        syms = syms[:limit]
    return syms


def _quote_ts_ms(day: str) -> int:
    """当日 15:00 北京时间对应的毫秒时间戳 (历史数据没有真实快照时刻)。"""
    from app.market_time import CN_TZ

    y, m, d = int(day[:4]), int(day[4:6]), int(day[6:8])
    return int(datetime.combine(datetime(y, m, d).date(), dtime(15, 0), tzinfo=CN_TZ).timestamp() * 1000)


def _chunked_import(rd, symbols: list[str], fetch_one, build_df, out_dir: Path,
                    label: str, dry_run: bool, overwrite: bool,
                    workers: int, chunk: int, skip: int = 0) -> int:
    """通用分块导入。fetch_one(sym) -> (sym, rows|None); build_df(items, day) -> DataFrame。

    skip: 跳过前 N 只标的(用于断点续跑)。注意 ci 也从 skip 起算,
          这样 part-{ci:06d} 编号与已完成的分块严格对齐, 不会错写/漏写。
    """
    t0 = time.perf_counter()
    stats = {"sym": 0, "rows": 0, "skip": 0, "fail": 0}
    total = len(symbols)
    nchunk = (total + chunk - 1) // chunk

    for ci in range(skip, total, chunk):
        batch = symbols[ci : ci + chunk]
        by_day: dict[str, list[tuple[str, dict]]] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, rows in ex.map(fetch_one, batch):
                if rows is None:
                    stats["fail"] += 1
                    stats.setdefault("failed", []).append(sym)
                    continue
                if not rows:
                    stats["skip"] += 1
                    continue
                stats["sym"] += 1
                for r in rows:
                    by_day.setdefault(str(r["date"])[:8], []).append((sym, r))

        for day in sorted(by_day):
            fp = out_dir / f"date={_d(day)}" / f"part-{ci:06d}.parquet"
            if fp.exists() and not overwrite:
                continue
            df = build_df(by_day[day], day)
            stats["rows"] += df.height
            if not dry_run:
                fp.parent.mkdir(parents=True, exist_ok=True)
                df.write_parquet(fp)

        el = time.perf_counter() - t0
        print(f"  chunk {ci // chunk + 1}/{nchunk}: 累计 {stats['rows']:,} 行, {el:.0f}s", flush=True)

    el = time.perf_counter() - t0
    print(f"\n[{label}] -> {out_dir.name}/")
    print(f"  标的 {total} 只: 写入 {stats['sym']} / 无数据 {stats['skip']} / 失败 {stats['fail']}")
    if stats.get("failed"):
        print(f"  失败标的 ({len(stats['failed'])}): {stats['failed'][:10]}")
    print(f"  行数 {stats['rows']:,}, 耗时 {el:.0f}s" + ("  (dry-run 未落盘)" if dry_run else ""))
    return 0


# ---------------------------------------------------------------- 日K


def import_daily(rd, symbols: list[str], start: str, end: str,
                 dry_run: bool, overwrite: bool, workers: int, chunk: int,
                 skip: int = 0) -> int:
    import polars as pl

    def fetch_one(sym: str):
        code = _plain(sym)
        try:
            res = rd.get_data(code, start=start, end=end, frequency="1d", fq=None)
        except Exception:
            logger.debug("日K 拉取失败 %s", sym)
            return sym, None
        return sym, (list(res.get(code) or []) if isinstance(res, dict) else list(res or []))

    def build_df(items: list[tuple[str, dict]], day: str):
        ts = _quote_ts_ms(day)
        dt = datetime.strptime(day, "%Y%m%d").date()
        return pl.DataFrame(
            {
                "symbol": [s for s, _ in items],
                "close": [float(r["close"]) for _, r in items],
                "open": [float(r["open"]) for _, r in items],
                "high": [float(r["high"]) for _, r in items],
                "low": [float(r["low"]) for _, r in items],
                # 源端 volume 单位是股 -> /100 转手
                "volume": [float(r["volume"]) / 100.0 for _, r in items],
                "amount": [float(r["amount"]) for _, r in items],
                "quote_ts": [ts] * len(items),
                "date": [dt] * len(items),
            }
        )

    out_dir = DATA_DIR / "kline_daily"
    label = f"日K {_d(start)} ~ {_d(end)}"
    return _chunked_import(rd, symbols, fetch_one, build_df, out_dir,
                           label, dry_run, overwrite, workers, chunk, skip)


# ---------------------------------------------------------------- 分钟


def import_minute(rd, symbols: list[str], freq: str,
                  dry_run: bool, overwrite: bool, workers: int, chunk: int,
                  skip: int = 0) -> int:
    import polars as pl

    def fetch_one(sym: str):
        code = _plain(sym)
        try:
            res = rd.get_data(code, start=None, end=None, frequency=freq, fq=None)
        except Exception:
            logger.debug("分钟拉取失败 %s", sym)
            return sym, None
        return sym, (list(res.get(code) or []) if isinstance(res, dict) else list(res or []))

    def build_df(items: list[tuple[str, dict]], _day: str):
        def dt_of(r: dict) -> datetime:
            s = str(r["date"])
            return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]), int(s[10:12]))

        return pl.DataFrame(
            {
                "symbol": [s for s, _ in items],
                # 源端 date 是 14 位 int (20260929150000), 存北京墙钟 naive datetime
                "datetime": [dt_of(r) for _, r in items],
                "open": [float(r["open"]) for _, r in items],
                "high": [float(r["high"]) for _, r in items],
                "low": [float(r["low"]) for _, r in items],
                "close": [float(r["close"]) for _, r in items],
                # 源端 volume 单位是股 -> /100 转手
                "volume": [float(r["volume"]) / 100.0 for _, r in items],
                "amount": [float(r["amount"]) for _, r in items],
            }
        )

    out_dir = DATA_DIR / _MINUTE_TABLE[freq]
    rc = _chunked_import(rd, symbols, fetch_one, build_df, out_dir,
                         freq, dry_run, overwrite, workers, chunk, skip)
    if not dry_run and rc == 0:
        print("\n  别忘了: 后端需在 app/tickflow/repository.py 补 VIEW 才能查到, 然后重启后端")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 free-stockdb 导入历史数据")
    ap.add_argument("action", choices=["daily", "minute"])
    ap.add_argument("--freq", default="30m", choices=list(_MINUTE_TABLE), help="minute 用的周期")
    ap.add_argument("--pybao", default=str(_DEFAULT_PYBAO))
    ap.add_argument("--start", default=_DAILY_DEFAULT_START, help="daily 起始 YYYYMMDD")
    ap.add_argument("--end", default=_DAILY_DEFAULT_END, help="daily 结束 YYYYMMDD")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只标的 (0=全部)")
    ap.add_argument("--only", default="", help="只处理这些标的, 逗号分隔")
    ap.add_argument("--workers", type=int, default=4, help="并发数")
    ap.add_argument("--chunk", type=int, default=250, help="每块标的数, 决定每个分区的文件数")
    ap.add_argument("--skip", type=int, default=0,
                    help="跳过前 N 只标的(断点续跑)。ci 从 N 起算以保证 part 编号对齐, "
                         "必须与当初使用的 --chunk 一致")
    ap.add_argument("--overwrite", action="store_true", help="覆盖已存在的文件")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = ap.parse_args(argv)

    rd = load_sdk(Path(args.pybao))
    symbols = load_symbols(args.limit, args.only)
    if not symbols:
        print("[FAIL] 没有可处理的标的")
        return 2
    print(f"待处理标的: {len(symbols)} 只, chunk={args.chunk}"
          + ("  (dry-run)" if args.dry_run else ""))

    if args.action == "daily":
        return import_daily(rd, symbols, args.start, args.end,
                            args.dry_run, args.overwrite, args.workers, args.chunk, args.skip)
    return import_minute(rd, symbols, args.freq, args.dry_run,
                         args.overwrite, args.workers, args.chunk, args.skip)


if __name__ == "__main__":
    raise SystemExit(main())
