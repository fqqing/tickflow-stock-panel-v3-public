"""任意数据源的量纲自检 (unit contract check).

背景
----
面板内部有一套隐式量纲约定(见 MEMORY: volume=手 / amount=元 / change_pct=小数),
但这套约定**没有任何机器校验**, 只散落在源码注释里。于是每次接新数据源都可能踩两条坑:

  1. 源间单位不同   -- 同样是 A 股, tushare vol=手 amount=千元; 某些源 vmount=万元。
  2. 源内板块差异   -- 腾讯系接口对科创板(688/689)的 vol 单位是**股**, 其余板块是**手**。
                      这种坑最难发现: 全样本中位数看起来正常, 只有分板块看才暴露。

因此本脚本的核心设计是 **按代码前缀分组统计**, 而不是看全局聚合值。

检测原理(无需外部基准, 纯内部自洽)
----------------------------------
对 OHLCV 成立恒等式:

    amount [元] = volume [手] * 100 [股/手] * vwap [元/股]

vwap 未知, 但必然落在 [low, high] 区间内, 故定义

    r = amount / (volume * 100)

正常数据应有 r in [low*0.9, high*1.1] 附近。若:

    r 整体 << close  且约等于 close/100  -> volume 单位是「股」
    r 整体 >> close  且约等于 close*1000 -> amount 单位是「千元」
    r 整体 >> close  且约等于 close*1e4  -> amount 单位是「万元」

同理用 close 与 [low,high] 的关系可查 OHLC 是否错位(如腾讯第 3 位是 close 不是 high)。

用法
----
    # 自检某个 provider 的某个数据集(抽样 60 只)
    python backend/scripts/verify_source_units.py --source tencent --dataset minute

    # 全量 / 指定标的 / 交叉验证本地已入库数据
    python backend/scripts/verify_source_units.py --source tushare --dataset daily --limit 200
    python backend/scripts/verify_source_units.py --source stocksdk --dataset realtime --symbols 600519.SH,000001.SZ

退出码: 0 = 全部通过, 1 = 存在 FAIL。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import polars as pl  # noqa: E402

from app.data_providers.custom import loader  # noqa: E402

# A 股板块划分(按代码前 3 位)。分板块是发现"源内板块差异"的关键。
_BOARD_NAMES = {
    "000": "深主板",
    "001": "深主板",
    "002": "中小板",
    "003": "深主板",
    "300": "创业板",
    "301": "创业板",
    "302": "创业板",
    "600": "沪主板",
    "601": "沪主板",
    "603": "沪主板",
    "605": "沪主板",
    "688": "科创板",
    "689": "科创板",
    "920": "北交所",
    "430": "北交所",
    "83": "北交所",
    "87": "北交所",
}

# 容差: vwap 与收盘价的合理偏离。日内振幅通常在 +-5%, 放宽到 12% 避免误报。
_RATIO_OK = (0.88, 1.12)

# 常见量纲错位倍数 -> 人话解释
_UNIT_HINTS = {
    0.01: "volume 单位疑似为「股」(应为「手」, 需 /100)",
    100.0: "volume 单位疑似为「百手」或 amount 多了 100 倍",
    0.001: "amount 单位疑似为「千元」未换算 (应 *1000)",
    1000.0: "amount 单位疑似为「千元」被当成「元」",
    0.0001: "amount 单位疑似为「万元」未换算 (应 *1e4)",
    10000.0: "amount 单位疑似为「万元」被当成「元」",
}


def _board(symbol: str) -> str:
    code = str(symbol).split(".")[0]
    for pre in ("000", "001", "002", "003", "300", "301", "302",
                "600", "601", "603", "605", "688", "689", "920", "430", "83", "87"):
        if code.startswith(pre):
            return pre
    return code[:2]


def _hint(med: float) -> str:
    """按中位数偏离倍数给出最可能的量纲误判原因。"""
    if med <= 0:
        return "比值为 0 或负 (数据本身异常)"
    for factor, text in sorted(_UNIT_HINTS.items()):
        lo, hi = factor * 0.7, factor * 1.4
        if lo <= med <= hi:
            return text
    return "偏离非整倍数, 可能是源间口径差异或脏数据"


def pick_symbols(dataset: str, limit: int, only: list[str]) -> list[str]:
    """抽样标的。优先取各板块代表股, 保证 688/920 等小板块也能被覆盖。"""
    if only:
        return only
    # 各板块 Round-Robin 均匀取, 保证小板块(688 科创板 / 920 北交所)一定被覆盖。
    # ⚠️ 这点很关键: 若按 symbol 字典序截断, 抽样会全落在 000/300/600 等大板块,
    #    恰好漏掉存在量纲差异的小板块 -- 自检脚本自身的盲区会变成漏报源。
    buckets: dict[str, list[str]] = {}
    try:
        import duckdb

        con = duckdb.connect()
        rows = con.execute(
            "SELECT DISTINCT symbol FROM read_parquet(?) WHERE symbol IS NOT NULL",
            [(ROOT.parent / "data" / "instruments" / "**" / "*.parquet").as_posix()],
        ).fetchall()
        con.close()
        syms = sorted({r[0] for r in rows if str(r[0]).endswith((".SH", ".SZ", ".BJ"))})
    except Exception:
        return []
    for s in syms:
        buckets.setdefault(_board(s), []).append(s)
    order = sorted(buckets)
    out: list[str] = []
    i = 0
    while True:
        progressed = False
        for b in order:
            if i < len(buckets[b]):
                out.append(buckets[b][i])
                progressed = True
                if limit and len(out) >= limit:
                    return out
        if not progressed:
            break
        i += 1
    return out


def check_frame(df: pl.DataFrame, dataset: str) -> int:
    """对给定 DataFrame 做全套量纲校验, 返回 FAIL 数。"""
    if df.is_empty():
        print("  [INFO] provider 返回空, 跳过 (不代表通过, 可能是源已失效)")
        return 0

    need = {"symbol", "volume", "amount"}
    if dataset in {"daily", "minute"}:
        need |= {"open", "high", "low", "close"}
    missing = need - set(df.columns)
    if missing:
        print(f"  [FAIL] 返回缺少必需列: {sorted(missing)}")
        return 1

    # realtime 的行契约用 last_price 而不是 close。不归一化的话下面第 2) 项的
    # 核心量纲检查会被整段跳过(静默放行) —— 自检脚本自身的盲区等于漏报。
    if "close" not in df.columns and "last_price" in df.columns:
        df = df.with_columns(pl.col("last_price").alias("close"))

    print(f"  返回 {df.height} 行 / {df['symbol'].n_unique()} 只")
    failures = 0

    # ---- 1) OHLC 极值关系 (可捕获列错位, 如腾讯第 3 位是 close 不是 high) ----
    if {"open", "high", "low", "close"} <= set(df.columns):
        bad = df.filter(
            (pl.col("high") < pl.max_horizontal("open", "close"))
            | (pl.col("low") > pl.min_horizontal("open", "close"))
            | (pl.col("low") <= 0)
        )
        if bad.height:
            print(f"  [FAIL] OHLC 极值违例 {bad.height} 行 (疑似列错位/脏数据)")
            failures += 1
        else:
            print(f"  [OK]   OHLC 极值关系 {df.height} 行全部合法")

    # ---- 2) volume/amount 量纲, 按板块分组 (核心) ----
    has_px = {"close"} <= set(df.columns)
    base = df.filter((pl.col("volume") > 0) & (pl.col("amount") > 0))
    if base.is_empty():
        print("  [FAIL] 无 volume>0 且 amount>0 的行")
        return failures + 1

    if has_px:
        base = base.with_columns(
            (pl.col("amount") / (pl.col("volume") * 100.0)).alias("vwap_impl")
        )
        base = base.with_columns(
            (pl.col("vwap_impl") / pl.col("close")).alias("r")
        ).filter(pl.col("r") > 0)
        groups = (
            base.group_by("symbol")
            .agg([
                pl.col("r").median().alias("r_med"),
                pl.col("close").median().alias("px"),
            ])
            .with_columns(
                pl.col("symbol").map_elements(_board, return_dtype=pl.String).alias("board")
            )
        )
        print()
        print("  === volume/amount 量纲 (amount/(volume*100) / close, 应 ~1.0) ===")
        print(f"  {'板块':<8}{'名称':<8}{'n':>5}{'比值中位':>11}{'判定':>8}")
        rows = groups.group_by("board").agg(
            pl.col("r_med").median().alias("m"), pl.len().alias("n")
        ).sort("board")
        for board, med, cnt in zip(
            rows["board"].to_list(), rows["m"].to_list(), rows["n"].to_list(), strict=False
        ):
            ok = _RATIO_OK[0] <= med <= _RATIO_OK[1]
            if not ok:
                failures += 1
            verdict = "OK" if ok else "FAIL"
            name = _BOARD_NAMES.get(board, board)
            print(f"  {board:<8}{name:<8}{cnt:>5}{med:>11.4f}{verdict:>8}")
            if not ok:
                print(f"          -> {_hint(med)}")

        all_med = statistics.median(groups["r_med"].to_list())
        print(f"  {'全局中位':<16}{len(groups):>5}{all_med:>11.4f}")
        if abs(all_med - 1.0) < 0.12 and failures:
            print("  [WARN] 全局中位数正常但存在板块 FAIL -- 这正是"
                  "「源内板块差异」的典型特征, 切勿只看聚合值!")

    # ---- 3) change_pct 小数 vs 百分数 ----
    if "change_pct" in df.columns:
        vals = df["change_pct"].drop_nulls()
        if vals.len():
            mx = float(vals.abs().max())
            # A 股个股日涨跌限 +-10%(ST 5%, 创业板/科创板 20%), 指数亦然。
            # 小数口径应 <=0.30; 百分数口径会 <=30。
            if mx > 0.5:
                lo_s = float((vals.abs() > 0.2).sum()) / vals.len()
                print(f"  [FAIL] change_pct 疑似百分数口径 (max={mx:.2f}, "
                      f"超 20% 占比 {lo_s:.0%}) -- 内部约定应为小数")
                failures += 1
            else:
                print(f"  [OK]   change_pct 为小数口径 (max={mx:.4f})")

    # ---- 4) turnover_rate 百分数 vs 小数 ----
    # ⚠️ 口径随数据集不同, 不能一刀切:
    #   daily/minute -> 落盘即百分数 (5.0 = 5%)
    #   realtime     -> **入口契约是小数** (0.05 = 5%), quote_service._build_quote_extra
    #                   会再 *100 存成百分数。若这里按百分数返, 换手率会被放大 100 倍。
    if "turnover_rate" in df.columns:
        vals = df["turnover_rate"].drop_nulls()
        if vals.len():
            med = float(vals.median())
            if med is None:
                pass
            elif dataset == "realtime":
                if med > 1.0:
                    print(f"  [FAIL] turnover_rate 疑似百分数口径 (中位={med:.3f}) "
                          "-- realtime 入口约定为小数 (0.05 = 5%)")
                    failures += 1
                else:
                    print(f"  [OK]   turnover_rate 为小数口径 (中位={med:.5f})")
            elif med < 0.01:
                print(f"  [FAIL] turnover_rate 疑似小数口径 (中位={med:.5f}) "
                      "-- 内部约定应为百分数")
                failures += 1
            else:
                print(f"  [OK]   turnover_rate 为百分数口径 (中位={med:.3f}%)")

    return failures


def _fetch_realtime(pv, symbols: list[str]) -> pl.DataFrame:
    """实时快照。provider 契约以**无参** get_realtime() 为主(全市场)。

    无参 provider 拉全市场后再按抽样清单过滤 —— 否则 60 只抽样会被 5500 行全市场
    结果淹没, 失去抽样意义(也更慢)。接受 symbols 的 provider 直接按码单拉。
    """
    import inspect

    getter = pv.get_realtime
    try:
        takes_symbols = "symbols" in inspect.signature(getter).parameters
    except (TypeError, ValueError):
        takes_symbols = False
    if takes_symbols:
        rows = getter(symbols=symbols)
    else:
        want = set(symbols)
        rows = [r for r in (getter() or []) if str(r.get("symbol")) in want]
    return pl.DataFrame(rows or [])


def run(args: argparse.Namespace) -> int:
    loader.load_all()
    if not loader.is_custom_provider(args.source):
        available = sorted(loader.names())
        print(f"[FAIL] 未知数据源 '{args.source}', 可用: {available}")
        return 1

    pv = loader.get_provider(args.source)
    if args.dataset == "minute" and not hasattr(pv, "get_minute"):
        print(f"[FAIL] {args.source} 未实现 get_minute")
        return 1
    if args.dataset == "daily" and not hasattr(pv, "get_daily"):
        print(f"[FAIL] {args.source} 未实现 get_daily")
        return 1

    print(f"=== 量纲自检: source={args.source} dataset={args.dataset} ===")
    symbols = pick_symbols(args.dataset, args.limit, args.symbols)
    if not symbols:
        print("[FAIL] 无法取得标的列表")
        return 1
    print(f"抽样 {len(symbols)} 只: {symbols[:6]}{' ...' if len(symbols) > 6 else ''}")

    from datetime import datetime, timedelta

    end = datetime.now()
    start = end - timedelta(days=args.days)

    try:
        if args.dataset == "minute":
            df = pv.get_minute(symbols, start, end, "stock", freq=args.freq)
        elif args.dataset == "daily":
            df = pv.get_daily(symbols, start, end, "stock")
        else:
            df = _fetch_realtime(pv, symbols)
    except Exception as exc:
        print(f"[FAIL] provider 抛异常: {type(exc).__name__}: {exc}")
        return 1
    finally:
        closer = getattr(pv, "close", None)
        if callable(closer):
            closer()

    print()
    failures = check_frame(df, args.dataset)
    print()
    if failures:
        print(f"=== 结果: {failures} 项 FAIL ===")
        return 1
    print("=== 结果: 全部通过 ===")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="数据源量纲自检")
    ap.add_argument("--source", required=True, help="数据源名 (tushare/stocksdk/tencent)")
    ap.add_argument("--dataset", default="minute", choices=["minute", "daily", "realtime"])
    ap.add_argument("--limit", type=int, default=60, help="抽样标的数")
    ap.add_argument("--symbols", default="", help="逗号分隔的指定标的, 覆盖 --limit")
    ap.add_argument("--days", type=int, default=7, help="回溯天数")
    ap.add_argument("--freq", default="1m", help="分钟频率")
    args = ap.parse_args()
    if args.symbols:
        args.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        args.symbols = []
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
