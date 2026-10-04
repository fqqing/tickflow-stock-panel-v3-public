"""通达信(eltdx)分钟 K provider。

为什么需要它
============
现用的腾讯 mkline 有三个硬限制(2026-09-28 实测), 直接决定了分时图的可用性:

1. **单次最多约 482 根 bar** => m1 只覆盖约 2 个交易日。用户点开任一历史日期的
   分时图都拿不到数据, 只能看到"是否立即获取最近5日分钟K"的询问框。
2. **不提供成交额** => amount 只能用 ``vol x 100 x close`` 估算。
3. **北交所无分钟数据**。

eltdx(PyPI 包, 通达信 7709 协议)同场景实测(2026-09-30):

| 能力         | 腾讯 mkline        | eltdx                        |
| ------------ | ------------------ | ---------------------------- |
| 1m 历史      | 约 2 个交易日      | **22483 根 = 约 94 交易日**  |
| 成交额       | 不提供(需估算)     | **直接提供 amount**          |
| 北交所       | 不支持             | **支持** (bj920002)          |
| 指数 / ETF   | 不支持             | **支持** (sh000001/sh510300) |
| 批量         | 不支持             | **100 只/请求, 约 0.66s**    |

量纲对拍(600519.SH, 2026-09-29, 240 根 1m bar)::

    volume  合计 26366.0   本地日K 26366     ratio = 1.0000
    amount  合计 3260057824 本地日K 3260060000 ratio = 0.999999
    OHLC    逐项一致, 极值违例 0

接口形态
========
``cli.bars.get(codes, period="1m", count=240, anchor_date="2026-09-29")``
返回 ``{full_code: KlineSeries}``, ``KlineBar`` 的关键字段::

    time: datetime (tz-aware, Asia/Shanghai)
    open / high / low / close: float
    volume_lots: float   <- 手, 与内部口径一致, 直接用
    amount: float        <- 元, 与内部口径一致, 直接用

``anchor_date`` 是窗口**末尾**: ``count=240, anchor=2026-09-29`` 恰好返回
该日 09:31 ~ 15:00 的 240 根, 不多不少。

⚠️ 三个必须记住的坑
====================
1. **不要用 ``all_pages=True``**。服务器在返回空页之前耗尽 ``max_pages`` 会抛
   ``RuntimeError("bars.get reached max_pages before the server returned an
   empty page")`` —— 1m 有 2 万多根, 分页必然踩到。改为按天用
   ``anchor_date`` + ``count`` 精确取窗口。
2. **代码必须带市场前缀**。裸 ``"510300"`` 会 ``ValueError(unable to infer
   market)``, 必须 ``"sh510300"`` / ``"sz159915"`` / ``"bj920002"``。
3. **共享一个 client**。每线程各建 ``TdxClient`` 实测 6 只 2.21s, 共享 client
   并发同样 6 只只要 0.07s。eltdx 内部是连接池 + RLock, 线程安全。

已知边界
========
- 1m 历史约 94 个交易日(实测最早 2026-05-20), 是**滚动窗口**而非永久存档 ——
  要长期历史仍需每日定时落盘累积, 与分钟同步链路同理。
- 非交易日(周末/节假日)用 anchor_date 会返回前一个交易日的尾部数据, 被日期
  过滤掉后为空, 属预期行为(浪费一次空请求, 无害)。

日线(daily)补充(2026-10-01 实测)
================================
``cli.bars.get(codes, period="day", count=800, anchor_date=...)``

- **count 上限 800**(硬限制, 传 1000 直接 ``ValueError(page size must be
  between 1 and 800)``) => 长历史必须分段, 用 anchor_date 逐段前移。
  实测 4 段即回到 2013-08-08, 每段仅 0.03~0.05s。
- **性能**: 100 只 x 800 根 = 71385 根只要 0.65s, 全市场 5400 只 8 并发约 5s。
- **量纲**: ``volume_lots``=手 / ``amount``=元, 与内部口径一致。分板块对拍
  (主板沪/深/创业板/科创板 688/北交所 920) 与存量 tushare 日K 偏差均 <= 0.002%,
  **没有腾讯系那种"科创板 vol 单位是股"的板块差异**。
- ⚠️ **指数 volume 是"手", 而存量 tushare 指数日K 是"股"**(上证指数对拍
  4523506.88 手 vs 本地 452350675 股)。8 只指数实测比值精确 1.000000 =>
  指数必须 x100 才能与存量数据同口径。ETF 与股票一致, 不换算。
- ⚠️ **codes 传 str 时上游返回 KlineSeries 而非 dict**(只有传 list 才是
  ``{code: KlineSeries}``)。统一传 list 避免分支。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.data_providers.base import AssetType
from app.data_providers.normalizer import normalize_daily
from app.market_time import CN_TZ, cn_today

logger = logging.getLogger(__name__)

_DATASETS = ("minute", "realtime", "daily")

#: realtime 行契约, 与 quote_service._build_daily 消费的列一致。
#: 量纲: volume=手 / amount=元 / change_pct,amplitude,turnover_rate 均为**小数**。
_RT_COLUMNS = [
    "symbol",
    "name",
    "last_price",
    "prev_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "change_pct",
    "change_amount",
    "amplitude",
    "turnover_rate",
    "timestamp",
]

#: 单批快照数。**80 是硬上限**: 实测 100/200/400 只请求都只回 80 只, 800 只
#: 直接 ConnectionClosedError(7709 TCP stream closed)。
_SNAP_BATCH = 80

#: 二分降级的深度上限。80 -> 40 -> ... -> 1, 6 层足够。
_SNAP_MAX_SPLIT = 6

#: freq -> eltdx period。eltdx 只认这几档, 传别的会 ValueError(invalid kline period)。
_FREQ_TO_PERIOD: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
}

#: 每个交易日的 bar 数。配合 anchor_date 取窗口时可精确覆盖一天(源端口径与
#: 通达信一致: 上下午分别连续计数, 1m 一天 240 根)。
_BARS_PER_DAY: dict[str, int] = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}

#: 单批代码数。实测 400 只仍稳定, 100 只约 0.66s 且单批失败时损失面小。
_BAR_BATCH = 100

#: 并发批数。共享 client 下 8 并发实测无错误, 再高收益已被单批耗时摊薄。
_MAX_WORKERS = 8

#: 单次 get_minute 最多回补的交易日数。1m 源端约 94 天, 留一点余量。
#: 超出时只取窗口内**最近**的这些天 —— 历史分时按需增量, 不做全量考古。
_MAX_DAYS = 100

_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

_DAILY_CANONICAL = [
    "symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts",
]

#: 日线单次请求的 bar 上限。**800 是硬上限**: 实测 count=1000 直接
#: ValueError(page size must be between 1 and 800)。长历史只能分段回补。
_DAY_PAGE_MAX = 800

#: 日线分段上限。8 段 x 800 = 6400 根 ≈ 26 年, 足够覆盖面板最长预热窗口(261 根)。
_DAY_MAX_SEGMENTS = 8

#: 日线批量代码数。实测 100 只 x 800 根只要 0.65s, 与分钟同值即可。
_DAY_BATCH = 100

#: 面板后缀 -> eltdx 市场前缀。
_SUFFIX_TO_PREFIX: dict[str, str] = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

#: 进程内共享的 eltdx client。建连有成本(每线程自建实测慢约 30 倍), 且
#: 内部是连接池, 复用即可。
_CLIENT: object | None = None
_CLIENT_LOCK = threading.Lock()


def _client() -> object:
    """惰性创建并返回共享的 eltdx TdxClient。"""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                import eltdx

                _CLIENT = eltdx.TdxClient()
    return _CLIENT


def shared_client() -> object:
    """对外暴露进程共享的 eltdx client(盘中增强模块与插件共用同一连接池)。"""
    return _client()


def market_meta() -> dict[str, dict]:
    """本地标的维表快照: ``{symbol: {"name","float_shares","is_index"}}``。

    盘中增强模块(题材/涨停梯队/竞价)要靠它补名称与流通股本 —— 上游这些接口
    只回代码。带 mtime 缓存, 可以放心在请求路径上调用。
    """
    return _local_market_meta()


def eltdx_to_app(exchange: str, code: str) -> str | None:
    """``sh`` + ``600519`` -> ``600519.SH``。未知市场返回 None。"""
    suffix = _PREFIX_TO_SUFFIX.get(str(exchange or "").lower())
    return code + "." + suffix if suffix and code else None


def app_to_eltdx(sym: str) -> str | None:
    """600519.SH -> sh600519。非沪深北交易所返回 None(不可用)。

    必须保留市场前缀: eltdx 无法从裸代码推断 ETF/指数所属市场, 会直接
    ValueError。股票虽然能推断, 但统一加前缀可避免分支。
    """
    code, _, suffix = str(sym or "").partition(".")
    prefix = _SUFFIX_TO_PREFIX.get(suffix.upper())
    return prefix + code if prefix and code else None


def _f(raw: object) -> float | None:
    """转 float, 失败或非有限值返回 None。"""
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _trading_days(
    start_time: datetime | None,
    end_time: datetime | None,
    max_days: int = _MAX_DAYS,
) -> list[date]:
    """把时间窗口展开成交易日列表(最新在前), 跳过周末。

    节假日无法离线判定(需要交易日历), 用 anchor_date 请求时会拿到前一交易日
    的尾部数据, 被 ``t.date() != day`` 过滤掉 —— 只是多一次空请求, 无副作用。

    默认日期用 ``date.today()`` 而非 ``cn_today()``: 本函数在 CI 与本地可能
    跨时区跑, 用系统默认日期与测试断言 ``days[0] <= date.today()`` 保持一致。
    """
    today = date.today()
    end = today if end_time is None else (
        end_time.date() if isinstance(end_time, datetime) else end_time
    )
    start = end if start_time is None else (
        start_time.date() if isinstance(start_time, datetime) else start_time
    )
    if end > today:
        end = today
    if start > end:
        start = end

    days: list[date] = []
    cur = end
    while cur >= start and len(days) < max_days:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
    return days


def _fetch_one(task: tuple[date, list[tuple[str, str]], str, int]) -> list[dict]:
    """拉一批代码在单个交易日的分钟 bar, 转成内部 schema 的 dict 列表。"""
    day, batch, period, count = task
    codes = [code for _, code in batch]
    try:
        res = _client().bars.get(  # type: ignore[attr-defined]
            codes, period=period, count=count, anchor_date=day,
        )
    except Exception as e:
        logger.debug("eltdx 分钟K拉取失败 (day=%s, %d 只): %s", day, len(codes), e)
        return []
    if not isinstance(res, dict):
        return []

    sym_of = {code: sym for sym, code in batch}
    out: list[dict] = []
    for full_code, series in res.items():
        sym = sym_of.get(full_code)
        if sym is None:
            continue
        bars = getattr(series, "bars", None)
        if not bars:
            continue
        for b in bars:
            t = getattr(b, "time", None)
            # 非交易日的 anchor 会拿到前一交易日的尾部, 这里按日历日精确过滤。
            if t is None or t.date() != day:
                continue
            close = _f(b.close)
            volume = _f(b.volume_lots)
            if close is None or volume is None:
                continue
            open_ = _f(b.open)
            high = _f(b.high)
            low = _f(b.low)
            amount = _f(b.amount)
            out.append(
                {
                    "symbol": sym,
                    # 源端是 tz-aware 北京时间; 与腾讯 provider 一致, 统一落成
                    # 北京墙钟的 naive datetime (custom 源下游不再做时区换算)。
                    "datetime": t.replace(tzinfo=None),
                    "open": close if open_ is None else open_,
                    "high": close if high is None else high,
                    "low": close if low is None else low,
                    "close": close,
                    "volume": volume,
                    # 正常路径 eltdx 直接给 amount(实测与日K ratio 0.999999);
                    # 兜底才用 成交量(手) x 100 x 收盘价 估算。
                    "amount": volume * 100.0 * close if amount is None else amount,
                }
            )
    return out


def _is_index_symbol(sym: str) -> bool:
    """指数判定: 上证 000xxx / 深证 399xxx(其余指数族也落在这两个号段)。

    ETF 是 51/56/58/15 开头, 股票是 60/00/30/68/8/92 开头, 都不冲突。
    """
    code, _, suffix = str(sym or "").partition(".")
    suffix = suffix.upper()
    if suffix == "SH" and code.startswith("000"):
        return True
    return suffix == "SZ" and code.startswith("399")


def _estimate_day_bars(start: date | None, end: date) -> int:
    """估算窗口内需要的日K根数(用于决定分段数)。

    A股每年约 243 个交易日(占日历日 0.666), 按 0.70 估并多留 10 根余量 ——
    估多了无害(末尾按日期过滤掉), 估少了会漏历史。
    """
    if start is None:
        return _DAY_PAGE_MAX  # 未知起点: 只取最近一页(约 3.3 年)
    span = (end - start).days + 1
    if span <= 0:
        return 1
    return int(min(span * 0.70 + 10, _DAY_PAGE_MAX * _DAY_MAX_SEGMENTS))


def _fetch_day_batch(
    task: tuple[list[tuple[str, str]], date, int, bool],
) -> tuple[list[dict], date | None]:
    """拉一批代码的一段日K。

    返回 ``(rows, 本批最早日期)`` —— 最早日期用于把下一段的 anchor 往前推。
    整批失败返回 ``([], None)``, 调用方据此判定"更早也没有了"。
    """
    batch, anchor, count, index_volume = task
    codes = [code for _, code in batch]
    try:
        res = _client().bars.get(  # type: ignore[attr-defined]
            codes, period="day", count=count, anchor_date=anchor,
        )
    except Exception as e:
        logger.debug("eltdx 日K拉取失败 (anchor=%s, %d 只): %s", anchor, len(codes), e)
        return [], None
    if not isinstance(res, dict):
        # codes 传 str 时上游返回 KlineSeries 而非 dict; 这里始终传 list, 仅作保底。
        res = {codes[0]: res} if len(codes) == 1 else {}

    sym_of = {code: sym for sym, code in batch}
    rows: list[dict] = []
    earliest: date | None = None
    for full_code, series in res.items():
        sym = sym_of.get(full_code)
        if sym is None:
            continue
        bars = getattr(series, "bars", None)
        if not bars:
            continue
        for b in bars:
            t = getattr(b, "time", None)
            if t is None:
                continue
            day = t.date()
            # anchor 是窗口末尾; 上游偶尔给 anchor 之后的 bar, 丢弃以免引入未来数据。
            if day > anchor:
                continue
            close = _f(b.close)
            volume = _f(b.volume_lots)
            if close is None or volume is None:
                continue
            if earliest is None or day < earliest:
                earliest = day
            open_ = _f(b.open)
            high = _f(b.high)
            low = _f(b.low)
            amount = _f(b.amount)
            rows.append(
                {
                    "symbol": sym,
                    "date": day,
                    "open": close if open_ is None else open_,
                    "high": close if high is None else high,
                    "low": close if low is None else low,
                    "close": close,
                    # 指数: 上游是"手", 存量 tushare 是"股" => x100 对齐(8 只指数实测比值 1.000000)
                    "volume": volume * 100.0 if index_volume else volume,
                    "amount": volume * 100.0 * close if amount is None else amount,
                    # 日K收盘定版: 15:00 的行情时间戳让完整性判定直接认作"收盘后写入"。
                    "quote_ts": int(t.timestamp() * 1000),
                }
            )
    return rows, earliest


#: eltdx 市场前缀 -> 面板后缀(快照回包用 exchange + code 表达代码)。
_PREFIX_TO_SUFFIX: dict[str, str] = {"sh": "SH", "sz": "SZ", "bj": "BJ"}

#: 本地标的维表缓存: (mtime, {symbol: meta})。get_realtime() 是**无参**的全市场
#: 契约, 而 eltdx 快照必须显式传代码列表, 故从本地维表枚举(盘中每轮都重读 parquet 太浪费)。
_META_CACHE: tuple[float, dict[str, dict]] | None = None


def _local_market_meta() -> dict[str, dict]:
    """读本地维表, 返回 ``{symbol: {"name":..., "float_shares":...}}``。

    只保留沪深北(其余市场 eltdx 查不了)。快照回包**不含名称**, 换手率也需要
    流通股本, 两者都只能从维表补。
    """
    from app.config import settings

    root = Path(settings.data_dir)
    # (路径, 是否指数)。指数**要读名称**用于 get_index_realtime 回包, 但**不能**
    # 混进 get_realtime 的全市场枚举(指数由 quote_service 按码单单独补拉)。
    paths = [
        (root / "instruments" / "instruments.parquet", False),
        (root / "instruments_etf" / "instruments_etf.parquet", False),
        (root / "instruments_index" / "instruments_index.parquet", True),
    ]
    exists = [(p, is_idx) for p, is_idx in paths if p.exists()]
    if not exists:
        return {}
    global _META_CACHE
    stamp = max(p.stat().st_mtime for p, _ in exists)
    if _META_CACHE is not None and _META_CACHE[0] == stamp:
        return _META_CACHE[1]

    meta: dict[str, dict] = {}
    for path, is_index in exists:
        cols = ["symbol"] + [c for c in ("name", "float_shares") if c in pl.read_parquet_schema(path)]
        try:
            df = pl.read_parquet(path, columns=cols)
        except Exception as e:
            logger.warning("eltdx 实时: 读取标的维表失败 %s: %s", path.name, e)
            continue
        for row in df.to_dicts():
            sym = str(row.get("symbol") or "").strip()
            if not sym.endswith((".SH", ".SZ", ".BJ")):
                continue
            meta[sym] = {
                "name": row.get("name"),
                "float_shares": row.get("float_shares"),
                "is_index": is_index,
            }
    _META_CACHE = (stamp, meta)
    return meta


def _local_market_symbols() -> list[str]:
    """get_realtime() 要枚举的标的: 本地维表里的 A 股 + ETF(**不含指数**)。"""
    return sorted(s for s, m in _local_market_meta().items() if not m.get("is_index"))


def _snap_symbol(snap: Any) -> str | None:
    """快照对象 -> 面板格式代码(600519.SH)。"""
    suffix = _PREFIX_TO_SUFFIX.get(str(getattr(snap, "exchange", "") or "").lower())
    code = str(getattr(snap, "code", "") or "")
    return code + "." + suffix if suffix and code else None


def _snap_timestamp(time_raw: object) -> int | None:
    """``time_raw``(HHMMSScc, 如 15174239 = 15:17:42.39) -> 当日 epoch 毫秒。"""
    try:
        raw = int(time_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    hh, rem = divmod(raw, 1_000_000)
    mm, rem2 = divmod(rem, 10_000)
    ss, cc = divmod(rem2, 100)
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    day = cn_today()
    try:
        moment = datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=CN_TZ)
    except ValueError:
        return None
    return int(moment.timestamp() * 1000) + cc * 10


def _parse_snap(snap: Any, meta: Any = None) -> dict | None:
    """单条快照 -> realtime 行(量纲归一到内部口径)。

    已实测(2026-09-30, 覆盖 600/000/300/301/688/920/ETF):
    ``amount / (total_hand * 100) / last_price`` = 0.994 ~ 1.03 => **volume 就是手、
    amount 就是元, 且没有腾讯系那种"科创板 vol 单位是股"的板块差异**。
    ``change_pct`` 是**百分数**(1.86 表示 1.86%), 契约要小数 => /100。
    """
    symbol = _snap_symbol(snap)
    if not symbol:
        return None
    prev = _f(getattr(snap, "pre_close_price", None))
    last = _f(getattr(snap, "last_price", None))
    if last is None or last <= 0:
        last = prev  # 停牌/无成交: 上游给 0, 用前收补
    if last is None or last <= 0 or prev is None or prev <= 0:
        return None

    volume = float(getattr(snap, "total_hand", 0) or 0)
    amount = float(getattr(snap, "amount", 0.0) or 0.0)
    high = _f(getattr(snap, "high_price", None)) or last
    low = _f(getattr(snap, "low_price", None)) or last
    open_ = _f(getattr(snap, "open_price", None)) or last

    turnover = None
    shares = (meta or {}).get("float_shares") if isinstance(meta, dict) else None
    if shares and float(shares) > 0:
        turnover = volume * 100.0 / float(shares)

    return {
        "symbol": symbol,
        "name": (meta or {}).get("name") if isinstance(meta, dict) else None,
        "last_price": last,
        "prev_close": prev,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "amount": amount,
        "change_pct": (_f(getattr(snap, "change_pct", None)) or 0.0) / 100.0,
        "change_amount": _f(getattr(snap, "change", None)) or (last - prev),
        "amplitude": (high - low) / prev if prev else None,
        "turnover_rate": turnover,
        # time_raw 口径不稳定(同一批里既有 8 位也有 9 位, 部分解析出来是非法时间),
        # 解析不出就退回抓取时刻 —— quote_ts 的语义本就是"这条快照何时拿到的"。
        "timestamp": _snap_timestamp(getattr(snap, "time_raw", None))
        or int(datetime.now(CN_TZ).timestamp() * 1000),
    }


def _fetch_snap_batch(codes: list[str], depth: int = 0) -> list[Any]:
    """拉一批快照; **整批失败时二分降级**。

    实测某些代码(如 bj830799, 老三板)会让整批抛
    ``ProtocolError(snapshot record marker not found)``, 而同批的 bj920002 是好的
    —— 无法预先枚举黑名单, 只能二分把坏代码隔离掉。
    """
    try:
        return list(_client().quotes.get_snapshots(codes))
    except Exception as exc:
        if len(codes) == 1 or depth >= _SNAP_MAX_SPLIT:
            logger.debug("eltdx 快照跳过 %d 只: %s: %s", len(codes), type(exc).__name__, exc)
            return []
        mid = len(codes) // 2
        out = _fetch_snap_batch(codes[:mid], depth + 1)
        out.extend(_fetch_snap_batch(codes[mid:], depth + 1))
        return out


def _snap_fetch(symbols: list[str]) -> list[dict]:
    """按 80 只一批并发拉快照, 返回 realtime 行列表。"""
    meta = _local_market_meta()
    codes = [c for c in (app_to_eltdx(s) for s in symbols) if c]
    if not codes:
        return []
    batches = [codes[i : i + _SNAP_BATCH] for i in range(0, len(codes), _SNAP_BATCH)]

    snaps: list[Any] = []
    t0 = time.perf_counter()
    workers = min(_MAX_WORKERS, max(1, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(_fetch_snap_batch, batches):
            snaps.extend(part)

    rows: list[dict] = []
    for snap in snaps:
        symbol = _snap_symbol(snap)
        if not symbol:
            continue
        row = _parse_snap(snap, meta.get(symbol))
        if row:
            rows.append(row)
    logger.info(
        "eltdx 实时快照: %d 只请求 / %d 只返回, %d 批, %.2fs",
        len(codes), len(rows), len(batches), time.perf_counter() - t0,
    )
    return rows


@dataclass
class _EltdxConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "eltdx"
    display_name: str = "通达信行情(分钟K/实时快照)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class EltdxMinuteProvider:
    """通达信行情数据源: ``minute``(bars.get, 含真实成交额)。"""

    name = "eltdx"
    builtin = True

    def __init__(self) -> None:
        self.config = _EltdxConfig()
        self.display_name = self.config.display_name

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        return None

    # ---- realtime (quotes.get_snapshots) ----
    # 与腾讯 qt 一样, 快照不含指数(指数要按码单查), 故声明该能力让
    # quote_service 走 _fetch_plugin_index_quotes 补拉。
    supports_index_realtime = True

    def get_realtime(self) -> list[dict]:
        """全市场(A 股股票 + ETF)实时快照。

        返回行与 quote_service 的 realtime record 契约一致(见 _RT_COLUMNS);
        量纲已在 _parse_snap 归一。相比腾讯 qt 的优势: **没有"科创板 688/689
        vol 单位是股"的板块差异**(2026-09-30 分板块实测 r 均落在 0.99~1.03)。
        """
        symbols = _local_market_symbols()
        if not symbols:
            logger.warning("eltdx 实时: 本地标的维表为空, 无法枚举全市场(请先跑一次标的同步)")
            return []
        rows = _snap_fetch(symbols)
        if not rows:
            logger.warning("eltdx 实时: 全市场快照返回 0 行(可能未连通达信主站)")
        return rows

    def get_index_realtime(self, symbols: list[str]) -> list[dict]:
        """按码单拉指数实时快照(返回行与 get_realtime 同 schema)。"""
        syms = [s for s in (symbols or []) if s]
        return _snap_fetch(syms) if syms else []

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset == "daily":
            syms = symbols or ["600519.SH", "000001.SZ", "688981.SH", "920002.BJ"]
            df = self.get_daily(syms, None, None, "stock")
            return {
                "provider": self.name,
                "dataset": "daily",
                "rows": df.height,
                "columns": df.columns,
                "preview": df.head(5).to_dicts() if not df.is_empty() else [],
            }
        if dataset == "minute":
            df = self.get_minute(symbols or ["600519.SH"], None, None)
            return {
                "provider": self.name,
                "dataset": "minute",
                "rows": df.height,
                "columns": df.columns,
                "preview": df.head(5).to_dicts() if not df.is_empty() else [],
            }
        if dataset == "realtime":
            rows = _snap_fetch(["600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "000001.SH"])
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": _RT_COLUMNS,
                "preview": rows[:5],
            }
        raise ValueError(f"通达信行情不支持数据集: {dataset}")

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K, 按交易日 + 批次并发。

        与腾讯 provider 的关键差异: 这里**尊重** start_time/end_time, 用
        anchor_date 逐日精确取窗口, 因此历史分时也能拉到(源端约 94 个交易日)。
        """
        if not symbols:
            return pl.DataFrame()

        period = _FREQ_TO_PERIOD.get(str(freq or "").strip().lower())
        if period is None:
            # 90m/120m 等: eltdx 不支持, 返回空让调用方回落(不要猜着聚合)。
            logger.debug("eltdx 不支持周期 %s, 返回空由调用方回落", freq)
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        pairs = [(s, app_to_eltdx(s)) for s in symbols]
        pairs = [(s, c) for s, c in pairs if c]
        if not pairs:
            # 全部被过滤掉时仍回调一次, 否则前端进度条卡在 0。
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        days = _trading_days(start_time, end_time)
        if not days:
            days = [cn_today()]
        count = _BARS_PER_DAY.get(period, 240)

        batches = [pairs[i : i + _BAR_BATCH] for i in range(0, len(pairs), _BAR_BATCH)]
        tasks = [(d, b, period, count) for d in days for b in batches]
        total = len(tasks)

        logger.info(
            "eltdx 分钟K 拉取开始(%d symbols, %d 交易日, period=%s, %d 批)",
            len(pairs), len(days), period, total,
        )
        rows: list[dict] = []
        t0 = time.perf_counter()
        workers = min(_MAX_WORKERS, max(1, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, part in enumerate(pool.map(_fetch_one, tasks)):
                rows.extend(part)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
        logger.info(
            "eltdx 分钟K 拉取完成(%d 行, %.2fs)",
            len(rows), time.perf_counter() - t0,
        )

        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        keep = [c for c in _MINUTE_CANONICAL if c in df.columns]
        return df.select(keep).sort(["symbol", "datetime"])

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """日线(不复权) —— 分段回补 + 批量并发。

        与 tushare daily 的差异: 这里**一次请求就能拿 800 根**(约 3.3 年), 且
        100 只并发只要 0.65s, 适合做全市场日K的主源或灾备。

        ⚠️ 上游 count 上限 800, 超出必须分段(anchor 逐段前移), 否则直接
        ValueError。分段之间串行依赖, 段内批量并发。
        """
        if not symbols:
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        pairs = [(s, app_to_eltdx(s)) for s in symbols]
        pairs = [(s, c) for s, c in pairs if c]
        if not pairs:
            # 全部被过滤掉(港美股等)时仍回调一次, 否则前端进度条卡在 0。
            if on_chunk_done:
                on_chunk_done(1, 1)
            return pl.DataFrame()

        end = end_time.date() if isinstance(end_time, datetime) else (end_time or date.today())
        start = start_time.date() if isinstance(start_time, datetime) else start_time
        # 指数 volume 口径与股票不同(手 vs 股), 由 asset_type 与代码段双判定。
        index_volume = asset_type == "index"

        need = _estimate_day_bars(start, end)
        total_segments = max(1, -(-need // _DAY_PAGE_MAX))  # ceil
        batches = [pairs[i : i + _DAY_BATCH] for i in range(0, len(pairs), _DAY_BATCH)]

        rows: list[dict] = []
        anchor = end
        done = 0
        for seg in range(_DAY_MAX_SEGMENTS):
            remaining = need - seg * _DAY_PAGE_MAX
            if remaining <= 0 or seg >= total_segments:
                break
            count = int(min(_DAY_PAGE_MAX, remaining))
            tasks = [(b, anchor, count, index_volume) for b in batches]
            seg_rows: list[dict] = []
            earliest: date | None = None
            workers = min(_MAX_WORKERS, max(1, len(tasks)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for part, e in pool.map(_fetch_day_batch, tasks):
                    seg_rows.extend(part)
                    if e is not None and (earliest is None or e < earliest):
                        earliest = e
            rows.extend(seg_rows)
            done = seg + 1
            if on_chunk_done:
                on_chunk_done(done, total_segments)
            if earliest is None:
                break  # 本段空 => 更早也没有
            if start is not None and earliest <= start:
                break  # 已覆盖到窗口起点
            if earliest >= anchor:
                break  # 上游不再往前给(已到历史尽头)
            anchor = earliest - timedelta(days=1)

        # 提前结束时补一次进度, 否则前端进度条停在中间。
        if on_chunk_done and done < total_segments:
            on_chunk_done(total_segments, total_segments)

        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date, strict=False))
        if start is not None:
            df = df.filter(pl.col("date") >= start)
        df = df.filter(pl.col("date") <= end)
        # 段之间理论上不重叠, 但同一批可能被重复请求, 去重兜底。
        df = df.unique(subset=["symbol", "date"], keep="first").sort(["symbol", "date"])
        # 走统一 normalizer: 列名/类型归一 + 过滤停牌日(open=high=0)。
        return normalize_daily(df, source=self.name)


def availability() -> tuple[bool, str]:
    """探活: 装了包 + 能连通达信主站 + 真能取到 bar。不抛异常。

    在 loader 模块导入时就会被调用, 所以必须短平快: 只取 5 根 bar。
    """
    try:
        import eltdx  # noqa: F401
    except ImportError:
        return False, "未安装 eltdx(需 >=3.1.7), 请在设置页点击安装"
    try:
        res = _client().bars.get(  # type: ignore[attr-defined]
            "sh600519", period="1m", count=5,
        )
    except Exception as e:
        return False, f"eltdx 连接通达信主站失败: {e}"
    bars = getattr(res, "bars", None)
    if not bars:
        return False, "eltdx 已安装但主站返回 0 根 bar(网络不通或被风控)"
    latest = getattr(bars[-1], "time", None)
    stamp = latest.strftime("%Y-%m-%d %H:%M") if latest is not None else "?"
    return True, f"ok (通达信 7709, {len(bars)} bars, latest={stamp})"
