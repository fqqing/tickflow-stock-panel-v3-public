"""把 vendor 的 CChan 对象转成 :mod:`chanlab.schema` 定义的契约结构.

这一层是 v2 的边界: **前端只认这里的输出, 不认 chan.py 的任何类名**。
将来想换成自研引擎 / 换图表库 / 加渲染者, 都只改这一个文件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from chanlab.schema import (
    BuySellPoint,
    Center,
    ChanStructure,
    EngineInfo,
    LevelChan,
    Point,
    Segment,
    Stroke,
)

# KL_TYPE -> 前端可读的级别标识
_LEVEL_NAMES = {
    "K_DAY": "1d",
    "K_WEEK": "1w",
    "K_MON": "1M",
    "K_60M": "60m",
    "K_30M": "30m",
    "K_15M": "15m",
    "K_5M": "5m",
    "K_1M": "1m",
}


def epoch_ms(klu) -> int:
    """CKLine_Unit -> epoch 毫秒 (UTC).

    日线喂进去的时间是 CTime(y, m, d, 0, 0), 这里按 UTC 00:00 换算 --
    与 lightweight-charts 的日线口径一致。
    """
    t = klu.time
    # 正常情况下 time 是 CTime; 上游个别路径会把它退化成时间戳数值, 这里都兜住
    if not hasattr(t, "year"):
        return int(float(t) * 1000)
    hour = t.hour if hasattr(t, "hour") else 0
    minute = t.minute if hasattr(t, "minute") else 0
    return int(
        datetime(
            t.year,
            t.month,
            t.day,
            hour,
            minute,
            tzinfo=UTC,
        ).timestamp()
        * 1000
    )


def klu_point(klu, price: float) -> Point:
    return Point(i=int(klu.idx), t=epoch_ms(klu), p=float(price))


def bi_begin_point(bi) -> Point:
    """笔起点. 向上笔取低点, 向下笔取高点 -- 与上游 get_begin_val 同口径."""
    klu = bi.get_begin_klu()
    return klu_point(klu, klu.low if bi.is_up() else klu.high)


def bi_end_point(bi) -> Point:
    klu = bi.get_end_klu()
    return klu_point(klu, klu.high if bi.is_up() else klu.low)


def serialize_stroke(bi) -> Stroke:
    return Stroke(
        idx=int(bi.idx),
        dir="up" if bi.is_up() else "down",
        sure=bool(bi.is_sure),
        begin=bi_begin_point(bi),
        end=bi_end_point(bi),
        high=float(bi.end_klc.high if bi.is_up() else bi.begin_klc.high),
        low=float(bi.begin_klc.low if bi.is_up() else bi.end_klc.low),
        seg_idx=bi.seg_idx,
        klc_cnt=int(bi.end_klc.idx - bi.begin_klc.idx + 1),
        klu_cnt=int(bi.get_end_klu().idx - bi.get_begin_klu().idx + 1),
    )


def serialize_segment(seg) -> Segment:
    begin_bi, end_bi = seg.start_bi, seg.end_bi
    begin_val, end_val = seg.get_begin_val(), seg.get_end_val()
    return Segment(
        idx=int(seg.idx),
        dir="up" if seg.is_up() else "down",
        sure=bool(seg.is_sure),
        begin=bi_begin_point(begin_bi),
        end=bi_end_point(end_bi),
        high=float(max(begin_val, end_val)),
        low=float(min(begin_val, end_val)),
        stroke_begin=int(begin_bi.idx),
        stroke_end=int(end_bi.idx),
        stroke_count=len(seg.bi_list),
    )


def serialize_center(zs, idx: int) -> Center:
    # 中枢的 begin/end 只用来确定横向范围, 纵向由 zg/zd 决定,
    # 所以 p 取收盘价这种中立值, 不参与任何结构判定。
    return Center(
        idx=idx,
        zg=float(zs.high),
        zd=float(zs.low),
        peak_high=float(zs.peak_high),
        peak_low=float(zs.peak_low),
        begin=klu_point(zs.begin, zs.begin.close),
        end=klu_point(zs.end, zs.end.close),
        stroke_count=len(zs.bi_lst),
        sure=bool(zs.is_sure),
    )


def serialize_bsp(bsp, idx: int) -> BuySellPoint:
    return BuySellPoint(
        idx=idx,
        types=[t.value for t in bsp.type],
        is_buy=bool(bsp.is_buy),
        at=klu_point(bsp.klu, bsp.bi.get_end_val()),
        stroke_idx=int(bsp.bi.idx),
        features={k: float(v) for k, v in bsp.features.items() if v is not None},
    )


def build_parent_map(chan, lv_index: int) -> list[int] | None:
    """多级别时的父子 K 线映射: 本级别第 j 根 -> 父级别第 parent_map[j] 根.

    区间套渲染要靠它: 「日线定方向, 30 分钟找买点」的第一步就是知道某根 30 分钟
    K 线落在哪根日线里。单级别请求返回 None, 前端据此跳过这层。
    """
    if lv_index == 0 or len(chan.lv_list) <= 1:
        return None

    mapping: list[int] = []
    for klc in chan[lv_index].lst:
        for klu in klc.lst:
            sup = getattr(klu, "sup_kl", None)
            if sup is None:
                mapping.append(-1)
                continue
            # sup_kl 是父级别的某一根 K 线; 取它所属合并单元的最后一根原始 K 线,
            # 得到父级窗口内的 0-based 下标
            mapping.append(int(sup.lst[-1].idx if hasattr(sup, "lst") else sup.idx))
    return mapping


def serialize_level(chan, lv_index: int, elements: dict) -> LevelChan:
    kl_type = chan.lv_list[lv_index]
    return LevelChan(
        level=_LEVEL_NAMES.get(kl_type.name, kl_type.name),
        kl_type=kl_type.name,
        klu_count=sum(len(klc.lst) for klc in elements["merged"]),
        klc_count=len(elements["merged"]),
        strokes=[serialize_stroke(bi) for bi in elements["bi"]],
        segments=[serialize_segment(seg) for seg in elements["seg"]],
        centers=[serialize_center(zs, i) for i, zs in enumerate(elements["zs"])],
        buy_sell_points=[serialize_bsp(p, i) for i, p in enumerate(elements["bsp"])],
        parent_map=build_parent_map(chan, lv_index),
    )


def engine_info(chan, profile: str) -> EngineInfo:
    """把实际生效的关键配置项带出来, 供前端做缓存 key 与排障."""
    lv_config: dict[str, Any] = {}
    conf = getattr(chan, "conf", None)
    bi_conf = getattr(conf, "bi_conf", None)
    if bi_conf is not None:
        lv_config["bi_strict"] = bool(bi_conf.is_strict)
        lv_config["bi_fx_check"] = bi_conf.bi_fx_check.name.lower()
        lv_config["bi_end_is_peak"] = bool(bi_conf.bi_end_is_peak)
    seg_conf = getattr(conf, "seg_conf", None)
    if seg_conf is not None:
        lv_config["seg_algo"] = str(seg_conf.seg_algo)
    return EngineInfo(name="chan.py", profile=profile, lv_config=lv_config)


def serialize(chan, symbol: str, profile: str = "") -> ChanStructure:
    """CChan -> ChanStructure."""
    from chanlab.engine import level_elements

    levels = [
        serialize_level(chan, i, level_elements(chan, i))
        for i in range(len(chan.lv_list))
    ]

    times = [epoch_ms(klu) for klc in chan[0].lst for klu in klc.lst]
    return ChanStructure(
        symbol=symbol,
        window_begin=min(times) if times else None,
        window_end=max(times) if times else None,
        engine=engine_info(chan, profile),
        primary_level=levels[0].level,
        levels=levels,
    )
