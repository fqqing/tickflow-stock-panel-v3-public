"""市场主线全景 + 主线转换嗅探 + 快报热点聚合 — 缝合层。

这不是新数据源, 而是把项目里已沉淀的几块资产按「短线复盘」口径缝合成一个
结构化视图, 对应市场上成熟产品(开盘啦/同花顺)的三大能力:

  1. build_mainline_panorama —— 主线梯队全景(主线 → 龙头 → 前排 → 后排)
     主线 = market_mainline 的每日主线分排行(涨停集群加权);
     龙头 = 主线分里的 leader_symbol(最高连板);
     前排 = 主线内 2 板以上核心梯队, 中排 = 首板, 后排 = 未涨停但今日涨幅居前的跟风/补涨。
  2. sniff_rotation —— 主线转换嗅探(确定性规则版)
     复用 rps_rotation 的涨幅排名矩阵 + concept_rotation 的 rising/fading 信号,
     给出「可能转主线 / 可能退潮 / 连续霸榜主线」三类候选。
  3. build_flash_hotspot —— 快报 → 今日热点(东财 7x24 快讯按题材聚类 + 关联个股)
     复用 /api/news/flash 的东财电报流, 标题关键词匹配概念名归类,
     热点自带关联个股(东财 stockList) + 概念当日涨停/领涨股补强。

全部只读已落盘资产(enriched / mainline_history / ext_data 概念映射 / instruments),
不重算指标, 不依赖 AI(可选), 不依赖 tickflow 实时能力。
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, timedelta

import polars as pl

logger = logging.getLogger(__name__)

_NAME_MAP_CACHE: tuple[float, dict[str, str]] | None = None


def _load_name_map(data_dir) -> dict[str, str]:
    """instruments 维表 → {symbol: name}(大写 symbol)。600s 进程内缓存。"""
    global _NAME_MAP_CACHE
    now = time.time()
    if _NAME_MAP_CACHE is not None and now - _NAME_MAP_CACHE[0] < 600:
        return _NAME_MAP_CACHE[1]
    m: dict[str, str] = {}
    inst_dir = data_dir / "instruments"
    if inst_dir.exists():
        try:
            df = pl.read_parquet(inst_dir / "**" / "*.parquet")
            if "symbol" in df.columns and "name" in df.columns:
                for sym, nm in zip(
                    df["symbol"].cast(pl.Utf8).to_list(),
                    df["name"].cast(pl.Utf8).to_list(),
                ):
                    if sym and nm:
                        m[str(sym).strip().upper()] = str(nm).strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("load name map failed: %s", e)
    _NAME_MAP_CACHE = (now, m)
    return m


def _name_of(symbol: str, name_map: dict[str, str]) -> str:
    return name_map.get(str(symbol or "").strip().upper(), "")


# ────────────────────────────────────────────────────────────────
# 1. 主线梯队全景
# ────────────────────────────────────────────────────────────────


def build_mainline_panorama(repo, top_n: int = 8, kind: str = "concept") -> dict:
    """主线梯队全景: 主线题材 → 龙头 → 前排(连板) → 中排(首板) → 后排(跟风)。

    返回:
      {
        "date": "2026-10-26",
        "mainlines": [
          {
            "member": 题材名, "score": 主线分, "rank": 名次,
            "limit_up_count": 涨停家数, "max_boards": 最高板, "rungs_filled": 梯队档位,
            "leader": {"symbol","name","boards"},
            "front": [ {symbol,name,boards,amount} ...],   # 2板以上核心
            "mid":   [ {symbol,name,boards,amount} ...],   # 首板
            "rear":  [ {symbol,name,change_pct} ...],      # 未涨停但涨幅居前(跟风)
          }, ...
        ],
        "note": 概念成分为当前快照回看的历史口径说明
      }
    """
    from app.services.market_mainline import load_mainline_history, MEMBERSHIP_NOTE
    from app.services.rps_rotation import _load_concept_map_df

    data_dir = repo.store.data_dir
    hist = load_mainline_history(data_dir, kind)
    if hist.is_empty():
        return {"date": None, "mainlines": [], "note": MEMBERSHIP_NOTE}

    latest = hist["date"].max()
    top = hist.filter(pl.col("date") == latest).sort("rank").head(top_n)
    if top.is_empty():
        return {"date": str(latest), "mainlines": [], "note": MEMBERSHIP_NOTE}

    map_df, _ = _load_concept_map_df(repo, kind)
    if map_df.is_empty():
        return {"date": str(latest), "mainlines": [], "note": MEMBERSHIP_NOTE}

    name_map = _load_name_map(data_dir)
    enriched = repo.get_enriched_range(
        latest, latest,
        columns=["symbol", "date", "change_pct", "amount", "consecutive_limit_ups"],
    )
    has_enriched = enriched is not None and not enriched.is_empty()

    mainlines: list[dict] = []
    for row in top.to_dicts():
        member = str(row.get("member") or "")
        # 该题材的成分股(大写)
        member_syms = set(
            map_df.filter(pl.col(kind) == member)["_sym_up"].to_list()
        )
        leader_sym = str(row.get("leader_symbol") or "")
        leader_boards = int(row.get("max_boards") or 0)

        front, mid, rear = [], [], []
        if has_enriched:
            # 连板列由 enriched 历史缓存的 compute_limit_signals 提供; instruments
            # 缓存未就绪时会退化缺失该列, 此时只保留龙头/后排(涨幅), 梯队(前排/中排)留空。
            has_consec = "consecutive_limit_ups" in enriched.columns
            sub = enriched.filter(pl.col("symbol").str.to_uppercase().is_in(sorted(member_syms)))
            if not sub.is_empty():
                if has_consec:
                    # 前排: 2板以上核心梯队, 按连板高度/成交额降序
                    core = sub.filter(pl.col("consecutive_limit_ups") >= 2).sort(
                        ["consecutive_limit_ups", "amount"], descending=[True, True])
                    front = [
                        {"symbol": r["symbol"], "name": _name_of(r["symbol"], name_map),
                         "boards": int(r["consecutive_limit_ups"] or 0),
                         "amount": float(r["amount"] or 0.0) / 1e8}
                        for r in core.to_dicts()
                    ]
                    # 中排: 首板
                    first = sub.filter(pl.col("consecutive_limit_ups") == 1).sort(
                        "amount", descending=True)
                    mid = [
                        {"symbol": r["symbol"], "name": _name_of(r["symbol"], name_map),
                         "boards": 1, "amount": float(r["amount"] or 0.0) / 1e8}
                        for r in first.to_dicts()
                    ]
                # 后排: 未涨停但今日涨幅居前的跟风/补涨(取前 6)。有连板列时用 <1 过滤,
                # 否则仅按涨幅>0 兜底。
                follow = sub.filter(
                    (pl.col("consecutive_limit_ups") < 1)
                    & (pl.col("change_pct") > 0)
                ) if has_consec else sub.filter(pl.col("change_pct") > 0)
                follow = follow.sort("change_pct", descending=True).head(6)
                rear = [
                    {"symbol": r["symbol"], "name": _name_of(r["symbol"], name_map),
                     "change_pct": float(r["change_pct"] or 0.0) * 100}
                    for r in follow.to_dicts()
                ]

        mainlines.append({
            "member": member,
            "score": round(float(row.get("score") or 0.0), 1),
            "rank": int(row.get("rank") or 0),
            "limit_up_count": int(row.get("limit_up_count") or 0),
            "max_boards": leader_boards,
            "rungs_filled": int(row.get("rungs_filled") or 0),
            "ge2_count": int(row.get("ge2_count") or 0),
            "leader": {
                "symbol": leader_sym,
                "name": _name_of(leader_sym, name_map),
                "boards": leader_boards,
            },
            "front": front,
            "mid": mid,
            "rear": rear,
        })
    return {"date": str(latest), "mainlines": mainlines, "note": MEMBERSHIP_NOTE}


def build_mainline_calendar(repo, days: int = 30, top_n: int = 8,
                            kind: str = "concept") -> dict:
    """板块周期表(开盘啦「板块周期表」同款): 往日每日主线排行 → 日期 × 名次矩阵。

    数据源 = mainline_history(按日持久化的主线排行, enriched 全历史回算, 本地
    自 2000 年全覆盖), 无需增量等待。另计算每个题材「截至今日连续上榜天数」
    (streak)与「新晋」标记(前一日未上榜), 供前端做持续性高亮。

    返回:
      {
        "kind", "top_n",
        "dates": ["2026-09-15", ..., "2026-10-26"],   # 升序, 长度 ≤ days
        "rows": [                                       # 每个名次一行
          {"rank": 1, "cells": [
              {"member": "MLOps概念", "score": 89.0, "limit_up_count": 5,
               "max_boards": 4, "leader_symbol": "301049", "leader_name": "飞天经纬",
               "streak": 7, "is_new": false} | null,   # 该日该名次缺位时为 null
          ]}, ...
        ],
        "member_summary": [                              # 按近 30 日上榜总次数排序
          {"member": "...", "days_on": 18, "best_rank": 1, "streak": 7,
           "latest_score": 89.0}, ...
        ],
        "note": MEMBERSHIP_NOTE,
      }
    """
    from app.services.market_mainline import load_mainline_history, MEMBERSHIP_NOTE

    days = max(5, min(90, days))
    top_n = max(3, min(15, top_n))
    data_dir = repo.store.data_dir
    hist = load_mainline_history(data_dir, kind)
    if hist.is_empty():
        return {"kind": kind, "top_n": top_n, "dates": [], "rows": [],
                "member_summary": [], "note": MEMBERSHIP_NOTE}

    all_dates = sorted(hist["date"].unique().to_list())
    dates = all_dates[-days:]
    date_set = set(dates)
    sub = hist.filter(pl.col("date").is_in(sorted(date_set)) & (pl.col("rank") <= top_n))
    if sub.is_empty():
        return {"kind": kind, "top_n": top_n, "dates": [str(d) for d in dates],
                "rows": [], "member_summary": [], "note": MEMBERSHIP_NOTE}

    name_map = _load_name_map(data_dir)

    # 连续上榜 streak: 以最后一日为锚, 向前数该题材每天都在 top_n 的天数。
    # (date, member) 对一次建 set, 避免逐日 filter。
    anchor = dates[-1]
    all_top = hist.filter(pl.col("rank") <= top_n).select(["date", "member"])
    top_pairs = set(zip(
        all_top["date"].cast(pl.Utf8).to_list(),
        all_top["member"].cast(pl.Utf8).to_list(),
    ))
    streak_map: dict[str, int] = {}
    past_dates = [d for d in all_dates if d <= anchor]
    for m in {mm for (dd, mm) in top_pairs if dd == str(anchor)}:
        streak = 0
        for d in reversed(past_dates):
            if (str(d), m) in top_pairs:
                streak += 1
            else:
                break
        streak_map[m] = streak

    # 每日 top_n 的 (date, rank) → cell
    cell_by_key: dict[tuple, dict] = {}
    prev_date = dates[-2] if len(dates) >= 2 else None
    prev_members = set()
    if prev_date:
        prev_members = set(
            all_top.filter(pl.col("date") == prev_date)["member"].cast(pl.Utf8).to_list()
        )
    for r in sub.to_dicts():
        member = str(r.get("member") or "")
        leader_sym = str(r.get("leader_symbol") or "")
        cell_by_key[(str(r["date"]), int(r["rank"]))] = {
            "member": member,
            "score": round(float(r.get("score") or 0.0), 1),
            "limit_up_count": int(r.get("limit_up_count") or 0),
            "max_boards": int(r.get("max_boards") or 0),
            "leader_symbol": leader_sym,
            "leader_name": _name_of(leader_sym, name_map),
            "streak": streak_map.get(member, 1),
            "is_new": bool(prev_date and member not in prev_members),
        }

    rows = []
    for rank in range(1, top_n + 1):
        rows.append({
            "rank": rank,
            "cells": [cell_by_key.get((str(d), rank)) for d in dates],
        })

    # 题材汇总: 近 days 日上榜次数 / 最好名次 / 当前 streak / 最新分
    agg = (sub.group_by("member")
           .agg(pl.len().alias("days_on"), pl.col("rank").min().alias("best_rank"))
           .sort(["days_on", "best_rank"], descending=[True, False]))
    latest_cells = {c["member"]: c for c in
                    (cell_by_key.get((str(anchor), rk)) for rk in range(1, top_n + 1))
                    if c}
    member_summary = []
    for r in agg.to_dicts():
        m = str(r["member"])
        cur = latest_cells.get(m)
        member_summary.append({
            "member": m,
            "days_on": int(r["days_on"]),
            "best_rank": int(r["best_rank"]),
            "streak": streak_map.get(m, 0),
            "latest_score": cur["score"] if cur else None,
        })

    return {
        "kind": kind,
        "top_n": top_n,
        "dates": [str(d) for d in dates],
        "rows": rows,
        "member_summary": member_summary,
        "note": MEMBERSHIP_NOTE,
    }


# ────────────────────────────────────────────────────────────────
# 2. 主线转换嗅探(确定性规则版)
# ────────────────────────────────────────────────────────────────


def sniff_rotation(repo, days: int = 12, kind: str = "concept") -> dict:
    """主线转换嗅探: 从涨幅排名矩阵提炼「可能转主线 / 可能退潮 / 连续霸榜」。

    直接复用 concept_rotation_analyzer 的确定性信号计算(无 AI 依赖),
    外加 market_mainline 主线分的近期轨迹做交叉印证。
    """
    from app.services.rps_rotation import build_rps_rotation
    from app.services.concept_rotation_analyzer import _compute_rotation_signals

    rotation = build_rps_rotation(repo, days, kind)
    dates = rotation.get("dates") or []
    columns = rotation.get("columns") or {}
    if not dates or not columns:
        return {"dates": [], "ascending": [], "fading": [], "persistent": []}

    signals = _compute_rotation_signals(dates, columns)

    # 交叉印证: 主线分近期轨迹(近 3 日 score 排名变化)
    from app.services.market_mainline import load_mainline_history
    hist = load_mainline_history(repo.store.data_dir, kind)
    cross = {}
    if not hist.is_empty():
        recent = hist.sort("date").tail(3 * 40)  # 留足前 3 日各 30 条
        for m in recent["member"].unique().to_list():
            sub = recent.filter(pl.col("member") == m).sort("date")
            if sub.height >= 2:
                r0 = int(sub["rank"][-2] or 999)
                r1 = int(sub["rank"][-1] or 999)
                cross[m] = {"rank_prev": r0, "rank_now": r1, "delta": r0 - r1}

    def _decorate(items: list[dict]) -> list[dict]:
        out = []
        for it in items:
            c = cross.get(it.get("concept") or "")
            out.append({
                "member": it.get("concept"),
                "ranks": it.get("ranks"),
                "avg_rank": it.get("avg_rank"),
                "rank_std": it.get("rank_std"),
                "mainline_delta": c.get("delta") if c else None,
            })
        return out

    return {
        "dates": dates,
        "ascending": _decorate(signals.get("rising", [])),
        "fading": _decorate(signals.get("fading", [])),
        "persistent": _decorate(signals.get("persistent_leaders", [])),
    }


# ────────────────────────────────────────────────────────────────
# 3. 快报 → 今日热点聚合
# ────────────────────────────────────────────────────────────────

_FLASH_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
_FLASH_CACHE: dict[str, tuple[float, dict]] = {}
_FLASH_TTL = 60.0
_FLASH_CACHE_MAX = 200


def _fetch_flash_items(size: int = 60, column: int = 102) -> list[dict]:
    """拉东财 7x24 快讯(与 api/news.py 同源)。30s 短缓存。"""
    import httpx

    key = f"{column}:{size}"
    hit = _FLASH_CACHE.get(key)
    if hit and time.time() - hit[0] < _FLASH_TTL:
        return hit[1]

    params = {
        "client": "web", "biz": "web_724", "fastColumn": str(column),
        "sortEnd": "", "pageSize": str(size),
        "req_trace": str(int(time.time() * 1000)),
    }
    try:
        data = httpx.get(_FLASH_URL, params=params, timeout=12.0,
                         headers={"User-Agent": "Mozilla/5.0"}).json()
    except Exception as e:  # noqa: BLE001
        logger.warning("flash fetch failed: %s", e)
        return []

    raw = ((data.get("data") or {}).get("fastNewsList")) or []
    items = []
    for r in raw:
        stocks = []
        boards = []
        for s in (r.get("stockList") or []):
            prefix, _, code = s.partition(".")
            if prefix == "0" and code:
                stocks.append(f"{code}.SZ")
            elif prefix == "1" and code:
                stocks.append(f"{code}.SH")
            elif prefix in ("90", "1007") and code:
                # 板块 secid(90.BKxxxx / 1007.BKxxxx) -> 板块代码 BKxxxx
                boards.append(code)
        text = (r.get("title") or "").strip()
        items.append({
            "id": r.get("code") or "",
            "title": text,
            "summary": (r.get("summary") or "").strip(),
            "important": r.get("titleColor") or 0,
            "share": r.get("share") or 0,
            "stocks": stocks,
            "boards": boards,
        })
    _FLASH_CACHE[key] = (time.time(), items)
    if len(_FLASH_CACHE) > _FLASH_CACHE_MAX:
        oldest = min(_FLASH_CACHE, key=lambda k: _FLASH_CACHE[k][0])
        _FLASH_CACHE.pop(oldest, None)
    return items


_SUMMARY_CACHE: dict[str, tuple[float, str]] = {}
_SUMMARY_TTL = 300.0
_SUMMARY_CACHE_MAX = 50


def _hotspot_fingerprint(date_str: str, hotspots: list[dict]) -> str:
    """用日期 + 标题集合生成摘要缓存指纹。"""
    titles = "|".join(h.get("title", "") for h in hotspots[:20])
    return hashlib.md5(f"{date_str}:{titles}".encode("utf-8")).hexdigest()


def _rule_summarize_hotspots(hotspots: list[dict]) -> str:
    """规则版摘要: 直接按聚类结果拼「今日热点」编号列表(用户样例格式)。"""
    lines = ["今日热点："]
    for i, h in enumerate(hotspots, 1):
        topic = (h.get("topic") or "要闻").strip()
        names = "、".join(s.get("name") for s in h.get("stocks", []) if s.get("name"))
        suffix = f"（{topic}：{names}）" if names else f"（{topic}）"
        lines.append(f"{i}. {h.get('title', '')}{suffix}")
    return "\n".join(lines)


async def _llm_summarize_hotspots(hotspots: list[dict]) -> str | None:
    """LLM 版摘要: 把规则聚类结果归纳成自然语言「今日热点」列表。

    未配置 AI 或调用失败时返回 None, 调用方降级规则版。
    """
    from app.services.ai_provider import ai_configured, generate_ai_text

    if not hotspots or not ai_configured():
        return None

    feed = []
    for h in hotspots:
        topic = (h.get("topic") or "要闻").strip()
        names = "、".join(s.get("name") for s in h.get("stocks", []) if s.get("name"))
        feed.append(f"- {h.get('title', '')}（题材：{topic}；关联：{names or '无'}）")
    material = "\n".join(feed)

    system = (
        "你是 A 股盘后复盘助手。请把下面这些 7x24 快讯热点条目，归纳成简洁的「今日热点」列表。\n"
        "要求：\n"
        "1. 同一主题的多条合并为一条，去重去噪，最多 12 条。\n"
        "2. 每条格式：序号. 事件一句话简述（题材名：关联个股名，用顿号分隔）。\n"
        "3. 提炼核心事实，不要照抄原快讯标题；没有个股的不要硬凑个股。\n"
        "4. 只输出列表本身，第一行固定为「今日热点：」，不要任何前言、标题、解释或额外符号。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": material},
    ]
    try:
        text = await generate_ai_text(messages, temperature=0.3, max_tokens=1200, timeout=60.0)
        text = (text or "").strip()
        return text or None
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 归纳热点失败, 降级规则版: %s", e)
        return None


async def build_flash_hotspot(repo, size: int = 60, top_n: int = 12,
                              use_ai: bool = True) -> dict:
    """快报 → 今日热点: 把 7x24 快讯按题材聚类, 每个热点关联个股。

    归类逻辑(无 AI 依赖):
      1. 用标题关键词匹配概念名(概念列表来自本地概念映射, 长名优先)。
      2. 热点自带关联个股(东财 stockList), 不足时用该概念当日涨停/领涨股补强。
      3. 按 important + share + 关联个股数 排序, 输出「编号 + 事件 + (题材: 个股)」。

    归纳(可选 AI): use_ai=True 且 AI 已配置时, 用 LLM 把聚类结果归纳成自然语言
    「今日热点」列表(带 5 分钟缓存); AI 未配置或失败时降级为规则拼接。

    返回:
      { "date": ..., "hotspots": [ {title, topic, stocks:[{symbol,name}]} ],
        "summary": "今日热点：...", "summary_source": "ai"|"rule" }
    """
    from app.services.rps_rotation import _load_concept_map_df

    items = _fetch_flash_items(size)
    if not items:
        return {"date": str(date.today()), "hotspots": [], "summary": "", "summary_source": "rule"}

    data_dir = repo.store.data_dir
    name_map = _load_name_map(data_dir)
    map_df, _ = _load_concept_map_df(repo, "concept")
    concept_names: list[str] = []
    if not map_df.is_empty():
        concept_names = sorted(map_df["concept"].unique().to_list(), key=len, reverse=True)

    # 题材当日涨停/领涨股(用于补强关联)
    limit_by_concept: dict[str, list[str]] = {}
    try:
        from app.services.market_mainline import load_mainline_history
        hist = load_mainline_history(data_dir, "concept")
        if not hist.is_empty():
            latest = hist["date"].max()
            for r in hist.filter(pl.col("date") == latest).to_dicts():
                limit_by_concept.setdefault(str(r["member"]), []).append(str(r["leader_symbol"]))
    except Exception as e:  # noqa: BLE001
        logger.warning("load mainline leader for flash failed: %s", e)

    hotspots = []
    for it in items:
        title = it["title"]
        # 长名优先匹配标题里的概念
        topic = "要闻"
        for cname in concept_names:
            if cname and len(cname) >= 2 and cname in title:
                topic = cname
                break
        syms = it["stocks"][:]
        # 补强: 匹配到题材但东财没给个股时, 用该题材龙头/涨停股
        if not syms and topic in limit_by_concept:
            syms = limit_by_concept[topic][:4]
        stocks = [{"symbol": s, "name": _name_of(s, name_map)} for s in syms]
        hotspots.append({
            "title": title,
            "topic": topic,
            "important": bool(it["important"]),
            "share": int(it["share"] or 0),
            "stocks": stocks,
        })

    # 排序: important 优先 → share → 关联个股数
    hotspots.sort(key=lambda h: (h["important"], h["share"], len(h["stocks"])), reverse=True)
    result = hotspots[:top_n]
    date_str = str(date.today())

    summary: str
    source: str
    if use_ai:
        fp = _hotspot_fingerprint(date_str, result)
        hit = _SUMMARY_CACHE.get(fp)
        if hit and time.time() - hit[0] < _SUMMARY_TTL:
            summary, source = hit[1], "ai"
        else:
            summary = await _llm_summarize_hotspots(result)
            if summary:
                source = "ai"
                _SUMMARY_CACHE[fp] = (time.time(), summary)
                if len(_SUMMARY_CACHE) > _SUMMARY_CACHE_MAX:
                    oldest = min(_SUMMARY_CACHE, key=lambda k: _SUMMARY_CACHE[k][0])
                    _SUMMARY_CACHE.pop(oldest, None)
            else:
                summary, source = _rule_summarize_hotspots(result), "rule"
    else:
        summary, source = _rule_summarize_hotspots(result), "rule"

    return {
        "date": date_str,
        "hotspots": result,
        "summary": summary,
        "summary_source": source,
    }


# ────────────────────────────────────────────────────────────────
# 4. 东财板块榜(对比源) + 主线原因
# ────────────────────────────────────────────────────────────────

_EM_PLATE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EM_PLATE_CACHE: dict[str, tuple[float, dict]] = {}
_EM_PLATE_TTL = 120.0


def _fetch_em_boards(kind: str = "concept") -> dict[str, dict]:
    """拉东财板块榜(概念/行业)。返回 {BK代码: {name, change_pct, leader_name, up, down}}。

    fs: m:90+t:3 = 概念板块, m:90+t:2 = 行业板块; fid=f3 按涨跌幅降序。
    单次 pz=800 拉全量(概念约 500+ / 行业约 90), 云端直连免 key。
    """
    import httpx

    key = kind
    hit = _EM_PLATE_CACHE.get(key)
    if hit and time.time() - hit[0] < _EM_PLATE_TTL:
        return hit[1]

    fs = "m:90+t:3" if kind == "concept" else "m:90+t:2"
    url = (_EM_PLATE_URL + f"?pn=1&pz=800&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}"
           "&fields=f2,f3,f8,f12,f14,f104,f105,f128,f136")
    try:
        data = httpx.get(url, timeout=12.0,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "https://quote.eastmoney.com/"}).json()
    except Exception as e:  # noqa: BLE001
        logger.warning("em board fetch failed: %s", e)
        return {}

    rows = ((data.get("data") or {}).get("diff")) or []
    out: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("f12") or "").strip()
        name = str(r.get("f14") or "").strip()
        if not code:
            continue
        out[code] = {
            "name": name,
            "change_pct": float(r.get("f3") or 0.0),
            "leader_name": str(r.get("f128") or "").strip(),
            "up": int(r.get("f104") or 0),
            "down": int(r.get("f105") or 0),
        }
    _EM_PLATE_CACHE[key] = (time.time(), out)
    return out


def _normalize_plate_name(name: str) -> str:
    """板块名归一化: 去空格 + 去「概念/板块/指数」后缀, 用于跨源名称对齐。"""
    n = (name or "").strip().replace(" ", "")
    for suf in ("概念", "板块", "指数"):
        if n.endswith(suf) and len(n) > len(suf) + 1:
            n = n[: -len(suf)]
    return n


def _build_em_boards_mapping(em_boards: dict[str, dict],
                             concept_names: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """东财板块 ↔ 本地概念名 双向映射(归一化子串匹配, 长名优先)。

    返回 (em_to_ours: {BK代码: 概念名}, ours_to_em: {概念名: BK代码})。
    """
    em_to_ours: dict[str, str] = {}
    ours_to_em: dict[str, str] = {}
    ours_sorted = sorted(set(concept_names), key=len, reverse=True)
    norm_ours = {_normalize_plate_name(c): c for c in ours_sorted}
    for bk, info in em_boards.items():
        norm_em = _normalize_plate_name(info.get("name") or "")
        if not norm_em:
            continue
        target = norm_ours.get(norm_em)
        if target is None:
            for c in ours_sorted:
                nc = _normalize_plate_name(c)
                if nc and len(nc) >= 2 and (nc in norm_em or norm_em in nc):
                    target = c
                    break
        if target:
            em_to_ours[bk] = target
            ours_to_em.setdefault(target, bk)
    return em_to_ours, ours_to_em


def _match_name_to_ours(src_names: list[str],
                        concept_names: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """源板块名 ↔ 本地概念名 双向映射(归一化子串匹配, 长名优先)。

    返回 (src_to_ours: {源板块名: 概念名}, ours_to_src: {概念名: 源板块名})。
    """
    src_to_ours: dict[str, str] = {}
    ours_to_src: dict[str, str] = {}
    ours_sorted = sorted(set(concept_names), key=len, reverse=True)
    norm_ours = {_normalize_plate_name(c): c for c in ours_sorted}
    for sname in src_names:
        norm_s = _normalize_plate_name(sname)
        if not norm_s:
            continue
        target = norm_ours.get(norm_s)
        if target is None:
            for c in ours_sorted:
                nc = _normalize_plate_name(c)
                if nc and len(nc) >= 2 and (nc in norm_s or norm_s in nc):
                    target = c
                    break
        if target:
            src_to_ours[sname] = target
            ours_to_src.setdefault(target, sname)
    return src_to_ours, ours_to_src


_TX_PLATE_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
_TX_PLATE_CACHE: dict[str, tuple[float, dict]] = {}
_TX_PLATE_TTL = 120.0


def _fetch_tx_boards(kind: str = "concept") -> dict[str, dict]:
    """拉腾讯板块涨跌幅榜(概念/行业)。返回 {板块名: {change_pct, leader_name, code}}。

    board_type: gn=概念(全量约 800), hy2=行业(约 124); count 上限 200, offset 翻页。
    过滤 stock_type 含 FG 的风格类(昨日连板/昨日涨停等伪题材)。
    腾讯是项目主源, 云端直连稳定可用 —— 作为「对比其他数据源」的板块榜。
    """
    import httpx

    key = kind
    hit = _TX_PLATE_CACHE.get(key)
    if hit and time.time() - hit[0] < _TX_PLATE_TTL:
        return hit[1]

    bt = "gn" if kind == "concept" else "hy2"
    out: dict[str, dict] = {}
    offset = 0
    while True:
        url = f"{_TX_PLATE_URL}?board_type={bt}&sort_type=Price&direct=down&offset={offset}&count=200"
        try:
            data = httpx.get(url, timeout=12.0, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}).json()
        except Exception as e:  # noqa: BLE001
            logger.warning("tx board fetch failed: %s", e)
            break
        rows = ((data.get("data") or {}).get("rank_list")) or []
        if not rows:
            break
        for r in rows:
            if "FG" in (r.get("stock_type") or ""):  # 风格类伪题材
                continue
            name = str(r.get("name") or "").strip()
            if not name:
                continue
            lz = r.get("lzg") or {}
            out[name] = {
                "change_pct": float(r.get("zdf") or 0.0),
                "leader_name": str(lz.get("name") or "").strip(),
                "code": str(r.get("code") or "").strip(),
            }
        offset += len(rows)
        if len(rows) < 200:
            break
    _TX_PLATE_CACHE[key] = (time.time(), out)
    return out


# ────────────────────────────────────────────────────────────────
# 同花顺「涨停原因」源(zx.10jqka.com.cn/event/api/getharden)
# ────────────────────────────────────────────────────────────────
# 这是同花顺口径的题材热榜原子数据: 当日全部涨停股, 每只带 reason 题材标签
# (如「人形机器人+智能包装+半年报增长」)。用它给主线题材做「涨停集群 + 涨停原因」
# 级证据, 命中率远高于东财 7x24 快讯(不按题材打标)。

_THS_HARDEN_URL = "https://zx.10jqka.com.cn/event/api/getharden"
_THS_HARDEN_CACHE: dict[str, tuple[float, list[dict]]] = {}
_THS_HARDEN_TTL = 120.0
_THS_SEPS = ("+", "，", "、", ",", "；", ";", " ")


def _parse_ths_symbol(code: str) -> str:
    """6 位股票代码 → 带交易所后缀的大写 symbol(如 300949 → 300949.SZ)。"""
    c = (code or "").strip()[-6:]
    if c.startswith("6"):
        return f"{c}.SH"
    if c.startswith(("4", "8")) or c.startswith("92"):
        return f"{c}.BJ"
    return f"{c}.SZ"


def _split_reason_tags(reason: str) -> list[str]:
    """把涨停原因拆成题材标签列表(去引号/空白/空项)。"""
    text = (reason or "").replace("“", "").replace("”", "").replace("\"", "")
    parts = [text]
    for sep in _THS_SEPS:
        parts = [p for chunk in parts for p in chunk.split(sep)]
    return [p.strip() for p in parts if p.strip()]


def _fetch_ths_harden() -> list[dict]:
    """拉同花顺涨停原因池。返回 [{symbol,name,reason,date}]。120s 缓存。

    云端/本地直连免 key(与腾讯/东财快讯同网段)。失败返回空列表, 不抛。
    """
    import httpx

    hit = _THS_HARDEN_CACHE.get("harden")
    if hit and time.time() - hit[0] < _THS_HARDEN_TTL:
        return hit[1]

    try:
        data = httpx.get(_THS_HARDEN_URL, timeout=12.0, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.10jqka.com.cn/",
        }).json()
    except Exception as e:  # noqa: BLE001
        logger.warning("ths harden fetch failed: %s", e)
        return []

    out: list[dict] = []
    for r in (data.get("data") or []):
        code = str(r.get("code") or "").strip()
        if not code:
            continue
        out.append({
            "symbol": _parse_ths_symbol(code).upper(),
            "name": str(r.get("name") or "").strip(),
            "reason": str(r.get("reason") or "").strip(),
            "date": str(r.get("date") or "").strip(),
        })
    _THS_HARDEN_CACHE["harden"] = (time.time(), out)
    return out


def _ths_hits_for_member(member: str, harden: list[dict],
                         member_syms: set[str]) -> list[dict]:
    """找该主线题材在同花顺涨停原因池里命中的涨停股。

    路 A(主): 涨停股 symbol 在该主线成分股里(主线 = 涨停集群, 成分股里的涨停股
    必被同花顺收录) → 最准。
    路 B(辅): 涨停 reason 的题材标签归一化后命中 member 名(跨口径兜底)。
    """
    hits: dict[str, dict] = {}
    norm_member = _normalize_plate_name(member)
    for h in harden:
        sym = h["symbol"]
        if sym in member_syms:
            hit = True
        else:
            hit = False
            for tag in _split_reason_tags(h["reason"]):
                if norm_member and len(norm_member) >= 2 and (
                        tag == norm_member or norm_member in tag or tag in norm_member):
                    hit = True
                    break
        if hit:
            hits[sym] = {"symbol": sym, "name": h["name"], "reason": h["reason"]}
    return list(hits.values())


def _rule_reason_for_mainline(member: str, leader_sym: str, name_map: dict,
                              catalysts: list[dict],
                              ths_hits: list[dict] | None = None) -> str:
    leader_name = _name_of(leader_sym, name_map)
    head = member
    if leader_name:
        head += f"（龙头 {leader_name}）"
    if ths_hits:
        segs = [f"{h['name'] or h['symbol']}（{h['reason']}）" for h in ths_hits[:5]]
        return f"{head}：同花顺涨停原因命中 {len(ths_hits)} 家 —— {'；'.join(segs)}"
    tops = [c["title"] for c in catalysts[:3]]
    if tops:
        return f"{head}：驱动消息「{'；'.join(tops)}」"
    return f"{head}：今日快讯暂无直接驱动消息。"


async def _llm_reason_for_mainline(member: str, leader_sym: str, name_map: dict,
                                   catalysts: list[dict],
                                   ths_hits: list[dict] | None = None) -> str | None:
    """LLM 版主线原因: 用同花顺涨停原因(一等) + 东财快讯(二等)归纳成一句话。"""
    from app.services.ai_provider import ai_configured, generate_ai_text

    if not ai_configured():
        return None
    leader_name = _name_of(leader_sym, name_map)
    feed_lines = []
    for h in (ths_hits or []):
        feed_lines.append(f"- [涨停] {h['name']}：涨停原因「{h['reason']}」")
    for c in catalysts:
        feed_lines.append(f"- [快讯] {c['title']}")
    feed = "\n".join(feed_lines) or "（无）"
    system = (
        "你是 A 股盘后复盘助手。给定一个当日主线题材、龙头股、该题材今日涨停股"
        "（含同花顺涨停原因标签）与若干条相关快讯，用一句话说明「这个题材今日为什么走强/领涨」。\n"
        "要求：优先依据涨停股的涨停原因标签归纳核心驱动（题材/事件），再补充快讯要点；"
        "只输出这一句话，格式：题材名：因 <核心驱动> 刺激，龙头 <股名> 领涨（涨停 N 家）。"
        "若证据不足，客观写「暂无明显消息面驱动，属资金/情绪推动」，不要编造事件。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"主线题材：{member}\n龙头：{leader_name or '未知'}\n证据：\n{feed}"},
    ]
    try:
        text = await generate_ai_text(messages, temperature=0.3, max_tokens=300, timeout=45.0)
        return (text or "").strip() or None
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 主线原因失败: %s", e)
        return None


async def build_mainline_reason(repo, top_n: int = 8, kind: str = "concept",
                                use_ai: bool = True) -> dict:
    """主线原因: 同花顺涨停原因(一等) + 东财快讯(二等) 反查每条主线的驱动。

    一等证据 = 同花顺涨停原因池(getharden: 当日涨停股 + reason 题材标签)。主线成分股里
    今日涨停的个股被同花顺收录, 直接给出「涨停 N 家 + 每只涨停原因」, 命中率贴近主线
    定义本身(主线 = 涨停集群)。
    二等证据 = 东财 7x24 快讯(标题关键词 + boards 板块字段)。
    叠 LLM 归纳成一句「因 XX 刺激, 龙头 XX 领涨(涨停 N 家)」; AI 未配置/失败降级规则拼接。

    返回:
      { "date", "reasons": [ {member, score, rank, limit_up_count, max_boards,
          leader:{symbol,name,boards}, ths_count, ths_hits:[{symbol,name,reason}],
          catalysts:[{title,important,share}], reason, reason_source} ], "note" }
    """
    from app.services.market_mainline import load_mainline_history, MEMBERSHIP_NOTE
    from app.services.rps_rotation import _load_concept_map_df

    data_dir = repo.store.data_dir
    hist = load_mainline_history(data_dir, kind)
    if hist.is_empty():
        return {"date": None, "reasons": [], "note": MEMBERSHIP_NOTE}
    latest = hist["date"].max()
    top = hist.filter(pl.col("date") == latest).sort("rank").head(top_n)
    if top.is_empty():
        return {"date": str(latest), "reasons": [], "note": MEMBERSHIP_NOTE}

    name_map = _load_name_map(data_dir)
    map_df, _ = _load_concept_map_df(repo, kind)
    concept_names: list[str] = (sorted(map_df[kind].unique().to_list(), key=len, reverse=True)
                                if not map_df.is_empty() else [])

    items = _fetch_flash_items(size=200)
    harden = _fetch_ths_harden()

    reasons: list[dict] = []
    for row in top.to_dicts():
        member = str(row.get("member") or "")
        leader_sym = str(row.get("leader_symbol") or "")

        # 一等证据: 同花顺涨停原因(主线成分股 ∩ 涨停池, 辅以 reason 标签兜底)
        member_syms: set[str] = set()
        if not map_df.is_empty() and member:
            member_syms = {
                str(s).strip().upper()
                for s in map_df.filter(pl.col(kind) == member)["_sym_up"].to_list()
            }
        ths_hits = _ths_hits_for_member(member, harden, member_syms)

        # 二等证据: 东财快讯(仅标题关键词匹配; boards 板块反查依赖东财 push2,
        # 云端永久限流已弃用)
        catalysts: list[dict] = []
        seen_ids: set[str] = set()
        for it in items:
            if it["id"] in seen_ids:
                continue
            title = it["title"]
            if member and len(member) >= 2 and member in title:
                seen_ids.add(it["id"])
                catalysts.append({
                    "title": title,
                    "important": bool(it["important"]),
                    "share": int(it["share"] or 0),
                })
        catalysts.sort(key=lambda x: (x["important"], x["share"]), reverse=True)
        catalysts = catalysts[:6]

        reason, source = "", "rule"
        if use_ai and (ths_hits or catalysts):
            reason = await _llm_reason_for_mainline(member, leader_sym, name_map,
                                                    catalysts, ths_hits)
            if reason:
                source = "ai"
        if not reason:
            reason = _rule_reason_for_mainline(member, leader_sym, name_map,
                                               catalysts, ths_hits)

        reasons.append({
            "member": member,
            "score": round(float(row.get("score") or 0.0), 1),
            "rank": int(row.get("rank") or 0),
            "limit_up_count": int(row.get("limit_up_count") or 0),
            "max_boards": int(row.get("max_boards") or 0),
            "leader": {"symbol": leader_sym, "name": _name_of(leader_sym, name_map),
                       "boards": int(row.get("max_boards") or 0)},
            "ths_count": len(ths_hits),
            "ths_hits": ths_hits,
            "catalysts": catalysts,
            "reason": reason,
            "reason_source": source,
        })

    return {"date": str(latest), "reasons": reasons, "note": MEMBERSHIP_NOTE}


def build_mainline_compare(repo, top_n: int = 8, kind: str = "concept",
                           plate_scope: int = 30) -> dict:
    """自算主线 vs 腾讯板块涨跌幅榜 交叉对照。

    - 自算主线(mainline_history 最新 top_n) → 在腾讯板块榜里找对应板块的涨跌幅排名。
    - consensus: both=两边都强(真主线) / ours_only=自算强但腾讯涨幅榜不认(警惕) /
      no_match=腾讯榜里找不到对应板块(概念口径差异)。
    - plate_top: 腾讯涨幅榜前 plate_scope, 标注是否已在我们主线里(未覆盖 = 潜在漏掉的新主线)。

    返回:
      { "date", "kind", "source": "tencent", "plate_total",
        "mainlines": [ {member,score,rank,plate_name,plate_change_pct,plate_rank,
           plate_leader,consensus} ],
        "plate_top": [ {name,change_pct,leader,in_ours,ours_member} ], "note" }
    """
    from app.services.market_mainline import load_mainline_history, MEMBERSHIP_NOTE
    from app.services.rps_rotation import _load_concept_map_df

    data_dir = repo.store.data_dir
    hist = load_mainline_history(data_dir, kind)
    if hist.is_empty():
        return {"date": None, "kind": kind, "source": "tencent", "plate_total": 0,
                "mainlines": [], "plate_top": [], "note": MEMBERSHIP_NOTE}
    latest = hist["date"].max()
    top = hist.filter(pl.col("date") == latest).sort("rank").head(top_n)

    map_df, _ = _load_concept_map_df(repo, kind)
    concept_names = (sorted(map_df[kind].unique().to_list(), key=len, reverse=True)
                     if not map_df.is_empty() else [])

    tx_boards = _fetch_tx_boards(kind)
    tx_to_ours, ours_to_tx = _match_name_to_ours(list(tx_boards.keys()), concept_names)

    tx_sorted = sorted(tx_boards.items(), key=lambda kv: kv[1].get("change_pct", 0.0),
                       reverse=True)
    tx_rank_of_name = {name: i + 1 for i, (name, _) in enumerate(tx_sorted)}
    plate_total = len(tx_sorted) or 1
    top_pct_cut = max(10, int(plate_total * 0.2))

    our_members = {str(r["member"]) for r in top.to_dicts()}

    mainlines: list[dict] = []
    for row in top.to_dicts():
        member = str(row.get("member") or "")
        tx_name = ours_to_tx.get(member)
        info = tx_boards.get(tx_name) if tx_name else None
        if info is None:
            mainlines.append({
                "member": member,
                "score": round(float(row.get("score") or 0.0), 1),
                "rank": int(row.get("rank") or 0),
                "plate_name": None, "plate_change_pct": None, "plate_rank": None,
                "plate_leader": None, "consensus": "no_match",
            })
            continue
        plate_rank = tx_rank_of_name.get(tx_name)
        mainlines.append({
            "member": member,
            "score": round(float(row.get("score") or 0.0), 1),
            "rank": int(row.get("rank") or 0),
            "plate_name": tx_name,
            "plate_change_pct": round(float(info.get("change_pct") or 0.0), 2),
            "plate_rank": plate_rank,
            "plate_leader": info.get("leader_name"),
            "consensus": "both" if (plate_rank is not None and plate_rank <= top_pct_cut) else "ours_only",
        })

    plate_top: list[dict] = []
    for name, info in tx_sorted[:plate_scope]:
        ours = tx_to_ours.get(name)
        plate_top.append({
            "name": name,
            "change_pct": round(float(info.get("change_pct") or 0.0), 2),
            "leader": info.get("leader_name"),
            "in_ours": ours in our_members,
            "ours_member": ours,
        })

    return {
        "date": str(latest),
        "kind": kind,
        "source": "tencent",
        "plate_total": plate_total,
        "mainlines": mainlines,
        "plate_top": plate_top,
        "note": MEMBERSHIP_NOTE,
    }
