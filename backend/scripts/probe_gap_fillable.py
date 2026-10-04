"""探测「项目现有数据源」能否补齐 08-28 ~ 09-21 的历史分钟缺口.

分别验证:
  1) 腾讯 mkline (现役插件 tencent/provider.py 用的) 实际能回溯几个交易日
  2) 腾讯 day/query 的 5 天滚动窗口边界
  3) Tushare stk_mins 按 trade_date 拉全市场是否可行(行数上限 / 限速)
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.request
from datetime import datetime
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

SYMS = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH", "000725.SZ"]


def http_get(url: str, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return resp.read().decode("utf-8", "replace")


def to_tcode(sym: str) -> str:
    code, _, mkt = sym.partition(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt, "sh") + code


def probe_mkline() -> None:
    print("=== 1) 腾讯 mkline 实际回溯深度 (count 留空, 官方给 ~482 根) ===")
    for sym in SYMS[:3]:
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={to_tcode(sym)},m1,,"
        try:
            raw = json.loads(http_get(url))
            node = raw["data"][to_tcode(sym)]["m1"]
        except Exception as exc:
            print(f"   {sym} FAIL: {type(exc).__name__} {exc}")
            continue
        days: dict[str, int] = {}
        for row in node:
            days[row[0][:8]] = days.get(row[0][:8], 0) + 1
        keys = sorted(days)
        print(f"   {sym}: {len(node)} 根, {len(keys)} 天, {keys[0]} ~ {keys[-1]}, 每天 {days[keys[-1]]} 根")
    print("   -> 结论: mkline 是滚动窗口, 只能拿到最近这些天, 无法指定更早日期\n")


def probe_day_query() -> None:
    print("=== 2) 腾讯 day/query 窗口边界 (注意 host 是 web.ifzq, 参数是 code=) ===")
    sym = SYMS[0]
    tcode = to_tcode(sym)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={tcode}"
    try:
        raw = json.loads(http_get(url))
        node = raw["data"][to_tcode(sym)]["data"]
    except Exception as exc:
        print(f"   FAIL 取数: {type(exc).__name__} {exc}")
        return
    wrapper = raw.get("data")
    if isinstance(wrapper, list):
        wrapper = wrapper[0] if wrapper else {}
    node = wrapper.get(to_tcode(sym)) if isinstance(wrapper, dict) else None
    if node is None:
        print(f"   结构异常: keys={list(raw)[:5]}  body={str(raw)[:160]}")
        return
    rows = node.get("data", node) if isinstance(node, dict) else node
    days: dict[str, int] = {}
    for item in rows:
        key = str(item["date"]).replace("-", "")
        days.setdefault(key, 0)
        days[key] += len(item.get("data", []))
    keys = sorted(days)
    print(f"   {sym}: {len(keys)} 天, {keys[0]} ~ {keys[-1]}, 每天约 {days[keys[-1]]} 根")
    print("   -> date 参数被服务端忽略(详见 §十一实测), 同样只能取最近窗口\n")


def read_tushare_token() -> str:
    path = Path(__file__).resolve().parents[2] / ".env"
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, val = line.partition("=")
        if sep and "TUSHARE" in key.upper():
            return val.strip().strip('"').strip("'")
    return ""


def probe_tushare() -> None:
    print("=== 3) Tushare stk_mins 按 trade_date 拉全市场 ===")
    token = read_tushare_token()
    if not token:
        print("   未读到 token, 跳过")
        return
    try:
        import tushare as ts
    except Exception as exc:
        print(f"   tushare 未安装: {exc}")
        return
    pro = ts.pro_api(token)
    # 3a: 单只票带日期区间, 看能回溯多远
    t0 = time.perf_counter()
    try:
        df = pro.stk_mins(
            ts_code="600519.SH", freq="1min",
            start_date="2026-08-20 09:00:00", end_date="2026-08-29 15:00:00",
        )
        print(f"   3a 单票 8/20~8/29: {len(df)} 行, {time.perf_counter() - t0:.1f}s")
        if len(df):
            days = sorted({str(d)[:10] for d in df["trade_time"]})
            print(f"      覆盖 {len(days)} 天: {days[0]} ~ {days[-1]}")
    except Exception as exc:
        print(f"   3a FAIL: {str(exc)[:120]}")

    # 3b: 按 trade_date 拉全市场一天
    time.sleep(3)
    try:
        df2 = pro.stk_mins(trade_date="2026-08-28", freq="1min")
        print(f"   3b trade_date=2026-08-28 全市场: {len(df2)} 行")
        if len(df2):
            print(f"      标的数 {df2['ts_code'].nunique()}, 列 {list(df2.columns)[:8]}")
    except Exception as exc:
        print(f"   3b FAIL: {str(exc)[:120]}")
    print()


def main() -> None:
    print(f"探测时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    probe_mkline()
    probe_day_query()
    probe_tushare()
    print("=== 判定 ===")
    print("若腾讯窗口最早日期 >> 2026-09-21, 则 08-28~09-21 的缺口用现有网络源无法补齐.")
    print("唯一的长期手段: 每日定时同步, 让未来每一天都被完整留住.")


if __name__ == "__main__":
    main()
