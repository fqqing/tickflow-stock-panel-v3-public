"""个股资讯(新闻 / 公告)接口。

数据源是东方财富的公开接口: 不需要 token, 也不需要额外 pip 依赖。
- 新闻: search-api-web.eastmoney.com (cmsArticleWebOld)
- 公告: np-anotice-stock.eastmoney.com/api/security/ann

实测结论(改代码前先看):
1. 两个接口都是 GET + JSON, 无需鉴权; 但别高频打, 所以加了 TTL 缓存。
2. 新闻的 title/content 里带 <em>...</em> 高亮标签(搜索命中标记), 展示前必须剥掉,
   否则正文里会露出尖括号。
3. 新闻 content 只是截断摘要, 不是全文; 完整正文要跳东财详情页(用 url 字段)。
4. 公告列表只给 art_code, 正文要再请求 /api/news/ann-content?art_code=...。
5. symbol 要去掉交易所后缀再查(600519.SH -> 600519), 东财按 6 位代码查。
6. 搜索关键词用「股票名称」比用代码命中更好, 所以 name 可选传入; 不传则用代码。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])

_NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_ANN_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
_FLASH_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
_TIMEOUT = 15.0
_CACHE_TTL = 300.0
_CACHE_MAX = 500

# 快讯(电报)要实时, 用独立短缓存(30s), 与上面 5 分钟的个股新闻缓存分开。
_FLASH_TTL = 30.0
_flash_cache: dict[str, tuple[float, Any]] = {}

# 搜索命中的高亮标签, 展示前剥掉
_EM_RE = re.compile(r"</?em>")
_HTML_RE = re.compile(r"<[^>]+>")

_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _cache_put(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)
    if len(_cache) > _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)


def _code_of(symbol: str) -> str:
    """600519.SH -> 600519 (东财按 6 位代码查)。"""
    return symbol.split(".")[0] if symbol else ""


def _clean(text: str | None) -> str:
    """剥掉高亮/HTML 标签并压缩空白。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", _HTML_RE.sub("", text)).strip()


def _get_json(url: str, params: dict[str, Any]) -> Any:
    """GET 一个东财 JSON 接口; 失败时抛 HTTPException(502)。"""
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("资讯接口请求失败 %s: %s", url, e)
        raise HTTPException(status_code=502, detail=f"资讯源请求失败: {e}") from e


@router.get("/stock")
def get_stock_news(
    symbol: str = Query(..., description="标的代码, 如 600519.SH"),
    name: str | None = Query(None, description="股票名称; 传了搜索命中更好"),
    size: int = Query(20, ge=1, le=50),
):
    """个股新闻流 (东财搜索接口)。"""
    code = _code_of(symbol)
    keyword = (name or "").strip() or code
    if not code:
        return {"symbol": symbol, "items": []}

    key = f"news:{code}:{keyword}:{size}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    param = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default",
            "sort": "time",
            "pageIndex": 1,
            "pageSize": size,
        }},
    }
    data = _get_json(_NEWS_URL, {"cb": "", "param": json.dumps(param, ensure_ascii=False)})
    raw = (data.get("result") or {}).get("cmsArticleWebOld") or []
    items = []
    for r in raw:
        items.append({
            "id": r.get("code") or "",
            "title": _clean(r.get("title")),
            "date": (r.get("date") or "")[:16],
            "summary": _clean(r.get("content")),
            "url": r.get("url") or "",
            "source": r.get("mediaName") or r.get("source") or "",
        })
    resp = {"symbol": symbol, "keyword": keyword, "items": items}
    _cache_put(key, resp)
    return resp


@router.get("/ann")
def get_announcements(
    symbol: str = Query(..., description="标的代码, 如 600519.SH"),
    size: int = Query(20, ge=1, le=50),
):
    """个股公告列表。只返回元信息, 正文用 /api/news/ann-content 取。"""
    code = _code_of(symbol)
    if not code:
        return {"symbol": symbol, "items": []}

    key = f"ann:{code}:{size}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data = _get_json(_ANN_URL, {
        "sr": -1,
        "page_size": size,
        "page_index": 1,
        "ann_type": "A",
        "client_source": "web",
        "stock_list": code,
    })
    raw = (data.get("data") or {}).get("list") or []
    items = []
    for r in raw:
        columns = [c.get("column_name") for c in (r.get("columns") or [])]
        items.append({
            "art_code": r.get("art_code") or "",
            "title": _clean(r.get("title")),
            "date": (r.get("notice_date") or "")[:10],
            "display_time": (r.get("display_time") or "")[:16],
            "columns": [c for c in columns if c],
        })
    resp = {"symbol": symbol, "items": items}
    _cache_put(key, resp)
    return resp


@router.get("/ann-content")
def get_announcement_content(art_code: str = Query(..., description="公告 art_code")):
    """公告正文。"""
    if not art_code:
        return {"art_code": art_code, "content": ""}
    key = f"annc:{art_code}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data = _get_json(_ANN_CONTENT_URL, {"art_code": art_code, "client_source": "web"})
    content = (data.get("data") or {}).get("notice_content") or ""
    resp = {"art_code": art_code, "content": content}
    _cache_put(key, resp)
    return resp


# 快讯(电报)栏目 fastColumn -> 栏目名。102=全部/全球 7x24 是主频道, 24h 滚动更新。
# 其余栏目低频(周末/夜间可能长时间无新条目), 供前端下拉切换, 默认全部。
_FLASH_COLUMNS = {
    101: "要闻",
    102: "全部",
    104: "公司",
    105: "市场",
    106: "机构",
    107: "宏观",
    108: "债券",
    109: "基金",
    110: "大宗",
}


def _secid_to_symbol(secid: str) -> str | None:
    """东财 secid -> 本项目 symbol。0.300401 -> 300401.SZ; 1.600519 -> 600519.SH。

    其他市场前缀(90/1007 板块, 105/106/116/150/177/999 基金债券等)不是 A 股个股,
    返回 None。
    """
    prefix, _, code = secid.partition(".")
    if prefix == "0" and code:
        return f"{code}.SZ"
    if prefix == "1" and code:
        return f"{code}.SH"
    return None


def _is_board_secid(secid: str) -> bool:
    return secid.startswith(("90.", "1007."))


@router.get("/flash")
def get_flash_news(
    size: int = Query(50, ge=1, le=200),
    column: int = Query(102, ge=100, le=200),
    sort_end: str = Query("", description="上一页返回的 sortEnd, 用于翻页"),
):
    """全市场快讯电报流 (东财 getFastNewsList)。

    与财联社电报同类: 一句话快讯, 按时间倒序, titleColor!=0 为重要快讯(前端标红)。
    summary 即完整电报正文(含【标题】前缀), 无需二次抓取。
    stockList 里的 0/1 前缀映射成 A 股 symbol, 供前端跳个股详情。
    """
    key = f"flash:{column}:{size}:{sort_end}"
    hit = _flash_cache.get(key)
    if hit and time.time() - hit[0] < _FLASH_TTL:
        return hit[1]

    params = {
        "client": "web",
        "biz": "web_724",
        "fastColumn": str(column),
        "sortEnd": sort_end,
        "pageSize": str(size),
        "req_trace": str(int(time.time() * 1000)),
    }
    data = _get_json(_FLASH_URL, params)
    d = data.get("data") or {}
    raw = d.get("fastNewsList") or []
    items = []
    for r in raw:
        stocks = []
        boards = []
        for s in (r.get("stockList") or []):
            sym = _secid_to_symbol(s)
            if sym:
                stocks.append({"symbol": sym, "code": sym.split(".")[0]})
            elif _is_board_secid(s):
                boards.append(s.split(".")[-1])
        code = r.get("code") or ""
        items.append({
            "id": code,
            "title": _clean(r.get("title")),
            "summary": _clean(r.get("summary")),
            "showTime": r.get("showTime") or "",
            "important": r.get("titleColor") or 0,
            "stocks": stocks,
            "boards": boards,
            "share": r.get("share") or 0,
            "url": f"https://finance.eastmoney.com/a/{code}.html" if code else "",
        })
    resp = {
        "items": items,
        "sortEnd": d.get("sortEnd") or "",
        "total": d.get("total") or 0,
        "column": column,
        "columnName": _FLASH_COLUMNS.get(column, str(column)),
    }
    _flash_cache[key] = (time.time(), resp)
    if len(_flash_cache) > _CACHE_MAX:
        oldest = min(_flash_cache, key=lambda k: _flash_cache[k][0])
        _flash_cache.pop(oldest, None)
    return resp
