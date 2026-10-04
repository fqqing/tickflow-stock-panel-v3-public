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
_TIMEOUT = 15.0
_CACHE_TTL = 300.0
_CACHE_MAX = 500

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
