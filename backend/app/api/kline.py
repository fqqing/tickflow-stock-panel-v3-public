"""K 线 / 同步 API。"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from datetime import date, timedelta
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from app.indicators.pipeline import compute_enriched, compute_enriched_single
from app.market_time import cn_now, cn_today
from app.markets import ALL_MARKETS, market_of
from app.price_limits import is_risk_warning_name, price_limit_pct
from app.db_safe import is_valid_ext_ident
from app.services import kline_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kline", tags=["kline"])


def _minute_allowed(capset) -> bool:
    """是否有分钟K权限 (TickFlow Pro+ 或 custom minute 源)。"""
    from app.tickflow.capabilities import Cap

    if capset.has(Cap.KLINE_MINUTE_BATCH):
        return True
    from app.services import preferences

    provider = preferences.get_minute_data_provider()
    _, fallback, error = kline_sync._resolve_minute_provider(provider)
    if error is not None:
        logger.warning("minute provider resolution failed while checking access: %s", error)
    return not fallback


@lru_cache(maxsize=8192)
def _name_pinyin_keys(name: str) -> tuple[str, ...]:
    """返回中文名称所有可能的拼音首字母串 (多音字展开为笛卡尔积)。

    '平安银行' -> ('PAYH',); '重庆百货' -> ('CQBH', 'CQMH', 'ZQBH', 'ZQMH')。
    非汉字字符原样保留: '万科A' -> ('WKA',)。
    股票名总量有限且不变, lru_cache 命中后单次查询 ≈ dict 查找, 全市场遍历 < 1ms。
    """
    from pypinyin import pinyin, Style

    if not name:
        return ()
    keys = [""]
    for group in pinyin(name, style=Style.FIRST_LETTER, heteronym=True):
        keys = [k + g.upper() for k in keys for g in group]
    return tuple(keys)


def _init_pinyin_dict() -> None:
    """加载 A 股高频多音字地名/词组词典, 使常见误读也能命中。

    pypinyin 默认词典对部分地名取常见读音 (如「重」→ chóng), 补充后「重庆」
    同时接受 zhòng/qìng (zq) 与 chóng/qīng (cq) 两种首字母, 与同花顺行为一致。
    幂等: 多次调用安全。
    """
    try:
        from pypinyin import load_phrases_dict

        # value 用二维 list: 每个字给一个或多个读音
        load_phrases_dict(
            {
                "重庆": [["zhòng", "chóng"], ["qīng"]],
                "长安": [["cháng", "zhǎng"], ["ān"]],
                "长春": [["cháng", "zhǎng"], ["chūn"]],
                "长沙": [["cháng", "zhǎng"], ["shā"]],
                "长城": [["cháng", "zhǎng"], ["chéng"]],
                "长江": [["cháng", "zhǎng"], ["jiāng"]],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pypinyin phrases dict load failed (polyphone coverage may degrade): %s", exc
        )


_init_pinyin_dict()


def _match_pinyin(name: str, keyword: str) -> bool:
    """keyword 是否匹配 name 任一拼音首字母串的前缀 (支持多音字)。"""
    return any(k.startswith(keyword) for k in _name_pinyin_keys(name))


@router.get("/instruments/search")
def search_instruments(
    request: Request,
    q: str = Query("", min_length=0, max_length=50, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
    asset_types: str = Query("stock", description="逗号分隔的资产类型: stock,etf"),
):
    """模糊搜索标的 (代码 / 名称)。从内存 instruments 缓存中查。

    默认只搜股票, 保持既有调用方行为不变; 自选等场景传 asset_types=stock,etf
    可一并搜出 ETF, 结果附带 asset_type 字段供前端区分。
    """
    if not q.strip():
        return {"results": []}

    repo = request.app.state.repo
    import polars as pl

    types = [t.strip() for t in asset_types.split(",") if t.strip()]
    parts: list[pl.DataFrame] = []
    for t in types:
        df_t = repo.get_instruments_asset(t)
        if df_t.is_empty() or "symbol" not in df_t.columns:
            continue
        # dtype 全部归一到 Utf8: 股票/ETF 两份缓存来源不同 (ETF 含 legacy 合并), 防 concat SchemaError
        parts.append(
            df_t.with_columns(
                [
                    pl.col("symbol").cast(pl.Utf8).alias("symbol"),
                    (pl.col("name").cast(pl.Utf8) if "name" in df_t.columns else pl.lit("")).alias(
                        "name"
                    ),
                    (pl.col("code").cast(pl.Utf8) if "code" in df_t.columns else pl.lit("")).alias(
                        "code"
                    ),
                    pl.lit(t).alias("asset_type"),
                ]
            ).select(["symbol", "name", "code", "asset_type"])
        )
    if not parts:
        return {"results": []}
    df = pl.concat(parts, how="vertical")

    keyword = q.strip().upper()
    is_pinyin_query = keyword.isalpha() and keyword.isascii()

    # code/symbol 前缀优先，再 name 包含匹配
    prefix_mask = pl.col("code").str.starts_with(keyword) | pl.col(
        "symbol"
    ).str.to_uppercase().str.starts_with(keyword)
    contains_mask = (
        pl.col("code").str.contains(keyword, literal=True)
        | pl.col("symbol").str.to_uppercase().str.contains(keyword, literal=True)
        | pl.col("name").str.contains(keyword, literal=True)
    )

    # 分层匹配: ① code/symbol 前缀 → ② 拼音首字母前缀(纯字母输入) → ③ 包含匹配
    prefix_hits = df.filter(prefix_mask).head(limit)
    if prefix_hits.height >= limit:
        matched = prefix_hits
    else:
        collected = [prefix_hits] if prefix_hits.height else []
        seen = set(prefix_hits["symbol"].to_list()) if prefix_hits.height else set()
        remaining = limit - prefix_hits.height

        # ② 拼音首字母前缀: 仅纯字母输入触发 (如 payh → 平安银行); 中文/代码输入零开销跳过
        if is_pinyin_query and remaining > 0:
            pinyin_rows = []
            for row in df.filter(~pl.col("symbol").is_in(seen)).iter_rows(named=True):
                if _match_pinyin(row["name"], keyword):
                    pinyin_rows.append(row)
                    if len(pinyin_rows) >= remaining:
                        break
            if pinyin_rows:
                collected.append(pl.DataFrame(pinyin_rows))
                seen.update(r["symbol"] for r in pinyin_rows)
                remaining -= len(pinyin_rows)

        # ③ 包含匹配补充
        if remaining > 0:
            contain_hits = df.filter(contains_mask & ~pl.col("symbol").is_in(seen)).head(remaining)
            if contain_hits.height:
                collected.append(contain_hits)

        matched = (
            pl.concat(collected, how="vertical")
            if len(collected) > 1
            else (collected[0] if collected else df.head(0))
        )
    rows = matched.select(["symbol", "name", "code", "asset_type"]).to_dicts()
    return {"results": rows}


@router.post("/instruments/names")
def instruments_names(request: Request, symbols: list[str]):
    """批量查标的名称 (股票 + ETF + 指数)。传入 symbol 列表, 返回 {symbol: name}。"""
    if not symbols:
        return {"names": {}}
    repo = request.app.state.repo
    return {"names": repo.get_name_map(symbols)}


def _get_stock_info(repo, symbol: str) -> dict:
    """从 instruments 内存缓存查标的名称 + 股本。

    该接口在个股弹窗打开时每秒被调用 (SSE invalidate 触发重拉), 走
    repo.get_instruments() 的 Polars 内存缓存按 symbol 过滤, 不再每请求
    DuckDB 扫 instruments parquet。列缺失时返回空 dict, 与旧 SQL 报错路径一致。
    """
    import polars as pl

    try:
        df = repo.get_instruments()
        needed = ("symbol", "name", "total_shares", "float_shares")
        if df.is_empty() or not all(c in df.columns for c in needed):
            return {}
        hit = df.filter(pl.col("symbol") == symbol).head(1)
        if hit.is_empty():
            return {}
        return {
            "name": hit["name"][0],
            "total_shares": hit["total_shares"][0],
            "float_shares": hit["float_shares"][0],
        }
    except Exception:  # noqa: BLE001
        return {}


def _get_asset_info(repo, symbol: str, asset_type: str) -> dict:
    """非股票标的 (ETF / 指数) 的名称信息 — 从对应 instruments 缓存查, 无股本概念。"""
    import polars as pl

    try:
        df = repo.get_instruments_asset(asset_type)
        if df.is_empty() or "symbol" not in df.columns or "name" not in df.columns:
            return {}
        hit = df.filter(pl.col("symbol") == symbol).head(1)
        if hit.is_empty():
            return {}
        return {"name": hit["name"][0]}
    except Exception:
        return {}


def _get_price_limit_info(
    repo,
    symbol: str,
    trade_date: date,
    asset_type: str,
    instrument_name: str | None,
) -> dict | None:
    """Return the date-aware limit rule and today's authoritative prices."""
    if asset_type == "index":
        return None

    info = {
        "rate": price_limit_pct(
            symbol,
            trade_date,
            is_risk_warning=(asset_type == "stock" and is_risk_warning_name(instrument_name)),
        ),
        "limit_up": None,
        "limit_down": None,
        "source": "rule",
    }
    if trade_date != cn_today():
        return info

    try:
        import polars as pl

        instruments = repo.get_instruments_asset(asset_type)
        available = [
            column
            for column in ("symbol", "limit_up", "limit_down")
            if column in instruments.columns
        ]
        if "symbol" not in available or len(available) == 1:
            return info
        hit = instruments.filter(pl.col("symbol") == symbol).select(available).head(1)
        row = hit.to_dicts()[0] if not hit.is_empty() else None
    except Exception:
        return info
    if row is None:
        return info

    has_authoritative_price = False
    for field in ("limit_up", "limit_down"):
        value = row.get(field)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric) and 0 < numeric < 10_000:
            info[field] = numeric
            has_authoritative_price = True
    if has_authoritative_price:
        info["source"] = "instrument"
    return info


def _get_previous_closes(
    repo,
    symbol: str,
    trade_dates: list[date],
    asset_type: str,
    market: str = "cn",
) -> dict[date, float | None]:
    """Return the previous trading day's adjusted close for each session.

    market 必须透传: cn/hk/us 三市的日K落在不同目录, 默认 cn 会让港美股恒读不到数据.
    """
    if not trade_dates:
        return {}
    start = min(trade_dates) - timedelta(days=45)
    end = max(trade_dates)
    try:
        daily = repo.get_daily_asset(
            asset_type,
            symbol,
            start,
            end,
            columns=["date", "close"],
            market=market,
        ).sort("date")
    except Exception:
        daily = None
    if daily is None or daily.is_empty():
        return {trade_date: None for trade_date in trade_dates}

    closes: list[tuple[date, float]] = []
    for daily_date, close in daily.select(["date", "close"]).iter_rows():
        if close is None:
            continue
        numeric = float(close)
        if math.isfinite(numeric) and numeric > 0:
            closes.append((daily_date, numeric))

    result: dict[date, float | None] = {}
    for trade_date in trade_dates:
        result[trade_date] = next(
            (close for daily_date, close in reversed(closes) if daily_date < trade_date),
            None,
        )
    return result


def _realtime_prev_close(symbol: str) -> float | None:
    """从实时快照取昨收, 用于本地日K未同步时的兜底。

    「盘后没跑同步, 第二天开盘就看不了」的症结之一: _get_previous_closes 只读
    本地 kline_daily, 本地停在更早交易日时昨收为 None, 分时图的涨跌幅与参考
    基准全部失效。实时快照直连数据源, 不依赖本地历史, 是最可靠的兜底来源。

    口径差异: 本地值是前复权 close, 快照值通常是未复权昨收。除权日两者会有偏
    差, 但「有近似基准」远好于「完全没有」; 且本函数只在本地缺失时才被调用。
    """
    try:
        from app.data_providers.custom import loader as custom_loader

        provider = custom_loader.get_provider("stocksdk")
        rows = provider.get_depth([symbol])
    except Exception as e:
        logger.debug("realtime prev_close unavailable for %s: %s", symbol, e)
        return None
    if not rows:
        return None

    target = str(symbol).upper()
    row = rows[0]
    for candidate in rows:
        if str(candidate.get("symbol", "")).upper() == target:
            row = candidate
            break
    try:
        value = float(row.get("prev_close"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _prev_close_with_fallback(
    repo,
    symbol: str,
    trade_dates: list[date],
    asset_type: str,
    market: str = "cn",
) -> tuple[dict[date, float | None], dict[date, str]]:
    """取昨收并在本地缺失时用实时快照兜底。

    返回 (值字典, 来源字典)。来源取值:
      local    — 本地日K算出(前复权, 最准)
      realtime — 本地缺失, 用实时快照兜底(未复权口径)
      none     — 两者都没有, 调用方应按「数据未就绪」处理而不是当成 0
    """
    local = _get_previous_closes(repo, symbol, trade_dates, asset_type, market=market)
    values: dict[date, float | None] = {}
    sources: dict[date, str] = {}
    for trade_date in trade_dates:
        value = local.get(trade_date)
        if value is not None:
            values[trade_date] = value
            sources[trade_date] = "local"
            continue
        fallback = _realtime_prev_close(symbol)
        values[trade_date] = fallback
        sources[trade_date] = "realtime" if fallback is not None else "none"
    return values, sources


def _resolve_market(symbol: str, market: str) -> str:
    """确定标的市场: 显式传参优先, 留空则按 symbol 后缀推导.

    A 股之外 (hk/us) 的日K落在独立目录, 而 ``repo.get_daily_asset`` 的 market
    默认是 cn -- 少了这一层, 港美股 K 线恒返回空 (既不是报错也不是缺数据,
    排查时极易误判成"没同步"). 所以每个读日K的入口都必须显式带 market.
    """
    wanted = (market or "").strip().lower()
    if not wanted:
        return market_of(symbol)
    if wanted not in ALL_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的市场: {wanted} (可选 {','.join(ALL_MARKETS)})",
        )
    return wanted


# ===== 条件请求 (ETag / 304) =====
# 切周期/切票/分钟轮询会反复打同一个 URL, 而响应体动辄 0.4MB (1000 天) 到
# 1MB (5 分钟/120 日)。没有 ETag 时这些请求每次都要重新传一遍并重新 JSON 解析。
# 前端是 fetch + react-query, 浏览器会自动带 If-None-Match 并把 304 还原成
# 缓存的 200, 所以后端加完前端零改动。


def _rows_digest(rows: list[dict]) -> str:
    """对 K 线行做轻量摘要 (md5), 作为 ETag 的内容指纹.

    比「先 json.dumps 全量再 hash」便宜一个数量级 (1000 行约 1ms vs 20ms),
    且能抓到「中间历史行被重写」这种情况 -- 后者正是只按末日 + 行数做指纹
    会漏掉的场景 (重写不改变末日和行数)。

    时间列两种口径: 日K是 date, 分钟K是 datetime, 都要取到。
    """
    h = hashlib.md5()
    for r in rows:
        stamp = r.get("date")
        if stamp is None:
            stamp = r.get("datetime")
        h.update(
            "{},{},{},{},{},{}".format(
                stamp,
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                r.get("volume"),
            ).encode("utf-8")
        )
    return h.hexdigest()


def _conditional_json(request: Request, payload: dict):
    """命中 If-None-Match 返回 304 (空 body), 否则返回带 ETag 的 JSON.

    指纹只取「名称 + 行数 + 行摘要」: 浏览器按 URL 缓存, 同一 URL 内的响应
    差异只来自数据本身, 所以 period/adjust/fields 这些已经体现在 URL 里的
    参数不必重复进指纹。名称进指纹是为了 ST 摘帽/改名能及时刷新。

    用弱校验 (W/ 前缀): 同一份数据的两种等价序列化 (浮点精度/字段顺序差异)
    应算同一个 ETag, 弱校验的语义正是「语义等价即可复用」。

    Cache-Control 用 no-cache 而不是 no-store: 允许浏览器缓存响应体, 但每次
    必须先回源校验。日K会注入实时蜡烛, 盘中价格一动摘要就变, 所以 304 只在
    真的没变时才出现, 不会让盘中的图停在旧价上。
    """
    rows = payload.get("rows") or []
    raw = f"{payload.get('name')};{len(rows)};{_rows_digest(rows)}"
    etag = 'W/"' + hashlib.md5(raw.encode("utf-8")).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(jsonable_encoder(payload), headers=headers)


@router.get("/daily")
def get_daily(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
    days: int = Query(120, ge=10, le=2000),
    start_date: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
    ext_columns: Optional[str] = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
    indicators: Optional[str] = Query(
        None,
        description=(
            "逗号分隔的派生指标: trend_dragon,capital_momentum,structure,macd_structure "
            "(解密公式, 按需计算)"
        ),
    ),
    fields: str | None = Query(
        None,
        description=(
            "逗号分隔的列名白名单: 只返回这些列, 用于裁剪响应体。"
            "K 线图场景可只取绘图所需列(实测 1000 天请求 1.32MB -> 约 0.4MB)。"
            "不传则返回全部列(兼容既有调用方)。未知列名静默忽略。"
        ),
    ),
    period: str = Query(
        "day",
        description="K 线周期: day(日) / week(周) / month(月)。周月由日 K 聚合并在聚合后重算指标。",
    ),
    adjust: str = Query(
        "qfq",
        description="复权方式: qfq(前复权, 数据源口径, 默认) / none(不复权) / hfq(后复权)。",
    ),
    market: str = Query(
        "",
        description=(
            "市场: cn/hk/us. 留空按 symbol 后缀自动推导 "
            "(600000.SH->cn, 00700.HK->hk, AAPL.US->us). 三市日K分目录存储, "
            "取错市场会恒返回空."
        ),
    ),
):
    """读取本地 enriched 表中某只股票的日 K。

    - 若 QuoteService 有实时行情, 追加/覆盖今日实时蜡烛
    - Free 用户: 若 enriched 表里没有该股票, 实时拉取 + 本地算 enriched 返回
    - ext_columns: 可选，动态 LEFT JOIN 扩展数据表，结果平铺到 stock_info.ext 下
      (key 为 "{config_id}__{field_name}")，供日K信息条等场景展示自定义字段
    - indicators: 可选, 为每根 K 线附加解密公式结果 (蛟龙出海/资金动能/
      主图定量结构/MACD 定量结构), 在实时蜡烛注入之后计算, 保证与图上最后一根
      K 线一致
    - market: 三市日K落在不同目录, 必须显式透传给仓库层, 否则港美股恒读不到.
    """
    import polars as pl

    repo = request.app.state.repo
    market = _resolve_market(symbol, market)
    end = date.fromisoformat(end_date) if end_date else date.today()
    if start_date:
        start = date.fromisoformat(start_date)
    else:
        start = end - timedelta(days=days)

    asset_type = repo.resolve_asset_type(symbol)
    stock_info = (
        _get_stock_info(repo, symbol)
        if asset_type == "stock"
        else _get_asset_info(repo, symbol, asset_type)
    )
    stock_name = stock_info.get("name")

    # 从 enriched 表读取 (已含前复权 OHLCV + 技术指标 + 信号); ETF/指数走独立存储
    df = repo.get_daily_asset(asset_type, symbol, start, end, market=market)

    if df.is_empty():
        try:
            raw = kline_sync.sync_daily_batch([symbol], count=days + 30)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"TickFlow fetch failed: {e}") from e
        if raw.is_empty():
            return _conditional_json(
                request,
                {
                    "symbol": symbol,
                    "name": stock_name,
                    "market": market,
                    "stock_info": stock_info,
                    "rows": [],
                },
            )
        # 拉除权因子做前复权 (Starter+ 有权限), 否则空 df → compute_enriched 退回未复权
        factors = pl.DataFrame()
        capset = getattr(request.app.state, "capabilities", None)
        try:
            from app.tickflow.capabilities import Cap

            if capset and capset.has(Cap.ADJ_FACTOR):
                factors = kline_sync.fetch_adj_factor_single(symbol)
        except Exception as e:  # noqa: BLE001
            logger.debug("单股除权因子拉取失败 %s: %s", symbol, e)
        enriched = compute_enriched(raw, factors=factors)
        rows = enriched.tail(days).to_dicts()
        # 即使 live 模式也尝试追加实时蜡烛
        rows = _maybe_inject_live_candle(request, symbol, rows, asset_type)
        resp = {
            "symbol": symbol,
            "name": stock_name,
            "market": market,
            "stock_info": stock_info,
            "rows": rows,
            "source": "live",
        }
        resp = _attach_indicators(
            request, repo, resp, symbol, indicators, start, end, asset_type, market=market
        )
        return _conditional_json(
            request, _apply_fields(_attach_ext(resp, repo, symbol, ext_columns), fields)
        )

    # 复权要在周期聚合之前做: 聚合取 last/first/max/min, 若先聚合再复权,
    # 周末那根用的是「周期末」的价格, 与逐根缩放的结果不等价。
    df = _apply_adjust(df, adjust, repo.store.data_dir)
    if period in _VALID_PERIODS and period != "day":
        # 周/月线: 聚合后直接返回, 不注入当日实时蜡烛 —— 实时蜡烛是「日」粒度,
        # 直接追加会在周线上多出一根错误的短周期 K 线。
        agg = _aggregate_period_cached(df, period, symbol, adjust)
        rows = _select_fields_df(agg, fields).to_dicts()
    else:
        rows = _select_fields_df(df, fields).to_dicts()
        rows = _maybe_inject_live_candle(request, symbol, rows, asset_type)
        rows = _adjust_live_row(rows, adjust, df, repo.store.data_dir)

    resp = {
        "symbol": symbol,
        "name": stock_name,
        "market": market,
        "stock_info": stock_info,
        "rows": rows,
        "source": "enriched",
    }
    resp = _attach_indicators(
        request, repo, resp, symbol, indicators, start, end, asset_type, market=market
    )
    return _conditional_json(
        request, _apply_fields(_attach_ext(resp, repo, symbol, ext_columns), fields)
    )


def _attach_ext(resp: dict, repo, symbol: str, ext_columns: Optional[str]) -> dict:
    """按 ext_columns 规格为单只股票 LEFT JOIN 扩展数据，平铺到 stock_info['ext']。

    key 形如 "{config_id}__{field_name}"，与自选列表 enriched 接口保持一致。
    委托 screener._load_ext_value_maps 取值: 复用其 (路径,mtime) 签名缓存,
    个股弹窗每秒重拉时不再重复读 ext parquet; 任何 ext 表/字段缺失都静默跳过。
    """
    if not ext_columns or not ext_columns.strip():
        return resp

    specs: list[tuple[str, str]] = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id, field_name = config_id.strip(), field_name.strip()
        if config_id and field_name and is_valid_ext_ident(config_id):
            specs.append((config_id, field_name))
    if not specs:
        return resp

    try:
        from app.api.screener import _load_ext_value_maps

        value_maps = _load_ext_value_maps(repo, ext_columns)
    except Exception:  # noqa: BLE001
        value_maps = {}

    ext_values: dict = {}
    for config_id, field_name in specs:
        ext_col_name = f"{config_id}__{field_name}"
        vmap = value_maps.get(ext_col_name) or {}
        ext_values[ext_col_name] = vmap.get(symbol)

    stock_info = dict(resp.get("stock_info") or {})
    stock_info["ext"] = ext_values
    resp["stock_info"] = stock_info
    return resp


_VALID_PERIODS = frozenset({"day", "week", "month"})
_TRUNC_UNIT = {"week": "1w", "month": "1mo"}


def _aggregate_period(df, period: str):
    """把日 K 聚合成周/月 K, 并在聚合后**重算**技术指标。

    为什么指标不能沿用: 周线的 MA20 是「20 周均线」, 不等于日线 MA20 在周末
    那天的取值; MACD/KDJ/RSI 同理。所以只保留聚合后的 OHLCV, 其余列全部
    交给 indicators.pipeline.compute_indicators 重算。

    聚合口径(与主流行情软件一致):
      open=周期内第一根, high=周期内最高, low=周期内最低,
      close=周期内最后一根, volume/amount=周期内求和,
      date=周期内最后一个交易日。
    """
    if period not in _TRUNC_UNIT or df.is_empty() or "date" not in df.columns:
        return df
    import polars as pl

    from app.indicators.pipeline import compute_indicators

    d = df.sort("date").with_columns(pl.col("date").cast(pl.Date))
    base_cols = [c for c in ("symbol", "date", "open", "high", "low", "close", "volume")
                 if c in d.columns]
    agg_exprs = [
        pl.col("date").max().alias("date"),
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
    ]
    agg_exprs.extend(
        pl.col(c).sum().alias(c) if c in ("volume", "amount") else pl.col(c).last().alias(c)
        for c in d.columns if c not in ("date", "open", "high", "low", "close")
    )
    agg = (
        d.with_columns(pl.col("date").dt.truncate(_TRUNC_UNIT[period]).alias("_p"))
        .group_by("_p")
        .agg(agg_exprs)
        .sort("_p")
        .drop("_p")
    )
    out = compute_indicators(agg.select(base_cols))
    # 把聚合里保留、但指标管线不产出的列(如 amount/turnover_rate)补回来
    extra = [c for c in agg.columns if c not in out.columns]
    if extra:
        out = out.join(agg.select(["date", *extra]), on="date", how="left")
    return out


# 周/月K 聚合结果缓存. 切周期档 / 开关叠加层 / 刷新面板都会重新打一次 /daily,
# 而聚合后还要把全套指标重算一遍 (compute_indicators 约等于一次全表扫描).
# 结果只由 (标的, 周期, 复权, 数据边界) 决定, 所以按这个指纹缓存是安全的.
_PERIOD_CACHE_MAX = 64
_PERIOD_CACHE_TTL = 900.0  # 秒。日线一天一更, 15 分钟足够新鲜
_period_cache: dict[tuple, tuple[float, object]] = {}
_period_lock = threading.Lock()


def _period_cache_key(symbol: str, period: str, adjust: str, df) -> tuple:
    """聚合结果指纹: (标的, 周期, 复权, 末日, 行数, 末收).

    不 hash 全表 (成本高). 日K只在「末尾新增一天」或「整体重写」时变化,
    这两个动作都会至少改掉末日 / 行数 / 末收之一, 三者同时不变即可复用.
    adjust 必须进 key: 复权在聚合之前做, 三种口径的 df 行数完全相同.
    """
    last_date = df["date"].max() if "date" in df.columns and df.height else None
    last_close = None
    if "close" in df.columns and df.height:
        try:
            last_close = round(float(df["close"][-1] or 0.0), 6)
        except (TypeError, ValueError):
            last_close = None
    return (symbol, period, adjust, str(last_date), df.height, last_close)


def _aggregate_period_cached(df, period: str, symbol: str, adjust: str):
    """``_aggregate_period`` 的缓存版 (未命中才真的算)。"""
    key = _period_cache_key(symbol, period, adjust, df)
    now = time.monotonic()
    with _period_lock:
        hit = _period_cache.get(key)
        if hit is not None and now - hit[0] < _PERIOD_CACHE_TTL:
            return hit[1]

    out = _aggregate_period(df, period)

    with _period_lock:
        _period_cache[key] = (time.monotonic(), out)
        if len(_period_cache) > _PERIOD_CACHE_MAX:
            oldest = min(_period_cache.items(), key=lambda kv: kv[1][0])[0]
            _period_cache.pop(oldest, None)
    return out


def clear_period_cache() -> None:
    """清空周/月K聚合缓存 (数据同步后手动调用, 或测试用)。"""
    with _period_lock:
        _period_cache.clear()


_ADJUST_PRICE_COLS = (
    "open", "high", "low", "close", "prev_close",
    "ma5", "ma10", "ma20", "ma30", "ma60",
    "ema5", "ema10", "ema20", "ema30", "ema60",
    "high_60d", "low_60d",
    "macd_dif", "macd_dea", "macd_hist",
    "boll_upper", "boll_mid", "boll_lower",
    "atr_14", "change_amount",
)


def _hfq_factor(df, data_dir) -> float:
    """截至该股票最新交易日的累积除权因子。

    后复权价 = 前复权价 x 本常数(推导: qfq_t = raw_t * F_t / F_last,
    hfq_t = raw_t * F_t, 两式相除得 hfq_t = qfq_t * F_last)。
    所以后复权不需要逐根 join 除权表, 一个常数即可。
    """
    if data_dir is None or "symbol" not in df.columns or df.is_empty():
        return 1.0
    import glob as _glob

    import polars as pl

    sym = df["symbol"][0]
    # Windows 上 str(Path) 带反斜杠, glob 两种分隔符都认, 这里统一成正斜杠取巧
    base = str(data_dir).replace("\\", "/")
    paths = sorted(_glob.glob(f"{base}/adj_factor/**/*.parquet", recursive=True))
    if not paths or not sym:
        return 1.0
    try:
        af = pl.read_parquet(paths).filter(pl.col("symbol") == sym)
    except Exception:
        return 1.0
    if af.is_empty() or "ex_factor" not in af.columns:
        return 1.0
    af = af.with_columns(
        pl.col("trade_date").cast(pl.Utf8).str.to_date("%Y-%m-%d").alias("trade_date")
    )
    if "date" in df.columns:
        latest = df["date"].max()
        if latest is not None:
            af = af.filter(pl.col("trade_date") <= latest)
    if af.is_empty():
        return 1.0
    try:
        return float(af["ex_factor"].product()) or 1.0
    except Exception:
        return 1.0


def _apply_adjust(df, adjust: str, data_dir=None):
    """按复权方式缩放价格型列。

    qfq (默认, 数据源口径): 以最新价为基准把历史价向下调整 -> 原样返回
    none (不复权):          乘 raw_close/close, 还原成交易所真实成交价
    hfq (后复权):           乘「截至最新交易日的累积除权因子」, 让历史价显示为真实价

    为什么用逐根缩放而不是整表重算指标: enriched 表里的自定义列
    (momentum_* / signal_* / consecutive_limit_* 等) 无法在 API 层重算, 整表重算
    会把它们全部丢掉。而除权因子是分段常数(一年一两次、分红类通常接近 1), 对
    跨越除权点的均线窗口只带来很小误差, 视觉上无差别。

    表里只有 raw_close/raw_high/raw_low 没有 raw_open: open 用同根 close 的
    比例反推 —— open/close 这个比值在任意复权口径下都相同。
    """
    if adjust == "qfq" or df.is_empty():
        return df
    import polars as pl

    if adjust == "none":
        if "raw_close" not in df.columns or "close" not in df.columns:
            return df
        ratio = pl.col("raw_close") / pl.col("close")
        # prev_close 是「前一日」的价格, 必须乘前一日的换算比例: 除权日当天
        # close 的比例会突变, 若跟着当日比例走, 除权日的 prev_close 会被算成
        # 除权后的价格(实测比亚迪 2025-07-29: 337.00 被算成 111.01)。
        ratio_prev = ratio.shift(1)
    elif adjust == "hfq":
        # 后复权相对前复权是常数倍, 前后日比例相同, 无需 shift
        ratio = pl.lit(_hfq_factor(df, data_dir))
        ratio_prev = ratio
    else:
        return df

    cols = [c for c in _ADJUST_PRICE_COLS if c in df.columns and c != "prev_close"]
    if not cols:
        return df
    # 必须在同一个 with_columns 里一次算完: polars 的多个表达式同时基于原 df 求值,
    # 若分成两步, 第二步的 close 已被改成 raw_close, ratio 会退化成 1。
    exprs = [(pl.col(c) * ratio).alias(c) for c in cols]
    if "prev_close" in df.columns:
        exprs.append(
            (pl.col("prev_close") * ratio_prev)
            .fill_null(pl.col("prev_close") * ratio)  # 首根没有前值, 退回当日比例
            .alias("prev_close")
        )
    out = df.with_columns(exprs)
    # 涨跌幅/振幅/涨跌额是相对量, 价格缩放后按新列重算:
    # 不复权下除权日会显示真实跳空, 前复权下则被抹平。
    if {"close", "prev_close"}.issubset(out.columns):
        out = out.with_columns(
            ((pl.col("close") - pl.col("prev_close")) / pl.col("prev_close"))
            .alias("change_pct")
        )
        out = out.with_columns((pl.col("close") - pl.col("prev_close")).alias("change_amount"))
    if {"high", "low", "prev_close"}.issubset(out.columns):
        out = out.with_columns(
            ((pl.col("high") - pl.col("low")) / pl.col("prev_close")).alias("amplitude")
        )
    return out


def _adjust_live_row(rows: list, adjust: str, df, data_dir=None) -> list:
    """给「实时蜡烛」补上复权缩放。

    实时蜡烛由 _maybe_inject_live_candle 在 df 转成 rows 之后追加/替换, 因此躲过了
    _apply_adjust; 它来自行情服务, 是交易所真实价(等价于不复权口径), 要按目标
    口径再缩放一次。

    判据: 仅当注入值与 df 末根不同才需要补 —— 两者相等说明今日不存在除权
    (前复权价 == 真实价), 此时补与不补结果一致, 直接跳过可避免重复缩放。
    """
    if adjust == "qfq" or adjust == "none" or not rows or df.is_empty():
        return rows
    if adjust != "hfq":
        return rows
    try:
        last = df["close"][-1]
    except Exception:
        return rows
    if last is None:
        return rows
    try:
        if abs(float(rows[-1].get("close") or 0) - float(last)) <= 1e-9:
            return rows
    except (TypeError, ValueError):
        return rows

    factor = _hfq_factor(df, data_dir)
    if abs(factor - 1.0) <= 1e-12:
        return rows
    patched = dict(rows[-1])
    for c in _ADJUST_PRICE_COLS:
        v = patched.get(c)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            patched[c] = v * factor
    rows[-1] = patched
    return rows


def _select_fields_df(df, fields: str | None):
    """在 polars 层按白名单裁列, 让 to_dicts() 只转换真正要返回的列。

    实测只做 dict 层过滤(在 to_dicts 之后)只能省掉传输体积, 拿不到耗时收益:
    664 行 x 71 列的 to_dicts() 才是大头。放到 df 层后, 响应体与耗时一起降。
    """
    if not fields or not fields.strip():
        return df
    keep = [c.strip() for c in fields.split(",") if c.strip()]
    keep = [c for c in keep if c in df.columns]
    return df.select(keep) if keep else df


def _apply_fields(resp: dict, fields: str | None) -> dict:
    """按 fields 白名单裁剪 resp['rows'] 的列。

    只在最后一步过滤 dict, 不改上游 polars 处理: 实时蜡烛注入与解密指标
    (_attach_indicators) 都在裁剪之前完成, 因此白名单里可以只包含最终要用的列。
    不传 fields 或白名单为空时原样返回, 保证既有调用方行为不变。
    """
    if not fields or not fields.strip():
        return resp
    keep = {c.strip() for c in fields.split(",") if c.strip()}
    if not keep:
        return resp
    rows = resp.get("rows") or []
    if rows and isinstance(rows[0], dict):
        resp["rows"] = [{k: v for k, v in row.items() if k in keep} for row in rows]
    return resp


# ===== 解密公式派生指标 (按需计算, 不落盘) =====

# 资金动能的「对应指数」口径与源脚本一致: 沪市用上证指数, 深市用深证成指
_BENCHMARK_INDEX_BY_EXCHANGE = {
    "SH": "000001.SH",
    "SZ": "399001.SZ",
    "BJ": "899050.BJ",
}
_DEFAULT_BENCHMARK_INDEX = "000001.SH"

# 已实现的指标 key; 未知 key 静默忽略, 便于前端渐进接入
_SUPPORTED_INDICATORS = frozenset(
    {"trend_dragon", "capital_momentum", "structure", "macd_structure"}
)

# 指标预热天数: 每个指标依赖的前置窗口差异很大, 按所选指标取最大值一次性取数
# - trend_dragon / capital_momentum: MA10 与 52 日均值 / BARSLAST 链
# - structure / macd_structure: EMA89 与跨峰 REF 链, 需要更长的历史才收敛
_INDICATOR_WARMUP_DAYS = {
    "trend_dragon": 180,
    "capital_momentum": 180,
    "structure": 540,
    "macd_structure": 540,
}


def _date_key(value: object) -> str:
    """把 date/datetime/字符串统一成 YYYY-MM-DD, 用于跨表按日对齐。"""
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _as_float(value: object) -> float:
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan
    return numeric if math.isfinite(numeric) else math.nan


def _finite_or_none(value: object, digits: int = 4) -> float | None:
    """把数值收敛成 JSON 友好的 float; NaN/inf 统一输出 None。"""
    numeric = _as_float(value)
    return round(numeric, digits) if math.isfinite(numeric) else None


def _benchmark_index_symbol(symbol: str) -> str:
    exchange = symbol.partition(".")[2].upper()
    return _BENCHMARK_INDEX_BY_EXCHANGE.get(exchange, _DEFAULT_BENCHMARK_INDEX)


def _attach_indicators(
    request: Request,
    repo,
    resp: dict,
    symbol: str,
    indicators: Optional[str],
    start: date,
    end: date,
    asset_type: str = "stock",
    market: str = "cn",
) -> dict:
    """为 rows 逐根附加解密公式结果 (原地写入并返回 resp)。

    market 用于个股历史取数 (hk/us 日K在独立目录); 资金动能的对比指数恒为
    A 股指数 (见 _BENCHMARK_INDEX_BY_EXCHANGE), 那一路显式传 cn.


    - trend_dragon: ``td_signal`` (bool) / ``td_a3`` (int|null)
    - capital_momentum: ``cm_value`` (float|null, 与源脚本同口径, 已乘 10)
    - structure: ``st_dsg``/``st_dxg``/``st_csg``/``st_cxg`` 双轨线,
      ``st_icon`` (0 无 / 4 上穿短上轨 / 5 跌破短下轨),
      ``st_dn``/``st_up`` (九转标注数字, 0 表示当日无标注, 否则 6~9)
    - macd_structure: ``ms_diff``/``ms_dea``/``ms_hist`` 三条 MACD 序列, 加
      ``ms_btext``/``ms_by`` (底部) 与 ``ms_ttext``/``ms_ty`` (顶部) 结构标注
      (1 结构形成 / 2 钝化 / 3 钝化消失)

    这些指标都有前置窗口依赖 (MA10 / 52 日均值 / EMA89 / BARSLAST 跨峰链), 因此
    计算时按所选指标额外向前多取 ``_INDICATOR_WARMUP_DAYS`` 天历史, 算完再
    按日期回填到请求区间 —— 否则用户把区间缩到一两个月时, 前段数值会整体失真。
    资金动能沿用源脚本口径: 先取个股与指数都有数据的交易日, 再滚动 52 根;
    不足 52 根时该段为 null。
    """
    keys = {k.strip() for k in (indicators or "").split(",") if k.strip()} & _SUPPORTED_INDICATORS
    rows = resp.get("rows") or []
    if not keys or not rows:
        return resp

    import numpy as np

    from app.indicators.formula_signals import (
        CAPITAL_MOMENTUM_WINDOW,
        MACD_SLOW_SPAN,
        STRUCTURE_LONG_SPAN,
        capital_momentum,
        macd_quant_structure,
        quant_structure_main,
        trend_dragon,
    )

    today_key = cn_today().isoformat()
    warmup_start = start - timedelta(days=max(_INDICATOR_WARMUP_DAYS[key] for key in keys))

    # 逐日 OHLC: 先用仓库里的长历史打底, 再用请求区间内的行覆盖 (含今日实时蜡烛)
    history: dict[str, dict] = {}
    try:
        hist_df = repo.get_daily_asset(
            asset_type,
            symbol,
            warmup_start,
            end,
            columns=["date", "open", "high", "low", "close"],
            market=market,
        )
        for record in hist_df.iter_rows(named=True):
            history[_date_key(record.get("date"))] = record
    except Exception as exc:  # noqa: BLE001
        logger.debug("指标预热取历史 %s 失败: %s", symbol, exc)

    for row in rows:
        # 合并而不是覆盖: fields 过滤后的请求行可能缺 OHLC (如 fields=date,cm_value),
        # 直接覆盖会把仓库历史里的 close 抹成 NaN, 资金动能等指标链全 null (2026-09-30 实测)。
        # 行里带 close 时(实时蜡烛)仍以行内值为准。
        key = _date_key(row.get("date"))
        base = history.get(key)
        history[key] = {**base, **row} if base else row

    ordered = sorted(history.items())
    days = [day for day, _ in ordered]
    index_of = {day: i for i, day in enumerate(days)}
    open_series = np.array([_as_float(r.get("open")) for _, r in ordered], dtype=np.float64)
    high_series = np.array([_as_float(r.get("high")) for _, r in ordered], dtype=np.float64)
    low_series = np.array([_as_float(r.get("low")) for _, r in ordered], dtype=np.float64)
    close_series = np.array([_as_float(r.get("close")) for _, r in ordered], dtype=np.float64)

    if "trend_dragon" in keys and len(ordered) >= 20:
        signal, a3 = trend_dragon(open_series, high_series, low_series, close_series)
        by_date = {
            day: (bool(hit), int(bars) if bars >= 0 else None)
            for day, hit, bars in zip(days, signal, a3, strict=False)
        }
        for row in rows:
            hit, bars = by_date.get(_date_key(row.get("date")), (False, None))
            row["td_signal"] = hit
            row["td_a3"] = bars

    if "structure" in keys and len(ordered) > STRUCTURE_LONG_SPAN:
        structure = quant_structure_main(high_series, low_series, close_series)
        for row in rows:
            position = index_of.get(_date_key(row.get("date")))
            if position is None:
                continue
            row["st_dsg"] = _finite_or_none(structure["dsg"][position])
            row["st_dxg"] = _finite_or_none(structure["dxg"][position])
            row["st_csg"] = _finite_or_none(structure["csg"][position])
            row["st_cxg"] = _finite_or_none(structure["cxg"][position])
            row["st_icon"] = int(structure["icon"][position])
            row["st_dn"] = int(structure["dn_digit"][position])
            row["st_up"] = int(structure["up_digit"][position])

    if "macd_structure" in keys and len(ordered) > MACD_SLOW_SPAN:
        quant = macd_quant_structure(close_series)
        for row in rows:
            position = index_of.get(_date_key(row.get("date")))
            if position is None:
                continue
            row["ms_diff"] = _finite_or_none(quant["diff"][position])
            row["ms_dea"] = _finite_or_none(quant["dea"][position])
            row["ms_hist"] = _finite_or_none(quant["macd"][position])
            row["ms_btext"] = int(quant["bottom_text"][position])
            row["ms_by"] = _finite_or_none(quant["bottom_y"][position])
            row["ms_ttext"] = int(quant["top_text"][position])
            row["ms_ty"] = _finite_or_none(quant["top_y"][position])

    if "capital_momentum" in keys:
        index_symbol = _benchmark_index_symbol(symbol)
        index_map: dict[str, float] = {}
        try:
            # 基准恒为 A 股指数, 故显式 cn -- 不跟随个股市场
            index_df = repo.get_daily_asset(
                "index", index_symbol, warmup_start, end, columns=["date", "close"], market="cn"
            )
            if not index_df.is_empty():
                index_map = {
                    _date_key(d): float(c)
                    for d, c in index_df.select(["date", "close"]).iter_rows()
                    if c is not None and math.isfinite(float(c)) and float(c) > 0
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("资金动能取指数 %s 失败: %s", index_symbol, exc)

        # 指数 parquet 通常落后一个交易日; 若个股已有今日蜡烛, 用实时指数补上,
        # 否则最后一根 K 线的资金动能会凭空缺失 (盘中图看起来"断了")。
        _inject_live_index_close(request, index_map, index_symbol, rows, today_key)

        close_by_date = dict(zip(days, close_series, strict=False))
        # 双方都有数据的交易日 (inner join), 与源脚本一致
        common = sorted(day for day in close_by_date if day in index_map)
        momentum_by_date: dict[str, float] = {}
        if len(common) >= CAPITAL_MOMENTUM_WINDOW:
            momentum = capital_momentum(
                np.array([close_by_date[day] for day in common], dtype=np.float64),
                np.array([index_map[day] for day in common], dtype=np.float64),
            )
            momentum_by_date = dict(zip(common, momentum, strict=False))

        for row in rows:
            value = momentum_by_date.get(_date_key(row.get("date")), math.nan)
            row["cm_value"] = round(float(value), 4) if math.isfinite(float(value)) else None

    return resp


def _inject_live_index_close(
    request: Request,
    index_map: dict[str, float],
    index_symbol: str,
    rows: list[dict],
    today_key: str,
) -> None:
    """若个股末根 K 线是今天且指数序列缺今天, 用实时指数价补齐 (原地更新 index_map)。

    只在指数确实落后时补; 指数 parquet 已有今天数据时不动, 保证口径优先级:
    落盘数据 > 实时快照。
    """
    if today_key in index_map:
        return
    if not any(_date_key(row.get("date")) == today_key for row in rows):
        return
    quote_service = getattr(request.app.state, "quote_service", None)
    if quote_service is None:
        return
    try:
        quotes = quote_service.get_index_quotes([index_symbol])
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取实时指数 %s 失败: %s", index_symbol, exc)
        return
    if quotes.is_empty() or "last_price" not in quotes.columns:
        return
    price = _as_float(quotes["last_price"][0])
    if math.isfinite(price) and price > 0:
        index_map[today_key] = price


def _maybe_inject_live_candle(
    request: Request, symbol: str, rows: list[dict], asset_type: str = "stock"
) -> list[dict]:
    """如果有当日实时 enriched 数据, 用实时数据生成今日蜡烛并追加/覆盖。

    stock 走 QuoteService 的股票实时缓存; etf 走 ETF enriched 缓存 (开启实时 ETF
    拉取时为盘中数据, 否则为磁盘最新日, 由下方"非今日不注入"守卫自然跳过)。
    """
    if asset_type == "stock":
        qs = getattr(request.app.state, "quote_service", None)
        if not qs:
            return rows
        df_today, enriched_date = qs.get_enriched_today()
    elif asset_type == "etf":
        df_today, enriched_date = request.app.state.repo.get_enriched_latest_asset("etf")
    else:
        return rows
    if df_today.is_empty():
        return rows

    # 非交易日（周末/假日）缓存的行情日期 != 今天，跳过注入避免产生重复蜡烛
    if not enriched_date or enriched_date != date.today():
        return rows

    # 查找该 symbol 的实时 enriched 行
    import polars as pl

    try:
        q = df_today.filter(pl.col("symbol") == symbol).to_dicts()
        if not q:
            return rows
        q = q[0]
    except Exception:  # noqa: BLE001
        return rows

    close_price = q.get("close")
    if not close_price or close_price <= 0:
        return rows

    today_str = str(enriched_date)

    # enriched 行已包含 OHLCV + 全套指标, 直接用它
    # 修复: API 在非交易时段可能返回 open/high/low=0, 用 close 填充避免异常蜡烛
    raw_open = q.get("open")
    raw_high = q.get("high")
    raw_low = q.get("low")
    live_row: dict = {
        "date": today_str,
        "symbol": symbol,
        "open": raw_open if raw_open and raw_open > 0 else close_price,
        "high": raw_high if raw_high and raw_high > 0 else close_price,
        "low": raw_low if raw_low and raw_low > 0 else close_price,
        "close": close_price,
        "volume": q.get("volume"),
        "amount": q.get("amount"),
        "change_pct": q.get("change_pct"),
        "is_live": True,
    }
    # 补上 enriched 的技术指标字段
    for key in (
        "ma5",
        "ma10",
        "ma20",
        "ma30",
        "ma60",
        "macd_dif",
        "macd_dea",
        "macd_hist",
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "boll_upper",
        "boll_lower",
        "rsi_6",
        "rsi_14",
        "rsi_24",
        "atr_14",
        "vol_ratio_5d",
    ):
        if key in q and q[key] is not None:
            live_row[key] = q[key]

    # 如果已有今天的 enriched 行, 覆盖; 否则追加
    found = False
    for i, r in enumerate(rows):
        if str(r.get("date")) == today_str:
            r.update(live_row)
            found = True
            break

    if not found:
        rows.append(live_row)

    return rows


class DailyBatchRequest:
    """批量日K请求。"""

    symbols: list[str]
    days: int = 12


@router.post("/daily-batch")
def get_daily_batch(request: Request, body: dict):
    """批量获取多只股票最近 N 天日K (OHLCV)。

    用于自选列表迷你蜡烛图等场景，只返回基础列，不返回全部 enriched 指标。
    """
    symbols = body.get("symbols", [])
    days = body.get("days", 12)
    if not symbols:
        return {"data": {}}
    days = max(5, min(60, days))

    repo = request.app.state.repo
    import polars as pl
    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=days * 2)  # 多取一些确保交易日够

    cols = ["symbol", "date", "open", "high", "low", "close", "volume"]

    # 按资产类型分组: stock 走批量缓存; etf/index 逐只查独立存储 (数量少, 成本可忽略)
    stock_symbols: list[str] = []
    etf_symbols: list[str] = []
    index_symbols: list[str] = []
    for s in symbols:
        t = repo.resolve_asset_type(s)
        if t == "etf":
            etf_symbols.append(s)
        elif t == "index":
            index_symbols.append(s)
        else:
            stock_symbols.append(s)

    frames: list[pl.DataFrame] = []
    if stock_symbols:
        df_stock = repo.get_daily_batch(stock_symbols, start, end, columns=cols)
        if not df_stock.is_empty():
            frames.append(df_stock)
    for sym in etf_symbols:
        sub = repo.get_etf_daily(sym, start, end, columns=cols)
        if not sub.is_empty():
            frames.append(sub)
    for sym in index_symbols:
        sub = repo.get_index_daily(sym, start, end, columns=cols)
        if not sub.is_empty():
            frames.append(sub)

    if not frames:
        return {"data": {}}
    df = pl.concat(frames, how="diagonal_relaxed")

    # 按 symbol 分组, 每只取最近 N 条。
    # partition_by 一次切分, 避免 N 只自选时对同一批数据做 N 次全帧过滤。
    result: dict[str, list[dict]] = {}
    for part in df.partition_by("symbol", maintain_order=True):
        sub = part.sort("date").tail(days)
        if not sub.is_empty():
            result[sub["symbol"][0]] = sub.to_dicts()

    return {"data": result}


@router.post("/minute-batch")
def get_minute_batch(request: Request, body: dict):
    """批量获取多只股票某天的分钟K (分时图用)。

    - 本地优先: 先从 kline_minute parquet 读, 完整的直接用
    - 缺失补拉: 本地不完整的 symbol 用 sync_minute_batch 批量实时拉 (不落库)
    - 需 Pro+ 权限 (kline.minute.batch)
    """
    from datetime import datetime
    import polars as pl
    from app.tickflow.capabilities import Cap

    symbols: list[str] = body.get("symbols", [])
    trade_date_str: str | None = body.get("date")
    if not symbols:
        return {"data": {}}

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 权限守卫: 分钟K批量是 Pro+ 能力
    if not capset.has(Cap.KLINE_MINUTE_BATCH):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (kline.minute.batch)")

    trade_date = date.fromisoformat(trade_date_str) if trade_date_str else cn_today()

    # 非交易日(周末/节假日)才回退到最近有数据的交易日; 否则盘中会显示昨天而非今天。
    # 注意: 不能用 latest_minute_date_global() 判断盘中是否为交易日 —— 批量实时补拉
    # 不落库 (见下方 sync_minute_batch 无 on_segment), 盘中它恒返回上次全量同步日,
    # 用它做判据会导致 trade_date 永久回退到昨天, 再因 expected=240 判定昨日"完整"
    # 而不再补拉今天, 形成永远显示昨日的死循环。
    # 判据改为: 周末必回退; 工作日收盘后(>=15:30)仍无今日日K → 节假日, 回退。
    if not trade_date_str:
        today = cn_today()
        need_fallback = today.weekday() >= 5  # 周六/周日必非交易日
        if not need_fallback:
            now_cn = cn_now()
            after_close = now_cn.hour > 15 or (now_cn.hour == 15 and now_cn.minute >= 30)
            if after_close:
                latest_daily = repo.latest_daily_date()
                if latest_daily is None or latest_daily < today:
                    need_fallback = True
        if need_fallback:
            recent_date = repo.latest_minute_date_global()
            if recent_date is None:
                recent_date = repo.latest_daily_date()
            if recent_date is not None:
                trade_date = recent_date

    # Step 1: 本地优先 — 一次 scan 读全部 symbol 当日分钟K (股票 / ETF 分钟数据分开存储)
    etf_set = repo.get_etf_symbol_set()
    stock_syms = [s for s in symbols if s not in etf_set]
    etf_syms = [s for s in symbols if s in etf_set]
    df_local = repo.get_minute_batch(stock_syms, trade_date)
    if etf_syms:
        df_etf = repo.get_minute_batch(etf_syms, trade_date, asset_type="etf")
        if df_local.is_empty():
            df_local = df_etf
        elif not df_etf.is_empty():
            df_local = pl.concat([df_local, df_etf], how="diagonal_relaxed")

    # 期望条数 (盘中按当前时刻估算, 盘后 240)
    now = cn_now()
    h, m = now.hour, now.minute
    if trade_date != cn_today():
        expected = 240
    elif h < 9 or (h == 9 and m < 30):
        expected = 0
    elif h < 12 or (h == 12 and m == 0):
        expected = (h - 9) * 60 + m - 30
    elif h < 13:
        expected = 120
    elif h < 15:
        expected = 120 + (h - 13) * 60 + m
    else:
        expected = 240

    # 按 symbol 分组, 判定哪些不完整需要补拉 (partition_by 一次切分, 同 daily-batch)
    result: dict[str, list[dict]] = {}
    incomplete: list[str] = []
    local_parts: dict[str, pl.DataFrame] = {}
    if not df_local.is_empty():
        for part in df_local.partition_by("symbol", maintain_order=True):
            local_parts[part["symbol"][0]] = part.sort("datetime")
    for sym in symbols:
        sub = local_parts.get(sym, pl.DataFrame())
        if expected > 0 and (sub.is_empty() or len(sub) < expected * 0.9):
            incomplete.append(sym)
        elif not sub.is_empty():
            result[sym] = sub.to_dicts()

    # Step 2: 缺失的 symbol 批量实时拉取 (不落库)
    if incomplete:
        start_time = datetime(trade_date.year, trade_date.month, trade_date.day, 9, 25, 0)
        end_time = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 5, 0)
        lim = capset.limits(Cap.KLINE_MINUTE_BATCH)
        # etf_set 已在上方获取, 直接复用 — 按 asset_type 拆分调用 sync_minute_batch
        # (自定义源 / TickFlow 路由均依赖 asset_type 正确传递)
        # 契约: 本端点只接受 stock/ETF (指数分钟K走 /api/index/minute 独立路径),
        # 故两分支已覆盖全部 incomplete。若未来放开指数支持, 需额外加 index 分支
        # 以避免被误路由为 stock。
        stock_incomplete = [s for s in incomplete if s not in etf_set]
        etf_incomplete = [s for s in incomplete if s in etf_set]
        live_parts: list[pl.DataFrame] = []
        if stock_incomplete:
            df_s = kline_sync.sync_minute_batch(
                stock_incomplete,
                start_time=start_time,
                end_time=end_time,
                batch_size=lim.batch if lim else None,
                rpm=lim.rpm if lim else None,
                asset_type="stock",
            )
            if not df_s.is_empty():
                live_parts.append(df_s)
        if etf_incomplete:
            df_e = kline_sync.sync_minute_batch(
                etf_incomplete,
                start_time=start_time,
                end_time=end_time,
                batch_size=lim.batch if lim else None,
                rpm=lim.rpm if lim else None,
                asset_type="etf",
            )
            if not df_e.is_empty():
                live_parts.append(df_e)
        if live_parts:
            live_df = pl.concat(live_parts, how="diagonal_relaxed")
            live_map: dict[str, pl.DataFrame] = {
                part["symbol"][0]: part.sort("datetime")
                for part in live_df.partition_by("symbol", maintain_order=True)
            }
            for sym in incomplete:
                sub = live_map.get(sym)
                if sub is not None and not sub.is_empty():
                    result[sym] = sub.to_dicts()

    return {"data": result}


@router.get("/minute-range")
def get_minute_range(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    days: int = Query(10, ge=1, le=20, description="最近交易日数量"),
):
    """读取单只标的最近 N 个已落库交易日的分钟 K。"""
    import polars as pl

    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    stock_info = (
        _get_stock_info(repo, symbol)
        if asset_type == "stock"
        else _get_asset_info(repo, symbol, asset_type)
    )
    base_response = {
        "symbol": symbol,
        "name": stock_info.get("name"),
        "asset_type": asset_type,
        "requested_days": days,
    }

    # 指数分钟 K 不落本地仓库, 最新分时仍由 /api/index/minute 实时读取。
    if asset_type == "index":
        return {**base_response, "sessions": [], "source": "none"}

    end = cn_today()
    start = end - timedelta(days=days * 3 + 20)
    minute = repo.get_minute_range([symbol], start, end, asset_type=asset_type)
    if minute.is_empty() or "datetime" not in minute.columns:
        return {**base_response, "sessions": [], "source": "none"}

    minute = minute.with_columns(
        pl.col("datetime").dt.date().alias("_trade_date"),
    )
    trade_dates = sorted(minute["_trade_date"].unique().to_list())[-days:]
    previous_closes = _get_previous_closes(
        repo, symbol, trade_dates, asset_type, market=market_of(symbol)
    )
    row_columns = [
        column
        for column in ("datetime", "open", "high", "low", "close", "volume", "amount")
        if column in minute.columns
    ]
    sessions = []
    for trade_date in trade_dates:
        rows = (
            minute.filter(pl.col("_trade_date") == trade_date)
            .sort("datetime")
            .select(row_columns)
            .to_dicts()
        )
        if rows:
            sessions.append(
                {
                    "date": trade_date.isoformat(),
                    "prev_close": previous_closes.get(trade_date),
                    "rows": rows,
                }
            )

    return {
        **base_response,
        "sessions": sessions,
        "source": "local" if sessions else "none",
    }


_MINUTE_PERIODS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "90m": 90, "120m": 120}


def _aggregate_minute(df, minutes: int):
    """把 1 分钟 K 聚合成 N 分钟 K, 并在聚合后**重算**技术指标。

    聚合口径(与通达信一致): 上午(09:30-11:30)与下午(13:00-15:00)**分别连续计数**
    再按 N 分钟切段。这样 30 分钟 = 8 根/日、60 分钟 = 4 根/日、120 分钟 = 2 根/日,
    与主流软件一致。90 分钟不能整除 120 分钟的半天, 按 90+30 切(半天 2 根, 全天 4 根)。

    指标必须重算: 30 分钟线的 MA20 是「20 根 30 分钟」均线, 不等于任何日线取值。
    """
    import polars as pl

    from app.indicators.pipeline import compute_indicators

    # 09:30 那根是集合竞价: 项目里它不计入日 K 成交量
    # (实测 分钟求和 - 09:30那根 == 日线 volume), 留着会让每天变成 241 根,
    # 聚合时尾部多出一根只有 1 分钟的残缺 K(30 分钟档变成 9 根/日而非 8 根)。
    d = (
        df.filter(
            ~((pl.col("datetime").dt.hour() == 9) & (pl.col("datetime").dt.minute() == 30))
        )
        .sort("datetime")
        .with_columns(
            pl.when(pl.col("datetime").dt.hour() < 12).then(0).otherwise(1).alias("_sess"),
            pl.col("datetime").dt.date().alias("_d"),
        )
    )
    d = d.with_columns((pl.int_range(pl.len()).over(["_d", "_sess"]) // minutes).alias("_g"))
    agg_exprs = [
        pl.col("datetime").last().alias("_dt"),
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
    ]
    agg_exprs.extend(
        pl.col(c).sum().alias(c) if c in ("volume", "amount") else pl.col(c).first().alias(c)
        for c in d.columns
        if c not in ("datetime", "open", "high", "low", "close", "_d", "_sess", "_g")
    )
    agg = (
        d.group_by(["_d", "_sess", "_g"])
        .agg(agg_exprs)
        .sort(["_d", "_sess", "_g"])
        .with_row_index("_i")
    )
    base = agg.select([
        "_i",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        pl.col("_dt").dt.date().alias("date"),
    ])
    out = compute_indicators(base, assume_sorted=True)
    # 指标管线不产出的列(如 amount)按行号补回; 行号而不是 date 做 key,
    # 因为同一天有多根分钟 K, 用 date join 会笛卡尔爆炸。
    extra = [c for c in agg.columns if c not in out.columns]
    if extra:
        out = out.join(agg.select(["_i", *extra]), on="_i", how="left")
    # 输出用「分钟时间戳」当 x 轴标签(日线用的是 date), 故丢掉临时的 date 列
    return out.sort("_i").drop("date", "_i").rename({"_dt": "date"})


def _finalize_preagg(df):
    """预聚合多周期K收尾: 补行号/日期后**重算指标**, 与 _aggregate_minute 同口径。

    预聚合数据本身已是目标周期(如 30m), 不需要再切段, 但指标必须重算:
    30 分钟线的 MA20 是「20 根 30 分钟」均线, 不等于任何日线取值。
    """
    import polars as pl

    from app.indicators.pipeline import compute_indicators

    d = df.sort("datetime").with_row_index("_i").with_columns(
        pl.col("datetime").dt.date().alias("_day")
    )
    base = d.select([
        "_i",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        pl.col("_day").alias("date"),
    ])
    out = compute_indicators(base, assume_sorted=True)
    # 指标管线不产出的列(如 amount/datetime)按行号补回, 用行号而非 date 做 key
    # (同一天有多根分钟K, 用 date join 会笛卡尔爆炸)
    extra = [c for c in d.columns if c not in out.columns and c not in ("_i", "_day")]
    if extra:
        out = out.join(d.select(["_i", *extra]), on="_i", how="left")
    return out.sort("_i").drop(["date", "_i"]).rename({"datetime": "date"})


@router.get("/minute-k")
def get_minute_k(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    period: str = Query("30m", description="分钟周期: 1m/5m/15m/30m/60m/90m/120m"),
    days: int = Query(120, ge=1, le=400, description="读取最近 N 个交易日的分钟数据"),
    fields: str | None = Query(None, description="逗号分隔的列名白名单"),
    limit: int = Query(0, ge=0, le=20000, description="只返回最近 N 根 K (0=不限)"),
):
    """多分钟周期 K 线 (5/15/30/60/90/120 分钟)。

    数据源两级, 响应里的 source 字段标明实际走了哪条:
    - ``preagg``: 预聚合多周期K (free-stockdb 导入的 5m/15m/30m/60m 目录),
      可回溯到 2025-01 起约 423 个交易日 —— 长周期历史只能靠它。
    - ``local``: 由本地 1 分钟 K 现场聚合。1m 表只覆盖最近 2 天 (腾讯 mkline 限制),
      故仅用于 1m/90m/120m, 或预聚合目录缺该标的时回退。

    ``limit`` 只截断**返回**的行数(取最近 N 根), 指标仍在完整数据上算好之后再截,
    所以 MA60 之类的前置窗口不会因为截断而失真。图只需要最近若干根时用它省流量:
    5m/120 交易日实测 8640 根 ≈ 1MB, 截断到 800 根后 ≈ 80KB。
    """
    minutes = _MINUTE_PERIODS.get(period)
    if minutes is None:
        raise HTTPException(
            status_code=400,
            detail="unsupported period: {} (可选 {})".format(period, ",".join(_MINUTE_PERIODS)),
        )
    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    stock_info = (
        _get_stock_info(repo, symbol)
        if asset_type == "stock"
        else _get_asset_info(repo, symbol, asset_type)
    )
    end = cn_today()
    start = end - timedelta(days=days * 2 + 30)
    base_resp = {
        "symbol": symbol,
        "name": stock_info.get("name"),
        "period": period,
        "source": "none",
        "rows": [],
    }

    # 数据源两级:
    #   1) 预聚合多周期K (source=preagg): free-stockdb 导入的 5m/15m/30m/60m 目录,
    #      可回溯到 2025-01, 是长周期历史的唯一来源 (1m 表只有最近 2 天)。
    #   2) 1m 现场聚合 (source=local): 用于 1m/90m/120m, 以及预聚合缺失时的回退。
    source = "none"
    agg = None
    if asset_type == "stock":
        pre = repo.get_minute_period([symbol], period, start, end)
        if not pre.is_empty() and "datetime" in pre.columns:
            agg = _finalize_preagg(pre)
            source = "preagg"
    if agg is None or agg.is_empty():
        minute = repo.get_minute_range([symbol], start, end, asset_type=asset_type)
        if minute.is_empty() or "datetime" not in minute.columns:
            return _conditional_json(request, base_resp)
        agg = _aggregate_minute(minute, minutes)
        source = "local"
    if agg.is_empty():
        return _conditional_json(request, base_resp)
    if limit and agg.height > limit:
        # 尾部截断: 指标已在完整数据上算完(见 docstring), 这里只省传输量
        agg = agg.tail(limit)
    return _conditional_json(
        request,
        {**base_resp, "source": source, "rows": _select_fields_df(agg, fields).to_dicts()},
    )


@router.get("/minute")
def get_minute(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    trade_date: date | None = Query(None, alias="date", description="交易日期, 默认最新"),
):
    """读取某只股票某天的分钟 K 线。

    - 本地有完整数据(240条) → 直接返回
    - 本地无数据或不完整 → 从 TickFlow 实时拉取返回（不写入）
    """
    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    market = market_of(symbol)
    stock_info = (
        _get_stock_info(repo, symbol)
        if asset_type == "stock"
        else _get_asset_info(repo, symbol, asset_type)
    )
    stock_name = stock_info.get("name")

    if trade_date is None:
        # 默认看今天, 而不是本地落盘的最近日 (盘中后者是昨天)。
        # 非交易日(周末/节假日)才回退到本地最近有数据的交易日。
        today = cn_today()
        need_fallback = today.weekday() >= 5  # 周六/周日必非交易日
        if not need_fallback:
            now_cn = cn_now()
            after_close = now_cn.hour > 15 or (now_cn.hour == 15 and now_cn.minute >= 30)
            if after_close:
                latest_daily = repo.latest_daily_date()
                if latest_daily is None or latest_daily < today:
                    need_fallback = True
        if need_fallback:
            recent = repo.latest_minute_date(symbol, asset_type=asset_type)
            if recent is None:
                recent = repo.latest_daily_date()
            trade_date = recent if recent is not None else today
        else:
            trade_date = today
    if trade_date is None:
        # 本地无任何分钟K，尝试从 TickFlow 拉取当天
        trade_date = cn_today()
        df = kline_sync.fetch_minute_single(symbol, trade_date, asset_type=asset_type)
        price_limit = _get_price_limit_info(
            repo,
            symbol,
            trade_date,
            asset_type,
            stock_name,
        )
        _pc_map, _pc_src = _prev_close_with_fallback(
            repo,
            symbol,
            [trade_date],
            asset_type,
            market=market,
        )
        prev_close = _pc_map.get(trade_date)
        return {
            "symbol": symbol,
            "name": stock_name,
            "stock_info": stock_info,
            "date": str(trade_date),
            "rows": df.to_dicts(),
            "source": "live",
            "asset_type": asset_type,
            "price_limit": price_limit,
            "prev_close": prev_close,
            "prev_close_source": _pc_src.get(trade_date, "none"),
        }

    _pc_map, _pc_src = _prev_close_with_fallback(
        repo,
        symbol,
        [trade_date],
        asset_type,
        market=market,
    )
    prev_close = _pc_map.get(trade_date)
    price_limit = _get_price_limit_info(
        repo,
        symbol,
        trade_date,
        asset_type,
        stock_name,
    )
    df = repo.get_minute(symbol, trade_date, asset_type=asset_type)

    # 完整交易日应有 240 条分钟K；如果是今天(盘中)，期望条数按已交易分钟估算
    expected = 240
    today = cn_today()
    if trade_date == today:
        now = cn_now()
        h, m = now.hour, now.minute
        if h < 9 or (h == 9 and m < 30):
            expected = 0  # 还没开盘
        elif h < 12 or (h == 12 and m == 0):
            expected = (h - 9) * 60 + m - 30  # 9:30 起
        elif h < 13:
            expected = 120  # 午休
        elif h < 15:
            expected = 120 + (h - 13) * 60 + m
        else:
            expected = 240

    is_complete = not df.is_empty() and len(df) >= expected * 0.9  # 允许 10% 容差

    if is_complete:
        return {
            "symbol": symbol,
            "name": stock_name,
            "stock_info": stock_info,
            "date": str(trade_date),
            "rows": df.to_dicts(),
            "source": "local",
            "asset_type": asset_type,
            "price_limit": price_limit,
            "prev_close": prev_close,
            "prev_close_source": _pc_src.get(trade_date, "none"),
        }

    # 本地不完整或无数据 → 从 TickFlow 实时拉取
    live_df = kline_sync.fetch_minute_single(symbol, trade_date, asset_type=asset_type)
    return {
        "symbol": symbol,
        "name": stock_name,
        "stock_info": stock_info,
        "date": str(trade_date),
        "rows": live_df.to_dicts(),
        "source": "live" if not live_df.is_empty() else "none",
        "asset_type": asset_type,
        "price_limit": price_limit,
        "prev_close": prev_close,
        "prev_close_source": _pc_src.get(trade_date, "none"),
    }


@router.post("/sync")
def sync_symbol(
    request: Request,
    symbol: str = Query(...),
    days: int = Query(250, ge=10, le=2000),
):
    """手动触发单股同步(Free 用户在 K 线页用)。"""
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    n = kline_sync.sync_and_persist_daily_batch([symbol], repo, capset, count=days)
    return {"symbol": symbol, "rows_written": n}


@router.post("/sync_batch")
def sync_batch(
    request: Request,
    symbols: list[str],
    days: int = Query(250, ge=10, le=2000),
):
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    n = kline_sync.sync_and_persist_daily_batch(symbols, repo, capset, count=days)
    return {"symbols": symbols, "rows_written": n}


@router.post("/refresh_views")
def refresh_views(request: Request):
    """刷新所有 DuckDB 视图(解决视图状态不一致问题)。"""
    from app.jobs.daily_pipeline import _refresh_views

    repo = request.app.state.repo
    _refresh_views(repo)
    return {"status": "ok"}


@router.post("/sync_minute")
async def sync_minute(request: Request):
    """手动触发分钟 K 同步(全市场)。返回 pipeline job_id 可轮询进度。

    body 可选: { "days": int } — 指定拉取天数 (不传则用偏好设置)。
    """
    import asyncio

    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        try_acquire_run_slot,
    )
    from app.api.data import invalidate_storage_cache
    from app.services.preferences import get_minute_sync_days
    from app.tickflow.capabilities import Cap
    from app.tickflow.pools import get_pool

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    if not _minute_allowed(capset):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限")

    # 可选 body: { "days": int, "extend": bool }
    # days: 拉取天数; extend: 向前扩展模式 (从最早数据往前补)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    override_days = body.get("days")
    extend_flag = body.get("extend")

    # 分钟K全市场同步是长任务(数据量是日K的 ~240 倍),用更宽松的卡死阈值
    job_id, is_new = job_store.create(long_running=True)
    if not is_new:
        return {"status": "reused", "job_id": job_id}

    async def task() -> None:
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        loop = asyncio.get_event_loop()

        def progress(stage: str, pct: int, msg: str) -> None:
            job_store.progress(job_id, stage, pct, msg)

        try:
            job_store.start(job_id)
            progress("sync_minute", 5, "解析标的池…")
            universe = sorted(set(get_pool("watchlist")) | set(get_pool("CN_Equity_A")))
            # 补充 instruments 全量标的，覆盖北交所、新股等
            inst_path = repo.store.data_dir / "instruments" / "instruments.parquet"
            if inst_path.exists():
                try:
                    import polars as pl

                    inst = pl.read_parquet(inst_path, columns=["symbol"])
                    universe = sorted(set(universe) | set(inst["symbol"].to_list()))
                except Exception:  # noqa: BLE001
                    pass
            # 剔除指数 symbol: 指数分钟K无本地存储, 落库会污染 kline_minute
            index_set = repo.get_index_symbol_set()
            universe = [s for s in universe if s not in index_set]
            # 只同步 A 股: instruments 全量里 73% 是美股(.US)/港股(.HK),
            # 数据源对非 A 股返回空, 请求了也不入库, 白白多花 3.76 倍时间
            from app.jobs.daily_pipeline import _CN_SYMBOL_SUFFIXES, apply_minute_scope

            universe = [s for s in universe if str(s).upper().endswith(_CN_SYMBOL_SUFFIXES)]
            # 再按 minute_sync_scope 收窄到核心池(可选, 默认全量)
            universe = apply_minute_scope(universe, repo)
            progress("sync_minute", 10, f"标的池 {len(universe)} 只")

            days = override_days if override_days else get_minute_sync_days()
            # extend=1 → 向前扩展; days>=365 也自动向前扩展
            extend_backward = bool(extend_flag) or days >= 365

            def _on_chunk(done: int, total: int, seg_label: str) -> None:
                # 进度映射: 10% (标的池解析完) → 95%, 留 5% 给写入+刷新
                pct = 10 + int((done / max(total, 1)) * 85)
                progress("sync_minute", pct, f"拉取分钟K… {done}/{total} 批 [{seg_label}]")

            def _run():
                return kline_sync.sync_and_persist_minute(
                    universe,
                    repo,
                    capset,
                    days=days,
                    extend_backward=extend_backward,
                    on_chunk_done=_on_chunk,
                )

            written = await loop.run_in_executor(_long_task_executor, _run)

            # 刷新视图
            from app.jobs.daily_pipeline import _refresh_single_view

            _refresh_single_view(repo, "kline_minute")

            progress("done", 100, f"分钟 K 同步完成,{written} 行")
            job_store.succeed(job_id, {"minute_rows": written, "universe_size": len(universe)})
            invalidate_storage_cache()
        except JobCancelledError:
            # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
            invalidate_storage_cache()
        except Exception as e:  # noqa: BLE001
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    asyncio.create_task(task())
    return {"status": "started", "job_id": job_id}


@router.post("/sync_minute_single")
async def sync_minute_single(request: Request, body: dict):
    """手动拉取单只股票的分钟K并落库 (前复权)。

    body: { "symbol": "000001.SZ" }
    用于个股分时图"获取数据"按钮: 本地无数据时单独拉取并持久化。
    """
    import asyncio

    from app.services.preferences import get_minute_sync_days

    symbol = body.get("symbol", "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    requested_days = body.get("days")
    if requested_days is not None:
        if isinstance(requested_days, bool) or not isinstance(requested_days, int):
            raise HTTPException(status_code=400, detail="days 必须是整数")
        if requested_days < 1 or requested_days > 30:
            raise HTTPException(status_code=400, detail="days 必须在 1 到 30 之间")

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 指数分钟K无本地存储, 落库会污染股票分钟表 kline_minute;
    # 指数分钟数据走 /api/index/minute 实时读取, 此端点显式拒绝。
    if repo.resolve_asset_type(symbol) == "index":
        raise HTTPException(
            status_code=400,
            detail="指数分钟K不支持落库同步 (指数分钟数据走 /api/index/minute 实时读取)",
        )

    if not _minute_allowed(capset):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限")

    days = requested_days if requested_days is not None else get_minute_sync_days()
    loop = asyncio.get_event_loop()

    def _run():
        return kline_sync.sync_and_persist_minute(
            [symbol], repo, capset, days=days, force_full_days=True
        )

    written = await loop.run_in_executor(_long_task_executor, _run)

    # 刷新视图
    from app.jobs.daily_pipeline import _refresh_single_view

    _refresh_single_view(repo, "kline_minute")

    return {"status": "ok", "symbol": symbol, "rows": written}


@router.post("/clear_minute")
async def clear_minute(request: Request):
    """清空全部分钟K数据 (仅 kline_minute, 不影响其他数据)。

    删除 data/kline_minute/ 下所有分区 parquet, 刷新视图。
    需二次确认: body { "confirm": true }。
    """
    import shutil

    body = await request.json() if request.method == "POST" else {}
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="需传 confirm: true 以确认清空")

    repo = request.app.state.repo
    minute_dir = repo.store.data_dir / "kline_minute"

    # 统计待删除行数 (用于返回)
    removed = 0
    if minute_dir.exists():
        try:
            result = repo.db.execute("SELECT COUNT(*) AS cnt FROM kline_minute").fetchone()
            removed = result[0] if result else 0
        except Exception:  # noqa: BLE001
            pass
        # 仅删 kline_minute 目录, 绝不触碰其他目录
        shutil.rmtree(minute_dir, ignore_errors=True)

    # 刷新视图 (重建空视图)
    from app.jobs.daily_pipeline import _refresh_single_view

    _refresh_single_view(repo, "kline_minute")

    from app.api.data import invalidate_storage_cache

    invalidate_storage_cache()

    logger.info("minute K cleared: %d rows removed", removed)
    return {"status": "ok", "removed": removed}


@router.post("/extend_history")
async def extend_history(request: Request):
    """向前扩展历史日K数据 — 独立于盘后管道。

    body: { "value": int, "unit": "day"|"month"|"year" }
    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    import traceback as _tb

    try:
        body = await request.json()
        value = body.get("value")
        unit = body.get("unit", "month")
        if not value or value <= 0:
            raise HTTPException(status_code=400, detail="value 必须为正整数")
        if unit not in ("day", "month", "year"):
            raise HTTPException(status_code=400, detail="unit 只支持 day/month/year")

        repo = request.app.state.repo
        capset = request.app.state.capabilities

        from app.tickflow.capabilities import Cap

        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")

        from app.services.extend_history import run_extend_history
        from app.services.pipeline_jobs import (
            JobCancelledError,
            job_store,
            release_run_slot,
            try_acquire_run_slot,
        )
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()

            def progress(
                stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False
            ) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            try:
                job_store.start(job_id)
                result = await loop.run_in_executor(
                    _long_task_executor,
                    lambda: run_extend_history(repo, capset, value, unit, on_progress=progress),
                )
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("extend_history failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("extend_history error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/repair_daily")
async def repair_daily(request: Request):
    """修正 / 补全日K数据 — 从指定起始日期重拉到今天。

    典型场景: 昨天没看盘 / 服务挂了,本地日K缺了若干天。
    用户选起始日期,复用盘后管道全流程重拉 [start_date ~ 今天]。

    body: { "start_date": "YYYY-MM-DD" }
    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    import traceback as _tb
    from datetime import date as _date

    try:
        body = await request.json()
        raw = body.get("start_date")
        if not raw:
            raise HTTPException(status_code=400, detail="start_date 必填 (YYYY-MM-DD)")
        try:
            start_date = _date.fromisoformat(str(raw))
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date 格式错误 (应为 YYYY-MM-DD)")

        if start_date > _date.today():
            raise HTTPException(status_code=400, detail="起始日期不能晚于今天")

        repo = request.app.state.repo
        capset = request.app.state.capabilities

        from app.tickflow.capabilities import Cap

        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")

        from app.services.repair_daily import run_repair_daily
        from app.services.pipeline_jobs import (
            JobCancelledError,
            job_store,
            release_run_slot,
            try_acquire_run_slot,
        )
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()
            qs = getattr(request.app.state, "quote_service", None)

            def progress(
                stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False
            ) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                # 修正运行期间暂停实时行情, 防止覆写同一批 parquet 竞态
                if qs:
                    with qs.paused():
                        return run_repair_daily(repo, capset, start_date, on_progress=progress)
                return run_repair_daily(repo, capset, start_date, on_progress=progress)

            try:
                job_store.start(job_id)
                result = await loop.run_in_executor(_long_task_executor, _run)
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("repair_daily failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("repair_daily error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/rebuild_enriched")
async def rebuild_enriched(request: Request):
    """全量重算 enriched 表 — 不获取任何数据,仅基于已有 kline_daily + adj_factor 重算复权+指标。

    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio

    try:
        repo = request.app.state.repo

        from app.services.pipeline_jobs import (
            JobCancelledError,
            job_store,
            release_run_slot,
            try_acquire_run_slot,
        )
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()

            def progress(
                stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False
            ) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            try:
                job_store.start(job_id)
                progress("rebuild_enriched", 10, "全量计算 enriched…")
                from app.indicators.pipeline import run_pipeline

                def _batch_progress(cur: int, tot: int) -> None:
                    pct = 10 + int(85 * cur / tot)
                    progress(
                        "rebuild_enriched",
                        pct,
                        f"计算指标 批次 {cur}/{tot}",
                        stage_pct=int(100 * cur / tot),
                        skip_log=True,
                    )

                written = await loop.run_in_executor(
                    _long_task_executor,
                    lambda: run_pipeline(on_batch_done=_batch_progress),
                )

                enriched_dir = repo.store.data_dir / "kline_daily_enriched"
                enriched_days = (
                    len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0
                )

                # 刷新视图
                d = repo.store.data_dir.as_posix()
                for view_name, glob in [
                    ("kline_enriched", f"{d}/kline_daily_enriched/**/*.parquet"),
                ]:
                    try:
                        repo.db.execute(
                            f"CREATE OR REPLACE VIEW {view_name} AS "
                            f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
                        )
                    except Exception:
                        pass

                progress("rebuild_enriched", 100, f"完成,覆盖 {enriched_days} 天")
                job_store.succeed(
                    job_id,
                    {
                        "enriched_days": enriched_days,
                        "enriched_rows": written,
                    },
                )
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("rebuild_enriched failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except Exception as e:
        import traceback as _tb

        logger.error("rebuild_enriched error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
import concurrent.futures as _cf

_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")
