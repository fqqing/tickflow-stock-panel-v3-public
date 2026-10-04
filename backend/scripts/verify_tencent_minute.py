"""腾讯分钟 provider 离线/在线混合验收 (不依赖 running server)。

覆盖:
  1. 可用性探测 availability()
  2. 符号映射 600519.SH -> sh600519 (含非法/非沪深输入)
  3. get_minute 正常标的 -> schema 完整 + 极值与 open/close 自洽
  4. 非沪深代码被过滤港美股被剔除, 且不会发请求给上游
  5. 北交所回落路径 (打桩, 不打真实 stocksdk 避免 180s 超时)
  6. freq 映射 1m/5m -> m1/m5
  7. amount 估算与本地日K amount 量级一致
  8. 空 symbol 列表 / 全被过滤时进度回调仍触发

跑法: backend/.venv/Scripts/python.exe backend/scripts/verify_tencent_minute.py
"""
from __future__ import annotations

import logging
import time

import duckdb
import polars as pl

from app.plugins.tencent import provider as tencent

DATA_DIR = r"D:\project\GP\tickflow-stock-panel\data"

logging.basicConfig(level=logging.WARNING)

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


REQ_COLS = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}


def t1_availability() -> None:
    print("\n[T1] availability 探活")
    ok, reason = tencent.availability()
    check("探活返回可用", ok, reason)


def t2_symbol_map() -> None:
    print("\n[T2] 符号映射 app_to_tencent")
    cases = [
        ("600519.SH", "sh600519"),
        ("000001.SZ", "sz000001"),
        ("300750.SZ", "sz300750"),
        ("688981.SH", "sh688981"),
        ("920229.BJ", None),        # 北交所不支持 -> None
        ("AAPL.US", None),          # 非沪深
        ("00700.HK", None),
        ("", None),
    ]
    for src, want in cases:
        got = tencent.app_to_tencent(src)
        check(f"{src or '(空)'} -> {want}", got == want, f"得到 {got}")


def t3_get_minute() -> None:
    print("\n[T3] get_minute 正常标的")
    provider = tencent.TencentMinuteProvider()
    t0 = time.perf_counter()
    df = provider.get_minute(["600519.SH", "000001.SZ"], None, None, freq="1m")
    elapsed = time.perf_counter() - t0
    provider.close()
    check("返回非空", not df.is_empty(), f"{df.height} 行, {elapsed:.2f}s")
    if df.is_empty():
        return
    check("schema 覆盖必需列", REQ_COLS.issubset(set(df.columns)), str(df.columns))
    check("symbol 保持 app 格式",
          set(df["symbol"].unique().to_list()) <= {"600519.SH", "000001.SZ"},
          str(df["symbol"].unique().to_list()))
    check("datetime 为 Datetime 且无 null",
          df.schema["datetime"] == pl.Datetime("us") and df["datetime"].null_count() == 0)
    check("volume 非负", df["volume"].min() >= 0)
    # 核心: 第 2 位是 close 而非 high, 极值必须包住 open/close
    bad_h = df.filter(pl.col("high") < pl.max_horizontal("open", "close"))
    bad_l = df.filter(pl.col("low") > pl.min_horizontal("open", "close"))
    check("high >= max(open,close)", bad_h.height == 0, f"违例 {bad_h.height} 行")
    check("low <= min(open,close)", bad_l.height == 0, f"违例 {bad_l.height} 行")
    check("无 NaN/inf 价格",
          df.select(pl.col(["open", "high", "low", "close"]).is_finite().all()).row(0)[0])


def t4_amount_scale() -> None:
    print("\n[T4] amount 估算量纲 vs 本地日K")
    provider = tencent.TencentMinuteProvider()
    df = provider.get_minute(["600519.SH"], None, None, freq="1m")
    provider.close()
    if df.is_empty():
        check("拿到 600519 分钟数据", False)
        return
    day = df["datetime"].max().date()
    today = df.filter(pl.col("datetime").dt.date() == day)
    est = float(today["amount"].sum())
    vol = float(today["volume"].sum())
    con = duckdb.connect()
    pat = DATA_DIR.replace("\\", "/") + "/kline_daily_enriched/**/*.parquet"
    row = con.execute(
        f"SELECT amount, volume FROM read_parquet('{pat}') "
        f"WHERE symbol='600519.SH' AND date='{day}' LIMIT 1"
    ).fetchone()
    con.close()
    if not row:
        check("本地有当日 enriched 可比对", False)
        return
    ref_amount, ref_vol = float(row[0]), float(row[1])
    rel_amount = abs(est - ref_amount) / ref_amount if ref_amount else 1.0
    rel_vol = abs(vol - ref_vol) / ref_vol if ref_vol else 1.0
    # volume 用 1e-4 而非 1e-6: 腾讯返回的是整数「手」(如 28218.0), 而本地
    # enriched 的 volume 是浮点(同日 28218.3) —— 源间本身有亚手级差异,
    # 实测相对差 ~1.1e-5, 属精度噪声不是映射错误。
    check(f"volume 合计与本地一致 (日={day})", rel_vol < 1e-4,
          f"腾讯 {vol:,.4f} vs 本地 {ref_vol:,.4f}, 相对差 {rel_vol:.2e}")
    # amount 是「每分钟成交量 x 收盘价」累加的估算, 允许较大误差
    check("amount 估算量级正确 (<5%)", rel_amount < 0.05,
          f"腾讯 {est / 1e8:.2f}亿 vs 本地 {ref_amount / 1e8:.2f}亿, 相对差 {rel_amount:.2%}")


def t5_market_filter() -> None:
    print("\n[T5] 非沪深标的过滤与进度回调")
    provider = tencent.TencentMinuteProvider()
    seen: list[str] = []
    orig = tencent._fetch_bars

    def spy(tcode: str, period: str) -> list[list]:
        seen.append(tcode)
        return orig(tcode, period)

    tencent._fetch_bars = spy  # type: ignore[assignment]
    try:
        mixed = ["600519.SH", "00700.HK", "AAPL.US", "000001.SZ"]
        df = provider.get_minute(mixed, None, None, freq="1m")
        check("只向上游请求沪深代码", seen == ["sh600519", "sz000001"], f"实际 {seen}")
        if not df.is_empty():
            suffixes = {s.rsplit(".", 1)[-1] for s in df["symbol"].unique().to_list()}
            check("返回结果无 .US/.HK", not (suffixes & {"US", "HK"}), str(suffixes))
        # 全被过滤时进度仍回调
        calls: list[tuple[int, int]] = []
        empty_df = provider.get_minute(["AAPL.US", "00700.HK"], None, None,
                                       on_chunk_done=lambda c, t: calls.append((c, t)))
        check("全过滤时仍回调进度", calls == [(1, 1)], f"回调 {calls}")
        check("全过滤时返回空", empty_df.is_empty())
    finally:
        tencent._fetch_bars = orig  # type: ignore[assignment]
        provider.close()


def t6_bj_fallback() -> None:
    print("\n[T6] 北交所回落 (打桩, 避免真实 180s 超时)")
    provider = tencent.TencentMinuteProvider()
    captured: dict = {}

    class FakeSDK:
        def get_minute(self, symbols, s, e, freq="1m"):
            captured["symbols"] = list(symbols)
            captured["freq"] = freq
            return pl.DataFrame({
                "symbol": symbols,
                "datetime": [None] * len(symbols),
                "open": [1.0] * len(symbols),
                "high": [1.0] * len(symbols),
                "low": [1.0] * len(symbols),
                "close": [1.0] * len(symbols),
                "volume": [1.0] * len(symbols),
                "amount": [1.0] * len(symbols),
            })

        def close(self):
            return None

    import types

    fake_mod = types.ModuleType("app.plugins.stocksdk.provider")
    fake_mod.StockSDKProvider = FakeSDK  # type: ignore[attr-defined]
    import sys

    real = sys.modules.get("app.plugins.stocksdk.provider")
    sys.modules["app.plugins.stocksdk.provider"] = fake_mod
    try:
        calls: list[tuple[int, int]] = []
        df = provider.get_minute(["920229.BJ"], None, None, freq="5m",
                                 on_chunk_done=lambda c, t: calls.append((c, t)))
        check("北交所标的被路由到回落", captured.get("symbols") == ["920229.BJ"],
              str(captured.get("symbols")))
        check("freq 原样透传", captured.get("freq") == "5m", str(captured.get("freq")))
        check("回落结果非空", not df.is_empty(), f"{df.height} 行")
        check("进度回调到最后一步", calls and calls[-1] == (1, 1), f"{calls}")
    finally:
        if real is not None:
            sys.modules["app.plugins.stocksdk.provider"] = real
        else:
            sys.modules.pop("app.plugins.stocksdk.provider", None)
        provider.close()


def t7_freq_map() -> None:
    print("\n[T7] freq -> 腾讯周期映射")
    for freq, want in [("1m", "m1"), ("5m", "m5"), ("15m", "m15"),
                       ("30m", "m30"), ("60m", "m60"), ("", "m1"), ("7m", "m1")]:
        got = tencent._FREQ_TO_PERIOD.get(str(freq or "").strip().lower(), tencent._DEFAULT_PERIOD)
        check(f"freq={freq or '(空)'} -> {want}", got == want, f"得到 {got}")


def t8_edge_cases() -> None:
    print("\n[T8] 边界输入")
    provider = tencent.TencentMinuteProvider()
    df = provider.get_minute([], None, None)
    check("空 symbol 列表返回空表", df.is_empty())
    calls: list[tuple[int, int]] = []
    provider.get_minute([], None, None, on_chunk_done=lambda c, t: calls.append((c, t)))
    check("空列表不回调进度", calls == [], f"{calls}")
    provider.close()


if __name__ == "__main__":
    for fn in (t1_availability, t2_symbol_map, t3_get_minute, t4_amount_scale,
               t5_market_filter, t6_bj_fallback, t7_freq_map, t8_edge_cases):
        try:
            fn()
        except Exception as e:
            print(f"  [ERROR] {fn.__name__}: {type(e).__name__}: {e}")
    passed = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{len(_results)} 通过")
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL {name} — {detail}")
