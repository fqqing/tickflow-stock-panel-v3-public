"""eltdx(通达信)分钟K接入验收脚本。

用法::

    python scripts/verify_eltdx.py                 # 默认验最近 1 个交易日
    python scripts/verify_eltdx.py --days 5        # 验最近 5 个交易日
    python scripts/verify_eltdx.py --date 2026-09-29

退出码 = FAIL 数(0 表示全通过)。

为什么单独写这个脚本而不是复用 verify_source_units.py
======================================================
本源最关键的验收是**与本地日K对拍**: 把某日全部分钟 bar 的 volume / amount
求和, 应等于当日日K的 volume / amount。这一步能一次抓出量纲错误(手 vs 股、
元 vs 千元), 而 verify_source_units.py 只做单 bar 的
``amount/(volume*100)/close`` 比值, 抓不到"逐根都对但合计对不上"的情形。

⚠️ 抽样必须 Round-Robin 覆盖全部板块。按 symbol 字典序取前 N 只会让样本
全部落在 000/300/600 大板块, 恰好漏掉 688(科创板)与 920(北交所) ——
腾讯 mkline 那个"科创板 vol 单位是股"的坑就是这样被漏掉的。
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

from app.market_time import cn_today
from app.plugins.eltdx.provider import (
    EltdxMinuteProvider,
    app_to_eltdx,
    availability,
)

#: 按板块 Round-Robin 取样的候选池(各板块内部按代码间隔取样, 避免全落头部)。
_SEEDS: dict[str, list[str]] = {
    "沪主板": ["600519.SH", "601318.SH", "600036.SH", "601899.SH"],
    "深主板": ["000001.SZ", "000858.SZ", "002594.SZ", "000651.SZ"],
    "创业板": ["300750.SZ", "300059.SZ", "301269.SZ", "300124.SZ"],
    "科创板": ["688981.SH", "688111.SH", "688256.SH", "688599.SH"],
    "北交所": ["920002.BJ", "920819.BJ", "920060.BJ"],
    "指数": ["000001.SH", "399001.SZ", "399006.SZ"],
    "ETF": ["510300.SH", "159915.SZ", "588000.SH"],
}

#: 非本市场标的 —— 应被 app_to_eltdx 过滤掉, 不得出现在结果里。
_FOREIGN = ["AAPL.US", "00700.HK", "TSLA.US"]

#: 仓库根(backend/scripts/x.py -> parents[2])。脚本里所有 data/ 路径都按它拼,
#: 否则 cwd 在 backend/ 时会找不到 data/(相对路径陷阱)。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"

#: 指数代码前缀。指数的 volume 是成分股合计成交量、close 是点位,
#: ``amount/(volume*100)/close`` 对指数**没有意义**(实测 0.0078~0.0262),
#: 与前端 EChartsIntraday 对指数不画均价线的处理同源 —— 指数跳过单位校验。
_INDEX_CODES = {"000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "399005.SZ"}


def _round_robin(per_board: int) -> list[str]:
    pools = [v for v in _SEEDS.values()]
    out: list[str] = []
    for i in range(per_board):
        for pool in pools:
            if i < len(pool):
                out.append(pool[i])
    return out


def _daily_row(symbol: str, day) -> dict | None:
    """读本地日K该标的当日行(用于 volume/amount 对拍)。"""
    d = _DATA_DIR / "kline_daily" / f"date={day.isoformat()}"
    if not d.exists():
        return None
    files = sorted(d.glob("*.parquet"))
    if not files:
        return None
    for f in files:
        try:
            df = pl.read_parquet(f, columns=["symbol", "volume", "amount"])
        except Exception:
            continue
        hit = df.filter(pl.col("symbol") == symbol)
        if not hit.is_empty():
            return hit.row(0, named=True)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="eltdx 分钟K接入验收")
    ap.add_argument("--date", default=None, help="验证日期 YYYY-MM-DD, 默认最近交易日")
    ap.add_argument("--days", type=int, default=1, help="验证最近 N 个交易日(按自然日回溯)")
    ap.add_argument("--per-board", type=int, default=2, help="每个板块取样只数")
    args = ap.parse_args()

    fails: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(label)

    print("=" * 72)
    print("0) 可用性")
    print("=" * 72)
    ok, reason = availability()
    check(ok, "availability()", reason)
    if not ok:
        print("\n插件不可用, 后续检查无法进行。")
        return 1

    today = cn_today()
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
        days = [target]
    else:
        days = []
        cur = today
        while len(days) < args.days:
            if cur.weekday() < 5:
                days.append(cur)
            cur -= timedelta(days=1)

    provider = EltdxMinuteProvider()
    symbols = _round_robin(args.per_board)
    print(f"\n取样 {len(symbols)} 只(覆盖 {len(_SEEDS)} 个板块), 交易日 {days}")

    for day in days:
        print()
        print("=" * 72)
        print(f"1) get_minute  {day}")
        print("=" * 72)
        start = datetime(day.year, day.month, day.day, 9, 25)
        end = datetime(day.year, day.month, day.day, 15, 5)
        df = provider.get_minute(symbols, start_time=start, end_time=end, freq="1m")
        if df.is_empty():
            check(False, f"{day} 返回非空", "空 DataFrame")
            continue
        check(True, f"{day} 返回非空", f"{df.height} 行 / {df['symbol'].n_unique()} 只")

        required = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
        check(required.issubset(set(df.columns)), "必需列齐全", str(sorted(df.columns)))

        # datetime 应为北京墙钟 naive
        dts = df["datetime"].to_list()
        naive = all(getattr(d, "tzinfo", None) is None for d in dts)
        check(naive, "datetime 为 naive 北京墙钟")

        # 逐只校验
        for sym in sorted(df["symbol"].unique().to_list()):
            sub = df.filter(pl.col("symbol") == sym)
            n = sub.height
            # OHLC 极值关系
            bad = sub.filter(
                ~(
                    (pl.col("high") >= pl.max_horizontal(["open", "close"]) - 1e-6)
                    & (pl.col("low") <= pl.min_horizontal(["open", "close"]) + 1e-6)
                )
            ).height
            # 量纲: amount / (volume 手 x 100) / close 应 ~1.0。
            # 指数无「股数」概念, 该比值不适用, 跳过(见 _INDEX_CODES 注释)。
            is_index = sym in _INDEX_CODES
            ratio = (
                sub.select(
                    (pl.col("amount") / (pl.col("volume") * 100.0) / pl.col("close")).median()
                ).item()
                or 0.0
            )
            ratio_ok = True if is_index else 0.85 <= ratio <= 1.15
            # 与本地日K对拍
            drow = _daily_row(sym, day)
            pair = ""
            pair_ok = True
            if drow and drow.get("volume"):
                v = sub["volume"].sum()
                a = sub["amount"].sum()
                vr = v / drow["volume"]
                ar = (a / drow["amount"]) if drow.get("amount") else 0.0
                pair = f"vol={vr:.4f}x amt={ar:.4f}x"
                pair_ok = 0.98 <= vr <= 1.02 and 0.98 <= ar <= 1.02
            ok_one = bad == 0 and ratio_ok and pair_ok
            ratio_txt = "指数跳过" if is_index else f"{ratio:.4f}"
            check(
                ok_one,
                f"{sym}",
                f"bars={n} OHLC违例={bad} 单位ratio={ratio_txt} "
                f"日K对拍[{pair or '本地无日K'}]",
            )

        # 非本市场过滤
        print()
        print("=" * 72)
        print("2) 非本市场过滤")
        print("=" * 72)
        mapped = [app_to_eltdx(s) for s in _FOREIGN]
        check(all(m is None for m in mapped), "港美股被 app_to_eltdx 过滤", str(mapped))
        mixed = provider.get_minute(
            symbols[:3] + _FOREIGN, start_time=start, end_time=end, freq="1m",
        )
        if not mixed.is_empty():
            bad_suffix = [
                s for s in mixed["symbol"].unique().to_list()
                if not s.upper().endswith((".SH", ".SZ", ".BJ"))
            ]
            check(not bad_suffix, "结果不含港美股", str(bad_suffix))
        else:
            check(True, "结果不含港美股", "混合请求返回空(或未落库)")

    # 不支持的周期
    print()
    print("=" * 72)
    print("3) 不支持的周期应返回空(交给调用方回落)")
    print("=" * 72)
    for freq in ["90m", "120m"]:
        d2 = provider.get_minute(["600519.SH"], None, None, freq=freq)
        check(d2.is_empty(), f"freq={freq} 返回空", f"{d2.height} 行")

    # 全市场速度抽样
    print()
    print("=" * 72)
    print("4) 批量速度抽样")
    print("=" * 72)
    try:
        import time

        inst = sorted((_DATA_DIR / "instruments").glob("instruments*.parquet"))
        if not inst:
            raise FileNotFoundError(f"未找到标的维表: {_DATA_DIR / 'instruments'}")
        dfp = pl.read_parquet(inst[0])
        pool = dfp.filter(
            pl.col("symbol").str.ends_with(".SH") | pl.col("symbol").str.ends_with(".SZ")
        )["symbol"].to_list()[:200]
        t0 = time.perf_counter()
        big = provider.get_minute(pool, datetime(days[0].year, days[0].month, days[0].day, 9, 25),
                                  datetime(days[0].year, days[0].month, days[0].day, 15, 5),
                                  freq="1m")
        el = time.perf_counter() - t0
        got = big["symbol"].n_unique()
        print(f"  200 只: {got} 只返回 / {big.height} 行, {el:.2f}s "
              f"-> 全市场 5400 只约 {el * 27:.0f}s")
        check(got >= 150, "批量拉取覆盖 >= 150/200", f"实际 {got}")
    except Exception:
        traceback.print_exc()
        check(False, "批量速度抽样", "异常")

    print()
    print("=" * 72)
    print(f"结果: {len(fails)} 项失败")
    for f in fails:
        print(f"  - {f}")
    print("=" * 72)
    return len(fails)


if __name__ == "__main__":
    raise SystemExit(main())
