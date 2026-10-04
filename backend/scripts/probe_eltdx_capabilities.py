"""探明 eltdx 3.x 除分钟K之外的全部能力(字段 / 量纲 / 耗时 / 可用性)。

背景
====
插件 ``app/plugins/eltdx`` 目前只用了 ``bars.get(period='1m')``。而 TdxClient 上还挂着
quotes(五档+快照) / trades(逐笔+竞价匹配) / auctions(竞价序列) / money_flow(资金流) /
corporate(复权因子+财务) / helpers(题材+涨停梯队+实时排行) 等一批接口。本项目没有
任何文档说明它们的返回形状, 接之前必须实测 —— 铁律: 量纲没有机器校验, 只能靠探针。

用法
====
    python scripts/probe_eltdx_capabilities.py            # 全量
    python scripts/probe_eltdx_capabilities.py --only depth,trades
    python scripts/probe_eltdx_capabilities.py --symbol 600519.SH

输出: 每个接口一行摘要(耗时 / 条数 / 字段), 失败打印异常类型+消息。

可选组(逗号分隔): depth, snapshot, trades, auction, moneyflow, corporate, topic,
ladder, rank, daily
"""

from __future__ import annotations

import argparse
import inspect
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ALL_GROUPS = (
    "depth",
    "snapshot",
    "trades",
    "auction",
    "moneyflow",
    "corporate",
    "topic",
    "ladder",
    "rank",
    "daily",
)


def _fmt(value: Any, limit: int = 160) -> str:
    """把任意返回值压成一行, 超长截断。"""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _shape(obj: Any) -> str:
    """描述返回对象的形状: 类型 + 长度(若有)。"""
    name = type(obj).__name__
    try:
        return f"{name}(len={len(obj)})"  # type: ignore[arg-type]
    except TypeError:
        return name


def _peek(obj: Any, n: int = 2) -> str:
    """取前 n 条元素做样例; 非序列则返回字段字典。"""
    if isinstance(obj, dict):
        return _fmt({k: obj[k] for k in list(obj)[:n]}, 400)
    try:
        items = list(obj)[:n]  # type: ignore[call-overload]
    except TypeError:
        fields = {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_") and not callable(getattr(obj, k, None))}
        return _fmt(fields, 400)
    return _fmt(items, 500)


def probe(label: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    """跑一个接口并打印签名 / 耗时 / 形状 / 样例。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = "?"
    t0 = time.perf_counter()
    try:
        out = fn(*args, **kwargs)
    except Exception as exc:  # 探针脚本: 任何异常都要记下来而不是中断
        cost = time.perf_counter() - t0
        print(f"[FAIL] {label:<16} {cost:6.2f}s  {type(exc).__name__}: {_fmt(str(exc), 200)}")
        print(f"       sig={sig}")
        return
    cost = time.perf_counter() - t0
    print(f"[ OK ] {label:<16} {cost:6.2f}s  {_shape(out)}")
    print(f"       sig={sig}")
    print(f"       head={_peek(out)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="探明 eltdx 剩余能力")
    parser.add_argument("--symbol", default="600519.SH", help="面板格式代码, 默认 600519.SH")
    parser.add_argument("--only", default="", help=f"只跑指定组, 逗号分隔: {','.join(_ALL_GROUPS)}")
    args = parser.parse_args()

    groups = [g.strip() for g in args.only.split(",") if g.strip()] or list(_ALL_GROUPS)
    unknown = [g for g in groups if g not in _ALL_GROUPS]
    if unknown:
        print(f"未知组: {unknown}; 可选: {_ALL_GROUPS}")
        return 2

    import eltdx

    from app.plugins.eltdx.provider import app_to_eltdx

    code = app_to_eltdx(args.symbol)
    if code is None:
        print(f"无法把 {args.symbol} 转成 eltdx 代码")
        return 2
    print(f"eltdx {getattr(eltdx, '__version__', '?')} | symbol={args.symbol} -> {code}")
    print(f"groups={groups}\n")

    cli = eltdx.TdxClient()

    if "depth" in groups:
        probe("quotes.depth", cli.quotes.get_depth, [code])
    if "snapshot" in groups:
        probe("quotes.snapshot", cli.quotes.get_snapshots, [code])
        probe("helpers.full_quote", cli.helpers.full_quotes, [code])
    if "trades" in groups:
        probe("trades.today", cli.trades.today, code)
        probe("trades.open_match", cli.trades.opening_match_today, code)
    if "auction" in groups:
        probe("auctions.series", cli.auctions.series, code)
        probe("helpers.auction", cli.helpers.auction_data, code)
    if "moneyflow" in groups:
        probe("money_flow.daily", cli.money_flow.daily, [code])
    if "corporate" in groups:
        probe("corp.adj_factor", cli.corporate.adjustment_factors, code)
        probe("corp.capital", cli.corporate.capital_changes, code)
    if "topic" in groups:
        probe("topic.stock", cli.helpers.stock_topics, code)
        probe("topic.strength", cli.helpers.theme_strength_rank, None, count=10)
    if "ladder" in groups:
        probe("limit_ladder", cli.helpers.limit_ladder, None, count=10)
        probe("daily_limits", cli.helpers.daily_price_limits, [code])
    if "rank" in groups:
        probe("realtime_rank", cli.helpers.realtime_rank, count=10)
        probe("buy_sell_strength", cli.helpers.buy_sell_strength, code)
    if "daily" in groups:
        probe("bars.1d", cli.bars.get, [code], period="1d", count=10)

    cli.close()
    print("\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
