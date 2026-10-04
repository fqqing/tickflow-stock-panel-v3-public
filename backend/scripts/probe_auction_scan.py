"""探针: 集合竞价全市场扫描 + 盘中走强监测的可行性实测。

要回答三件事:
  1. 竞价接口(auction_data)并发下的单只耗时与成功率 —— 决定「全市场竞价扫描」能不能做。
  2. 批量快照(get_snapshots)能否一次拿到全市场的开盘价/涨幅 —— 「低开 -> 盘中走强」
     要同时知道**竞价开盘涨幅**和**当前涨幅**, 后者只能靠快照轮询。
  3. 竞价序列(points)在收盘后还剩多少 —— 决定「盘后复盘 / 历史样本积累」是否可能。

用法::

    python scripts/probe_auction_scan.py [--n 20] [--workers 8] [--full-snapshot]
"""
from __future__ import annotations

import argparse
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.WARNING)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="抽样多少只做竞价并发实测")
    ap.add_argument("--workers", type=int, default=8, help="并发线程数")
    ap.add_argument("--full-snapshot", action="store_true", help="实测全市场快照耗时")
    args = ap.parse_args()

    from app.plugins.eltdx.provider import (
        _local_market_symbols,
        app_to_eltdx,
        market_meta,
    )
    from app.pulse.gateway import client

    syms = [s for s in _local_market_symbols() if not market_meta().get(s, {}).get("is_index")]
    print(f"全市场标的: {len(syms)} 只 (含 ETF)")

    # ---- 1) 批量快照字段 ----
    print("\n=== 1) 批量快照 get_snapshots 字段 ===")
    sample = random.sample(syms, min(80, len(syms)))
    codes = [c for c in (app_to_eltdx(s) for s in sample) if c]
    t0 = time.perf_counter()
    try:
        snaps = list(client().quotes.get_snapshots(codes))
        dt = time.perf_counter() - t0
        print(f"请求 {len(codes)} 只 -> 回 {len(snaps)} 条, {dt:.2f}s")
        if snaps:
            for f in ("open_price", "last_price", "pre_close_price", "change_pct",
                      "high_price", "low_price", "amount", "volume"):
                print(f"  {f} = {getattr(snaps[0], f, '<无>')}")
            print(f"  可用属性: {[a for a in dir(snaps[0]) if not a.startswith('_')][:24]}")
    except Exception as e:
        print(f"批量快照失败: {type(e).__name__}: {e}")

    # ---- 2) 竞价接口并发实测 ----
    print(f"\n=== 2) 竞价 auction_data 并发 (n={args.n}, workers={args.workers}) ===")
    probe = random.sample(syms, min(args.n, len(syms)))
    pairs = [(s, c) for s in probe if (c := app_to_eltdx(s))]

    def one(item: tuple[str, str]) -> tuple[str, float, int, str]:
        sym, code = item
        t = time.perf_counter()
        try:
            data = client().helpers.auction_data(code)
            pts = getattr(getattr(data, "series", None), "points", ()) or ()
            return sym, time.perf_counter() - t, len(pts), str(getattr(data, "trading_date", "") or "")
        except Exception as e:
            return sym, time.perf_counter() - t, -1, f"{type(e).__name__}: {e}"[:80]

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, pairs))
    wall = time.perf_counter() - t0

    ok = [r for r in results if r[2] >= 0]
    bad = [r for r in results if r[2] < 0]
    print(f"墙钟 {wall:.2f}s, 成功 {len(ok)}/{len(results)}, 失败 {len(bad)}")
    if ok:
        costs = sorted(r[1] for r in ok)
        print(f"  单只耗时: 中位 {costs[len(costs)//2]:.2f}s  最快 {costs[0]:.2f}s  最慢 {costs[-1]:.2f}s")
        pcounts = [r[2] for r in ok]
        print(f"  points 数: 中位 {sorted(pcounts)[len(pcounts)//2]}  最多 {max(pcounts)}  最少 {min(pcounts)}")
        print(f"  trading_date: {[r[3] for r in ok[:5]]}")
        per = costs[len(costs) // 2]
        for total in (500, 2000, 5000):
            print(f"  外推 {total} 只 @{args.workers} 并发: 约 {total * per / args.workers:.0f}s")
    if bad:
        print(f"  失败样本: {[(r[0], r[3]) for r in bad[:5]]}")

    # ---- 3) 全市场快照耗时(盘中走强监测要轮询它) ----
    if args.full_snapshot:
        print("\n=== 3) 全市场快照耗时 ===")
        all_codes = [c for c in (app_to_eltdx(s) for s in syms) if c]
        batches = [all_codes[i:i + 80] for i in range(0, len(all_codes), 80)]
        t0 = time.perf_counter()
        got = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for part in pool.map(lambda cs: list(client().quotes.get_snapshots(cs)), batches):
                got += len(part)
        print(f"全市场 {len(all_codes)} 只 -> 回 {got} 条, {time.perf_counter() - t0:.1f}s")

    # ---- 4) 收盘后竞价序列可用性 ----
    print("\n=== 4) 竞价序列可用性(当前时点) ===")
    if ok:
        print(f"  points >= 6 (可评分): {len([r for r in ok if r[2] >= 6])}/{len(ok)}")
        print("  >0 说明收盘后仍能取到当日竞价 => 可盘后落盘积累历史样本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
