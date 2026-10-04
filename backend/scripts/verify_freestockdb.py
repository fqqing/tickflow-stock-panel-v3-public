#!/usr/bin/env python3
"""free-stockdb 数据源验证脚本 (只验证, 不导入).

前置条件
--------
1. 双击 stockdb 目录下的「数据更新.exe」同步数据到 ./data (23GB 量级)
2. 双击「stockdb.exe」启动服务 (默认 127.0.0.1:7899, 窗口不要关)

为什么必须走 Python SDK 而不是 HTTP
----------------------------------
HTTP 通道 (``http://127.0.0.1:7899/?cmd=get&t=...``) 返回的是**二进制**负载,
不是 JSON, 直接 json.loads 会得到空结果. 实测:

    GET /?cmd=get&t=1d:600519:20260926  -> 200, body = b'\\xff'  (1 字节)

所以本脚本一律走官方 Python SDK (``pybao/stock_sdk.py``), 它内部用二进制协议
和 stockdb.exe 通信.

连接安全
--------
**只连 127.0.0.1**. 官方示例里有远程体验服务 8.138.149.215:7899,
文档明确写了「暴力拉取分钟会被永久封禁设备 (本地也无法使用)」,
本脚本不提供任何远程地址入口.

实测结论 (2026-09-30, 包 free-stockdb-windows-v0.3.5-more-power)
---------------------------------------------------------------
- 日K: 600519 全量 6014 条, 2001-08-27 ~ 2026-09-29 (超过我们要的 2018 起)
- 分钟K: 600519 全量 101952 条, **最早 2025-01-02** ~ 2026-09-29 (约 425 个交易日)
  ==> 分钟数据**回溯不到 2018**, 只有不到 2 年
- 量纲: 日K和分钟K的 volume 单位都是**股**, amount 单位是元.
  我们的内部口径 volume=手, 所以导入时必须 volume / 100.
  判据: amount / volume / close 应约等于 1.0 (若误当手, 该比值会是 0.01)
- 复权: fq=None 即不复权, 与本地 kline_daily 口径一致 (对拍价格差 0.00e+00)
- 额外字段: pre_close turnover(百分数) pct_chg(百分数) amplitude vol_ratio
  is_st total_share float_share total_mv float_mv pe_ttm pb

用法
----
    python scripts/verify_freestockdb.py probe      # 连通 + 范围探测
    python scripts/verify_freestockdb.py daily      # 与本地 kline_daily 对拍
    python scripts/verify_freestockdb.py minute     # 分钟回溯深度 + 体积估算
    python scripts/verify_freestockdb.py coverage   # 覆盖率抽检
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: pybao 目录默认位置 (含 stock_sdk.py + stockdb.pyd)
_DEFAULT_PYBAO = Path(r"D:\MyDownload\free-stockdb-windows-v0.3.5-more-power\stockdb\pybao")

#: 本地日K分区根目录
_LOCAL_DAILY = ROOT.parent / "data" / "kline_daily"

#: 对拍标杆: 主板/创业板/科创板 + 大盘股
_PROBE_SYMBOLS = ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "601398.SH"]

#: volume 单位判定: amount / volume / close 约等于 1.0 表示单位是股
_VOL_SHARES_TOL = 0.5

logger = logging.getLogger("verify_freestockdb")

_SDK_CACHE: dict[str, object] = {}


def load_sdk(pybao: Path):
    """加载 stock_sdk 并返回其 rd 对象. 失败返回 None.

    注意: 不要用官方的「安装.py」--它会往 venv 写 .pth 污染环境.
    这里只是把 pybao 临时插到 sys.path 再 import.
    """
    if "rd" in _SDK_CACHE:
        return _SDK_CACHE["rd"]
    if not pybao.is_dir():
        print(f"  [FAIL] pybao 目录不存在: {pybao}")
        print("         用 --pybao 指定正确路径")
        _SDK_CACHE["rd"] = None
        return None
    p = str(pybao)
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        import stock_sdk

        rd = stock_sdk.rd
    except Exception as e:
        print(f"  [FAIL] SDK 加载失败 ({type(e).__name__}: {e})")
        _SDK_CACHE["rd"] = None
        return None
    _SDK_CACHE["rd"] = rd
    return rd


def _plain(symbol: str) -> str:
    """600519.SH -> 600519"""
    return symbol.split(".")[0]


def _day_str(v: object) -> str:
    """20260921 (int) -> 2026-09-21"""
    s = str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _minute_dt(v: object) -> str:
    """20260929150000 (int) -> 2026-09-29 15:00"""
    s = str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"


def fetch_daily(rd, code: str, start: str | None, end: str | None) -> list[dict]:
    """取一只标的的日K (不复权)."""
    try:
        res = rd.get_data(code, start=start, end=end, frequency="1d", fq=None)
    except Exception as e:
        logger.debug("get_data 失败 %s: %s", code, e)
        return []
    if isinstance(res, dict):
        return list(res.get(code) or [])
    return list(res or [])


def fetch_minute(rd, code: str, start: str | None, end: str | None, freq: str) -> list[dict]:
    """取分钟K. freq: 1m/5m/15m/30m/60m (底层只有 1m, 其余由 SDK 聚合)."""
    try:
        res = rd.get_data(code, start=start, end=end, frequency=freq, fq=None)
    except Exception as e:
        logger.debug("get_data 分钟失败 %s: %s", code, e)
        return []
    if isinstance(res, dict):
        return list(res.get(code) or [])
    return list(res or [])


def check_volume_unit(rows: list[dict], price_key: str = "close") -> tuple[bool, float]:
    """判定 volume 单位是否为「股」. 返回 (是股, 比值中位数)."""
    ratios = []
    for r in rows:
        v, a, c = r.get("volume"), r.get("amount"), r.get(price_key)
        if not v or not a or not c:
            continue
        ratios.append(a / v / c)
    if not ratios:
        return False, 0.0
    ratios.sort()
    med = ratios[len(ratios) // 2]
    # 约等于 1.0 => volume 是股;  约等于 0.01 => 已经是手
    return abs(med - 1.0) < _VOL_SHARES_TOL, med


def cmd_probe(args: argparse.Namespace) -> int:
    """连通性 + 数据范围探测."""
    rd = load_sdk(Path(args.pybao))
    if rd is None:
        return 2

    print("--- 日K 范围 (标杆 600519) ---")
    rows = fetch_daily(rd, "600519", None, None)
    if not rows:
        print("  [FAIL] 日K 取不到任何数据 -- 服务没起或数据未同步完")
        return 2
    is_shares, ratio = check_volume_unit(rows)
    unit = "股 (导入需 /100 转手)" if is_shares else "手 (无需转换)"
    print(f"  条数: {len(rows)}   最早: {_day_str(rows[0]['date'])}   最晚: {_day_str(rows[-1]['date'])}")
    print(f"  量纲: amount/volume/close 中位 = {ratio:.6f} -> volume 单位是 {unit}")
    print(f"  字段: {sorted(rows[0].keys())}")

    print("\n--- 分钟K 范围 (标杆 600519, 1m) ---")
    mrows = fetch_minute(rd, "600519", None, None, "1m")
    if not mrows:
        print("  [WARN] 分钟K 取不到数据")
        return 0
    mis_shares, mratio = check_volume_unit(mrows)
    munit = "股 (导入需 /100 转手)" if mis_shares else "手 (无需转换)"
    n = len(mrows)
    print(f"  条数: {n}   最早: {_minute_dt(mrows[0]['date'])}   最晚: {_minute_dt(mrows[-1]['date'])}")
    print(f"  折合交易日: 约 {n // 240} 天")
    print(f"  量纲: amount/volume/close 中位 = {mratio:.6f} -> volume 单位是 {munit}")
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    """与本地 kline_daily 做重叠区间对拍."""
    rd = load_sdk(Path(args.pybao))
    if rd is None:
        return 2
    if not _LOCAL_DAILY.is_dir():
        print(f"  [FAIL] 本地日K目录不存在: {_LOCAL_DAILY}")
        return 2

    import polars as pl

    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or _PROBE_SYMBOLS
    start, end = args.start, args.end
    d_start = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    d_end = f"{end[:4]}-{end[4:6]}-{end[6:]}"

    def local_rows(sym: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in sorted(_LOCAL_DAILY.glob("date=*")):
            day = p.name.split("=", 1)[1]
            if not (d_start <= day <= d_end):
                continue
            try:
                df = pl.read_parquet(p, columns=["symbol", "close", "volume", "amount"])
            except Exception:
                continue
            r = df.filter(pl.col("symbol") == sym)
            if r.height:
                out[day] = r.row(0, named=True)
        return out

    print(f"对拍区间 {d_start} ~ {d_end}   (本地口径: 不复权, volume=手)")
    print(f"{'标的':<11} {'日期':<11} {'本地close':>10} {'远端close':>10} {'价格差':>10} "
          f"{'本地vol':>12} {'远端vol/100':>12} {'量差':>9}")
    print("-" * 92)

    max_px = 0.0
    max_vol = 0.0
    compared = 0
    for sym in symbols:
        code = _plain(sym)
        loc = local_rows(sym)
        rem = {_day_str(r["date"]): r for r in fetch_daily(rd, code, start, end)}
        common = sorted(set(loc) & set(rem))
        if not common:
            print(f"{sym:<11} 无重叠 (本地 {len(loc)} 天 / 远端 {len(rem)} 天)")
            continue
        for day in common[-args.show :]:
            loc_row, rem_row = loc[day], rem[day]
            px = abs(rem_row["close"] - loc_row["close"]) / loc_row["close"] if loc_row["close"] else 0.0
            rv = rem_row["volume"] / 100.0
            vd = abs(rv - loc_row["volume"]) / loc_row["volume"] if loc_row["volume"] else 0.0
            max_px = max(max_px, px)
            max_vol = max(max_vol, vd)
            compared += 1
            print(f"{sym:<11} {day:<11} {loc_row['close']:>10.2f} {rem_row['close']:>10.2f} {px:>10.2e} "
                  f"{loc_row['volume']:>12.0f} {rv:>12.0f} {vd:>9.2e}")

    print(f"\n共比对 {compared} 个交易日")
    ok_px = "一致 (复权口径相同)" if max_px < 1e-6 else "不一致, 可能是复权差异"
    ok_vol = "一致 (volume 确为股, /100 后吻合)" if max_vol < 1e-4 else "量纲不符, 需复核"
    print(f"  最大价格差:   {max_px:.3e}  -> {ok_px}")
    print(f"  最大成交量差: {max_vol:.3e}  -> {ok_vol}")
    return 0 if max_px < 1e-6 and max_vol < 1e-4 else 1


def cmd_minute(args: argparse.Namespace) -> int:
    """分钟数据回溯深度探测 + 按周期估算行数/体积."""
    rd = load_sdk(Path(args.pybao))
    if rd is None:
        return 2

    code = _plain(args.symbol)
    print(f"--- 分钟回溯深度 (标的 {code}) ---")
    rows = fetch_minute(rd, code, None, None, "1m")
    if not rows:
        print("  [WARN] 无分钟数据")
        return 1
    n = len(rows)
    days = n // 240
    print(f"  1m 全量: {n} 条, {_minute_dt(rows[0]['date'])} ~ {_minute_dt(rows[-1]['date'])}, "
          f"折合 {days} 个交易日")

    print("\n  逐年前探 (每年 6 月是否有 1m 数据):")
    for year in range(2018, 2027):
        r = fetch_minute(rd, code, f"{year}0601", f"{year}0630", "1m")
        print(f"    {year}-06: {'有' if r else '无':<3} ({len(r)} 条)")

    print("\n--- 体积估算 (按 5400 只 A 股, 每条约 90 字节 parquet) ---")
    per_day = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}
    for freq, k in per_day.items():
        total = days * k * 5400
        gb = total * 90 / 1024**3
        mark = "推荐" if freq in ("30m", "60m") else ("偏大" if freq == "5m" else "过大")
        print(f"  {freq:<4}: {total:>12,} 行, 约 {gb:>6.1f} GB   {mark}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """覆盖率抽检: 指定区间内每只标的能取到多少条."""
    rd = load_sdk(Path(args.pybao))
    if rd is None:
        return 2

    print("--- 覆盖率抽检 ---")
    for sym in _PROBE_SYMBOLS:
        code = _plain(sym)
        rows = fetch_daily(rd, code, args.start, args.end)
        rng = f"{_day_str(rows[0]['date'])} ~ {_day_str(rows[-1]['date'])}" if rows else "无数据"
        print(f"  {sym:<11} {len(rows):>5} 条   {rng}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="free-stockdb 数据源验证 (只验证不导入)")
    ap.add_argument("action", choices=["probe", "daily", "minute", "coverage"])
    ap.add_argument("--pybao", default=str(_DEFAULT_PYBAO), help="pybao 目录路径")
    ap.add_argument("--symbols", default="", help="逗号分隔, 默认一组标杆")
    ap.add_argument("--symbol", default="600519.SH", help="minute 用的单只标的")
    ap.add_argument("--start", default="20260915")
    ap.add_argument("--end", default="20260926")
    ap.add_argument("--show", type=int, default=4, help="daily 每只显示几天")
    args = ap.parse_args(argv)

    fn = {"probe": cmd_probe, "daily": cmd_daily, "minute": cmd_minute, "coverage": cmd_coverage}
    return fn[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
