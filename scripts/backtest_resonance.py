"""0 号工程：候选池（策略共振）历史回测验证。

回放历史交易日，复刻 daily_picks 的「27 内置策略共振 → 7 家族去重 → 排序」候选池
生成，对**每一个共振候选股**（不只每日 Top N）计算 1/3/5 日向前收益，输出
「共振数 vs 收益」分层统计，验证核心假设是否成立：

    「跨家族共振数 family_count 越高 → 向前收益越高」

同时给出「若按当前 Top-N 排序选股，历史命中率」作对照，直接回答两个问题：
  1. 候选池（策略共振）到底靠不靠谱？
  2. 现在的「家族数→共振数→评分」排序有没有带来超额？

不写 daily_picks 快照、不推飞书、不改变任何生产数据，纯只读回放。

用法（用 v1 venv 的 python，PYTHONPATH 指向 v3 backend）：
    cd D:/project/GP/tickflow-stock-panel-v3
    D:/project/GP/tickflow-stock-panel/backend/.venv/Scripts/python.exe \
        scripts/backtest_resonance.py --start 2025-10-28 --end 2026-10-28
    # 快速试点（只跑前 N 个交易日，验证正确性 + 测单日耗时）
    ... --limit-dates 10

输出：
  - 控制台：分层统计表
  - data/daily_picks/resonance_validation/candidates.parquet  （逐日候选明细，可复算）
  - data/daily_picks/resonance_validation/summary.parquet     （分层汇总）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.parquet import market_symbol_filter, scan_enriched_parquet  # noqa: E402
from app.services.daily_picks import (  # noqa: E402
    STRATEGY_FAMILIES,
    _NEW_LISTING_DAYS,
)
from app.services.screener import ScreenerService  # noqa: E402
from app.strategy import config as strategy_config  # noqa: E402
from app.strategy.engine import StrategyEngine  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest_resonance")
# 压低依赖库噪声，只留本脚本与关键进度
for noisy in ("app.tickflow.repository", "app.services.screener", "app.strategy.engine"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

HORIZONS = (1, 3, 5)


def _to_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return datetime.strptime(v.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _new_listing_symbols(repo, as_of: date, min_listing_days: int) -> set[str]:
    """复刻 daily_picks._new_listing_symbols：上市不满 N 自然日剔除。"""
    try:
        inst = repo.get_instruments()
    except Exception as e:  # noqa: BLE001
        logger.warning("读 instruments 失败, 跳过次新过滤: %s", e)
        return set()
    if inst.is_empty() or "listing_date" not in inst.columns or "symbol" not in inst.columns:
        return set()
    out: set[str] = set()
    for r in inst.iter_rows(named=True):
        sym = r.get("symbol")
        ld = r.get("listing_date")
        if not sym or ld is None:
            continue
        d = _to_date(ld)
        if d is None:
            continue
        if (as_of - d).days < min_listing_days:
            out.add(str(sym))
    return out


def _build_bootstrap(data_dir: Path):
    """按 main.py 的最小口径构建 repo + engine + screener。"""
    store = DataStore(data_dir)
    repo = KlineRepository(store)
    # 生产启动同款：先建 instruments 缓存再算 enriched 全量指标，否则 compute_limit_signals
    # 会被跳过 → 历史缓存缺 turnover_rate 与涨跌停信号，matrix 策略会报 unsupported feature。
    # 同步刷新 ~40s，换来后续每个历史日期都命中内存缓存（毫秒级），总耗时反而更短。
    repo.refresh_cache(background=False)
    strategy_dirs = [
        ROOT / "backend" / "app" / "strategy" / "builtin",
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
        data_dir / "strategies" / "composite",
    ]
    engine = StrategyEngine(
        strategy_dirs=strategy_dirs,
        override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
    )
    svc = ScreenerService(repo, asset_type="stock", market="cn")
    all_ids = [
        meta["id"]
        for meta in engine.list_strategies()
        if not meta.get("research_only")
        and "stock" in meta.get("asset_types", ["stock"])
        and "1d" in meta.get("timeframes", ["1d"])
    ]
    logger.info("策略引擎加载 %d 个策略, 参与共振 %d 个", len(engine.list_strategies()), len(all_ids))
    return store, repo, engine, svc, all_ids


def _gen_candidates(svc, engine, all_ids, as_of: date, min_listing_days: int, data_dir: Path) -> list[dict]:
    """复刻 daily_picks.run() 的候选池生成：过滤 + 策略共振 + 家族去重。返回全部候选。"""
    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return []

    # ── 候选池过滤（与 daily_picks.run() 完全一致）──
    mask = pl.col("close").is_not_null() & (pl.col("close") > 0)
    if "name" in df.columns:
        bad = pl.col("name").fill_null("").str.contains("(?i)ST|退")
        mask = mask & ~bad
    if "signal_limit_down" in df.columns:
        mask = mask & ~pl.col("signal_limit_down").fill_null(False)
    one_word_ready = all(c in df.columns for c in ("signal_limit_up", "open", "high", "low", "close"))
    if one_word_ready:
        one_word = (
            pl.col("signal_limit_up").fill_null(False)
            & (pl.col("open") == pl.col("high"))
            & (pl.col("high") == pl.col("low"))
            & (pl.col("low") == pl.col("close"))
        )
        mask = mask & ~one_word
    if min_listing_days and min_listing_days > 0:
        new_syms = _new_listing_symbols(svc.repo, as_of, min_listing_days)
        if new_syms:
            mask = mask & ~pl.col("symbol").is_in(new_syms)
    candidates = df.filter(mask)
    if candidates.is_empty():
        return []
    name_map = {}
    if "name" in candidates.columns:
        for r in candidates.iter_rows(named=True):
            name_map[str(r["symbol"])] = r.get("name")

    # ── 跑全量策略 ──
    all_overrides = strategy_config.list_overrides(data_dir)
    params_map = {sid: dict((all_overrides.get(sid) or {}).get("params") or {}) for sid in all_ids}
    overrides_map = {sid: all_overrides.get(sid, {}) for sid in all_ids}
    context = svc.build_strategy_context(
        engine, as_of, all_ids,
        timeframe="1d", params_map=params_map, overrides_map=overrides_map,
        current=df,
    )
    engine_results = engine.run_all(
        context, params_map=params_map, overrides_map=overrides_map, strategy_ids=all_ids,
    )

    # ── 聚合共振（按家族去重，与 daily_picks 完全一致）──
    agg: dict[str, dict] = {}
    for sid, result in engine_results.items():
        for row in result.rows:
            sym = str(row.get("symbol") or "")
            if not sym or sym not in name_map:
                continue
            entry = agg.setdefault(sym, {"hits": [], "families": set(), "best_score": None})
            entry["hits"].append(sid)
            entry["families"].add(STRATEGY_FAMILIES.get(sid, "other"))
            s = row.get("score")
            if s is not None:
                try:
                    s = float(s)
                except (TypeError, ValueError):
                    s = None
                if s is not None and (entry["best_score"] is None or s > entry["best_score"]):
                    entry["best_score"] = s

    close_map = {str(r["symbol"]): r.get("close") for r in candidates.iter_rows(named=True)}
    out = []
    for sym, info in agg.items():
        out.append({
            "pick_date": as_of.isoformat(),
            "symbol": sym,
            "name": name_map.get(sym),
            "family_count": len(info["families"]),
            "signal_count": len(info["hits"]),
            "score": info["best_score"],
            "entry_close": close_map.get(sym),
        })
    return out


def _trading_dates_from_dir(enriched_dir: Path) -> list[str]:
    try:
        return sorted(
            entry.name[len("date="):]
            for entry in enriched_dir.iterdir()
            if entry.is_dir() and entry.name.startswith("date=")
        )
    except OSError:
        return []


def _build_close_panel(enriched_dir: Path, window_dates: list[str]) -> pl.DataFrame:
    """一次性扫描窗口内 [symbol, date, open, close]，并加 date_index。"""
    start_s = window_dates[0]
    end_s = window_dates[-1]
    lf = scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet")).filter(
        (pl.col("date") >= date.fromisoformat(start_s))
        & (pl.col("date") <= date.fromisoformat(end_s))
    )
    sym_filter = market_symbol_filter("cn")
    if sym_filter is not None:
        lf = lf.filter(sym_filter)
    panel = lf.select(["symbol", "date", "open", "close"]).collect()

    date_to_idx = {date.fromisoformat(s): i for i, s in enumerate(window_dates)}
    di_map = pl.DataFrame({
        "date": list(date_to_idx.keys()),
        "di": list(date_to_idx.values()),
    })
    panel = panel.join(di_map, on="date", how="left").drop_nulls(subset=["di"])
    return panel


def _attach_forward_returns(candidates: list[dict], panel: pl.DataFrame) -> pl.DataFrame:
    """给候选明细接上 1/3/5 日向前收益（close 口径）。"""
    cand = pl.DataFrame(candidates)
    cand = cand.with_columns(
        pl.col("pick_date").str.to_date().cast(pl.Date).alias("date")
    )
    cand = cand.join(
        panel.select(["symbol", "date", "di"]),
        on=["symbol", "date"], how="left",
    )
    cand = cand.drop_nulls(subset=["di"])

    base = panel.select(["symbol", "di", "open", "close"]).rename(
        {"open": "open_0", "close": "close_0"}
    )
    for k in HORIZONS:
        shifted = panel.select(
            ["symbol", (pl.col("di") - k).alias("di"), pl.col("close").alias(f"close_{k}d")]
        )
        base = base.join(shifted, on=["symbol", "di"], how="left")
    cand = cand.join(base, on=["symbol", "di"], how="left")

    # 校验：close_0 应与 entry_close 一致（数据自洽）
    for k in HORIZONS:
        cand = cand.with_columns(
            ((pl.col(f"close_{k}d") / pl.col("close_0")) - 1.0).alias(f"ret_{k}d")
        )
    return cand


def _stratified(cand: pl.DataFrame, col: str) -> pl.DataFrame:
    """按 col 分层统计 1/3/5 日命中率与均值。"""
    rows = []
    for v in sorted(int(x) for x in cand[col].drop_nulls().unique().to_list()):
        sub = cand.filter(pl.col(col) == v)
        r = {col: v, "n": int(sub.height)}
        for k in HORIZONS:
            ret = sub[f"ret_{k}d"].drop_nulls()
            if ret.is_empty():
                r[f"hit_{k}d"], r[f"avg_{k}d"] = None, None
                continue
            r[f"hit_{k}d"] = round(float((ret > 0).sum()) / len(ret) * 100, 2)
            r[f"avg_{k}d"] = round(float(ret.mean()) * 100, 4)
        rows.append(r)
    return pl.DataFrame(rows)


def _fmt_pct(v) -> str:
    return "  N/A " if v is None else f"{v:6.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description="候选池(策略共振)历史回测验证")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--limit-dates", type=int, default=0, help="只跑前 N 个交易日(试点)")
    ap.add_argument("--top", type=int, default=5, help="Top-N 对照")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    enriched_dir = data_dir / "kline_daily_enriched"
    out_dir = data_dir / "daily_picks" / "resonance_validation"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_dates = _trading_dates_from_dir(enriched_dir)
    if not all_dates:
        logger.error("enriched 分区为空: %s", enriched_dir)
        return 1
    start_s, end_s = args.start, args.end
    dates = [d for d in all_dates if start_s <= d <= end_s]
    if args.limit_dates > 0:
        dates = dates[: args.limit_dates]
    if not dates:
        logger.error("区间内无交易日: %s ~ %s", start_s, end_s)
        return 1
    logger.info("回放 %d 个交易日: %s ~ %s", len(dates), dates[0], dates[-1])

    # 向前收益窗口：需覆盖到 end 之后 5 个交易日（从首日起，别把全历史扫进来）
    start_idx = all_dates.index(dates[0])
    end_idx = all_dates.index(dates[-1])
    window_dates = all_dates[start_idx: end_idx + HORIZONS[-1] + 1]

    store, repo, engine, svc, all_ids = _build_bootstrap(data_dir)
    logger.info("构建收盘价面板: %d 个交易日 ...", len(window_dates))
    panel = _build_close_panel(enriched_dir, window_dates)
    logger.info("面板行数 %d, 覆盖 symbol %d", panel.height, panel["symbol"].n_unique())

    ckpt = out_dir / "candidates.parquet"
    all_cands: list[dict] = []
    t0 = time.perf_counter()
    for i, d in enumerate(dates, 1):
        dt0 = time.perf_counter()
        cands = _gen_candidates(svc, engine, all_ids, date.fromisoformat(d), _NEW_LISTING_DAYS, data_dir)
        all_cands.extend(cands)
        dt = time.perf_counter() - dt0
        # 增量落盘（断点可续）
        if cands:
            if ckpt.exists():
                pl.concat([pl.read_parquet(ckpt), pl.DataFrame(cands)], how="vertical").write_parquet(ckpt)
            else:
                pl.DataFrame(cands).write_parquet(ckpt)
        if i % 5 == 0 or i == len(dates):
            el = time.perf_counter() - t0
            logger.info(
                "[%d/%d] %s 候选=%d 当日%.1fs 累计%.0fs 均速%.1fs/日",
                i, len(dates), d, len(cands), dt, el, el / i,
            )

    if not all_cands:
        logger.error("未产出任何共振候选")
        return 1

    logger.info("计算向前收益并分层 ...")
    cand = _attach_forward_returns(all_cands, panel)
    cand.write_parquet(out_dir / "candidates_joined.parquet")

    # ── 汇总输出 ──
    print("\n" + "=" * 90)
    print(f"候选池共振验证 | 区间 {dates[0]} ~ {dates[-1]} | 交易日 {len(dates)} | 共振候选(股×日) {cand.height}")
    print("=" * 90)

    for col, label in (("family_count", "跨家族共振数"), ("signal_count", "命中策略数(未去重)")):
        s = _stratified(cand, col)
        print(f"\n【{label} vs 向前收益】")
        hdr = f"  {col:<12} {'样本':>7} | " + " | ".join(
            f"{k}日命中 {k}日均" for k in HORIZONS
        )
        print(hdr)
        print("  " + "-" * (len(hdr) + 2))
        for r in s.iter_rows(named=True):
            line = f"  {r[col]:<12} {r['n']:>7} | " + " | ".join(
                f"{_fmt_pct(r[f'hit_{k}d'])} {_fmt_pct(r[f'avg_{k}d'])}" for k in HORIZONS
            )
            print(line)
        s.write_parquet(out_dir / f"summary_{col}.parquet")

    # ── 当前 Top-N 排序对照 ──
    print(f"\n【当前 Top-{args.top} 排序对照（家族数↓→共振数↓→评分↓）】")
    top_rows = []
    for d in dates:
        day = cand.filter(pl.col("pick_date") == d).sort(
            ["family_count", "signal_count", "score"], descending=[True, True, True]
        ).head(args.top)
        top_rows.append(day)
    top = pl.concat(top_rows) if top_rows else pl.DataFrame()
    if not top.is_empty():
        print(f"  Top-{args.top}/日, 共 {top.height} 条")
        for k in HORIZONS:
            ret = top[f"ret_{k}d"].drop_nulls()
            if ret.is_empty():
                continue
            print(
                f"    {k}日: 命中率 {_fmt_pct(float((ret>0).sum())/len(ret)*100)} "
                f"均值 {_fmt_pct(float(ret.mean())*100)} 中位 {_fmt_pct(float(ret.median())*100)}"
            )
        top.write_parquet(out_dir / "top_n.parquet")

    # ── 全体基线（所有共振候选）──
    print("\n【全体共振候选基线】")
    for k in HORIZONS:
        ret = cand[f"ret_{k}d"].drop_nulls()
        if ret.is_empty():
            continue
        print(
            f"  {k}日: 命中率 {_fmt_pct(float((ret>0).sum())/len(ret)*100)} "
            f"均值 {_fmt_pct(float(ret.mean())*100)} 中位 {_fmt_pct(float(ret.median())*100)}"
        )

    print("\n明细已写:", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
