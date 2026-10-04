"""历史分钟回补可行性探针(只读, 不写数据目录)。

背景
----
常驻链路已切到腾讯 mkline, 但它单次上限约 482 根(m1 约 2 个交易日),
且**不支持 beg/end**, 因此拿不到更早的历史。本脚本专门回答一个问题:
"过去的分钟数据能不能补回来"。

2026-09-28 实测结论(重要, 别重复排查)
-------------------------------------
没有任何免费公开接口提供"按任意历史日期"取 1 分钟数据的能力 ——
这些接口的设计用途是给 App 看**近期分时**, 不是给回测用的。逐个实测:

| 源                    | 1 分钟深度      | 能否指定历史日期 | 备注                              |
|-----------------------|-----------------|------------------|-----------------------------------|
| 腾讯 mkline           | 482 根 ~2 交易日 | 否(beg/end 无效) | 常驻链路在用; 北交所 0 根         |
| 腾讯 day/query        | 267 根 x 5 交易日 | 否(date 被忽略)  | **北交所有数据**; 带真实 amount   |
| 新浪 getKLineData     | 1023 根 ~4.3 天  | 否(只有 datalen) | datalen > 1023 返回 0 根          |
| 东财 push2his klt=1   | 240 根 = 1 天    | 否               | **打几个请求就封 IP**             |
| 东财 push2his klt>=5  | 理论无限         | 是               | 同样被 IP 封; 4s 间隔仍全 BLOCKED |
| Tushare stk_mins      | 真历史 OK        | 是               | **限速 1 次/小时**, 大量补不可行  |
| baostock              | 5min 起, 真历史  | 是               | **本机连不上**(握手超时)         |

=> 能立刻兑现的只有腾讯 day/query 的"最近 5 个交易日", 且它比 mkline 多两样:
   支持北交所、带真实成交额(现在 mkline 的 amount 是 vol*100*close 估算)。

=> 真正的长期历史只能靠**每天按时同步累积**, 或接受 5 分钟及以上粒度走付费源。

跑法: backend/.venv/Scripts/python.exe backend/scripts/probe_history_backfill.py
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

DATA_DIR = Path(r"D:\project\GP\tickflow-stock-panel\data")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TX_DAY_QUERY = "https://web.ifzq.gtimg.cn/appstock/app/day/query"
TX_MKLINE = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"


def fetch(url: str, timeout: int = 25, referer: str = "https://gu.qq.com/") -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def to_tx(sym: str) -> str | None:
    code, _, suf = sym.partition(".")
    suf = suf.upper()
    if suf == "SH":
        return "sh" + code
    if suf == "SZ":
        return "sz" + code
    if suf == "BJ":
        return "bj" + code
    return None


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def tx_day_query(code: str, date: str = "") -> dict[str, list[str]]:
    """腾讯历史分时(按 code)。返回 {交易日: [行...]}, 行格式 'HHMM px cumVol cumAmt'。"""
    url = f"{TX_DAY_QUERY}?code={code}" + (f"&date={date}" if date else "")
    status, body = fetch(url)
    if status != 200:
        return {}
    node = (json.loads(body).get("data") or {}).get(code) or {}
    return {d["date"]: d["data"] for d in (node.get("data") or [])}


def tx_mkline(code: str, period: str) -> list[list]:
    status, body = fetch(f"{TX_MKLINE}?param={code},{period},,")
    if status != 200:
        return []
    node = (json.loads(body).get("data") or {}).get(code) or {}
    return node.get(period) or []


def inventory() -> None:
    """盘点本地分钟库现有覆盖, 量化回补缺口。"""
    section("本地分钟库盘点 (data/kline_minute)")
    con = duckdb.connect()
    pat = (DATA_DIR / "kline_minute" / "**" / "*.parquet").as_posix()
    rows = con.execute(
        f"""
        SELECT str_split(filename, 'date=')[2] AS day,
               count(*)                        AS n,
               count(DISTINCT symbol)          AS syms
        FROM read_parquet('{pat}', filename=true) GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    if not rows:
        print("  本地暂无分钟数据")
        con.close()
        return
    for day, n, syms in rows:
        print(f"  {str(day)[:10]:<12} {n:>10,} 行 {syms:>6} 只")
    print(f"  合计 {len(rows)} 个交易日, {sum(r[1] for r in rows):,} 行")

    present = {str(r[0])[:10] for r in rows}
    all_days = [
        str(d[0])
        for d in con.execute(
            f"SELECT DISTINCT date FROM read_parquet("
            f"'{(DATA_DIR / 'kline_daily_enriched' / '**' / '*.parquet').as_posix()}') "
            f"WHERE date >= DATE '2026-08-01' ORDER BY date"
        ).fetchall()
    ]
    missing = [d for d in all_days if d not in present]
    print(f"\n  2026-08-01 起共 {len(all_days)} 个交易日, 已有分钟 {len(present)} 个")
    print(f"  缺失 {len(missing)} 个: {missing[:10]}"
          + (" ..." if len(missing) > 10 else ""))
    con.close()


def probe_day_query() -> None:
    """腾讯 day/query: 验证 date 是否被忽略, 以及滚动窗口有多深。"""
    section("腾讯 day/query —— date 参数是否被服务端忽略?")
    base = tx_day_query("sh600519")
    labels = sorted(base)
    print(f"  不带 date: {len(base)} 个交易日 {labels}")
    same = True
    for d in ("20260901", "20260102", "20240102"):
        got = tx_day_query("sh600519", d)
        if sorted(got) != labels:
            same = False
            print(f"  date={d} -> {sorted(got)}  (不同!)")
    print(f"  => date 参数 {'无效(总是返回最近 N 天)' if same else '有效'}")

    if labels:
        rows = base[labels[-1]]
        print(f"\n  最新交易日 {labels[-1]}: {len(rows)} 行")
        print(f"    首行 {rows[0]}")
        print(f"    末行 {rows[-1]}")
        cum = [float(r.split()[2]) for r in rows]
        mono = all(cum[i] >= cum[i - 1] for i in range(1, len(cum)))
        print(f"    第 3 列单调 => {'累计量(需差分)' if mono else '已是每分钟'}")


def probe_bj() -> None:
    """腾讯 mkline 北交所是空的, day/query 是否有救?"""
    section("北交所分钟覆盖对比")
    con = duckdb.connect()
    pat = (DATA_DIR / "instruments" / "**" / "*.parquet").as_posix()
    bj = con.execute(
        f"SELECT symbol FROM read_parquet('{pat}') WHERE symbol LIKE '%.BJ' LIMIT 3"
    ).fetchall()
    con.close()
    for (sym,) in bj:
        code = to_tx(sym) or ""
        mk = len(tx_mkline(code, "m5"))
        dq = sum(len(v) for v in tx_day_query(code).values())
        print(f"  {sym:<12} mkline(m5) {mk:>5} 根 | day/query {dq:>6} 行")


def probe_amount_gain() -> None:
    """day/query 自带成交额, 对比 mkline 的估算值。"""
    section("成交额精度: day/query 实测值 vs mkline 估算值")
    days = tx_day_query("sh600519")
    if not days:
        return
    label = sorted(days)[-1]
    rows = days[label]
    real_amt = float(rows[-1].split()[3])
    real_vol = float(rows[-1].split()[2])
    est = sum(float(r[5]) * 100 * float(r[2]) for r in tx_mkline("sh600519", "m1")
              if str(r[0]).startswith(label))
    print(f"  {label}: 真实 amount={real_amt / 1e8:.4f} 亿, volume={real_vol:,.0f} 手")
    print(f"  mkline 估算 amount={est / 1e8:.4f} 亿 (差异 {(est - real_amt) / real_amt:+.2%})")


def probe_throughput() -> None:
    """全市场回补的速度天花板。"""
    section("day/query 吞吐 (决定每日自动累积的成本)")
    con = duckdb.connect()
    pat = (DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet").as_posix()
    syms = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT symbol FROM read_parquet('{pat}') LIMIT 2000"
        ).fetchall()
    ]
    con.close()
    picked, seen = [], set()
    for s in syms:
        suf = s.rsplit(".", 1)[-1]
        if suf in ("SH", "SZ") and suf not in seen:
            picked.append(s)
        if len(picked) >= 24:
            break

    def one(sym: str) -> int:
        return sum(len(v) for v in tx_day_query(to_tx(sym) or "").values())

    for workers in (8, 16):
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            res = list(ex.map(one, picked))
        el = time.perf_counter() - t0
        ok = sum(1 for n in res if n > 0)
        rate = ok / el if el else 0.0
        print(f"  workers={workers:<3} 成功 {ok}/{len(picked)} | {el:.2f}s | {rate:.1f} 只/s")
        if rate:
            print(f"     全A 5569 只: {5569 / rate:.0f}s")


def conclusion() -> None:
    section("结论")
    print("  1) 1 分钟历史无法回补 —— 免费公开接口均只保留最近 2~5 个交易日。")
    print("  2) 立刻可兑现: day/query 把窗口从 2 天提到 5 天, 顺带补上北交所与真实成交额。")
    print("  3) 更久的历史只能靠每日自动累积, 或降级到 5 分钟粒度走付费/受限数据源。")


if __name__ == "__main__":
    inventory()
    probe_day_query()
    probe_bj()
    probe_amount_gain()
    probe_throughput()
    conclusion()
