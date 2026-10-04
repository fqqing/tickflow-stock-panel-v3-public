"""探针: 摸清 eltdx 日K(bars.get period="day") 的真实形态与量纲。

只做只读探测, 不写任何数据。用于决定能否把它接成 ``daily`` dataset 替代/备份
tushare 日K。复跑::

    backend/.venv/Scripts/python.exe scripts/probe_eltdx_daily.py

重点回答 5 个问题, 全都要靠实测不能猜:
  1. period="day" 是否可用, 单只最多能拿到多少根(决定能否做历史回补)
  2. 是否支持 anchor_date / count(决定能否按窗口取增量)
  3. volume_lots / amount 的量纲(手? 元?) —— 用 r = amount/(vol*100)/close 判定
  4. 批量能力(多少只/次, 耗时)
  5. 覆盖范围(沪深/创业板/科创板/北交所/ETF/指数)
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import eltdx

from app.plugins.eltdx.provider import app_to_eltdx

# 覆盖各板块: 主板沪/深/创业板/科创板/北交所/ETF/指数
SAMPLES = [
    "600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "920002.BJ",
    "510300.SH", "000001.SH",
]


def _probe_single(cli) -> None:
    print("== 1. 单只 period=day 上限 ==")
    for count in (10, 1000, 8000):
        t0 = time.perf_counter()
        try:
            res = cli.bars.get("sh600519", period="day", count=count)
        except Exception as e:
            print(f"  count={count}: 失败 {type(e).__name__}: {e}")
            continue
        bars = getattr(res, "bars", None) or []
        first = getattr(bars[0], "time", None) if bars else None
        last = getattr(bars[-1], "time", None) if bars else None
        print(
            f"  count={count}: 返回 {len(bars)} 根, "
            f"{first} ~ {last}, {time.perf_counter() - t0:.2f}s"
        )


def _probe_fields(cli) -> None:
    print("\n== 2. 字段与量纲 ==")
    res = cli.bars.get("sh600519", period="day", count=5)
    bars = getattr(res, "bars", None) or []
    if not bars:
        print("  无数据")
        return
    b = bars[-1]
    print(f"  字段: {[k for k in vars(b)] if hasattr(b, '__dict__') else dir(b)}")
    print(f"  末根: {b}")
    for bar in bars[-3:]:
        vol = float(getattr(bar, "volume_lots", 0) or 0)
        amt = float(getattr(bar, "amount", 0) or 0)
        close = float(getattr(bar, "close", 0) or 0)
        r = amt / (vol * 100) / close if vol and close else 0
        print(
            f"  {bar.time} close={close} vol={vol} amt={amt:.0f} "
            f"r={r:.4f}  <= 1.0=手/元, 0.01=股, 0.001=千元"
        )


def _probe_anchor(cli) -> None:
    print("\n== 3. anchor_date 是否生效 ==")
    for anchor in (date(2026, 9, 29), date(2026, 6, 30)):
        try:
            res = cli.bars.get("sh600519", period="day", count=5, anchor_date=anchor)
        except Exception as e:
            print(f"  anchor={anchor}: 失败 {type(e).__name__}: {e}")
            continue
        bars = getattr(res, "bars", None) or []
        tail = [str(b.time)[:10] for b in bars]
        print(f"  anchor={anchor}: {len(bars)} 根, 末根日期={tail[-1] if tail else '?'}")


def _local_codes(limit: int) -> list[str]:
    """从本地标的维表取真实代码(重复代码会被 eltdx 去重, 测不出批量规模)。"""
    from pathlib import Path

    import polars as pl

    from app.config import settings

    path = Path(settings.data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return []
    df = pl.read_parquet(path, columns=["symbol"])
    out = []
    for sym in df["symbol"].to_list():
        code = app_to_eltdx(str(sym))
        if code:
            out.append(code)
        if len(out) >= limit:
            break
    return out


def _probe_batch(cli) -> None:
    print("\n== 4. 批量能力(用真实代码, 重复代码会被上游去重) ==")
    pool = _local_codes(200)
    print(f"  本地可取代码 {len(pool)} 只")
    for n in (10, 50, 100, 200):
        if len(pool) < n:
            break
        probe = pool[:n]
        t0 = time.perf_counter()
        try:
            res = cli.bars.get(probe, period="day", count=10)
        except Exception as e:
            print(f"  {n} 只: 失败 {type(e).__name__}: {e}")
            continue
        counts = [len(getattr(v, "bars", None) or []) for v in res.values()]
        print(
            f"  {n} 只: 返回 {len(res)} 只, 根数 min/max="
            f"{min(counts) if counts else 0}/{max(counts) if counts else 0}, "
            f"{time.perf_counter() - t0:.2f}s"
        )


def _probe_coverage(cli) -> None:
    """⚠️ eltdx 的返回形态随入参变化: codes 传 **str** 返回 KlineSeries,
    传 **list** 才返回 {code: KlineSeries}。统一用 list 避免分支。"""
    print("\n== 5. 板块覆盖(codes 必须传 list 才是 dict 回包) ==")
    for sym in SAMPLES:
        code = app_to_eltdx(sym)
        try:
            res = cli.bars.get([code], period="day", count=3)
        except Exception as e:
            print(f"  {sym}({code}): 失败 {type(e).__name__}: {e}")
            continue
        series = res.get(code) if isinstance(res, dict) else res
        bars = getattr(series, "bars", None) or []
        if not bars:
            print(f"  {sym}({code}): 0 根")
            continue
        b = bars[-1]
        vol = float(getattr(b, "volume_lots", 0) or 0)
        amt = float(getattr(b, "amount", 0) or 0)
        close = float(getattr(b, "close", 0) or 0)
        r = amt / (vol * 100) / close if vol and close else 0
        print(f"  {sym}({code}): {len(bars)} 根, 末根 {b.time} close={close} r={r:.4f}")


def _probe_paged_history(cli) -> None:
    """count 上限 800 => 长历史必须分段。验证 anchor 分段能否拼出连续历史。"""
    print("\n== 6. 分段回补(每段 800 根, anchor 逐段前移) ==")
    total: dict[str, int] = {}
    anchor = date(2026, 9, 30)
    for seg in range(4):
        t0 = time.perf_counter()
        try:
            res = cli.bars.get(["sh600519"], period="day", count=800, anchor_date=anchor)
        except Exception as e:
            print(f"  段{seg}: 失败 {type(e).__name__}: {e}")
            break
        series = res.get("sh600519") if isinstance(res, dict) else res
        bars = getattr(series, "bars", None) or []
        if not bars:
            print(f"  段{seg}: 0 根, 到此为止")
            break
        total[str(bars[0].time)[:10]] = len(bars)
        print(
            f"  段{seg}: {len(bars)} 根, {str(bars[0].time)[:10]} ~ "
            f"{str(bars[-1].time)[:10]}, {time.perf_counter() - t0:.2f}s"
        )
        anchor = bars[0].time.date()
    print(f"  覆盖起始日: {sorted(total)[:1]}")


def _probe_crosscheck(cli) -> None:
    """与本地(存量的 tushare 日K)分板块对拍 —— 量纲坑只在分板块看才暴露。

    抽样必须 Round-Robin 覆盖全部板块, 否则按字典序会全落大板块、漏掉 688/920。
    """
    print("\n== 7. 与本地日K对拍(最近 5 个交易日) ==")
    import polars as pl

    from app.config import settings

    root = Path(settings.data_dir) / "kline_daily"
    if not root.exists():
        print("  本地无 kline_daily, 跳过")
        return
    days = sorted(p.name.split("=")[1] for p in root.iterdir() if p.name.startswith("date="))[-5:]
    # 各分区列不齐(部分含 quote_ts), 只能逐区读再 diagonal_relaxed 拼接。
    parts = [
        pl.read_parquet(str(root / f"date={d}"), columns=["symbol", "date", "close", "volume", "amount"])
        for d in days
    ]
    local = pl.concat([p for p in parts if not p.is_empty()], how="diagonal_relaxed")
    for sym in SAMPLES:
        code = app_to_eltdx(sym)
        try:
            res = cli.bars.get([code], period="day", count=len(days) + 4)
        except Exception as e:
            print(f"  {sym}: 拉取失败 {type(e).__name__}: {e}")
            continue
        series = res.get(code) if isinstance(res, dict) else res
        bars = getattr(series, "bars", None) or []
        mine = local.filter(pl.col("symbol") == sym)
        if mine.is_empty():
            print(f"  {sym}: 本地无该标的, 跳过")
            continue
        remote = {b.time.date(): b for b in bars}
        diffs: list[str] = []
        for row in mine.sort("date").to_dicts():
            b = remote.get(row["date"])
            if b is None:
                diffs.append(f"{row['date']} 缺失")
                continue
            c = float(b.close or 0)
            v = float(b.volume_lots or 0)
            a = float(b.amount or 0)
            dc = abs(c - float(row["close"] or 0)) / float(row["close"] or 1)
            dv = abs(v - float(row["volume"] or 0)) / float(row["volume"] or 1)
            da = abs(a - float(row["amount"] or 0)) / float(row["amount"] or 1)
            diffs.append(f"{row['date']} dc={dc:.4%} dv={dv:.4%} da={da:.4%}")
        print(f"  {sym}: {'; '.join(diffs)}")


def main() -> int:
    cli = eltdx.TdxClient()
    _probe_single(cli)
    _probe_fields(cli)
    _probe_anchor(cli)
    _probe_batch(cli)
    _probe_coverage(cli)
    _probe_paged_history(cli)
    _probe_crosscheck(cli)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
