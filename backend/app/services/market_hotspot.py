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
            sub = enriched.filter(pl.col("symbol").str.to_uppercase().is_in(sorted(member_syms)))
            if not sub.is_empty():
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
                # 后排: 未涨停但今日涨幅居前的跟风/补涨(取前 6)
                follow = sub.filter(
                    (pl.col("consecutive_limit_ups") < 1)
                    & (pl.col("change_pct") > 0)
                ).sort("change_pct", descending=True).head(6)
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
        for s in (r.get("stockList") or []):
            prefix, _, code = s.partition(".")
            if prefix == "0" and code:
                stocks.append(f"{code}.SZ")
            elif prefix == "1" and code:
                stocks.append(f"{code}.SH")
        text = (r.get("title") or "").strip()
        items.append({
            "id": r.get("code") or "",
            "title": text,
            "summary": (r.get("summary") or "").strip(),
            "important": r.get("titleColor") or 0,
            "share": r.get("share") or 0,
            "stocks": stocks,
        })
    _FLASH_CACHE[key] = (time.time(), items)
    if len(_FLASH_CACHE) > _FLASH_CACHE_MAX:
        oldest = min(_FLASH_CACHE, key=lambda k: _FLASH_CACHE[k][0])
        _FLASH_CACHE.pop(oldest, None)
    return items


def build_flash_hotspot(repo, size: int = 60, top_n: int = 12) -> dict:
    """快报 → 今日热点: 把 7x24 快讯按题材聚类, 每个热点关联个股。

    归类逻辑(无 AI 依赖):
      1. 用标题关键词匹配概念名(概念列表来自本地概念映射, 长名优先)。
      2. 热点自带关联个股(东财 stockList), 不足时用该概念当日涨停/领涨股补强。
      3. 按 important + share + 关联个股数 排序, 输出「编号 + 事件 + (题材: 个股)」。

    返回:
      { "date": ..., "hotspots": [ {rank, title, topic, stocks:[{symbol,name}]} ] }
    """
    from app.services.rps_rotation import _load_concept_map_df

    items = _fetch_flash_items(size)
    if not items:
        return {"date": str(date.today()), "hotspots": []}

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
    return {"date": str(date.today()), "hotspots": hotspots[:top_n]}
