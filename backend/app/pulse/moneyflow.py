"""M1 资金流: 主力净额 / 超大·大·中·小单分档。

上游
====
``cli.money_flow.daily([codes])`` -> ``MoneyFlowBatch.blocks`` -> ``MoneyFlowBlock.records``
(``MoneyFlowDaily``)。records 按日期**倒序**(最新在前), 实测 4 只 0.07s。

口径(2026-09-30 实测 600519.SH 反推确认)
========================================
- ``main_net`` = ``main_super_large_net + main_large_net`` —— 通达信的"主力"= 超大单 + 大单。
  验证: 412275361 + 52385931 = 464661292 = main_net, 完全相等。
- ``main_ratio`` = ``main_net / total_amount`` 的**百分数**(464661292/4797246464 = 9.686%)。
  同理 ``main_buy_ratio`` = ``main_buy_net / total_amount``。**契约要小数 => /100**。
- ``main_buy_*`` 是买入侧四档之和(含中小单), 与"主力"不是同一个口径, 别混用。
- 金额单位**元**, 直接可用。
- ``buckets`` 是 16 个原始分档桶, **口径未完全确认**(既不像纯手数也不像纯笔数),
  故原样透出 ``buckets`` 供后续反推, 不参与任何计算。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.plugins.eltdx.provider import app_to_eltdx, eltdx_to_app, market_meta
from app.pulse.gateway import client

logger = logging.getLogger(__name__)

#: 输出列。比率一律小数, 金额一律元。
_MF_FIELDS = (
    "symbol",
    "date",
    "total_amount",
    "main_net",
    "main_ratio",
    "super_large_net",
    "large_net",
    "medium_net",
    "small_net",
    "buy_net",
    "buy_ratio",
    "buckets",
)

#: 单批代码数。实测 100 只仍只回 80(与快照同一硬上限), 故取 80。
_MF_BATCH = 80

_MAX_WORKERS = 8


def _f(raw: object) -> float | None:
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _parse_record(symbol: str, rec: Any) -> dict | None:
    """MoneyFlowDaily -> 一行。date 丢失时整行丢弃(无法定位到交易日)。"""
    day = getattr(rec, "date", None)
    if day is None:
        raw = getattr(rec, "date_raw", None)
        if raw is None:
            return None
        text = str(raw)
        if len(text) != 8:
            return None
        day = f"{text[:4]}-{text[4:6]}-{text[6:]}"

    def num(name: str) -> float | None:
        return _f(getattr(rec, name, None))

    return {
        "symbol": symbol,
        "date": str(day),
        "total_amount": num("total_amount"),
        "main_net": num("main_net"),
        # 上游是百分数(9.686), 契约要小数(0.09686)。
        "main_ratio": (num("main_ratio") or 0.0) / 100.0,
        "super_large_net": num("main_super_large_net"),
        "large_net": num("main_large_net"),
        "medium_net": num("main_medium_net"),
        "small_net": num("main_small_net"),
        "buy_net": num("main_buy_net"),
        "buy_ratio": (num("main_buy_ratio") or 0.0) / 100.0,
        "buckets": list(getattr(rec, "buckets", ()) or ()),
    }


def _fetch_batch(codes: list[str], days: int) -> list[dict]:
    """拉一批代码的资金流。整批失败时二分, 隔离坏代码(与快照同款套路)。"""
    try:
        batch = client().money_flow.daily(codes)
    except Exception as e:
        if len(codes) == 1:
            logger.debug("eltdx 资金流跳过 %s: %s: %s", codes[0], type(e).__name__, e)
            return []
        mid = len(codes) // 2
        out = _fetch_batch(codes[:mid], days)
        out.extend(_fetch_batch(codes[mid:], days))
        return out

    rows: list[dict] = []
    for blk in getattr(batch, "blocks", ()) or ():
        symbol = eltdx_to_app(getattr(blk, "exchange", ""), getattr(blk, "code", ""))
        if not symbol:
            continue
        for rec in (getattr(blk, "records", ()) or ())[: max(1, days)]:
            row = _parse_record(symbol, rec)
            if row:
                rows.append(row)
    return rows


def fetch_moneyflow(
    symbols: Sequence[str],
    days: int = 5,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """取一批标的最近 ``days`` 个交易日的资金流。

    ``days`` 是**每只保留的记录数**(上游倒序给), 不是日历天数。
    """
    pairs = [(s, app_to_eltdx(s)) for s in symbols or ()]
    pairs = [(s, c) for s, c in pairs if c]
    if not pairs:
        return []

    batches = [pairs[i : i + _MF_BATCH] for i in range(0, len(pairs), _MF_BATCH)]
    codes_of = {c: s for s, c in pairs}
    t0 = time.perf_counter()

    rows: list[dict] = []
    workers = min(_MAX_WORKERS, max(1, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = [[c for _, c in b] for b in batches]
        for i, part in enumerate(pool.map(lambda cs: _fetch_batch(cs, days), tasks)):
            rows.extend(part)
            if on_progress:
                on_progress(i + 1, len(tasks))

    # 回包代码可能带市场前缀不一致(极端情况下上游回的不是请求的代码), 统一过滤一次。
    valid = set(codes_of.values())
    rows = [r for r in rows if r["symbol"] in valid]
    logger.info("eltdx 资金流: %d 只请求 / %d 行, %d 批, %.2fs",
                len(pairs), len(rows), len(batches), time.perf_counter() - t0)
    return rows


def summarize(rows: list[dict]) -> dict:
    """把多日记录压成每只一行: 当日主力净额 + 区间累计 + 流入天数占比。"""
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    meta = market_meta()
    out: list[dict] = []
    for symbol, items in by_symbol.items():
        items.sort(key=lambda r: str(r["date"]), reverse=True)
        latest = items[0]
        nets = [r["main_net"] for r in items if r["main_net"] is not None]
        out.append({
            "symbol": symbol,
            "name": meta.get(symbol, {}).get("name"),
            "date": latest["date"],
            "main_net": latest["main_net"],
            "main_ratio": latest["main_ratio"],
            "total_amount": latest["total_amount"],
            "super_large_net": latest["super_large_net"],
            "large_net": latest["large_net"],
            "medium_net": latest["medium_net"],
            "small_net": latest["small_net"],
            "sum_main_net": sum(nets) if nets else None,
            "inflow_days": sum(1 for n in nets if n > 0),
            "days": len(nets),
        })
    out.sort(key=lambda r: (r["main_net"] or float("-inf")), reverse=True)
    return {"fields": list(_MF_FIELDS), "rows": out}
