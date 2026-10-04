"""从通达信(pytdx)拉取 1 分钟 K, 写入本项目的 kline_minute 分区。

用法(必须用装了 pytdx 的解释器, 本机是 Anaconda)::

    D:\\Environment\\Anaconda\\python.exe scripts/import_minute_tdx.py --limit 5   # 小批量试
    D:\\Environment\\Anaconda\\python.exe scripts/import_minute_tdx.py            # 全部自选股
    D:\\Environment\\Anaconda\\python.exe scripts/import_minute_tdx.py --skip-existing   # 续跑

设计要点(都是实测结论, 改代码前先看):

1. **pytdx 必须跳过握手**: `api.need_setup = False`。pytdx 1.72 的 SetupCmd1/2/3
   握手包被现代服务器拒, 握手后 K 线请求只回 2 字节错误码 -> "TCP 通但取不到数据"。
   另外 `need_setup` 是**实例属性**, 不是构造参数(`TdxHq_API(need_setup=False)` 会 TypeError)。
2. **单次最多 800 根**: count>800 返回空; start 偏移翻页取不到更早数据。
   => 本脚本是增量式的(每天跑, 逐步累积), 拿不到几个月前的分钟历史。
3. **服务器只有少数可用**: 实测 115.238.56.198 / 60.191.117.167 可用, 其余要么
   TCP 不通要么拒 K 线请求。调用偶发失败(TdxFunctionCallError), 必须重试。
4. **量纲: pytdx 分钟 vol 单位是「股」, 本项目是「手」 => 必须 /100**。
   日线反而不一样(pytdx 日线 vol 已是「手」, 与项目一致), 别照抄。
   判据: amount / vol / close, 分钟线比值约 1(股), 项目存量约 100(手)。
5. **datetime 是北京墙钟**, 与项目存量一致(09:30~15:00), 不做时区转换。
   前端 formatMinuteTime 是 hour<8 才 +8, 存墙钟才不会显示错乱。
6. **只支持 1 分钟**: 项目 kline_minute 没有 freq 列, 混入 5/15 分钟会串数据。
7. **落盘必须攒批**: 每只标的都"读回日期分区 -> 合并 -> 重写"是 O(n^2) IO。
   实测 914 只跑到约 7.7s/只(而试跑 3 只时只有 0.5s/只), 瓶颈就在这里。
   改为内存攒批, 每 --flush-every 只落一次盘(每个日期分区只读回一次)。
8. **重建连接要快, 别乱重建**: 实测失败模式是 pytdx 抛 TdxFunctionCallError, 一抛
   连接就废了(同连接重试没用), 所以异常即重建; 但重建必须优先复用上次连上的服务器
   (`_good` 排最前), 且 connect 超时只给 2s。旧版每次重建都遍历 5 台 x 8s deadline,
   单只成本被抬到约 7.7s(其中真正的网络请求只有 0.06s)。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

# --- 常量 -------------------------------------------------------------------

CATEGORY_1MIN = 8       # 实测: 8=1分钟, 0=5分钟, 1=15分钟, 9=日线
MAX_COUNT = 800         # 实测硬上限, 超过返回空
MINUTE_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

SERVERS = [
    ("115.238.56.198", 7709),
    ("60.191.117.167", 7709),
    ("115.238.90.165", 7709),
    ("218.75.126.9", 7709),
    ("218.108.98.244", 7709),
]

CONNECT_TIMEOUT = 2.0   # pytdx connect 的 socket 超时(秒); 建连重试成本的主要来源
MAX_REBUILD = 2         # 单只最多重建几次连接(实测抛异常后连接基本就废了, 必须重建)
FETCH_SLEEP = 0.02      # 每只之间的间隔(秒), 别把服务器打爆

REPO = Path(__file__).resolve().parents[1]
WATCHLIST = REPO / "data" / "user_data" / "watchlist.parquet"
DEFAULT_OUT = REPO / "data" / "kline_minute"


def market_of(symbol: str) -> int | None:
    """600522.SH -> 1(沪), 000001.SZ -> 0(深); 其余返回 None 跳过。"""
    if symbol.endswith(".SH"):
        return 1
    if symbol.endswith(".SZ"):
        return 0
    return None


def normalize_bars(bars) -> pd.DataFrame:
    """pytdx 原始 bars -> 项目口径 DataFrame(股转手, datetime 转 us)。"""
    d = pd.DataFrame(bars)
    d["symbol"] = None  # 调用方回填
    d = d.rename(columns={"vol": "volume"})
    d["volume"] = pd.to_numeric(d["volume"], errors="coerce") / 100.0  # 股 -> 手
    d["datetime"] = pd.to_datetime(d["datetime"]).astype("datetime64[us]")
    for c in ("open", "high", "low", "close", "amount"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d[MINUTE_COLS]


class TdxSession:
    """pytdx 连接封装: 同连接优先重试, 重建时优先复用上次连上的服务器。"""

    def __init__(self) -> None:
        self._api = None
        self._good: tuple[str, int] | None = None
        self.rebuilds = 0
        self.last_err: Exception | None = None

    def _open(self, server: tuple[str, int]):
        from pytdx.hq import TdxHq_API

        ip, port = server
        api = TdxHq_API(heartbeat=False, raise_exception=True)
        api.need_setup = False
        try:
            api.connect(ip, port, time_out=CONNECT_TIMEOUT)
        except Exception:
            try:
                api.disconnect()
            except Exception:
                pass
            return None
        return api

    def connect(self) -> bool:
        """连上任意一台可用服务器; 上次成功的那台排在最前面。"""
        order = list(SERVERS)
        if self._good is not None and self._good in order:
            order.remove(self._good)
            order.insert(0, self._good)
        for server in order:
            api = self._open(server)
            if api is not None:
                self._api = api
                self._good = server
                return True
        return False

    def _rebuild(self) -> bool:
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:
                pass
        self._api = None
        self.rebuilds += 1
        return self.connect()

    def _try_bars(self, market: int, code: str):
        try:
            return self._api.get_security_bars(CATEGORY_1MIN, market, code, 0, MAX_COUNT)
        except Exception as e:  # noqa: BLE001
            self.last_err = e
            return None

    def history_minute(self, market: int, code: str, ymd: int) -> list | None:
        """拉某一天的历史分时(整天 240 根), 供 backfill_minute_tdx.py 按天回补。

        - 返回 **空列表**: 非交易日 / 未来日期 / 当天停牌, 正常现象, 不是错误。
        - 返回 **None**: 接口抛异常(多半是被服务器限流), 已试过重建连接仍失败。
        """
        for attempt in range(MAX_REBUILD + 1):
            try:
                return self._api.get_history_minute_time_data(market, code, ymd)
            except Exception as e:
                self.last_err = e
                if attempt < MAX_REBUILD and self._rebuild():
                    continue
                return None
        return None

    def fetch(self, market: int, code: str) -> pd.DataFrame:
        """拉单只 1 分钟 K。

        实测两种"拿不到数据", 处理办法不同:
        - **抛异常**(TdxFunctionCallError "calling function error"): 连接基本已废,
          同连接重试没用, 必须重建。这是主要失败模式, 所以异常即重连。
        - **返回空列表**: 该标的没数据(停牌/退市), 重连也没用, 直接返回。
        """
        err = None
        for attempt in range(MAX_REBUILD + 1):
            bars = self._try_bars(market, code)
            if bars:
                return normalize_bars(bars)
            if self.last_err is None:  # 没抛异常, 只是没数据
                break
            err = self.last_err
            self.last_err = None
            if attempt < MAX_REBUILD and self._rebuild():
                continue
            break
        if err is not None:
            self.last_err = err  # 留给调用方打印
        return pd.DataFrame(columns=MINUTE_COLS)

    def close(self) -> None:
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:
                pass
            self._api = None


def flush(frames: list[pd.DataFrame], out_dir: Path) -> int:
    """把攒下的一批数据按日期分区合并落盘。返回本次落盘的总行数。"""
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["symbol", "datetime"])
    if df.empty:
        return 0
    total = 0
    df["_day"] = df["datetime"].dt.strftime("%Y-%m-%d")
    for day, g in df.groupby("_day"):
        part_dir = out_dir / ("date=%s" % day)
        part_dir.mkdir(parents=True, exist_ok=True)
        out = part_dir / "part.parquet"
        g = g.drop(columns=["_day"])
        if out.exists():
            try:
                old = pd.read_parquet(out)
            except Exception:
                old = pd.DataFrame(columns=MINUTE_COLS)
            if not old.empty:
                old["datetime"] = pd.to_datetime(old["datetime"]).astype("datetime64[us]")
                g = pd.concat([old, g], ignore_index=True)
        g = g.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        g = g.sort_values(["symbol", "datetime"]).reset_index(drop=True)
        g = g[MINUTE_COLS]
        # 分区 schema 必须与既有分区一致: datetime 统一 us。
        # pandas 默认是 ns, 与既有 us 分区混合会让 polars scan_parquet 跨分区 union 失败
        # (Datetime('ns') != Datetime('us')) -> 整个分钟K目录查不出来(分时图全空)。
        g["datetime"] = pd.to_datetime(g["datetime"]).astype("datetime64[us]")
        tmp = out.with_suffix(".parquet.tmp")
        g.to_parquet(tmp, index=False)
        os.replace(tmp, out)
        total += len(g)
    return total


def latest_partition_symbols(out_dir: Path) -> set[str]:
    """最新日期分区里已有的标的集合, 供 --skip-existing 跳过用。"""
    parts = sorted(p.name for p in out_dir.glob("date=*") if p.is_dir())
    if not parts:
        return set()
    f = out_dir / parts[-1] / "part.parquet"
    if not f.exists():
        return set()
    try:
        d = pd.read_parquet(f, columns=["symbol"])
    except Exception:
        return set()
    return set(d["symbol"].astype(str).dropna().tolist())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只(试跑用)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--skip-first", type=int, default=0, help="跳过前 N 只(续跑用)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="跳过最新日期分区里已有数据的标的(推荐续跑时加)")
    ap.add_argument("--flush-every", type=int, default=100,
                    help="攒够 N 只落一次盘(默认 100, 越小越省内存但越慢)")
    args = ap.parse_args()

    try:
        import pytdx  # noqa: F401
    except ImportError:
        print("ERROR: 当前解释器没有 pytdx。请用 Anaconda: "
              "D:\\Environment\\Anaconda\\python.exe")
        return 1

    if not WATCHLIST.exists():
        print("ERROR: 自选股文件不存在 %s" % WATCHLIST)
        return 1
    wl = pd.read_parquet(WATCHLIST)
    symbols = [str(s) for s in wl["symbol"].tolist()]
    print("watchlist symbols = %d" % len(symbols))

    todo = []
    for s in symbols:
        m = market_of(s)
        if m is None:
            continue
        todo.append((s, m, s.rsplit(".", 1)[0]))

    out_dir = Path(args.out_dir)
    if args.skip_existing:
        done = latest_partition_symbols(out_dir)
        if done:
            before = len(todo)
            todo = [t for t in todo if t[0] not in done]
            print("skip-existing: 最新分区已有 %d 只, 跳过 %d 只, 剩 %d 只"
                  % (len(done), before - len(todo), len(todo)))
    if args.skip_first:
        todo = todo[args.skip_first:]
    if args.limit:
        todo = todo[: args.limit]
    print("todo = %d (skip=%d limit=%d)" % (len(todo), args.skip_first, args.limit))
    if not todo:
        print("nothing to do")
        return 0

    sess = TdxSession()
    if not sess.connect():
        print("ERROR: 所有 pytdx 服务器都连不上")
        return 1
    print("pytdx connected")

    ok = fail = 0
    buf: list[pd.DataFrame] = []
    t_all = time.time()
    t_net = 0.0
    t_io = 0.0
    for i, (symbol, market, code) in enumerate(todo, 1):
        t0 = time.time()
        d = sess.fetch(market, code)
        t_net += time.time() - t0
        if d.empty:
            fail += 1
            if sess.last_err:
                print("    ! %s failed: %s" % (symbol, sess.last_err))
                sess.last_err = None
        else:
            d["symbol"] = symbol
            buf.append(d)
            ok += 1
        if len(buf) >= args.flush_every:
            t1 = time.time()
            n = flush(buf, out_dir)
            t_io += time.time() - t1
            buf = []
            print("[%d/%d] flush rows=%d ok=%d fail=%d net=%.1fs io=%.1fs"
                  % (i, len(todo), n, ok, fail, t_net, t_io))
        elif i % 50 == 0 or i == len(todo):
            print("[%d/%d] ok=%d fail=%d net=%.1fs io=%.1fs"
                  % (i, len(todo), ok, fail, t_net, t_io))
        time.sleep(FETCH_SLEEP)

    t1 = time.time()
    n = flush(buf, out_dir)
    t_io += time.time() - t1
    sess.close()
    el = time.time() - t_all
    print("DONE ok=%d fail=%d elapsed=%.1fs (net=%.1fs io=%.1fs rebuild=%d) out=%s"
          % (ok, fail, el, t_net, t_io, sess.rebuilds, out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
