"""对拍: 面板矩阵版「底部结构」/「钝化加低九」 vs 源公式逐 bar 参考实现。

读本地 data/kline_daily_enriched (前复权 OHLC): 先在矩阵管线上跑一遍策略, 再用
``app.indicators.formula_signals`` 的逐 bar 算子 (EMA / CROSS / BARSLAST / 变长
LLV / 变长 REF) 直译 AKL 源码, 在**有效 bar 压缩序列**上逐票独立计算, 最后比较
as_of 当日的命中集合。差异即移植偏差 (变长窗口 / BARSLAST 有效 bar 语义)。

参考实现里 ``barslast`` 的"从未成立 = -1" 会让 ``LLV(X, N1+1)`` 的窗口变成 0 而
被 ``llv_at`` 判为无效 —— 这与矩阵侧 ``_observed_only`` 的遮蔽处理等价, 是刻意
对齐的两条路径。

⚠️ 「底部结构」取的是 ``REF(底部钝化, 1)`` 而不是 ``REF(底钝化, 1)`` —— 源码原文
``底部结构:=DIFF>REF(DIFF,1) AND (REF(底部钝化,1) AND DIFL1*0.9884<DIFF);``。
``底钝化`` 只被 ``M4`` 引用, 服务于 ``底结构消失`` 的图表标注。早期版本两边一起
写错成了 ``底钝化``, 已修。

用法 (在 backend 目录下):
    ./.venv/Scripts/python.exe -m scripts.verify_bottom_structure
    ./.venv/Scripts/python.exe -m scripts.verify_bottom_structure --strategy stale_nine_turn
    ./.venv/Scripts/python.exe -m scripts.verify_bottom_structure --as-of 2026-03-19
    ./.venv/Scripts/python.exe -m scripts.verify_bottom_structure --symbols 603458,000001
"""

from __future__ import annotations

import argparse
import importlib
import re
from datetime import date
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl

from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_market_data_matrix,
)
from app.indicators.formula_signals import barslast, cross, ema, llv_at, ref_at

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "kline_daily_enriched"
_BUILTIN_DIR = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
_SHARE_RE = re.compile(r"\d{6}\.(SH|SZ|BJ)")

# 策略名 -> (模块名, 隔峰底钝化用的 REF(MACD, N), 是否需要叠加下跌九转 T9)
_STRATEGIES: dict[str, tuple[str, int, bool]] = {
    "bottom_structure": ("bottom_structure", 1, False),
    "stale_nine_turn": ("stale_nine_turn", 2, True),
}

_BOTTOM_RATIO = 0.9884
_NINE_TURN_LOOKBACK = 4


def _load_builtin(name: str) -> ModuleType:
    """经引擎加载一次内置策略, 再按模块名取回模块对象。

    策略文件里的 ``from _quant_structure import ...`` 依赖 builtin/ 在 sys.path 上,
    而那只在 ``StrategyEngine._load_file`` 内部做 —— 直接 ``import`` 会
    ``ModuleNotFoundError``。这里先走一次引擎加载铺好环境, 与生产同一条加载链。
    """
    from app.strategy.engine import StrategyEngine

    StrategyEngine._load_file(_BUILTIN_DIR / f"{name}.py")
    return importlib.import_module(f"app.strategy.builtin.{name}")


def _lt(left, right) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        result = np.asarray(left) < np.asarray(right)
    return np.where(np.isfinite(left) & np.isfinite(right), result, False)


def _gt(left, right) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        result = np.asarray(left) > np.asarray(right)
    return np.where(np.isfinite(left) & np.isfinite(right), result, False)


def _previous_true(condition: np.ndarray) -> np.ndarray:
    out = np.zeros(condition.shape, dtype=bool)
    out[1:] = condition[:-1]
    return out


def _previous_zero(condition: np.ndarray) -> np.ndarray:
    out = np.zeros(condition.shape, dtype=bool)
    out[1:] = ~condition[:-1]
    return out


def _nine_turn_down(close: np.ndarray, level: int = 9) -> np.ndarray:
    rising = np.zeros(close.shape, dtype=bool)
    falling = np.zeros(close.shape, dtype=bool)
    rising[_NINE_TURN_LOOKBACK:] = close[_NINE_TURN_LOOKBACK:] > close[:-_NINE_TURN_LOOKBACK]
    falling[_NINE_TURN_LOOKBACK:] = close[_NINE_TURN_LOOKBACK:] < close[:-_NINE_TURN_LOOKBACK]
    step = falling & _previous_true(rising)
    for _ in range(2, level + 1):
        step = falling & _previous_true(step)
    return step


def _reference_signals(
    close: np.ndarray,
    *,
    macd_prev_bars: int,
    with_nine_turn: bool,
) -> np.ndarray:
    """按 AKL 源码逐 bar 直译, 返回与 ``close`` 等长的信号序列。"""
    c = np.asarray(close, dtype=np.float64)
    size = c.size
    diff = ema(c, 12) - ema(c, 26)
    dea = ema(diff, 9)
    macd = (diff - dea) * 2.0

    n1 = barslast(cross(dea, diff))
    m1 = barslast(cross(diff, dea))

    cl1 = llv_at(c, n1 + 1)
    cl2 = ref_at(cl1, m1 + 1)
    cl3 = ref_at(cl2, m1 + 1)
    difl1 = llv_at(diff, n1 + 1)
    difl2 = ref_at(difl1, m1 + 1)
    difl3 = ref_at(difl2, m1 + 1)

    macd_prev = ref_at(macd, np.full(size, float(macd_prev_bars)))
    diff_prev = ref_at(diff, np.ones(size))
    negative = _lt(macd_prev, 0.0)

    direct = _lt(cl1, cl2) & _gt(difl1, difl2) & negative & _lt(difl2, 0.0)
    peak = (
        _lt(cl1, cl3)
        & _lt(difl1, difl2)
        & _gt(difl1, difl3)
        & _lt(diff, dea)
        & negative
        & _lt(difl3, 0.0)
    )
    stale = (direct | peak) & negative
    if with_nine_turn:
        # 钝化加低九: 输出 底钝化 AND T9 (底钝化 = 底部钝化的首次成立且 DIFF<DEA)
        first_stale = stale & _previous_zero(stale) & _lt(diff, dea)
        return first_stale & _nine_turn_down(c)

    # 底部结构取 REF(底部钝化, 1) —— 不是 REF(底钝化, 1)
    structure = (
        _gt(diff, diff_prev) & _previous_true(stale) & _lt(difl1 * _BOTTOM_RATIO, diff)
    )
    return structure & _previous_zero(structure)


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="bottom_structure", choices=sorted(_STRATEGIES))
    parser.add_argument("--as-of", default=None, help="对拍日期 (默认最新分区)")
    parser.add_argument("--symbols", default=None, help="逗号分隔的股票代码, 默认全市场")
    parser.add_argument(
        "--no-basic-filter",
        action="store_true",
        help="关闭 basic_filter (对拍必须关闭, 否则结果被市值/价格门槛裁剪)",
    )
    parser.add_argument("--show", type=int, default=20, help="打印前 N 个差异")
    args = parser.parse_args()

    module_name, macd_prev_bars, with_nine_turn = _STRATEGIES[args.strategy]
    module = _load_builtin(module_name)

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
        f"strategy={args.strategy}  as_of={as_of}  行数={panel.height}  "
        f"票数={panel['symbol'].n_unique()}  区间={panel['date'].min()} → {panel['date'].max()}"
    )

    market = build_market_data_matrix(panel)
    if not args.no_basic_filter:
        print("[WARN] 未加 --no-basic-filter: 结果会被 basic_filter 裁剪, 仅作冒烟")

    signals = MatrixStrategyPipeline().run(
        module.MATRIX_STRATEGY,
        market,
        {},
        MatrixPipelineConfig(
            basic_filter={"enabled": not args.no_basic_filter},
            scoring=dict(module.META["scoring"]),
            order_by=module.META.get("order_by"),
            descending=bool(module.META.get("descending", True)),
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
    reference_hits: set[str] = set()
    mismatched_suspension: list[str] = []
    for asset_id, symbol in enumerate(market.symbols):
        usable = np.isfinite(close[: as_of_row + 1, asset_id])
        rows = np.flatnonzero(usable)
        if rows.size == 0 or int(rows[-1]) != as_of_row:
            # 目标日停牌/未上市 —— 策略按无效 bar 处理, 必须为 False
            if entry[asset_id]:
                mismatched_suspension.append(symbol)
            continue
        series = close[rows, asset_id].astype(np.float64)
        if _reference_signals(
            series,
            macd_prev_bars=macd_prev_bars,
            with_nine_turn=with_nine_turn,
        )[-1]:
            reference_hits.add(symbol)

    reference_hits = {s for s in reference_hits if _SHARE_RE.fullmatch(s)}

    only_panel = sorted(panel_hits - reference_hits)
    only_reference = sorted(reference_hits - panel_hits)
    print(f"\n面板命中 {len(panel_hits)} 只 / 参考命中 {len(reference_hits)} 只")
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
