"""全市场缠论扫描: 把一只票的完整结构压成一行可排序 / 可筛选的信号.

为什么单独一层
--------------
``/api/chan`` 返回的是**完整结构**(数千个端点), 选股页要的是**一行一票**的
可比较特征。两者用途不同:

- 结构用于渲染, 必须完整, 单票一次
- 扫描用于筛选, 必须可比较, 一次几千票

扫描器刻意复用 :mod:`chanlab.engine` 与 :mod:`chanlab.profiles`, 保证选股页列出
的票点进终端页是同一套笔 -- 选出来说有「三买」、点进去却看不到, 是最伤信任的
一类 bug, 根源就是两条链路各跑各的配置。

输出字段见 :data:`SCAN_COLUMNS`; 日期与价格一律取窗口最后一根 K 线。
"""

from __future__ import annotations

import math
from typing import Any

from chanlab.engine import build_chan
from chanlab.loader import MINUTE_FREQUENCIES, load_symbol_daily, load_symbol_minute

# 少于这么多根日线的标的不参与扫描: 笔都凑不齐, 出来的信号没有意义
MIN_BARS = 60
# 买点回溯窗口 (交易日): 只统计这么近的买卖点, 更早的已经不算「当前信号」
RECENT_WINDOW = 60
# 力度比阈值: 后一笔 MACD 面积 / 前一同向笔 < 该值 判为背驰
DIVERGE_RATIO = 0.8

SCAN_COLUMNS: tuple[str, ...] = (
    # --- 基础行情 ---
    "symbol",      # 代码
    "name",        # 名称, 无名称表时为空串
    "last_date",   # 最后一根 K 线时间: 日线 YYYY-MM-DD, 分钟 YYYY-MM-DD HH:MM
    "close",       # 最新收盘 (前复权)
    "chg_pct",     # 最后一根 K 线涨跌幅, 百分数
    "bars",        # 参与计算的日线根数
    # --- 大级别方向 ---
    "seg_dir",     # 最新线段方向 up / down
    "seg_sure",    # 线段是否确认
    # --- 最近一笔 ---
    "bi_dir",      # 最后一笔方向
    "bi_sure",     # 最后一笔是否确认 (False = 进行中的虚笔)
    "bi_pct",      # 最后一笔幅度, 百分数
    "bi_bars",     # 最后一笔跨的原始 K 线数
    # --- 最近信号 (买或卖里更晚的那个) ---
    "bsp_type",    # 类型, 如 1 / 1p / 2 / 2s / 3a / 3b
    "bsp_side",    # buy / sell
    "bsp_date",    # 信号所在 K 线日期
    "bsp_ago",     # 距最后一根 K 线多少个交易日
    "bsp_price",   # 信号价
    "gain_pct",    # 当前价相对信号价的涨跌, 百分数
    # --- 买 / 卖分开看 (最近卖点会把更早的买点盖住, 选股需要单独一列) ---
    "buy_type",    # 最近买点类型
    "buy_date",
    "buy_ago",     # 最近买点距今交易日; 没有买点时为 null
    "buy_gain",    # 当前价相对最近买点的涨幅, 百分数
    "sell_type",   # 最近卖点类型
    "sell_date",
    "sell_ago",
    "recent_buy",  # RECENT_WINDOW 内出现过的买点类型, 竖线分隔
    "recent_sell", # RECENT_WINDOW 内出现过的卖点类型, 竖线分隔
    # --- 中枢 ---
    "zs_zg",       # 最新中枢上沿
    "zs_zd",       # 最新中枢下沿
    "zs_pos",      # 收盘价相对中枢位置: above / inside / below
    "zs_dist",     # 到最近那条中枢边界的距离, 百分数; inside 时为 0
    "zs_ago",      # 中枢结束距今多少个交易日; 太大说明中枢已被远远甩开, 只剩名义意义
    # --- 力度 ---
    "diverge",     # 最后一笔相对前一同向笔: bottom(底背驰) / top(顶背驰) / 空串
    "div_ratio",   # 上面那个判据的原始比值 (后笔 MACD 面积 / 前一同向笔)
    "cost_ms",     # 单票耗时, 毫秒
)


def _pct(new: float, old: float) -> float:
    """涨跌幅百分数. 分母为 0 时返回 0 而不是 inf."""
    if old == 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def _klu_ts(klu) -> str | None:
    """CKLine_Unit -> ``YYYY-MM-DD`` 或 ``YYYY-MM-DD HH:MM``.

    ⚠️ 之所以返回**字符串**而不是 date/datetime: 日线与分钟两套扫描结果要能按
    symbol 直接 join 做共振筛选, 而 polars 的 date 与 datetime 是两种类型, 混在
    一起 join 会静默匹配不上。统一成字符串后字典序 == 时间序, 排序也不用特殊处理。
    """
    t = getattr(klu, "time", None)
    if t is None or not hasattr(t, "year"):
        return None
    try:
        stamp = f"{int(t.year):04d}-{int(t.month):02d}-{int(t.day):02d}"
    except (TypeError, ValueError):
        return None
    hour = int(getattr(t, "hour", 0) or 0)
    minute = int(getattr(t, "minute", 0) or 0)
    # 日线是 00:00, 不带时间; 分钟线才补上时分
    return f"{stamp} {hour:02d}:{minute:02d}" if (hour or minute) else stamp


def _is_minute(level: str) -> bool:
    return level.strip().lower() in MINUTE_FREQUENCIES


def load_frame(symbol: str, level: str, lookback: int):
    """按级别取 K 线: 日线走自有全历史库, 分钟线走 free-stockdb."""
    if _is_minute(level):
        return load_symbol_minute(symbol, freq=level.strip().lower(), lookback=lookback)
    return load_symbol_daily(symbol, lookback=lookback)


def _last_klu(chan):
    """窗口最后一根原始 K 线."""
    klc_lst = chan[0].lst
    if not klc_lst:
        return None
    return klc_lst[-1].lst[-1]


def _klu_total(chan) -> int:
    return sum(len(klc.lst) for klc in chan[0].lst)


def _macd_area(bi) -> float | None:
    """笔的 MACD 面积. 上游没开 MACD 或数据不足时返回 None.

    ``Cal_MACD_area`` 内部会遍历笔内每根 K 线的 ``macd`` 字段, 配置里关掉
    MACD 时该字段不存在, 会抛异常 -- 扫描是几千票的批量任务, 不能让它整轮失败,
    所以这里吞掉异常退化为「不算背驰」。
    """
    try:
        return float(bi.Cal_MACD_area())
    except Exception:
        return None


def _diverge(bi_list: list) -> tuple[str, float]:
    """最后一笔相对前一同向笔的力度比.

    同向笔在序列里隔 2 个位置 (up-down-up), 所以取 ``bi_list[-3]``。
    返回 (标记, 比值): 比值 < DIVERGE_RATIO 时按笔方向判为底背驰 / 顶背驰。
    """
    if len(bi_list) < 3:
        return "", math.nan
    last, prev = bi_list[-1], bi_list[-3]
    a_last, a_prev = _macd_area(last), _macd_area(prev)
    # 上游在没有同号 MACD 柱时返回 1e-7 哨兵值, 拿它做分母会得到几十万的比值。
    # 这类比值不是「力度放大」, 而是「分母根本不存在」, 必须判掉。
    if a_last is None or a_prev is None or a_prev <= 1e-4:
        return "", math.nan
    ratio = a_last / a_prev
    if ratio >= DIVERGE_RATIO:
        return "", ratio
    return ("bottom" if last.is_down() else "top"), ratio


def _bsp_types(bsp) -> str:
    """买卖点类型串. 同一点可能同时满足多个口径 (如 2 与 2s), 竖线分隔."""
    return "|".join(t.value if hasattr(t, "value") else str(t) for t in bsp.type)


def _ago(bsp, total: int) -> int:
    """买卖点距最后一根 K 线多少个交易日."""
    return total - 1 - int(getattr(bsp.klu, "idx", 0))


def _recent_types(bsp_list: list, total: int, is_buy: bool) -> str:
    """回溯窗口内出现过的买卖点类型, 按时间升序去重拼接."""
    seen: list[str] = []
    for p in bsp_list:
        if bool(p.is_buy) != is_buy:
            continue
        idx = int(getattr(p.klu, "idx", -1))
        if total - 1 - idx > RECENT_WINDOW:
            continue
        for t in p.type:
            name = t.value if hasattr(t, "value") else str(t)
            if name not in seen:
                seen.append(name)
    return "|".join(seen)


def scan_symbol(
    symbol: str,
    lookback: int = 500,
    profile: str = "v1",
    level: str = "1d",
    name: str = "",
) -> dict[str, Any] | None:
    """扫描单只标的, 返回 :data:`SCAN_COLUMNS` 的一行; 数据不足或失败返回 None.

    ``lookback`` 是参与计算的 K 线根数 (从最新往回数)。窗口太短会丢掉形成中枢所需
    的前置结构, 太长又会让老数据影响分型。日线默认 500 根约两年半; 分钟级别要自己
    给更大的值 (30m 一天 8 根, 1500 根才约七个半月)。
    """
    frame = load_frame(symbol, level, lookback)
    if frame.is_empty() or frame.height < MIN_BARS:
        return None

    time_col = "datetime" if _is_minute(level) else "date"
    start_day = frame[time_col].min()
    try:
        chan = build_chan(symbol, start=start_day, lv_list=[level], profile=profile)
    except Exception:
        # 单票失败不该拖垮整轮扫描: 批量任务里逐票 try 是必须的
        return None

    kl = chan[0]
    bi_list: list = kl.bi_list.bi_list
    seg_list: list = kl.seg_list.lst
    zs_list: list = kl.zs_list.zs_lst
    bsp_list: list = kl.bs_point_lst.getSortedBspList()

    total = _klu_total(chan)
    tail = _last_klu(chan)
    if tail is None:
        return None

    row: dict[str, Any] = {col: None for col in SCAN_COLUMNS}
    row["symbol"] = symbol
    row["name"] = name
    row["bars"] = total

    row["last_date"] = _klu_ts(tail)
    row["close"] = round(float(tail.close), 4)
    # 涨跌幅自己按前一根收盘算: CKLine_Unit 没有 pre_close 字段, 而 frame 是
    # 前复权口径, 与喂进引擎的数据同源, 用它算才不会和图表上的价格打架
    closes = frame["close"].to_list()
    row["chg_pct"] = round(_pct(float(closes[-1]), float(closes[-2])), 2) if len(closes) >= 2 else None

    # --- 线段: 大级别方向 ---
    if seg_list:
        seg = seg_list[-1]
        row["seg_dir"] = "up" if seg.is_up() else "down"
        row["seg_sure"] = bool(seg.is_sure)

    # --- 最后一笔 ---
    if bi_list:
        bi = bi_list[-1]
        begin_val, end_val = float(bi.get_begin_val()), float(bi.get_end_val())
        row["bi_dir"] = "up" if bi.is_up() else "down"
        row["bi_sure"] = bool(bi.is_sure)
        row["bi_pct"] = round(_pct(end_val, begin_val), 2)
        row["bi_bars"] = int(bi.get_klu_cnt())

    # --- 最近买卖点 ---
    if bsp_list:
        bsp = bsp_list[-1]
        row["bsp_type"] = _bsp_types(bsp)
        row["bsp_side"] = "buy" if bsp.is_buy else "sell"
        row["bsp_date"] = _klu_ts(bsp.klu)
        row["bsp_ago"] = _ago(bsp, total)
        price = float(bsp.bi.get_end_val())
        row["bsp_price"] = round(price, 4)
        row["gain_pct"] = round(_pct(float(tail.close), price), 2)
        row["recent_buy"] = _recent_types(bsp_list, total, is_buy=True)
        row["recent_sell"] = _recent_types(bsp_list, total, is_buy=False)

    for side, prefix in ((True, "buy"), (False, "sell")):
        hit = next((p for p in reversed(bsp_list) if bool(p.is_buy) == side), None)
        if hit is None:
            continue
        row[f"{prefix}_type"] = _bsp_types(hit)
        row[f"{prefix}_date"] = _klu_ts(hit.klu)
        row[f"{prefix}_ago"] = _ago(hit, total)
        if side:
            row["buy_gain"] = round(_pct(float(tail.close), float(hit.bi.get_end_val())), 2)

    # --- 中枢 ---
    if zs_list:
        zs = zs_list[-1]
        zg, zd = float(zs.high), float(zs.low)
        row["zs_zg"] = round(zg, 4)
        row["zs_zd"] = round(zd, 4)
        close = float(tail.close)
        if close > zg:
            row["zs_pos"] = "above"
            row["zs_dist"] = round(_pct(close, zg), 2)
        elif close < zd:
            row["zs_pos"] = "below"
            row["zs_dist"] = round(_pct(close, zd), 2)
        else:
            row["zs_pos"] = "inside"
            row["zs_dist"] = 0.0
        row["zs_ago"] = total - 1 - int(getattr(zs.end, "idx", 0))

    row["diverge"], ratio = _diverge(bi_list)
    row["div_ratio"] = None if ratio != ratio else round(ratio, 3)  # NaN 落库会污染列类型
    return row
