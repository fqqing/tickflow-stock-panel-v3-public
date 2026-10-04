"""N9: 概念 / 行业板块 —— v2 自算涨跌 + 同花顺归属。

数据从哪来
==========
板块**归属**只有同花顺有, v2 不自己造: 走 v1 镜像 ``concept`` / ``industry``
(5579 / 5578 行)。但归属是低频数据 (季度级), 镜像冻不冻结无所谓 ——
真正每天变的是**板块涨跌幅**, 那部分 v2 自己算:

* 当日: :func:`chanlab.market.snapshot_rows` 的腾讯 qt 全市场快照
* 5 日 / 20 日: :func:`chanlab.market.recent_bars` 的前复权日线

这样板块页不依赖 v1 进程, 也不会出现「板块名是新的、涨幅是上周的」这种
半新半旧。

为什么要 5 日 / 20 日
====================
只有当日涨幅看不出**轮动**: 今天涨 3% 的板块可能是昨天跌 5% 反弹的。
当日 / 5 日 / 20 日三列一起看才知道资金是在搬家还是在接力。

口径
====
对外 ``pct`` / ``d5`` / ``d20`` 一律**百分数** (1.86 = +1.86%),
``amount`` 是**元**。``members`` 只统计「日线库里有、且快照里有」的票 ——
镜像里那些已退市/停牌的会被排除, 所以板块成分数与镜像总数可能不同,
返回里带 ``universe`` 说明。
"""

from __future__ import annotations

import time
from typing import Any

import polars as pl

from chanlab import market, v1mirror

#: 归属表不常变, 但每次请求都 explode 5579 行也没必要, 缓存 10 分钟。
_TAG_TTL = 600.0
_TAG_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}

KINDS = ("concept", "industry")

SORTS = ("d1", "d5", "d20", "amount", "limit_up", "members")


def _tag_map(kind: str) -> dict[str, list[str]]:
    """symbol -> 所属板块名列表。概念是多值 (分号分隔), 行业是单值。"""
    hit = _TAG_CACHE.get(kind)
    if hit and (time.perf_counter() - hit[0]) < _TAG_TTL:
        return hit[1]

    df, col = (
        (v1mirror.load("industry"), v1mirror.INDUSTRY_COL)
        if kind == "industry"
        else (v1mirror.load("concept"), v1mirror.CONCEPT_COL)
    )
    out: dict[str, list[str]] = {}
    if df.is_empty() or col not in df.columns:
        _TAG_CACHE[kind] = (time.perf_counter(), out)
        return out
    for symbol, raw in df.select(["symbol", col]).iter_rows():
        if not raw:
            continue
        sep = ";" if kind == "concept" else "-"
        tags = [s.strip() for s in str(raw).split(sep) if s.strip()]
        if tags:
            out[str(symbol).upper()] = tags
    _TAG_CACHE[kind] = (time.perf_counter(), out)
    return out


def _hist_returns(days: tuple[int, ...] = (5, 20)) -> dict[str, dict[int, float]]:
    """symbol -> {N: 近 N 个交易日涨幅(百分数)}。用前复权收盘算。

    不足 N 根的新股返回 None, 聚合时跳过 —— 把「上市 3 天涨 50%」算进
    板块 20 日涨幅会把整个板块拉飞。
    """
    bars = market.recent_bars()
    out: dict[str, dict[int, float]] = {}
    if bars.is_empty():
        return out
    maxd = max(days)
    grouped = (
        bars.sort(["symbol", "date"])
        .group_by("symbol")
        .agg(pl.col("close").tail(maxd + 1).alias("tail"))
    )
    for symbol, tail in grouped.iter_rows():
        if not tail:
            continue
        last = tail[-1]
        item: dict[int, float] = {}
        for d in days:
            if len(tail) > d and tail[-1 - d]:
                item[d] = (last / tail[-1 - d] - 1.0) * 100.0
        out[str(symbol)] = item
    return out


def rank(
    kind: str = "concept",
    *,
    sort: str = "d1",
    limit: int = 100,
    min_members: int = 3,
    keyword: str = "",
    desc: bool = True,
) -> dict[str, Any]:
    """板块排行: 当日 / 5 日 / 20 日涨幅 + 涨停家数 + 成交额 + 龙头股。

    ``desc=False`` 用来取**领跌**板块 —— 复盘页面要两边都看。
    """
    kind = kind if kind in KINDS else "concept"
    rows, meta = market.snapshot_rows()
    if not rows:
        return {
            "rows": [],
            "kind": kind,
            "as_of": None,
            "detail": meta.get("detail") or "实时快照不可用",
            "ok": False,
        }

    tags = _tag_map(kind)
    hist = _hist_returns()
    # name -> 聚合累加器
    acc: dict[str, dict[str, Any]] = {}
    for r in rows:
        sym = r["symbol"]
        for tag in tags.get(sym, ()):  # 一只票属于多个概念, 每个都要计一次
            a = acc.get(tag)
            if a is None:
                a = acc[tag] = {
                    "name": tag, "members": 0, "sum": 0.0, "pcts": [],
                    "up": 0, "down": 0, "limit_up": 0, "amount": 0.0,
                    "d5": [], "d20": [], "top": None, "top_pct": None,
                }
            pct = r.get("change_pct")
            pct = float(pct) if isinstance(pct, int | float) else 0.0
            a["members"] += 1
            a["sum"] += pct
            a["pcts"].append(pct)
            if pct > 0:
                a["up"] += 1
            elif pct < 0:
                a["down"] += 1
            price = r.get("price")
            lu = r.get("limit_up")
            if price and lu and float(price) >= float(lu) - 1e-6:
                a["limit_up"] += 1
            a["amount"] += float(r.get("amount") or 0.0)
            h = hist.get(sym) or {}
            if 5 in h:
                a["d5"].append(h[5])
            if 20 in h:
                a["d20"].append(h[20])
            if a["top_pct"] is None or pct > a["top_pct"]:
                a["top_pct"] = pct
                a["top"] = {"symbol": sym, "name": r.get("name") or "", "pct": pct}

    kw = keyword.strip()
    out: list[dict[str, Any]] = []
    for a in acc.values():
        if a["members"] < min_members:
            continue
        if kw and kw not in a["name"]:
            continue
        pcts = sorted(a["pcts"])
        mid = pcts[len(pcts) // 2] if pcts else 0.0
        out.append(
            {
                "name": a["name"],
                "members": a["members"],
                "d1": round(a["sum"] / a["members"], 2),
                "median": round(mid, 2),
                "d5": round(sum(a["d5"]) / len(a["d5"]), 2) if a["d5"] else None,
                "d20": round(sum(a["d20"]) / len(a["d20"]), 2) if a["d20"] else None,
                "up": a["up"],
                "down": a["down"],
                "limit_up": a["limit_up"],
                "amount": a["amount"],
                "top": a["top"],
            }
        )

    key = sort if sort in SORTS else "d1"
    # 涨幅类排序: None 沉底 (新股不足 N 根时); desc=False 时 None 也要沉底,
    # 所以统一按「升序取反」处理, 别直接 reverse —— 那样 None 会跑到榜首。
    if key == "members":
        out.sort(key=lambda x: x["members"], reverse=desc)
    elif key == "amount":
        out.sort(key=lambda x: (x["amount"] or 0), reverse=desc)
    elif key == "limit_up":
        out.sort(key=lambda x: (x["limit_up"], x["d1"]), reverse=desc)
    elif desc:
        out.sort(key=lambda x: -(x[key] if x[key] is not None else -1e9))
    else:
        out.sort(key=lambda x: (x[key] if x[key] is not None else 1e9))

    return {
        "rows": out[:limit],
        "kind": kind,
        "total": len(out),
        "ok": bool(meta.get("ok")),
        "detail": meta.get("detail") or "",
        "as_of": meta.get("updated_at"),
        # 5 日/20 日涨幅是从日线库算的, 与实时快照的 as_of 不是一回事, 分开给
        "data_date": _last_bar_date(),
        "universe": len(rows),
    }


def _last_bar_date() -> str | None:
    """日线库最后一根的日期 —— 5日/20日涨幅就截止到这里。"""
    try:
        value = market.recent_bars()["date"].max()
    except Exception:
        return None
    return str(value) if value else None


def board(kind: str, name: str, *, limit: int = 300) -> dict[str, Any]:
    """单个板块的成分股 + 实时行情。龙头股按当日涨幅降序。"""
    kind = kind if kind in KINDS else "concept"
    members = v1mirror.sector_members(kind, name, limit=limit)
    if not members:
        return {"rows": [], "name": name, "kind": kind, "stats": None}

    want = [str(m.get("symbol") or "").upper() for m in members]
    rows, meta = market.snapshot_rows(symbols=want)
    by_symbol = {r["symbol"]: r for r in rows}

    merged: list[dict[str, Any]] = []
    for m in members:
        sym = str(m.get("symbol") or "").upper()
        q = by_symbol.get(sym)
        if q is None:
            # 快照里没有 (停牌/退市/不在日线库): 也要列出来, 否则板块成分会
            # 莫名其妙少几只, 用户以为是 bug
            merged.append(
                {
                    "symbol": sym,
                    "name": m.get("股票简称") or "",
                    "price": None,
                    "change_pct": None,
                    "amount": None,
                    "turnover": None,
                    "quoted": False,
                }
            )
            continue
        merged.append(
            {
                **q,
                "name": q.get("name") or m.get("股票简称") or "",
                "quoted": True,
            }
        )

    quoted = [r for r in merged if r.get("quoted")]
    quoted.sort(key=lambda r: -(r.get("change_pct") or -1e9))
    pcts = [float(r["change_pct"]) for r in quoted if isinstance(r.get("change_pct"), int | float)]
    stats = {
        "members": len(members),
        "quoted": len(quoted),
        "d1": round(sum(pcts) / len(pcts), 2) if pcts else None,
        "up": sum(1 for p in pcts if p > 0),
        "down": sum(1 for p in pcts if p < 0),
        "limit_up": sum(
            1
            for r in quoted
            if r.get("price") and r.get("limit_up") and float(r["price"]) >= float(r["limit_up"]) - 1e-6
        ),
        "amount": sum(float(r.get("amount") or 0.0) for r in quoted),
    }
    return {
        "rows": quoted + [r for r in merged if not r.get("quoted")],
        "name": name,
        "kind": kind,
        "stats": stats,
        "ok": bool(meta.get("ok")),
        "detail": meta.get("detail") or "",
        "as_of": meta.get("updated_at"),
    }
