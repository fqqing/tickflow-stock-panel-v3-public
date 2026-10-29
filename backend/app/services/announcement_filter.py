"""巨潮资讯(cninfo)公告标题抓取 + 利空关键词过滤。

轻量利空规避: 盘后实时选股时, 对共振候选抓取近 N 天公告标题,
命中利空关键词(减持/违规/立案/预亏/风险提示/解禁/质押...)即剔除。

数据源: 巨潮资讯 hisAnnouncement/query (证监会指定披露平台, 免费公开)。
只抓标题做关键词命中, 不解析公告原文 —— 够用于「避开明显雷」, 不引重依赖。

设计约定:
- 逐只抓取(候选池通常 <= 30 只), 失败降级为「不剔除」(放行), 不阻断选股主流程。
- 抓取范围截止到 as_of(选股日), 不取未来公告, 避免历史回放时的 look-ahead bias。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_CNINFO_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"

#: 利空关键词(命中标题即视为利空, 剔除)。偏「明确利空」, 减少中性公告误杀。
_BEARISH_KEYWORDS = (
    "减持", "违规", "立案", "处罚", "预亏", "退市风险", "风险提示",
    "问询", "关注函", "监管函", "诉讼", "冻结", "减值", "解禁", "质押",
)

#: 利好/中性排除词: 标题命中这些词时「不算利空」(优先于利空词判定)。
#: 关键: 「减持」「质押」「冻结」等是多义词, 如「终止减持」「承诺不减持」
#: 「解除质押」「解除冻结」都是利好, 不排除会误杀。「解除」作通用排除词,
#: 覆盖「解除控股股东部分股份质押」这类「解除」与「质押」被隔开的标题。
_BULLISH_EXCLUSIONS = (
    "不减持", "终止减持", "取消减持", "承诺不", "解除", "撤销", "扭亏", "澄清",
)

#: 同批次去重缓存: {(symbol, start, end): bool}, 避免 run 重跑时重复抓。
_cache: dict[tuple[str, str, str], bool] = {}


def _split_symbol(symbol: str) -> tuple[str, str]:
    """'000001.SZ' -> ('000001', 'SZ')。异常返回 ('', '')。"""
    s = str(symbol or "").strip()
    if "." not in s:
        return s, ""
    code, market = s.split(".", 1)
    return code, market.upper()


def _stock_param(symbol: str) -> tuple[str, str] | None:
    """'000001.SZ' -> (column, stock)。上证 sse/gssh, 深证 szse/gssz。

    orgId 数字部分固定 7 位(代码 zfill(7)), 如 000001 -> gssz0000001、
    600000 -> gssh0600000。补零不对会导致 totalAnnouncement 恒为 0。
    """
    code, market = _split_symbol(symbol)
    if not code or not market:
        return None
    if market == "SH":
        return "sse", f"{code},gssh{code.zfill(7)}"
    if market == "SZ":
        return "szse", f"{code},gssz{code.zfill(7)}"
    return None  # 北交所等不抓(每日精选已剔除北交所)


def _is_bearish(title: str) -> bool:
    if not title:
        return False
    # 利好/中性表述优先排除(如「终止减持」「承诺不减持」「解除质押」是利好)
    if any(k in title for k in _BULLISH_EXCLUSIONS):
        return False
    return any(k in title for k in _BEARISH_KEYWORDS)


def _fetch_titles(symbol: str, start: str, end: str, timeout: float = 8.0) -> list[str]:
    """抓单只股票在 [start, end] 的公告标题列表。失败返回 []。"""
    param = _stock_param(symbol)
    if param is None:
        return []
    column, stock = param
    import httpx

    try:
        resp = httpx.post(
            _CNINFO_URL,
            data={
                "pageNum": "1", "pageSize": "100",
                "column": column, "tabName": "fulltext",
                "plate": "", "stock": stock,
                "searchkey": "", "secid": "", "category": "",
                "trade": "", "seDate": f"{start}~{end}",
                "sortName": "", "sortType": "", "isHLtitle": "true",
            },
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "http://www.cninfo.com.cn/"},
        )
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("cninfo 公告抓取失败(%s): %s", symbol, e)
        return []
    anns = data.get("announcements") or []
    return [str(a.get("announcementTitle") or "") for a in anns if a.get("announcementTitle")]


def fetch_bearish_symbols(
    symbols: list[str],
    lookback_days: int = 5,
    as_of: date | None = None,
    timeout: float = 8.0,
) -> set[str]:
    """抓取 symbols 近 lookback_days 天的公告标题, 返回标题命中利空关键词的 symbol 集合。

    as_of: 公告抓取截止日(选股日)。默认今天。历史回放时传选股日避免未来公告。
    逐只串行抓取; 单只失败降级为「不剔除」。返回的集合只含「抓取成功且命中」的 symbol。
    """
    if not symbols:
        return set()
    end = as_of or date.today()
    start = end - timedelta(days=lookback_days)
    end_s, start_s = end.isoformat(), start.isoformat()

    bearish: set[str] = set()
    for sym in symbols:
        key = (sym, start_s, end_s)
        if key not in _cache:
            titles = _fetch_titles(sym, start_s, end_s, timeout)
            _cache[key] = any(_is_bearish(t) for t in titles)
        if _cache[key]:
            bearish.add(sym)
    return bearish
