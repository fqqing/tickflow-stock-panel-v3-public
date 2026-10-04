"""对拍: 面板矩阵版「趋势擒龙」 vs 源 pandas 公式逐 bar 参考实现。

读本地 data/kline_daily_enriched (前复权 OHLC) —— 先在矩阵管线上跑一遍策略,
再用 ``app.indicators.formula_signals.trend_dragon`` (源脚本
``qushiqinlong/选股_趋势擒龙.py::compute_trend_dragon`` 的逐 bar 直译) 逐票独立
计算, 最后比较 as_of 当日的命中集合。差异即移植偏差 (BARSLAST / BARSLASTCOUNT /
变长 REF 的有效 bar 语义)。

可选用 ``--baseline`` 传入源脚本的历史扫描结果 CSV
(``qushiqinlong/趋势擒龙_选股结果_*.csv``), 额外报告覆盖率 —— 那份 CSV 出自另一条
数据源 (pytdx), 前复权基准与本地不一致, 因此只作参考不参与判定。

``--max-momentum`` / ``--max-bias20`` 分别打开资金动能与 MA20 乖离率过滤 —— 参数的
**两侧语义都跟源脚本 ``scan_one`` 对齐**: 值取不到 (动能/乖离为 None) 或超过上限
都直接剔除。

- 资金动能: 参考实现用 ``capital_momentum`` (与个股 K 线副图同口径) 在
  「个股有效 bar 序列 ∩ 指数可用日」上算 52 日均值, 取最后一根的值。指数序列缺值
  按矩阵口径**前向沿用** (源脚本是 inner join 直接丢行; 指数分区完整时两者等价)。
- MA20 乖离率: ``(信号日收盘 / MA20 - 1) * 100``, MA20 取最近 20 根有效收盘。

⚠️ 策略 ``META`` 里的默认值已经对齐源脚本的日常用法
(``--max-momentum 1 --max-bias20 10``), 所以本脚本**总是显式**写入
``use_momentum_filter`` / ``momentum_cap`` / ``bias20_cap_pct`` 三个键 ——
不给 ``--max-momentum`` / ``--max-bias20`` 就是「关掉对应过滤」, 而不是
「用策略默认值」。两边不能混, 否则面板过滤了参考没过滤。

**已知数值边界 (非移植偏差)**: 矩阵里的价格是 float32, 本脚本的参考实现是 float64。
MA10 这种「价格均线」在两侧的末位会差 ~1e-7 量级, 于是 ``C > MA10`` 这类**严格比较**
在「收盘价恰好等于 MA10」的 bar 上可能得出不同结论, 表现为某只票「仅参考有」。
这种差异会走 ``[NOTE]`` 单列报告 (用 float32 量化输入重跑参考公式即可复现), 不计入
偏差判定 —— 通达信内部同样是 float 口径, 面板侧反而更贴近原公式。

用法 (在 backend 目录下):
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon --as-of 2026-08-07
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon --symbols 600026,002506
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon --baseline ../qushiqinlong/趋势擒龙_选股结果_20260807.csv
    ./.venv/Scripts/python.exe -m scripts.verify_trend_dragon --max-momentum 0
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.benchmark import exchange_of, load_benchmark_closes
from app.backtest.matrix import (
    MarketDataMatrix,
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_market_data_matrix,
)
from app.indicators.formula_signals import capital_momentum, trend_dragon
from app.strategy.builtin.trend_dragon import MATRIX_STRATEGY, META

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "kline_daily_enriched"
_SHARE_RE = re.compile(r"\d{6}\.(SH|SZ|BJ)")

_BIAS20_WINDOW = 20  # 源脚本 df['close'].rolling(20)


def _partition_dates() -> list[date]:
    dates: list[date] = []
    for child in _DATA_DIR.glob("date=*"):
        raw = child.name.partition("=")[2]
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            continue
    return sorted(dates)


def _load_panel(as_of: date, symbols: list[str] | None) -> pl.DataFrame:
    """A 股面板 (kline_daily_enriched 里同时住着港股/美股, 策略 asset_types 只认 A 股)。"""
    frame = (
        pl.scan_parquet(str(_DATA_DIR / "date=*" / "*.parquet"))
        .select(["symbol", "date", "open", "high", "low", "close", "volume"])
        .filter(pl.col("date") <= as_of)
        .filter(pl.col("symbol").str.contains(r"\d{6}\.(SH|SZ|BJ)$"))
        .collect()
    )
    if symbols:
        wanted = set(symbols)
        frame = frame.filter(
            pl.col("symbol").is_in(wanted)
            | pl.col("symbol").str.split(".").list.first().is_in(wanted)
        )
    return frame.sort(["date", "symbol"])


def _reference_hit(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    scan_days: int,
) -> bool:
    """直译源公式, 返回序列末尾 ``scan_days`` 根内是否出现过信号。"""
    signal, _ = trend_dragon(open_, high, low, close)
    if not signal.any():
        return False
    return bool(signal[-scan_days:].any())


def _reference_hit_f32(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    scan_days: int,
) -> bool:
    """同上, 但先把 OHLC 量化到 float32 —— 复现矩阵的存储精度。

    矩阵价格是 float32, MA10 按 float32 累加; 参考实现是 float64。收盘价与 MA10
    之差落在 float32 舍入误差内 (~1e-7) 时, ``C > MA10`` 的严格比较会相差一根 bar。
    量化输入到 float32 后, 参考侧的 MA10 会落到与矩阵同一格, 从而复现面板的判定。
    """

    def quantize(values: np.ndarray) -> np.ndarray:
        return np.asarray(values, dtype=np.float32).astype(np.float64)

    signal, _ = trend_dragon(
        quantize(open_),
        quantize(high),
        quantize(low),
        quantize(close),
    )
    if not signal.any():
        return False
    return bool(signal[-scan_days:].any())


def _index_close_lookup() -> dict[str, dict[date, float]]:
    """交易所 -> {date: 基准指数收盘价}, 复用矩阵侧那条取数链 (取不到时空表)。"""
    frame = load_benchmark_closes()
    if frame is None:
        return {}
    lookup: dict[str, dict[date, float]] = {}
    for exchange, day, close in frame.select(["exchange", "date", "close"]).iter_rows():
        if close is not None and math.isfinite(float(close)):
            lookup.setdefault(str(exchange), {})[day] = float(close)
    return lookup


def _align_index_close(dates: list[date], closes: dict[date, float]) -> np.ndarray:
    """按矩阵口径对齐指数收盘价: 缺值的交易日沿用最近一根, 指数历史起点前为 NaN。"""
    values = np.full(len(dates), np.nan, dtype=np.float64)
    ordered = sorted(closes)
    if not ordered:
        return values
    position = 0
    for index, day in enumerate(dates):
        while position + 1 < len(ordered) and ordered[position + 1] <= day:
            position += 1
        if ordered[position] <= day:
            values[index] = closes[ordered[position]]
    return values


def _reference_momentum(
    close: np.ndarray,
    dates: list[date],
    index_lookup: dict[str, dict[date, float]],
    symbol: str,
) -> float | None:
    """源脚本 ``compute_capital_momentum`` 口径: 交集日期上 52 日均值, 取最后一根。

    不足 52 个可用交易日或指数缺失时返回 None (源脚本里 None 会被过滤掉)。
    """
    exchange = exchange_of(symbol)
    if exchange is None:
        return None
    closes = index_lookup.get(exchange)
    if not closes:
        return None
    index_close = _align_index_close(dates, closes)
    usable = np.isfinite(index_close) & np.isfinite(close)
    if int(usable.sum()) < 52:
        return None
    values = capital_momentum(close[usable], index_close[usable])
    if values.size == 0:
        return None
    last = float(values[-1])
    return last if math.isfinite(last) else None


def _reference_bias20(close: np.ndarray) -> float | None:
    """源脚本 ``scan_one`` 口径的 MA20 乖离率: ``(信号日收盘 / MA20 - 1) * 100``。

    源脚本用 ``df['close'].rolling(20).mean()`` 取**信号日**那一根 (不是最后一根),
    但 ``sig_idx`` 就是最近一次信号所在行, 而这里传进来的序列已经截到 as_of,
    只有 as_of 当根有信号时才会走到这里, 所以取最后一根等价。
    """
    if close.size < _BIAS20_WINDOW:
        return None
    average = float(np.mean(close[-_BIAS20_WINDOW:]))
    if not math.isfinite(average) or average == 0.0:
        return None
    return (float(close[-1]) / average - 1.0) * 100.0


def _split_float32_boundary(
    candidates: list[str],
    market: MarketDataMatrix,
    as_of_row: int,
    scan_days: int,
) -> tuple[list[str], list[str]]:
    """把「仅参考有」里由 float32 舍入边界造成的差异单独摘出来。

    只对差异票做 (通常个位数), 开销可忽略。
    """
    if not candidates:
        return [], []
    index_of = {symbol: index for index, symbol in enumerate(market.symbols)}
    close = market.close
    high = market.high
    low = market.low
    open_ = market.open
    ties: list[str] = []
    rest: list[str] = []
    for symbol in candidates:
        asset_id = index_of.get(symbol)
        if asset_id is None:
            rest.append(symbol)
            continue
        usable = (
            np.isfinite(close[: as_of_row + 1, asset_id])
            & np.isfinite(open_[: as_of_row + 1, asset_id])
            & np.isfinite(high[: as_of_row + 1, asset_id])
            & np.isfinite(low[: as_of_row + 1, asset_id])
        )
        rows = np.flatnonzero(usable)
        if (
            rows.size
            and int(rows[-1]) == as_of_row
            and _reference_hit_f32(
                open_[rows, asset_id],
                high[rows, asset_id],
                low[rows, asset_id],
                close[rows, asset_id],
                scan_days,
            )
        ):
            ties.append(symbol)
        else:
            rest.append(symbol)
    return ties, rest


def _load_baseline(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig") as handle:
        return {
            row["代码"].zfill(6): str(row.get("信号日期") or "") for row in csv.DictReader(handle)
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=None, help="对拍日期 (默认最新分区)")
    parser.add_argument("--symbols", default=None, help="逗号分隔的股票代码, 默认全市场")
    parser.add_argument(
        "--no-basic-filter",
        action="store_true",
        help="关闭 basic_filter (对拍必须关闭, 否则结果被市值/价格门槛裁剪)",
    )
    parser.add_argument(
        "--scan-days", type=int, default=5, help="信号扫描窗口 (源脚本 --days, 默认 5)"
    )
    parser.add_argument(
        "--max-momentum",
        type=float,
        default=None,
        help="资金动能上限 (源脚本 --max-momentum, 日常用法 1); 不给则关掉该过滤",
    )
    parser.add_argument(
        "--max-bias20",
        type=float,
        default=None,
        help="MA20 乖离率上限%% (源脚本 --max-bias20, 日常用法 10); 不给则关掉该过滤",
    )
    parser.add_argument("--baseline", default=None, help="源脚本历史选股结果 CSV, 仅作覆盖率参考")
    parser.add_argument("--show", type=int, default=20, help="打印前 N 个差异")
    args = parser.parse_args()

    dates = _partition_dates()
    if not dates:
        print(f"[ERR] 未找到 enriched 分区: {_DATA_DIR}")
        return 2
    as_of = date.fromisoformat(args.as_of) if args.as_of else dates[-1]
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None

    panel = _load_panel(as_of, symbols)
    if panel.is_empty():
        print(f"[ERR] {as_of} 无数据")
        return 2
    print(
        f"as_of={as_of}  scan_days={args.scan_days}  行数={panel.height}  "
        f"票数={panel['symbol'].n_unique()}  区间={panel['date'].min()} → {panel['date'].max()}"
    )

    market = build_market_data_matrix(panel)
    if not args.no_basic_filter:
        print("[WARN] 未加 --no-basic-filter: 结果会被 basic_filter 裁剪, 仅作冒烟")

    # 策略 META 的默认值已经对齐源脚本的日常用法 (--max-momentum 1 --max-bias20 10),
    # 对拍必须**显式**声明本次用哪一档, 否则「不给参数」会被当成默认开过滤。
    params: dict = {
        "scan_days": args.scan_days,
        "use_momentum_filter": args.max_momentum is not None,
        "momentum_cap": float(args.max_momentum) if args.max_momentum is not None else 0.0,
        "bias20_cap_pct": float(args.max_bias20) if args.max_bias20 is not None else 0.0,
    }
    index_lookup: dict[str, dict[date, float]] = {}
    if args.max_momentum is not None:
        index_lookup = _index_close_lookup()
        covered = ", ".join(f"{k}:{len(v)}" for k, v in sorted(index_lookup.items()))
        print(f"资金动能上限={args.max_momentum}  指数序列 {covered or '缺失'}")
    if args.max_bias20 is not None:
        print(f"MA20 乖离率上限={args.max_bias20}%")

    signals = MatrixStrategyPipeline().run(
        MATRIX_STRATEGY,
        market,
        params,
        MatrixPipelineConfig(
            basic_filter={"enabled": not args.no_basic_filter},
            scoring=dict(META["scoring"]),
            order_by=META.get("order_by"),
            descending=bool(META.get("descending", True)),
        ),
    )
    as_of_row = None
    for time_id, label in enumerate(market.timestamp_labels):
        if label[:10] == str(as_of):
            as_of_row = time_id
    if as_of_row is None:
        print(f"[ERR] 时间轴缺 {as_of}")
        return 2
    entry = signals.entry[as_of_row].astype(bool)
    panel_hits = {market.symbols[i] for i in np.flatnonzero(entry)}

    close = market.close
    high = market.high
    low = market.low
    open_ = market.open
    label_dates = [date.fromisoformat(str(label)[:10]) for label in market.timestamp_labels]
    reference_hits: set[str] = set()
    mismatched_suspension: list[str] = []
    skipped = 0
    momentum_missing = 0
    bias_missing = 0
    for asset_id, symbol in enumerate(market.symbols):
        usable = (
            np.isfinite(close[: as_of_row + 1, asset_id])
            & np.isfinite(open_[: as_of_row + 1, asset_id])
            & np.isfinite(high[: as_of_row + 1, asset_id])
            & np.isfinite(low[: as_of_row + 1, asset_id])
        )
        rows = np.flatnonzero(usable)
        if rows.size == 0 or int(rows[-1]) != as_of_row:
            # 目标日停牌/未上市 —— 策略按无效 bar 处理, 必须为 False
            if entry[asset_id]:
                mismatched_suspension.append(symbol)
            continue
        if rows.size < 20:  # 源脚本 n < 20 直接返回全 False
            skipped += 1
            continue
        hit = _reference_hit(
            open_[rows, asset_id],
            high[rows, asset_id],
            low[rows, asset_id],
            close[rows, asset_id],
            args.scan_days,
        )
        if hit and args.max_bias20 is not None:
            # 源脚本 scan_one: bias20 取 None 或超过上限都直接剔除
            bias = _reference_bias20(close[rows, asset_id])
            if bias is None or bias > float(args.max_bias20):
                bias_missing += 1
                hit = False
        if hit and args.max_momentum is not None:
            # 源脚本 scan_one: 动能取 None 或超过上限都直接剔除
            momentum = _reference_momentum(
                close[rows, asset_id],
                [label_dates[row] for row in rows],
                index_lookup,
                symbol,
            )
            if momentum is None:
                momentum_missing += 1
                hit = False
            elif momentum > float(args.max_momentum):
                hit = False
        if hit:
            reference_hits.add(symbol)

    # 参考实现不区分资产类别, 只对 A 股代码比对, 以免港美股/ETF 混进差异清单
    reference_hits = {s for s in reference_hits if _SHARE_RE.fullmatch(s)}

    only_panel = sorted(panel_hits - reference_hits)
    only_reference = sorted(reference_hits - panel_hits)
    tie_explained, only_reference = _split_float32_boundary(
        only_reference, market, as_of_row, args.scan_days
    )
    print(
        f"\n面板命中 {len(panel_hits)} 只 / 参考命中 {len(reference_hits)} 只 "
        f"(历史不足跳过 {skipped} 只)"
    )
    if args.max_momentum is not None:
        print(f"  参考口径因动能缺失被剔除 {momentum_missing} 只")
    if args.max_bias20 is not None:
        print(f"  参考口径因乖离缺失被剔除 {bias_missing} 只")
    if mismatched_suspension:
        print(
            f"[DIFF] 目标日停牌却命中 {len(mismatched_suspension)}: "
            f"{mismatched_suspension[: args.show]}"
        )
    if tie_explained:
        print(
            f"[NOTE] float32 舍入边界 (不计入偏差) {len(tie_explained)}: "
            f"{tie_explained[: args.show]}  —— 收盘价与 MA10 之差在 float32 精度内, "
            f"``C > MA10`` 两侧严格比较结论不同"
        )
    print(f"仅面板有 {len(only_panel)}: {only_panel[: args.show]}")
    print(f"仅参考有 {len(only_reference)}: {only_reference[: args.show]}")
    if panel_hits:
        print(f"命中样例: {sorted(panel_hits)[: args.show]}")

    if args.baseline:
        baseline = _load_baseline(Path(args.baseline))
        mine = {s[:6] for s in panel_hits if _SHARE_RE.fullmatch(s)}
        covered = set(baseline) & mine
        print(
            f"\n[基线参考] {args.baseline}: {len(baseline)} 只信号股, "
            f"被本次命中覆盖 {len(covered)} 只 ({100 * len(covered) / max(len(baseline), 1):.1f}%)"
        )
        print(f"  基线有本次没有: {sorted(set(baseline) - mine)[: args.show]}")

    consistent = not only_panel and not only_reference and not mismatched_suspension
    print("\n[OK] 两边完全一致" if consistent else "\n[DIFF] 存在偏差")
    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
