"""用 pytdx 历史分时接口回补分钟 K (可按天补齐过去约一年).

与同目录 import_minute_tdx.py 的分工:

- ``import_minute_tdx.py`` 用 ``get_security_bars(1min)``: 有真实 OHLC, 但服务器
  只给最近 800 根(约 3.3 个交易日), 且该接口会间歇性拒服务.
- 本脚本用 ``get_history_minute_time_data(market, code, YYYYMMDD)``: 每次给一整天
  240 根, 实测可回溯到约 13 个月前. 代价是只有 price/vol, 没有真实 OHLC.

用法(必须用装了 pytdx 的解释器, 本机是 Anaconda)::

    D:\\Environment\\Anaconda\\python.exe scripts/backfill_minute_tdx.py --limit 3 --days 5
    D:\\Environment\\Anaconda\\python.exe scripts/backfill_minute_tdx.py --days 60
    D:\\Environment\\Anaconda\\python.exe scripts/backfill_minute_tdx.py --days 60 --skip-existing

实测结论(改代码前必读, 都是踩过的坑):

1. **时间轴**: 与项目存量一致, 每天 240 根 —— 上午 09:31~11:30(120 根),
   下午 13:01~15:00(120 根). 第 0 根是 **09:31** 而不是 09:30.
   判据: data/kline_minute 里由 get_security_bars 导入的真实 1 分钟 K 就是这个分布.
2. **量纲: vol 单位是「手」, 与项目一致, 不要 /100**。
   判据: 单日 240 根 vol 求和 == 该股当日日线 vol (茅台 2026-09-23: 30981 == 30981).
   这与 get_security_bars 的分钟线相反(那个是「股」, 要 /100), 别照抄.
3. **只有 ['price', 'vol'] 两个字段**: price 是该分钟的价格(等价于当分钟收盘价),
   没有当分钟的高低点. 因此 OHLC 是**近似**的::

       open  = 上一根 price (首根用自身)
       close = 本根 price
       high  = max(open, close)
       low   = min(open, close)

   收盘序列与趋势是准的, 但每分钟振幅偏小 —— 缠论这类对高低点敏感的算法在
   回补区间上会比真实数据钝一些. 最近 800 根建议继续用 import_minute_tdx.py
   的真实 OHLC 覆盖(本项目 flush 是 keep='last' 去重, 后写的会覆盖).
4. **amount**: 项目口径是「元」, vol 是「手」 => ``amount = price * vol * 100``.
5. **限流很凶**: 连续快速请求会被服务器拒, 表现为所有接口一起抛
   TdxFunctionCallError("calling function error"). 必须限速, 且连续失败时指数退避.
6. **非交易日/未来日期返回空列表**, 直接跳过即可(不是错误).
7. **datetime 是北京墙钟**, 与项目存量一致, 不做时区转换.
8. **落盘必须攒批**: 见 import_minute_tdx.py 第 7 条.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_minute_tdx import (  # noqa: E402
    MINUTE_COLS,
    TdxSession,
    flush,
    market_of,
)

REPO = Path(__file__).resolve().parents[1]
WATCHLIST = REPO / "data" / "user_data" / "watchlist.parquet"
DAILY_DIR = REPO / "data" / "kline_daily"
DEFAULT_OUT = REPO / "data" / "kline_minute"

AM_BARS = 120          # 上午 09:31~11:30
PM_BARS = 120          # 下午 13:01~15:00
BARS_PER_DAY = AM_BARS + PM_BARS
AM_START_MIN = 9 * 60 + 31     # 09:31
PM_START_MIN = 13 * 60 + 1     # 13:01

FETCH_SLEEP = 0.05     # 每次请求间隔(秒); 实测请求太快会被服务器整体拒绝
BACKOFF_BASE = 3.0     # 连续失败时的退避基数(秒), 指数递增
BACKOFF_MAX = 60.0
MAX_BACKOFF = 6        # 单只最多退避几次, 超过就放弃这只


def trade_calendar(days: int) -> list[str]:
    """最近 N 个交易日(取本地日线分区目录名, 天然只含真实交易日)。"""
    if not DAILY_DIR.exists():
        return []
    names = sorted(p.name[len("date="):] for p in DAILY_DIR.glob("date=*") if p.is_dir())
    return names[-days:] if days > 0 else names


def existing_counts(out_dir: Path, days: list[str]) -> dict[tuple[str, str], int]:
    """已有分区里 (symbol, day) -> 根数, 供 --skip-existing 判断。"""
    counts: dict[tuple[str, str], int] = {}
    for day in days:
        f = out_dir / ("date=%s" % day) / "part.parquet"
        if not f.exists():
            continue
        try:
            d = pd.read_parquet(f, columns=["symbol"])
        except Exception:
            continue
        for sym, n in d["symbol"].astype(str).value_counts().items():
            counts[(sym, day)] = int(n)
    return counts


def all_a_share_symbols() -> list[str]:
    """从最新一个日线分区取全部 A 股标的(.SZ/.SH), 供 --source all 使用。

    只取 pytdx 覆盖的沪深两市(港美股 pytdx 拿不到分钟, 必须走 stocksdk)。
    """
    if not DAILY_DIR.exists():
        return []
    parts = sorted((p for p in DAILY_DIR.glob("date=*") if p.is_dir()),
                   key=lambda p: p.name)
    if not parts:
        return []
    files = sorted(parts[-1].glob("*.parquet"))
    if not files:
        return []
    # 逐文件读: 分区内多个 parquet 的 date 列类型可能不一致(date32 vs dictionary),
    # 一次性 read_parquet(files) 会因合并 schema 报 ArrowTypeError。
    syms: set[str] = set()
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["symbol"])
        except Exception:
            continue
        syms.update(s for s in d["symbol"].astype(str) if s.endswith((".SZ", ".SH")))
    return sorted(syms)


def day_timestamps(day: str) -> list[pd.Timestamp]:
    """某交易日的 240 个分钟时间戳(北京墙钟 09:31~11:30 / 13:01~15:00)。"""
    base = pd.Timestamp(day)
    am = [base + timedelta(minutes=AM_START_MIN + i) for i in range(AM_BARS)]
    pm = [base + timedelta(minutes=PM_START_MIN + i) for i in range(PM_BARS)]
    return am + pm


def build_ohlc(prices: list[float], vols: list[float], symbol: str, day: str) -> pd.DataFrame:
    """price/vol 序列 -> 项目口径 OHLC DataFrame(近似高低点, 见模块 docstring 第 3 条)。"""
    n = len(prices)
    stamps = day_timestamps(day)
    if len(stamps) != n:
        # 服务器返回根数异常时截断对齐, 避免 datetime 与数据错位
        n = min(n, len(stamps))
        prices, vols, stamps = prices[:n], vols[:n], stamps[:n]
    prev = prices[:-1]
    opens = [prices[0]] + prev
    closes = prices
    highs = [max(o, c) for o, c in zip(opens, closes)]
    lows = [min(o, c) for o, c in zip(opens, closes)]
    out = pd.DataFrame({
        "symbol": [symbol] * n,
        "datetime": stamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [float(v) for v in vols],                 # 手, 与项目一致
        "amount": [p * v * 100.0 for p, v in zip(closes, vols)],  # 元
    })[MINUTE_COLS]
    # 与既有分区保持一致: us 而非 pandas 默认的 ns(否则跨分区 union 失败)
    out["datetime"] = pd.to_datetime(out["datetime"]).astype("datetime64[us]")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="回补最近 N 个交易日(默认 60)")
    ap.add_argument("--end", default="", help="只回补 <= 该日期的交易日(YYYY-MM-DD)。"
                                              "盘中跑时务必设为昨天: 今天分区由后端实时写入, 两边同时写会撞 parquet")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只(试跑用)")
    ap.add_argument("--skip-first", type=int, default=0, help="跳过前 N 只(续跑用)")
    ap.add_argument("--symbols", default="", help="逗号分隔指定标的, 默认读自选股")
    ap.add_argument("--symbols-file", default="",
                    help="从文件逐行读标的(全市场回补用; 命令行 --symbols 有长度上限)")
    ap.add_argument("--source", default="", choices=["", "all"],
                    help="all=从最新日线分区自动取全部 A 股(定时任务用, 不依赖自选股/外部文件)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--skip-existing", action="store_true",
                    help="跳过已有完整 BARS_PER_DAY 根的 (标的, 日期)")
    ap.add_argument("--flush-every", type=int, default=20,
                    help="攒够 N 只落一次盘(默认 20)")
    args = ap.parse_args()

    try:
        import pytdx  # noqa: F401
    except ImportError:
        print("ERROR: 当前解释器没有 pytdx。请用 Anaconda: "
              "D:\\Environment\\Anaconda\\python.exe")
        return 1

    days = trade_calendar(args.days)
    if args.end.strip():
        days = [d for d in days if d <= args.end.strip()]
    if not days:
        print("ERROR: 拿不到交易日历, 检查 %s" % DAILY_DIR)
        return 1
    print("trade days = %d  (%s ~ %s)" % (len(days), days[0], days[-1]))

    if args.source == "all":
        symbols = all_a_share_symbols()
        if not symbols:
            print("ERROR: 从日线分区取不到 A 股标的, 检查 %s" % DAILY_DIR)
            return 1
    elif args.symbols_file.strip():
        symbols = [
            s.strip()
            for s in Path(args.symbols_file).read_text(encoding="utf-8").splitlines()
            if s.strip()
        ]
    elif args.symbols.strip():
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif WATCHLIST.exists():
        symbols = [str(s) for s in pd.read_parquet(WATCHLIST)["symbol"].tolist()]
    else:
        print("ERROR: 自选股文件不存在 %s" % WATCHLIST)
        return 1

    todo = []
    for s in symbols:
        m = market_of(s)
        if m is None:
            continue
        todo.append((s, m, s.rsplit(".", 1)[0]))
    if args.skip_first:
        todo = todo[args.skip_first:]
    if args.limit:
        todo = todo[: args.limit]
    print("todo symbols = %d" % len(todo))
    if not todo:
        print("nothing to do")
        return 0

    out_dir = Path(args.out_dir)
    done = existing_counts(out_dir, days) if args.skip_existing else {}
    if done:
        print("skip-existing: 已有 (symbol,day) 记录 %d 条" % len(done))

    sess = TdxSession()
    if not sess.connect():
        print("ERROR: 所有 pytdx 服务器都连不上")
        return 1
    print("pytdx connected")

    ok = fail = skipped = 0
    buf: list[pd.DataFrame] = []
    t_all = time.time()
    t_io = 0.0
    for i, (symbol, market, code) in enumerate(todo, 1):
        got = 0
        backoff = 0
        for day in days:
            if args.skip_existing and done.get((symbol, day), 0) >= BARS_PER_DAY:
                skipped += 1
                continue
            ymd = int(day.replace("-", ""))
            bars = sess.history_minute(market, code, ymd)
            if bars is None:
                # 接口异常, 多半是被限流: 退避后换下一天, 不重试同一天
                time.sleep(min(BACKOFF_BASE * (2 ** backoff), BACKOFF_MAX))
                backoff = min(backoff + 1, MAX_BACKOFF)
                continue
            backoff = 0
            if not bars:
                continue  # 非交易日/停牌, 正常
            d = build_ohlc([float(b["price"]) for b in bars],
                           [float(b["vol"]) for b in bars],
                           symbol, day)
            buf.append(d)
            got += len(d)
            time.sleep(FETCH_SLEEP)
        if got:
            ok += 1
        else:
            fail += 1
            print("    ! %s 无数据(停牌/退市/被限流)" % symbol)

        if len(buf) >= args.flush_every * len(days) or i == len(todo):
            t1 = time.time()
            n = flush(buf, out_dir)
            t_io += time.time() - t1
            buf = []
            print("[%d/%d] flush rows=%d ok=%d fail=%d skip=%d io=%.1fs elapsed=%.1fs"
                  % (i, len(todo), n, ok, fail, skipped, t_io, time.time() - t_all))

    sess.close()
    print("DONE ok=%d fail=%d skipped=%d elapsed=%.1fs out=%s"
          % (ok, fail, skipped, time.time() - t_all, out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
