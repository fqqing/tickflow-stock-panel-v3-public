"""分钟数据源选型探针 —— 腾讯 mkline vs 现用 stocksdk(东财)。

只读脚本, 不写任何数据目录。用于数据源选型/复盘复测, 不属于常驻链路。

结论摘要(2026-09-28 实测):
  腾讯 mkline  ~158 只/s, 沪深全支持, 零鉴权; 但单次上限约 482 根 bar
               (m1≈2 交易日 / m5≈11 交易日), 且**不支持北交所分钟数据**。
  stocksdk     同日实测单只 27s 后仍 0 根 + errors, 东财分钟接口已基本失效
               (bridge.mjs 注释早有记载: 分钟接口间歇性空返回, 单发成功率约 1/5)。

跑法: backend/.venv/Scripts/python.exe backend/scripts/probe_minute_sources.py
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import duckdb

DATA_DIR = r"D:\project\GP\tickflow-stock-panel\data"
MKLINE = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
    # 实测不带 Referer 也能通, 但带上更贴近浏览器来源, 降低被拦概率。
    "Referer": "https://gu.qq.com/",
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def app_to_tencent(sym: str) -> str | None:
    """600519.SH -> sh600519。北交所腾讯 mkline 无分钟数据, 但仍要探测故保留映射。"""
    code, _, suf = sym.partition(".")
    suf = suf.upper()
    if suf == "SH":
        return "sh" + code
    if suf == "SZ":
        return "sz" + code
    if suf == "BJ":
        return "bj" + code
    return None


def fetch(url: str, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def parse_payload(body: str) -> dict:
    if "=" in body[:40]:  # jsonp 形态
        body = body.split("=", 1)[1]
    return json.loads(body)


def fetch_rows(tcode: str, period: str, count: str = "") -> list[list]:
    url = f"{MKLINE}?param={tcode},{period},,{count}"
    status, body = fetch(url)
    if status != 200:
        return []
    node = (parse_payload(body).get("data") or {}).get(tcode) or {}
    return node.get(period) or []


def local_symbols(limit_per_exchange: int = 25) -> list[str]:
    con = duckdb.connect()
    pat = DATA_DIR.replace("\\", "/") + "/kline_daily_enriched/**/*.parquet"
    rows = con.execute(
        f"SELECT DISTINCT symbol FROM read_parquet('{pat}') LIMIT 4000"
    ).fetchall()
    con.close()
    buckets: dict[str, list[str]] = {"SH": [], "SZ": [], "BJ": []}
    for (sym,) in rows:
        suf = sym.rsplit(".", 1)[-1]
        if suf in buckets and len(buckets[suf]) < limit_per_exchange:
            buckets[suf].append(sym)
    return buckets["SH"] + buckets["SZ"] + buckets["BJ"]


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def probe_period_span() -> None:
    """各周期单次能拿多少根 / 覆盖多长跨度。"""
    section("腾讯 mkline 各周期覆盖能力 (sh600519)")
    for period in ("m1", "m5", "m15", "m30", "m60"):
        parts = []
        for count in ("320", ""):
            rows = fetch_rows("sh600519", period, count)
            label = f"count={count or '空'}"
            if rows:
                first, last = str(rows[0][0]), str(rows[-1][0])
                parts.append(f"{label}: {len(rows)}根 [{first}->{last}]")
            else:
                parts.append(f"{label}: 0根")
        print(f"  {period:<4} " + " | ".join(parts))


def probe_count_cap() -> None:
    """count 参数上限: 传大值是否被截断, 留空是否反而更多。"""
    section("count 参数上限探测 (sh600519, m1)")
    for count in ("10", "60", "320", "1000", "5000", ""):
        rows = fetch_rows("sh600519", "m1", count)
        span = f"{rows[0][0]}~{rows[-1][0]}" if rows else "-"
        print(f"  count={count or '空':<6} -> {len(rows):>5} 根 [{span}]")


def probe_bj() -> None:
    """北交所: mkline(分钟) 与 qt(实时) 分开验证。"""
    section("北交所覆盖")
    con = duckdb.connect()
    pat = DATA_DIR.replace("\\", "/") + "/instruments/**/*.parquet"
    bj = con.execute(
        f"SELECT symbol FROM read_parquet('{pat}') WHERE symbol LIKE '%.BJ' LIMIT 3"
    ).fetchall()
    con.close()
    for (sym,) in bj:
        tcode = app_to_tencent(sym) or ""
        rows = fetch_rows(tcode, "m5")
        status, body = fetch(f"https://qt.gtimg.cn/q={tcode}")
        rt_ok = status == 200 and "~" in body
        print(f"  {sym:<12} mkline(m5) {len(rows):>3} 根 | qt 实时 {'可用' if rt_ok else '不可用'}")


def probe_semantics() -> None:
    """量纲核对: 第6字段=成交量(手), 第8字段=换手率基点(不是成交额)。"""
    section("字段语义核对 (sh600519 / 600519.SH)")
    rows = fetch_rows("sh600519", "m1")
    print(f"  原始末行: {rows[-1]}")
    day = str(rows[-1][0])[:8]
    today = [r for r in rows if str(r[0]).startswith(day)]
    vol = sum(float(r[5]) for r in today)
    turnover_bp = sum(float(r[7]) for r in today)
    avg_px = sum(float(r[2]) for r in today) / len(today)
    print(f"  交易日 {day}: {len(today)} 根")
    print(f"    [5] 成交量合计 = {vol:,.0f} 手")
    print(f"    [7] 换手率基点 = {turnover_bp:.2f} bp -> {turnover_bp / 100:.3f}%")
    print(f"    均价 {avg_px:.2f} -> 估算成交额 {vol * 100 * avg_px / 1e8:.2f} 亿")

    con = duckdb.connect()
    pat = DATA_DIR.replace("\\", "/") + "/kline_daily_enriched/**/*.parquet"
    row = con.execute(
        f"SELECT date, amount, volume, turnover_rate FROM read_parquet('{pat}') "
        f"WHERE symbol='600519.SH' ORDER BY date DESC LIMIT 1"
    ).fetchone()
    con.close()
    if row:
        print(f"  本地 enriched: date={row[0]} amount={row[1] / 1e8:.2f}亿 "
              f"volume={row[2]:,.0f} turnover_rate={row[3]}")


def probe_throughput(syms: list[str], period: str, workers: int) -> float:
    """压测吞吐, 返回 只/秒。"""
    pairs = [(s, app_to_tencent(s)) for s in syms]
    pairs = [(s, c) for s, c in pairs if c]

    def one(pair: tuple[str, str]) -> int:
        _s, c = pair
        return len(fetch_rows(c, period))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        counts = list(ex.map(one, pairs))
    elapsed = time.perf_counter() - t0
    ok = sum(1 for n in counts if n > 0)
    rate = ok / elapsed if elapsed else 0.0
    print(f"  {period:<4} workers={workers:<3} 成功 {ok}/{len(pairs)} "
          f"| {elapsed:.2f}s | {rate:.1f} 只/s")
    return rate


def probe_stocksdk_baseline() -> None:
    """对照组: 现用 stocksdk(东财) 单只单周期的实测量。"""
    section("对照: 现用 stocksdk(东财) 单只 m1")
    try:
        from app.plugins.stocksdk import bridge
    except Exception as e:
        print(f"  跳过 (不可导入: {type(e).__name__}: {e})")
        return
    for sym in ("600519.SH", "000001.SZ"):
        t0 = time.perf_counter()
        try:
            res = bridge.run_job({"op": "minute", "symbols": [sym], "period": "1"}, timeout=60)
            n = sum(len(v) for v in (res.get("rows") or {}).values())
            errs = list(res.get("errors") or {})
            print(f"  {sym}: {n} 根, {time.perf_counter() - t0:.2f}s"
                  + (f", errors={errs[:2]}" if errs else ""))
        except Exception as e:
            print(f"  {sym}: 失败 {type(e).__name__}: {str(e)[:110]}")


if __name__ == "__main__":
    probe_period_span()
    probe_count_cap()
    probe_bj()
    probe_semantics()

    syms = local_symbols()
    section(f"吞吐压测 (本地真实标的 {len(syms)} 只)")
    rate = 0.0
    for period in ("m1", "m5"):
        for workers in (8, 24):
            rate = max(rate, probe_throughput(syms, period, workers))
        print()
    if rate:
        print(f"  最佳吞吐 {rate:.1f} 只/s")
        print(f"  -> focus 1000 只单次请求: {1000 / rate:.1f}s")
        print(f"  -> focus 1000 只覆盖5天(5次/只): {5000 / rate:.1f}s")
        print(f"  -> 全A 5569 只覆盖5天: {5569 * 5 / rate:.1f}s")

    probe_stocksdk_baseline()
