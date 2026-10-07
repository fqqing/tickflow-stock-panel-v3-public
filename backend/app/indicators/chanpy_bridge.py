"""chan.py 引擎 → v1 ChanAnalysis 的映射层（双引擎分工的「展示」半边）。

背景（双引擎分工，2026-10-04 定稿）:
- chan.py 引擎（Vespa314）单票 ~46ms（内存数据源），能产出**线段 + 多级别 +
  6 类买卖点**这些 v1 自研没有的能力，但纯 Python 对象图、不可向量化；
- v1 自研 ``analyze`` 纯 numpy，单票 ~2ms，快约 20 倍，是全市场扫描 / 回测
  唯一可行的内核。

所以分工是: **/analysis 单票展示走 chan.py 引擎**（换取线段等展示能力），
**/scan、/annotate、策略回测继续走 v1 自研 analyze**。本模块负责把 chan.py
引擎的输出**映射回 v1 的 ChanAnalysis dataclass**，使 ``api/chan.py`` 的
``_serialize`` 与前端零改动。

映射口径（关键，决定贴合度）:
- **笔端点索引用 last 口径**: v1 的 ``MergedBar.index`` 指向「合并单元覆盖的
  最后一根原始 K 线」，chan.py 的 ``get_begin_klu().idx`` 默认指向「极值所在
  那根」，两者系统性差 1~2 根（去包含后平均 1.34 根合成一根）。用
  ``bi.begin_klc.lst[-1].idx`` 才能对齐 v1 口径 —— 这是 v2 ``tune_bi.py``
  标定「贴合 97%」的关键，精确重合率 58% 也是因为这个约定差异而非算错。
- **笔端点价取分型极值**: ``get_begin_val()`` / ``get_end_val()`` 返回的正是
  顶分型 high / 底分型 low，与 v1 的 ``Stroke.start_price`` 同义。
- **买卖点 6 类折叠成 3 类**: chan.py 的 ``1/1p/2/2s/3a/3b`` 用 ``main_type()``
  折成 ``1/2/3``，再配 ``is_buy`` 得 ``1buy/2buy/3buy`` 等 6 种 v1 kind。
  同一 bsp 点可能同时命中多个 main_type（如 ``['2s','3b']``），按去重后的
  main_type 展开成多个 v1 signal，信息不丢。
"""

from __future__ import annotations

from typing import Any

from app.indicators.chan import (
    Center,
    ChanAnalysis,
    ChanSignal,
    MergedBar,
    Segment,
    Stroke,
    _build_snapshot,
    _classify_trend,
)


def _build_chan_memory(symbol: str, dates, high, low, close, *, strict: bool):
    """用内存数据源构造并算完的 CChan 实例（绕开 parquet 重扫）。

    所有 chan.py 的 import 都放在函数内 —— bootstrap() 要先改 sys.path 才能
    import 到 vendor 模块。``DataAPI.MemoryAPI`` 必须以这个名义加载（chan.py
    内部 ``import_module(f"DataAPI.MemoryAPI")`` 用的是同一份注册表）。
    """
    from chanlab.bootstrap import bootstrap
    from chanlab.profiles import resolve_profile

    bootstrap()

    import DataAPI.MemoryAPI as mem

    mem.set_frame(symbol, dates, list(high), list(low), list(close))
    try:
        from Chan import CChan
        from ChanConfig import CChanConfig
        from Common.CEnum import AUTYPE, KL_TYPE

        conf = resolve_profile("v1", {"bi_strict": bool(strict)})
        return CChan(
            code=symbol,
            data_src="custom:MemoryAPI.CMemory",
            lv_list=[KL_TYPE.K_DAY],
            config=CChanConfig(conf),
            autype=AUTYPE.QFQ,
        )
    finally:
        mem.clear_frame(symbol)


def _map_strokes(bi_list) -> tuple[Stroke, ...]:
    """chan.py 笔 → v1 Stroke（last 口径索引 + 分型极值价）。"""
    out: list[Stroke] = []
    for i, bi in enumerate(bi_list):
        # last 口径: 合并单元覆盖的最后一根原始 K 线，对齐 v1 MergedBar.index
        start_index = int(bi.begin_klc.lst[-1].idx)
        end_index = int(bi.end_klc.lst[-1].idx)
        start_price = float(bi.get_begin_val())
        end_price = float(bi.get_end_val())
        direction = 1 if bi.is_up() else -1
        out.append(
            Stroke(
                start_pos=i,
                end_pos=i,
                start_index=start_index,
                end_index=end_index,
                start_price=start_price,
                end_price=end_price,
                direction=direction,
                high=max(start_price, end_price),
                low=min(start_price, end_price),
            )
        )
    return tuple(out)


def _map_centers(zs_list) -> tuple[Center, ...]:
    """chan.py 中枢 → v1 Center。start/end 取首笔起点与末笔终点（last 口径）。"""
    out: list[Center] = []
    for i, zs in enumerate(zs_list):
        bi_lst = zs.bi_lst
        start_index = int(bi_lst[0].begin_klc.lst[-1].idx)
        end_index = int(bi_lst[-1].end_klc.lst[-1].idx)
        out.append(
            Center(
                start_stroke=int(bi_lst[0].idx),
                end_stroke=int(bi_lst[-1].idx),
                start_index=start_index,
                end_index=end_index,
                zg=float(zs.high),
                zd=float(zs.low),
                stroke_count=len(bi_lst),
            )
        )
    return tuple(out)


def _map_segments(seg_list) -> tuple[Segment, ...]:
    """chan.py 线段 → v1 Segment。端点索引取起笔起点 / 终笔终点（last 口径，对齐笔）。

    1~2 笔的过渡态线段仍保留（前端按 stroke_count 过滤渲染），信息不丢。
    """
    out: list[Segment] = []
    for seg in seg_list:
        begin_bi, end_bi = seg.start_bi, seg.end_bi
        out.append(
            Segment(
                start_stroke=int(begin_bi.idx),
                end_stroke=int(end_bi.idx),
                start_index=int(begin_bi.begin_klc.lst[-1].idx),
                end_index=int(end_bi.end_klc.lst[-1].idx),
                start_price=float(seg.get_begin_val()),
                end_price=float(seg.get_end_val()),
                direction=1 if seg.is_up() else -1,
                stroke_count=len(seg.bi_list),
            )
        )
    return tuple(out)


def _bsp_features(bsp) -> dict[str, float]:
    """把 chan.py 的 CFeatures 摊平成 dict，便于判断 divergence_rate 等标志。"""
    return dict(bsp.features.items())


def _map_signals(bsp_list) -> tuple[ChanSignal, ...]:
    """chan.py 买卖点 → v1 ChanSignal（6 类折叠成 3 类，按 main_type 展开）。"""
    out: list[ChanSignal] = []
    for p in bsp_list:
        features = _bsp_features(p)
        # 去重后的主类型（1/2/3），一个 bsp 点可能同时是多个
        main_types: list[str] = []
        for t in p.type:
            mt = str(t.main_type())
            if mt not in main_types:
                main_types.append(mt)
        divergence = "divergence_rate" in features
        for mt in main_types:
            kind = f"{mt}buy" if p.is_buy else f"{mt}sell"
            out.append(
                ChanSignal(
                    kind=kind,
                    index=int(p.klu.idx),
                    price=float(p.bi.get_end_val()),
                    stroke_pos=int(p.bi.idx),
                    center_pos=None,  # chan.py 未直接暴露中枢关联，快照里退回 last_center
                    divergence=divergence,
                )
            )
    out.sort(key=lambda s: (s.index, s.kind))
    return tuple(out)


def _map_merged(klc_list) -> tuple[MergedBar, ...]:
    """chan.py 合并 K 线 → v1 MergedBar（只用于 counts.merged 计数）。"""
    return tuple(
        MergedBar(index=int(c.lst[-1].idx), high=float(c.high), low=float(c.low))
        for c in klc_list
    )


def analyze_via_chanpy(
    dates,
    high,
    low,
    close,
    *,
    strict: bool = True,
    symbol: str = "_memory_",
) -> ChanAnalysis:
    """用 chan.py 引擎做单票缠论分析，返回 v1 兼容的 ChanAnalysis。

    输入与 v1 ``analyze`` 同源（一维数组，时间升序），但多了 ``dates``（供
    CTime 构造时间轴）与 ``symbol``（内存数据源的 code，天然隔离并发）。
    """
    chan = _build_chan_memory(symbol, dates, high, low, close, strict=strict)

    from chanlab.engine import level_elements

    elements = level_elements(chan, 0)
    strokes = _map_strokes(elements["bi"])
    centers = _map_centers(elements["zs"])
    segments = _map_segments(elements["seg"])
    signals = _map_signals(elements["bsp"])
    merged = _map_merged(elements["merged"])

    trend = _classify_trend(list(strokes), list(centers))
    n_bars = len(dates)
    snapshot = _build_snapshot(list(signals), list(centers), trend, n_bars, is_buy=True)
    sell_snapshot = _build_snapshot(list(signals), list(centers), trend, n_bars, is_buy=False)

    # fractals 未映射（chan.py 的 klc.fx 是 FX_TYPE 枚举，前端不消费该字段，
    # 只在 counts 里出现）；给空 tuple，counts.fractals 记 0。
    return ChanAnalysis(
        merged=merged,
        fractals=(),
        strokes=strokes,
        centers=centers,
        segments=segments,
        signals=signals,
        trend=trend,
        snapshot=snapshot,
        sell_snapshot=sell_snapshot,
    )
