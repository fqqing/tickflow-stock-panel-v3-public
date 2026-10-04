"""N18: 每日复盘 —— 把散在各页的事实收成一份「今天发生了什么」。

为什么单独一个模块
==================
复盘要的那些数**每一块都已经有页面了** (指数/情绪/涨停/板块/异动/策略池),
但它们散在 6 个接口里, 每天收盘要挨个点过去看。复盘页要做的不是重算,
而是**汇总 + 串成一句话**: 情绪几分、谁在涨、钱在哪、该看什么。

所以这里一行行情都不自己算, 全部调已有模块 —— 口径天然与那些页面一致,
不会出现「复盘页说涨停 57 家、涨停页说 55 家」。

两条硬约束
==========
1. **任何一块挂了都不能让整页白屏。** 复盘是收盘后第一眼要看的东西,
   指数源挂了就只缺指数那一段, 其余照出。每块单独 try/except。
2. **as_of 必须显眼。** 休市期间所有数据都停在最近一个交易日, 不写明的
   话用户会对着三天前的「今日复盘」做明天的决策。

可选的策略池段
==============
``pools=True`` 才跑 —— 每个池子跑一次 1~9 秒, 默认带上会让复盘页慢到
没法用。要的只是「今天我的策略选出几只」, 按需点。
"""

from __future__ import annotations

import logging
from typing import Any

from chanlab import index_source, market, regime, sectors

logger = logging.getLogger(__name__)

#: 复盘要盯的五个宽基。北证那支腾讯只给 1 根日线, 拿不到历史也不算失败。
HEADLINE = ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "899050.BJ")

SECTOR_TOP = 10
MOVER_TOP = 10
POOL_TOP = 5


def _safe(label: str, fn, default: Any = None) -> Any:
    """一块挂了只丢那一块。复盘页不能因为指数源超时就整页空白。"""
    try:
        return fn()
    except Exception:
        logger.warning("review: %s 段取数失败, 跳过", label, exc_info=True)
        return default


def _indexes() -> list[dict[str, Any]]:
    """五个宽基的涨跌幅。实时优先 (盘中), 回退日线最后两根 (收盘/休市)。"""
    quotes = _safe(
        "指数快照",
        lambda: index_source.quotes(list(HEADLINE)).get("quotes") or {},
        {},
    )
    out: list[dict[str, Any]] = []
    for sym in HEADLINE:
        q = quotes.get(sym) or {}
        name = q.get("name") or index_source.core_name(sym)
        pct = q.get("change_pct")
        price = q.get("last_price")
        if pct is None:
            # 实时拿不到 (休市/源挂了): 用日线最后两根算
            rows = _safe("指数日线", lambda s=sym: index_source.daily(s, 2).get("rows") or [], [])
            if len(rows) >= 2:
                prev = float(rows[-2]["close"])
                last = float(rows[-1]["close"])
                pct = (last / prev - 1.0) * 100.0 if prev else None
                price = last
        out.append(
            {
                "symbol": sym,
                "name": name,
                "price": price,
                "change_pct": None if pct is None else round(float(pct), 2),
            }
        )
    return out


def _pools(top: int = POOL_TOP) -> list[dict[str, Any]]:
    """逐个跑策略池, 只取命中数与前几行。"""
    from chanlab import pool as pool_mod

    items = _safe("策略池清单", lambda: pool_mod.list_items(), [])
    out: list[dict[str, Any]] = []
    for item in items:
        try:
            res = pool_mod.run(item, limit=top)
        except Exception:
            logger.warning("review: 策略 %s 跑失败, 跳过", item.get("name"), exc_info=True)
            continue
        out.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "mode": res.get("mode"),
                "total": res.get("total", 0),
                "signal_date": res.get("signal_date"),
                "cost_sec": res.get("cost_sec"),
                "rows": (res.get("rows") or [])[:top],
            }
        )
    return out


def build(*, pools: bool = False) -> dict[str, Any]:
    """攒一份复盘。返回各段 + 一段给人读的摘要。"""
    indexes = _safe("指数", _indexes, [])
    reg = _safe("情绪", regime.latest, {})
    ladder = _safe(
        "涨停梯队",
        lambda: market.limit_up_ladder(limit=20),
        {"rows": [], "stats": {}, "as_of": None},
    )
    gainers = _safe("涨幅榜", lambda: market.movers("gain", limit=MOVER_TOP).get("rows") or [], [])
    losers = _safe("跌幅榜", lambda: market.movers("loss", limit=MOVER_TOP).get("rows") or [], [])
    hot = _safe(
        "领涨板块",
        lambda: sectors.rank("concept", sort="d1", limit=SECTOR_TOP).get("rows") or [],
        [],
    )
    cold = _safe(
        "领跌板块",
        lambda: sectors.rank("concept", sort="d1", limit=SECTOR_TOP, desc=False).get("rows")
        or [],
        [],
    )
    pool_rows = _pools() if pools else []

    as_of = (
        reg.get("as_of")
        or (ladder.get("as_of") if isinstance(ladder, dict) else None)
        or ""
    )
    return {
        "as_of": as_of,
        "indexes": indexes,
        "regime": reg,
        "ladder": {
            "stats": (ladder.get("stats") if isinstance(ladder, dict) else {}) or {},
            "rows": (ladder.get("rows") if isinstance(ladder, dict) else []) or [],
        },
        "sectors": {"hot": hot, "cold": cold},
        "movers": {"gain": gainers, "loss": losers},
        "pools": pool_rows,
        "report": report(
            {
                "as_of": as_of,
                "indexes": indexes,
                "regime": reg,
                "ladder": {"stats": (ladder.get("stats") or {}) if isinstance(ladder, dict) else {}},
                "sectors": {"hot": hot, "cold": cold},
                "pools": pool_rows,
            }
        ),
    }


def report(data: dict[str, Any]) -> str:
    """把各段串成一段人能直接读的话 (飞书推送也用这段)。

    刻意写成**短句 + 数字**, 不写长段落: 复盘是扫一眼用的。
    """
    lines: list[str] = []
    as_of = data.get("as_of") or "-"
    lines.append(f"【{as_of} 收盘复盘】")

    idx = [i for i in (data.get("indexes") or []) if i.get("change_pct") is not None]
    if idx:
        lines.append(
            " · ".join(
                f"{i['name']} {i['change_pct']:+.2f}%"
                for i in idx
                if i.get("name") and i.get("change_pct") is not None
            )
        )

    reg = data.get("regime") or {}
    if reg:
        sub = (
            f"赚钱{reg.get('profit_score')}/投机{reg.get('speculation_score')}"
            f"/抗跌{reg.get('resilience_score')}/趋势{reg.get('trend_score')}"
        )
        lines.append(
            f"情绪 {reg.get('score')} 分 ({reg.get('state_label') or reg.get('state')}) · {sub}"
        )
        lines.append(
            f"涨停 {reg.get('limit_up')} · 跌停 {reg.get('limit_down')} · 炸板 {reg.get('broken_limit')}"
            f" · 最高 {reg.get('max_consecutive')} 板 · 晋级率 "
            + (
                f"{(reg.get('promo_rate') or 0) * 100:.0f}%"
                if reg.get("promo_rate") is not None
                else "-"
            )
        )

    stats = (data.get("ladder") or {}).get("stats") or {}
    # 只取梯队分布: 涨停/跌停/炸板上面已经用**盘后定稿**的 regime 数给了,
    # 这里再给一份实时快照的会让同一份复盘出现两个互相打架的涨停数。
    buckets = stats.get("buckets") or []
    if buckets:
        lines.append(
            "梯队 " + " / ".join(f"{b['boards']}板 {b['count']} 只" for b in buckets[:5])
        )

    hot = (data.get("sectors") or {}).get("hot") or []
    cold = (data.get("sectors") or {}).get("cold") or []
    if hot:
        lines.append("领涨 " + "、".join(f"{r['name']}({r['d1']:+.2f}%)" for r in hot[:5]))
    if cold:
        lines.append("领跌 " + "、".join(f"{r['name']}({r['d1']:+.2f}%)" for r in cold[:5]))

    for p in data.get("pools") or []:
        lines.append(f"策略「{p.get('name')}」命中 {p.get('total')} 只")

    return "\n".join(lines)


def review_record(data: dict[str, Any]) -> dict[str, Any]:
    """复盘 -> 飞书表的一行记录。

    去重键是 (代码, 信号日期), 所以这里把**日期**放在信号日期、代码写死
    ``MARKET`` —— 一天一条, 重跑不会灌重复。
    """
    reg = data.get("regime") or {}
    stats = (data.get("ladder") or {}).get("stats") or {}
    hot = (data.get("sectors") or {}).get("hot") or []
    cold = (data.get("sectors") or {}).get("cold") or []
    idx = {i.get("symbol"): i.get("change_pct") for i in (data.get("indexes") or [])}
    return {
        "代码": "MARKET",
        "名称": "大盘复盘",
        "市场": "A",
        "信号日期": data.get("as_of") or "",
        "收盘价": None,
        "涨跌幅%": idx.get("000001.SH"),
        "买点": "",
        "距今": 0,
        "中枢位置": "",
        "距中枢%": None,
        "背驰": "",
        "末笔%": None,
        "情绪分": reg.get("score"),
        "状态": reg.get("state_label") or reg.get("state") or "",
        "涨停": reg.get("limit_up"),
        "跌停": reg.get("limit_down"),
        "炸板": stats.get("broken", reg.get("broken_limit")),
        "最高板": reg.get("max_consecutive"),
        "领涨板块": "、".join(r["name"] for r in hot[:3]),
        "领跌板块": "、".join(r["name"] for r in cold[:3]),
        "复盘摘要": report(data),
    }
