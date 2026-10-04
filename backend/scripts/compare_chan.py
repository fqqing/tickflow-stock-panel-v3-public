"""对拍: v1 自研缠论 vs Vespa314/chan.py, 同标的同数据同窗口.

这是决定是否迁移的依据, 不看「谁画得好看」, 只看两个可量化的东西:
1. 结构是否收敛 -- 笔端点位置的重合率, 方向一致性
2. 信号是否有后验价值 -- 买卖点出现后的区间收益

用法:
    backend/scripts/compare_chan.py 600519.SH 000001.SZ 300750.SZ
"""

from __future__ import annotations

import contextlib
import importlib.util
import statistics
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
V1_BACKEND = Path("D:/project/GP/tickflow-stock-panel/backend")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

with contextlib.suppress(AttributeError):
    # 非 Windows 平台可能没有 reconfigure, 失败无所谓
    sys.stdout.reconfigure(encoding="utf-8")

from chanlab.engine import build_chan, level_elements  # noqa: E402
from chanlab.loader import load_symbol_daily  # noqa: E402

# 用于「信号后 N 日收益」的后验窗口
FORWARD_DAYS = 20


def load_v1_engine():
    """按文件路径直接加载 v1 的缠论模块.

    不走 ``import app.indicators.chan`` 是为了避开 v1 app 包的 import 副作用:
    那个模块自称纯函数, 只依赖 numpy, 单独加载完全够用.
    """
    path = V1_BACKEND / "app" / "indicators" / "chan.py"
    # 模块名必须先进 sys.modules 再 exec_module: 该文件里有 @dataclass, 而 dataclasses
    # 在解析注解时会去 sys.modules[cls.__module__] 取名空间, 没注册就直接 AttributeError.
    name = "v1_chan_indicator"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 v1 缠论模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def forward_return(close: list[float], index: int) -> float | None:
    """从 index 起持有 FORWARD_DAYS 个交易日的收益."""
    if index < 0 or index + FORWARD_DAYS >= len(close):
        return None
    entry, exit_price = close[index], close[index + FORWARD_DAYS]
    if entry <= 0:
        return None
    return (exit_price - entry) / entry


def compare_one(v1_mod, symbol: str, lookback: int = 800, signal_mode: str = "replay") -> dict:
    """对单只标的产出对比结果."""
    frame = load_symbol_daily(symbol)
    if frame.height > lookback:
        frame = frame.tail(lookback)
    if frame.height < 120:
        return {"symbol": symbol, "error": f"样本不足: {frame.height} 根"}

    dates = frame["date"].to_list()
    high = frame["high"].to_numpy()
    low = frame["low"].to_numpy()
    close = frame["close"].to_numpy()

    tick = time.perf_counter()
    v1_result = v1_mod.analyze(high, low, close, strict=True)
    v1_cost = time.perf_counter() - tick

    tick = time.perf_counter()
    chan = build_chan(symbol, start=dates[0])
    v2_static_cost = time.perf_counter() - tick
    drums = level_elements(chan)

    tick = time.perf_counter()
    v2_signal_pairs = collect_v2_signals(symbol, dates[0], mode=signal_mode)
    v2_cost = (time.perf_counter() - tick) + v2_static_cost

    # 索引对齐的前置校验: v2 的 klu.idx 是相对喂进去的 K 线编号, 只有它等于
    # frame.height 时「原始 K 线索引」这套坐标才和 v1 同源, 否则下面的重合率全是假的
    v2_klu = sum(len(klc.lst) for klc in drums["merged"])

    # 笔端点。v1 是原始 K 线索引, v2 的 klu.idx 同为喂进去的相对索引, 可直接比
    v1_strokes = {(s.start_index, s.end_index): s for s in v1_result.strokes}
    v2_bi = {(b.get_begin_klu().idx, b.get_end_klu().idx): b for b in drums["bi"]}
    common = set(v1_strokes) & set(v2_bi)

    # 方向一致性只看双方都认出来的笔
    agree = 0
    for key in common:
        v1_dir = 1 if v1_strokes[key].direction > 0 else -1
        v2_dir = 1 if v2_bi[key].dir.name == "UP" else -1
        if v1_dir == v2_dir:
            agree += 1

    v1_signals = {(s.index, s.is_buy) for s in v1_result.signals}
    v2_signals = set(v2_signal_pairs)

    def perf(signal_pairs: set, *, take: slice) -> dict:
        values = []
        for index, _is_buy in sorted(signal_pairs)[take]:
            got = forward_return(close.tolist(), index)
            if got is not None:
                values.append(got)
        if not values:
            return {"n": 0, "mean": None, "win": None}
        return {
            "n": len(values),
            "mean": statistics.fmean(values),
            "win": sum(1 for v in values if v > 0) / len(values),
        }

    return {
        "symbol": symbol,
        "bars": frame.height,
        "v2_klu": v2_klu,
        "aligned": v2_klu == frame.height,
        "v1_sec": v1_cost,
        "v2_sec": v2_cost,
        "v1_bi": len(v1_result.strokes),
        "v2_bi": len(drums["bi"]),
        "match_bi": len(common),
        "match_rate": len(common) / max(len(v1_strokes), 1),
        "dir_agree": agree / len(common) if common else 0.0,
        "v1_center": len(v1_result.centers),
        "v2_center": len(drums["zs"]),
        "v2_seg": len(drums["seg"]),
        "v1_sig": len(v1_result.signals),
        "v2_sig": len(drums["bsp"]),
        "sig_common": len(v1_signals & v2_signals),
        "v1_buy_perf": perf(v1_signals, take=slice(None)),
        "v2_buy_perf": perf(
            {(idx, buy) for idx, buy in v2_signals if buy}, take=slice(None)
        ),
    }


def collect_v2_signals(symbol: str, start, mode: str = "replay") -> list[tuple[int, bool]]:
    """取 chan.py 的买卖点, 返回 [(k线索引, 是否买点)].

    ⚠️ 为什么要区分两种模式:

    static 模式直接读算完之后 ``bs_point_lst`` 里剩下的点. 但上游会在后续走势
    证伪某个买卖点时调用 ``clear_store_end`` 把它删掉, 所以这个集合是**被后验
    筛选过的幸存者** -- 亏钱的信号已经被剔除, 拿它统计胜率必然虚高到 90%+.
    只能用于「当下结构」展示, 绝不能用于绩效统计.

    replay 模式用逐根回放, 记录每个买卖点**首次出现的那一帧**, 得到的是历史上
    真实出现过的信号全集, 与 v1 的 signals 才是同口径.
    """
    chan = build_chan(symbol, start=start)
    if mode == "static":
        return [(p.klu.idx, p.is_buy) for p in level_elements(chan)["bsp"]]

    chan = build_chan(symbol, start=start, config={"trigger_step": True})
    seen: dict[tuple[int, bool], None] = {}
    for snapshot in chan.step_load():
        for point in snapshot[0].bs_point_lst.getSortedBspList():
            # 首次出现的那一帧, 就是它在当时唯一可能被交易者观察到的时刻
            seen.setdefault((point.klu.idx, point.is_buy), None)
    return list(seen)


def pick_symbols(count: int, seed: int = 7) -> list[str]:
    """从最新交易日分区随机抽 A 股标的, 固定 seed 保证可复现."""
    import random

    import polars as pl
    from chanlab.loader import enriched_glob

    files = sorted(glob_leaf(enriched_glob()))
    latest = files[-1]
    symbols = pl.read_parquet(latest, columns=["symbol"])["symbol"].unique().to_list()
    # 只留 A 股: 港美股的历史长度与涨跌停规则都不同, 混进来会污染统计口径
    # 必须先排序再 shuffle: polars 的 unique() 返回顺序不稳定, 不排序的话同一个
    # seed 两次运行会抽到不同的标的, 跨轮对比就不可信了。
    symbols = sorted(set(symbols))
    symbols = [s for s in symbols if s.endswith((".SH", ".SZ", ".BJ"))]
    random.Random(seed).shuffle(symbols)
    return symbols[:count]


def glob_leaf(pattern: str) -> list[str]:
    import glob

    return glob.glob(pattern, recursive=True)


def aggregate(rows: list[dict]) -> None:
    """跨标的汇总 -- 单只标的的信号数只有个位数, 必须池化才有统计意义."""
    valid = [r for r in rows if "error" not in r]
    if not valid:
        return
    misaligned = [r["symbol"] for r in valid if not r["aligned"]]
    if misaligned:
        print(f"\n[警告] {len(misaligned)} 只标的 K 线索引未对齐, 重合率不可信: {misaligned[:5]}")

    v1_sig = sum(r["v1_sig"] for r in valid)
    v2_sig = sum(r["v2_sig"] for r in valid)
    print(f"\n[汇总] 有效标的 {len(valid)} 只")
    print(f"[汇总] 信号总数  v1={v1_sig}  v2={v2_sig}")
    print(f"[汇总] 笔重合率  中位 {statistics.median([r['match_rate'] for r in valid]):.1%}"
          f"  均值 {statistics.fmean([r['match_rate'] for r in valid]):.1%}")
    print(f"[汇总] 笔数      v1 中位 {statistics.median([r['v1_bi'] for r in valid]):.0f}"
          f"  v2 中位 {statistics.median([r['v2_bi'] for r in valid]):.0f}")
    for name in ("v1_buy_perf", "v2_buy_perf"):
        total = [r[name] for r in valid]
        n = sum(d["n"] for d in total)
        if n == 0:
            print(f"[汇总] {name}: 无样本")
            continue
        mean = statistics.fmean([d["mean"] * d["n"] for d in total if d["mean"] is not None]
                                ) / sum(d["n"] for d in total if d["mean"] is not None)
        win = sum(d["win"] * d["n"] for d in total if d["win"] is not None) / n
        print(f"[汇总] {name}  样本={n}  加权均值={mean:+.2%}  加权胜率={win:.1%}")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="v1 自研缠论 vs chan.py 对拍")
    parser.add_argument("symbols", nargs="*", help="标的代码; 留空则用 --sample 抽样")
    parser.add_argument("--sample", type=int, default=0, help="随机抽样标的数")
    parser.add_argument("--lookback", type=int, default=800, help="每只标的使用的日线根数")
    parser.add_argument("--mode", choices=("replay", "static"), default="replay",
                        help="v2 信号采集方式: replay 为历史全集(公平), static 为幸存者集合(仅供对照)")
    args = parser.parse_args()

    symbols = args.symbols or (pick_symbols(args.sample) if args.sample else
                               ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "601398.SH"])

    v1_mod = load_v1_engine()
    rows = []
    for symbol in symbols:
        try:
            rows.append(compare_one(v1_mod, symbol, lookback=args.lookback,
                                    signal_mode=args.mode))
        except Exception as exc:  # 单只失败不拖垮整批
            rows.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

    print(f"{'标的':<12}{'根数':>6}{'笔_v1':>7}{'笔_v2':>7}{'重合':>7}{'重合率':>9}{'方向一致':>10}"
          f"{'段_v2':>7}{'中枢_v1':>9}{'中枢_v2':>9}")
    print("-" * 92)
    for row in rows:
        if "error" in row:
            print(f"{row['symbol']:<12} ERROR {row['error']}")
            continue
        print(f"{row['symbol']:<12}{row['bars']:>6}{row['v1_bi']:>7}{row['v2_bi']:>7}"
              f"{row['match_bi']:>7}{row['match_rate']:>8.1%}{row['dir_agree']:>10.1%}"
              f"{row['v2_seg']:>7}{row['v1_center']:>9}{row['v2_center']:>9}")

    print(f"\n{'标的':<12}{'信号_v1':>9}{'信号_v2':>9}{'同位':>7}"
          f"{'v1后验均值':>13}{'v1胜率':>10}{'v2后验均值':>13}{'v2胜率':>10}{'耗时 v1/v2':>16}")
    print("-" * 100)
    for row in rows:
        if "error" in row:
            continue
        a, b = row["v1_buy_perf"], row["v2_buy_perf"]
        fmt = lambda d: f"{d['mean']:+.2%}({d['n']})" if d["mean"] is not None else "n/a"  # noqa: E731
        win = lambda d: f"{d['win']:.1%}" if d["win"] is not None else "n/a"  # noqa: E731
        print(f"{row['symbol']:<12}{row['v1_sig']:>9}{row['v2_sig']:>9}{row['sig_common']:>7}"
              f"{fmt(a):>13}{win(a):>10}{fmt(b):>13}{win(b):>10}"
              f"{row['v1_sec']:>8.2f}s/{row['v2_sec']:.2f}s")

    aggregate(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
