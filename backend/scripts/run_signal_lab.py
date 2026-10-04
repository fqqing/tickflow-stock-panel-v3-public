"""Signal Lab 端到端复盘: 生成某策略在指定区间的「全信号战绩台账」并汇总战绩。

与回测的区别(务必分清)
----------------------
回测回答「这套资金规则能赚多少」; 本脚本回答「这些信号本身后来怎么走」。
它把该策略在区间内产生的**每一条**入场信号都摊平成一行, 带上多个持有期的收益,
持有期内最大浮盈/浮亏, 以及相对全市场同期中位数的超额收益。资金是否被占用,
是否排得上持仓名额都不影响统计, 因此样本量远大于 trades。

口径声明
--------
- 信号由策略自身的 matrix-native 实现在整个区间上一次性算出(与回测同代码, 同参数)。
  信号在 row t 只用到 t 及之前的数据, 不存在未来函数; 但对于区间开头的若干行,
  指标尚未收敛, 故用 ``--drop-warmup``(默认取策略声明的 warmup)丢弃前 N 行的信号。
- 成交 = 信号次日开盘价(``--entry-delay 1``); 一字涨停与停牌顺延, 顺延失败记为未成交。
- 收益分母是实际成交价, 小数制(0.0366 = +3.66%)。

编排逻辑在 ``app/signallab/lab.py``(API 与本脚本共用同一份实现), 本文件只负责
命令行参数解析与打印。

用法
----
    # 复盘「定量底部结构选股」最近一年, 限制 300 只标的(先跑通用)
    python backend/scripts/run_signal_lab.py --strategy bottom_structure --limit 300

    # 三个短线策略一起跑, 输出到 data/signal_lab/
    python backend/scripts/run_signal_lab.py \
        --strategy bottom_structure --strategy chan_3buy_entry --strategy startup_surge \
        --limit 500 --horizons 1,3,5,10,20 --stop-loss -0.06 --take-profit 0.15

    # 指定自选股文件(每行或逗号分隔的代码)
    python backend/scripts/run_signal_lab.py --strategy trend_dragon --symbols-file my.txt
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import polars as pl  # noqa: E402

from app.signallab.lab import LabRunConfig, run_lab  # noqa: E402
from app.signallab.summary import summarize_outcomes  # noqa: E402
from app.strategy.engine import StrategyEngine  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

_DEFAULT_HORIZONS = (1, 3, 5, 10, 20, 60)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Signal Lab 信号台账复盘")
    parser.add_argument("--strategy", action="append", default=[], required=True,
                        help="策略 id, 可重复传入多个")
    parser.add_argument("--start", type=str, default=None, help="复盘起始日 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="复盘结束日 YYYY-MM-DD")
    parser.add_argument("--lookback-days", type=int, default=320,
                        help="未指定 --start 时向前取多少个交易日的数据")
    parser.add_argument("--limit", type=int, default=300, help="限制标的个数(按字典序)")
    parser.add_argument("--symbols-file", type=str, default=None, help="标的清单文件")
    parser.add_argument("--horizons", type=str, default=",".join(str(h) for h in _DEFAULT_HORIZONS))
    parser.add_argument("--entry-delay", type=int, default=1, help="信号后第几根以开盘价成交")
    parser.add_argument("--stop-loss", type=float, default=None, help="止损线(负小数, 如 -0.06)")
    parser.add_argument("--take-profit", type=float, default=None, help="止盈线(正小数, 如 0.15)")
    parser.add_argument("--drop-warmup", type=int, default=None,
                        help="丢弃开头若干行(指标未收敛)的信号, 默认用策略声明值")
    parser.add_argument("--data-dir", type=str, default=None, help="数据目录, 默认仓库根 data/")
    parser.add_argument("--matrix-cache-mb", type=int, default=768)
    parser.add_argument("--no-write", action="store_true", help="只打印不落 parquet")
    return parser.parse_args(argv)


def _resolve_symbols(repo: KlineRepository, args: argparse.Namespace) -> list[str] | None:
    if args.symbols_file:
        raw = Path(args.symbols_file).read_text(encoding="utf-8")
        symbols = [token.strip() for token in raw.replace("\n", ",").split(",") if token.strip()]
        return symbols or None
    if args.limit and args.limit > 0:
        instruments = repo.get_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return None
        column = instruments["symbol"]
        if "asset_type" in instruments.columns:
            column = instruments.filter(pl.col("asset_type") == "stock")["symbol"]
        values = sorted({str(v) for v in column.to_list() if v})
        return values[: args.limit]
    return None


def _print_summary(frame: pl.DataFrame, strategy_id: str, horizons: tuple[int, ...]) -> None:
    if frame.is_empty():
        print(f"[{strategy_id}] 区间内没有产生任何信号")
        return
    summary = summarize_outcomes(frame, horizons=horizons, with_excess=True)
    row = summary.row(0, named=True)
    print(f"\n=== {strategy_id} ===")
    print(f"信号数 {row['n_signals']} / 成交 {row['n_filled']} / 前瞻不足 {row['n_truncated']}")
    header = f"{'持有期':>6} {'样本':>7} {'胜率':>8} {'均值':>9} {'中位':>9} {'盈亏比':>8} {'超额':>9}"
    print(header)
    for horizon in horizons:
        n_key = f"ret{horizon}_n"
        if n_key not in row:
            continue
        def fmt(value, *, pct: bool = True, digits: int = 2) -> str:
            if value is None:
                return "-"
            return f"{value * 100:.{digits}f}%" if pct else f"{value:.{digits}f}"
        win = row[f"ret{horizon}_win_rate"]
        mean = row[f"ret{horizon}_mean"]
        median = row[f"ret{horizon}_median"]
        factor = row[f"ret{horizon}_profit_factor"]
        excess = row.get(f"exc{horizon}_mean")
        print(
            f"{horizon:>4}d {row[n_key]:>7} {fmt(win):>8} {fmt(mean):>9} "
            f"{fmt(median):>9} {fmt(factor, pct=False):>8} {fmt(excess):>9}"
        )
    mfe = row.get("mfe_mean")
    mae = row.get("mae_mean")
    if mfe is not None and mae is not None:
        print(f"窗口内平均最大浮盈 {mfe * 100:.2f}% / 平均最大浮亏 {mae * 100:.2f}%")


def _run_one(
    repo: KlineRepository,
    strategy_engine: StrategyEngine,
    strategy_id: str,
    symbols: list[str] | None,
    args: argparse.Namespace,
    horizons: tuple[int, ...],
    start: date,
    end: date,
    data_dir: Path,
) -> None:
    try:
        strategy = strategy_engine.get(strategy_id)
    except ValueError as exc:
        print(f"[{strategy_id}] 策略不可用: {exc}")
        return
    if strategy.execution_backend != "matrix_native":
        print(f"[{strategy_id}] 不是 matrix_native 策略, 暂不支持 Signal Lab 复盘")
        return

    config = LabRunConfig(
        strategy_id=strategy_id,
        start=start,
        end=end,
        symbols=tuple(symbols) if symbols else None,
        limit=args.limit,
        horizons=horizons,
        entry_delay=args.entry_delay,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
        drop_warmup=args.drop_warmup,
        matrix_cache_mb=args.matrix_cache_mb,
        write=not args.no_write,
    )

    def progress(stage: str, pct: int, msg: str) -> None:
        print(f"  [{pct:3d}%] {msg}")

    try:
        frame = run_lab(repo, strategy_engine, config, data_dir=data_dir, on_progress=progress)
    except Exception as exc:
        print(f"[{strategy_id}] 复盘失败: {exc}")
        return
    if not args.no_write and not frame.is_empty():
        print(f"  -> 台账已写入 {data_dir / 'signal_lab' / strategy_id / config.dataset_name}")
    _print_summary(frame, strategy_id, horizons)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    horizons = tuple(sorted({int(h) for h in args.horizons.split(",") if h}))
    if not horizons:
        print("horizons 不能为空")
        return 2

    data_dir = Path(args.data_dir).resolve() if args.data_dir else ROOT.parent / "data"
    repo = KlineRepository(DataStore(data_dir))
    _, latest = repo.get_enriched_latest()
    if latest is None:
        print("enriched 数据不可用, 请先完成数据同步")
        return 1
    end = date.fromisoformat(args.end) if args.end else latest
    if args.start:
        start = date.fromisoformat(args.start)
    else:
        start = end - timedelta(days=int(args.lookback_days * 1.6))

    symbols = _resolve_symbols(repo, args)
    strategy_engine = StrategyEngine(
        strategy_dirs=[
            ROOT / "app" / "strategy" / "builtin",
            data_dir / "strategies" / "custom",
            data_dir / "strategies" / "ai",
            data_dir / "strategies" / "composite",
        ]
    )

    print(f"区间 {start} ~ {end} | 标的 {len(symbols) if symbols else '全部'} | 持有期 {horizons}")
    for strategy_id in args.strategy:
        _run_one(
            repo, strategy_engine, strategy_id, symbols, args,
            horizons, start, end, data_dir,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
