"""对拍: 面板矩阵版「启动策略」 vs 公司版 filter_stock 直译参考实现。

读本地 data/kline_daily_enriched (前复权 OHLCV) —— 先在矩阵管线上跑一遍策略,
再用纯 NumPy 直译 filter_stock.is_valid_stock 逐票独立计算, 最后比较 as_of 当日
的命中集合。差异即移植偏差 (rolling 窗口 / 有效 bar / 量比基准口径)。

用法 (在 backend 目录下):
    ./.venv/Scripts/python.exe -m scripts.verify_startup_surge
    ./.venv/Scripts/python.exe -m scripts.verify_startup_surge --as-of 2026-09-15
    ./.venv/Scripts/python.exe -m scripts.verify_startup_surge --symbols 600025,600212
    ./.venv/Scripts/python.exe -m scripts.verify_startup_surge --no-basic-filter
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_market_data_matrix,
)
from app.strategy.builtin.startup_surge import MATRIX_STRATEGY, META

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "kline_daily_enriched"

_MIN_HISTORY = 31  # 源脚本 i 从 30 起步 => 至少 31 根有效 bar


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
    frame = (
        pl.scan_parquet(str(_DATA_DIR / "date=*" / "*.parquet"))
        .select(["symbol", "date", "open", "high", "low", "close", "volume"])
        .filter(pl.col("date") <= as_of)
        .collect()
    )
    if symbols:
        wanted = set(symbols)
        frame = frame.filter(
            pl.col("symbol").is_in(wanted)
            | pl.col("symbol").str.split(".").list.first().is_in(wanted)
        )
    return frame.sort(["date", "symbol"])


def _reference_startup(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray
) -> bool:
    """直译 filter_stock.is_valid_stock, 返回序列最后一根是否命中。"""
    t = close.size - 1
    if t < _MIN_HISTORY - 1:
        return False
    pct_window = close[t - 14 : t + 1] / close[t - 15 : t] - 1.0
    cond_limit = float(pct_window.max()) >= 0.098
    cond_gap = bool(np.any(low[t - 9 : t + 1] > high[t - 10 : t]))
    body = (close[t - 2 : t + 1] - open_[t - 2 : t + 1]) / open_[t - 2 : t + 1]
    cond_yang = bool((body > 0).all()) and float(body.mean()) > 0.01
    ratio = float(volume[t] / volume[t - 5 : t].mean())
    cond_volume = 1.5 <= ratio <= 3.0
    return bool(cond_limit and cond_gap and cond_yang and cond_volume)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=None, help="对拍日期 (默认最新分区)")
    parser.add_argument("--symbols", default=None, help="逗号分隔的股票代码, 默认全市场")
    parser.add_argument(
        "--no-basic-filter",
        action="store_true",
        help="关闭 basic_filter (对拍必须关闭, 否则结果被市值/价格门槛裁剪)",
    )
    parser.add_argument("--show", type=int, default=20, help="打印前 N 个命中")
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
        f"as_of={as_of}  行数={panel.height}  票数={panel['symbol'].n_unique()}  "
        f"区间={panel['date'].min()} → {panel['date'].max()}"
    )

    market = build_market_data_matrix(panel)
    if not args.no_basic_filter:
        print("[WARN] 未加 --no-basic-filter: 结果会被 basic_filter 裁剪, 仅作冒烟")

    signals = MatrixStrategyPipeline().run(
        MATRIX_STRATEGY,
        market,
        {},
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
    volume = market.volume
    reference_hits: set[str] = set()
    mismatched_suspension: list[str] = []
    skipped = 0
    for asset_id, symbol in enumerate(market.symbols):
        usable = (
            np.isfinite(close[: as_of_row + 1, asset_id])
            & np.isfinite(high[: as_of_row + 1, asset_id])
            & np.isfinite(low[: as_of_row + 1, asset_id])
            & np.isfinite(open_[: as_of_row + 1, asset_id])
            & np.isfinite(volume[: as_of_row + 1, asset_id])
        )
        rows = np.flatnonzero(usable)
        if rows.size == 0 or int(rows[-1]) != as_of_row:
            # 目标日停牌/未上市 —— 策略按无效 bar 处理, 必须为 False
            if entry[asset_id]:
                mismatched_suspension.append(symbol)
            continue
        if rows.size < _MIN_HISTORY:
            skipped += 1
            continue
        if _reference_startup(
            open_[rows, asset_id],
            high[rows, asset_id],
            low[rows, asset_id],
            close[rows, asset_id],
            volume[rows, asset_id],
        ):
            reference_hits.add(symbol)

    only_panel = sorted(panel_hits - reference_hits)
    only_reference = sorted(reference_hits - panel_hits)
    print(
        f"\n面板命中 {len(panel_hits)} 只 / 参考命中 {len(reference_hits)} 只 "
        f"(历史不足跳过 {skipped} 只)"
    )
    if mismatched_suspension:
        print(
            f"[DIFF] 目标日停牌却命中 {len(mismatched_suspension)}: "
            f"{mismatched_suspension[: args.show]}"
        )
    print(f"仅面板有 {len(only_panel)}: {only_panel[: args.show]}")
    print(f"仅参考有 {len(only_reference)}: {only_reference[: args.show]}")
    if panel_hits:
        print(f"命中样例: {sorted(panel_hits)[: args.show]}")
    consistent = not only_panel and not only_reference and not mismatched_suspension
    print("\n[OK] 两边完全一致" if consistent else "\n[DIFF] 存在偏差")
    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
