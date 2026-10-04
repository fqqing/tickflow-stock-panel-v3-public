"""Screener API。"""
from __future__ import annotations

import glob as _glob
import logging
import math
import os
import re
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db_safe import is_valid_ext_ident, quote_ident
from app.services import strategy_cache
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screener", tags=["screener"])


class CustomRequest(BaseModel):
    conditions: list[str]
    order_by: Optional[str] = None
    limit: int = 30
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"


class PresetRequest(BaseModel):
    strategy_id: str
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"
    timeframe: str = "1d"
    market: str = "cn"   # cn | hk | us（多市场扩展）


# 港美股不适用的策略：依赖涨跌停/连板信号（A 股专属概念）
_LIMIT_DEPENDENT_SIGNALS = {
    "signal_limit_up", "signal_limit_down", "signal_broken_limit_up",
    "signal_limit_down_recovery",
}

# 港美股「接近 60 日新高/新低」的容差: 收盘价触及 60 日极值的 99.5% / 100.5%
# 即算突破。提为常量而非散落字面量 —— 同一语义在新高计数与状态判定两处使用,
# 分别硬编码会在调参时静默分叉。
_NEAR_HIGH_RATIO = 0.995
_NEAR_LOW_RATIO = 1.005


def _market_compatible_strategy(meta: dict, market: str) -> bool:
    """策略是否适用于目标市场。港美股跳过依赖涨停/连板信号的策略。"""
    if market == "cn":
        return True
    entry = {s for s in (meta.get("entry_signals") or [])}
    exit_ = {s for s in (meta.get("exit_signals") or [])}
    signals = entry | exit_ | {a.get("field") for a in (meta.get("alerts") or []) if isinstance(a, dict)}
    return not bool(signals & _LIMIT_DEPENDENT_SIGNALS)


def _safe(result_dict: dict) -> dict:
    """sanitize for JSON(NaN / Inf → None)."""
    rows = result_dict.get("rows", [])
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return result_dict


def _one_word_limit_expr(status_main: str, columns: list[str]) -> Any:
    required = {"open", "high", "low", "close", "status"}
    if not required.issubset(columns):
        import polars as pl
        return pl.lit(False)

    import polars as pl
    return (
        (pl.col("status") == status_main)
        & (pl.col("close") > 0)
        & (pl.col("open") == pl.col("high"))
        & (pl.col("high") == pl.col("low"))
        & (pl.col("low") == pl.col("close"))
    ).fill_null(False)


def _safe_ext_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


# 标识符安全原语 (转义 + 白名单) 集中在 app.db_safe, 见 Issue #150 注入防护。


# ── 扩展列 value_map 缓存 ────────────────────────────────────────────
# 每次请求 _load_ext_value_maps 都会重新从磁盘读 ext parquet 并重建 {symbol: value}。
# 用底层 parquet 文件的 (路径, mtime) 签名做 memoize: 文件未变则复用上次的 map,
# parquet 被重写 (mtime 变化) 时自动失效重算。仅缓存基于 config 的快照/时序路径,
# 无 config 的 DuckDB view 回退路径不缓存 (少见)。
_ext_value_map_cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}


def _ext_parquet_signature(cfg, data_dir) -> Optional[tuple]:
    """该扩展配置底层 parquet 文件的 (路径, mtime) 签名; 出错返回 None (禁用缓存)。"""
    try:
        from app.api.ext_data import _parquet_glob
        pattern = _parquet_glob(cfg, data_dir)
        files = sorted(_glob.glob(pattern, recursive=True))
        if not files:
            return None
        return tuple((f, os.path.getmtime(f)) for f in files)
    except Exception:  # noqa: BLE001
        return None


def _load_ext_value_maps(repo, ext_columns: Optional[str]) -> dict[str, dict[str, Any]]:
    """按请求加载扩展列，返回 {输出列名: {symbol: value}}。

    策略结果缓存是共享文件，不能被不同 ext_columns 组合污染；因此扩展列只在
    返回前通过该投影映射追加到结果副本中。

    基于 config 的路径按 parquet 文件 mtime 签名 memoize, 文件未变时跳过磁盘重读。
    """
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if not ext_specs:
        return {}

    import polars as pl

    from app.api.ext_data import _read_ext_dataframe
    from app.services.ext_data import ExtConfigStore

    db = repo.store.db
    data_dir = repo.store.data_dir
    ext_store = ExtConfigStore(data_dir)
    configs = {c.id: c for c in ext_store.load_all()}
    value_maps: dict[str, dict[str, Any]] = {}

    for config_id, field_name in ext_specs:
        out_col = f"{config_id}__{field_name}"
        cfg = configs.get(config_id)
        cache_key = (config_id, field_name)
        sig = _ext_parquet_signature(cfg, data_dir) if cfg else None
        try:
            if cfg:
                # 命中缓存 (文件签名一致) → 复用, 免去磁盘重读
                cached = _ext_value_map_cache.get(cache_key)
                if cached is not None and sig is not None and cached[0] == sig:
                    value_maps[out_col] = cached[1]
                    continue
                # 时序扩展表只取最新分区，避免历史分区把同一 symbol JOIN 放大。
                ext_df, _ = _read_ext_dataframe(cfg, data_dir)
            else:
                view_name = f"ext_{config_id}"
                ext_df = pl.from_arrow(db.query(
                    f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                ).arrow())

            if ext_df.is_empty() or "symbol" not in ext_df.columns or field_name not in ext_df.columns:
                continue

            ext_df = ext_df.select(["symbol", field_name]).unique(subset=["symbol"], keep="last")
            vmap = {
                str(row["symbol"]): _safe_ext_value(row.get(field_name))
                for row in ext_df.to_dicts()
                if row.get("symbol")
            }
            value_maps[out_col] = vmap
            if cfg and sig is not None:
                _ext_value_map_cache[cache_key] = (sig, vmap)
        except Exception as e:  # noqa: BLE001
            logger.debug("screener ext column join skipped for %s.%s: %s", config_id, field_name, e)

    return value_maps


def _row_with_ext(row: dict, ext_values: dict[str, dict[str, Any]], symbol: Optional[str] = None) -> dict:
    next_row = dict(row)
    sym = symbol or next_row.get("symbol")
    for out_col, value_map in ext_values.items():
        next_row[out_col] = value_map.get(str(sym)) if sym else None
    return next_row


def _rows_with_ext(rows: list[dict], ext_values: dict[str, dict[str, Any]]) -> list[dict]:
    if not ext_values:
        return rows
    return [_row_with_ext(r, ext_values) for r in rows]


def _result_with_ext(result_dict: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return result_dict
    return {**result_dict, "rows": _rows_with_ext(result_dict.get("rows", []), ext_values)}


def _results_with_ext(results: dict[str, dict], ext_values: dict[str, dict[str, Any]]) -> dict[str, dict]:
    if not ext_values:
        return results
    return {sid: _result_with_ext(r, ext_values) for sid, r in results.items()}


def _cache_payload_with_ext(cached: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return cached

    payload = dict(cached)
    payload["results"] = _results_with_ext(cached.get("results", {}), ext_values)

    ever_rows = cached.get("today_ever_rows")
    if isinstance(ever_rows, dict):
        enriched_ever: dict[str, dict[str, dict]] = {}
        for sid, sym_map in ever_rows.items():
            if not isinstance(sym_map, dict):
                continue
            enriched_ever[sid] = {
                sym: _row_with_ext(row, ext_values, symbol=sym)
                for sym, row in sym_map.items()
                if isinstance(row, dict)
            }
        payload["today_ever_rows"] = enriched_ever

    return payload


def _update_cache_strategy(data_dir, as_of: str, strategy_id: str, safe_data: dict,
                           market: str = "cn") -> None:
    """单跑后更新缓存中该策略的结果，保持缓存与最新计算一致。"""
    from app.services import strategy_cache
    # 按日期读: 该日期没有缓存时返回 None, 与原 as_of 比对逻辑等价
    cached = strategy_cache.read_cache(data_dir, market, as_of)
    if cached is not None:
        results = cached.get("results", {})
        results[strategy_id] = {
            "total": safe_data.get("total", 0),
            "as_of": as_of,
            "rows": safe_data.get("rows", []),
        }
        strategy_cache.write_cache(data_dir, as_of, results, market)


@router.get("/strategies")
def strategies(
    request: Request,
    asset_type: str = Query("stock"),
    timeframe: str = Query("1d"),
    market: str = Query("cn", description="cn|hk|us"),
):
    """兼容策略清单端点；唯一数据源为 StrategyEngine。"""
    data_dir = request.app.state.repo.store.data_dir
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")
    presets = []
    for meta in engine.list_strategies():
        if meta.get("research_only"):
            continue
        if asset_type not in meta.get("asset_types", ["stock"]):
            continue
        if timeframe not in meta.get("timeframes", ["1d"]):
            continue
        if not _market_compatible_strategy(meta, market):
            continue
        sid = meta["id"]
        overrides = strategy_config.load_override(data_dir, sid)
        presets.append({
            **meta,
            "name": overrides.get("name") or meta["name"],
            "description": overrides.get("description") or meta.get("description", ""),
        })

    return {"presets": presets, "load_errors": engine.load_errors()}


@router.post("/run")
def run_custom(req: CustomRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400,
                            detail="无可用数据日期 — enriched 表为空,请先运行盘后管道")
    result = svc.run(
        as_of=as_of,
        conditions=req.conditions,
        order_by=req.order_by,
        limit=req.limit,
        pool=req.pool,
    )
    safe_data = _safe(asdict(result))
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    return _result_with_ext(safe_data, ext_values)


@router.post("/run_preset")
def run_preset(req: PresetRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type, market=req.market)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")    # 加载用户保存的策略配置
    data_dir = request.app.state.repo.store.data_dir
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    overrides = strategy_config.load_override(data_dir, req.strategy_id)
    engine = getattr(request.app.state, "strategy_engine", None)
    if not engine:
        raise HTTPException(status_code=404, detail=f"策略引擎未初始化或策略 {req.strategy_id} 不存在")

    try:
        if not engine.has(req.strategy_id):
            raise ValueError(f"unknown strategy: {req.strategy_id}")
        meta = engine.get(req.strategy_id).meta
        if meta.get("research_only"):
            raise ValueError(f"unknown strategy: {req.strategy_id}")
        # 港美股跳过依赖涨跌停/连板信号的策略
        if req.market in ("hk", "us") and not _market_compatible_strategy(meta, req.market):
            raise ValueError(
                f"strategy {req.strategy_id} 依赖涨停/连板信号,不适用于{req.market.upper()}市场"
            )
        params = dict(overrides.get("params") or {})
        context = svc.build_strategy_context(
            engine,
            as_of,
            [req.strategy_id],
            timeframe=req.timeframe,
            params_map={req.strategy_id: params},
            overrides_map={req.strategy_id: overrides or {}},
        )
        result = engine.run(
            req.strategy_id,
            context,
            pool=req.pool,
            params=params,
            overrides=overrides or None,
        )
    except ValueError as e:
        status_code = 404 if "unknown strategy" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e)) from e

    safe_data = _safe(asdict(result))
    _update_cache_strategy(data_dir, str(as_of), req.strategy_id, safe_data, req.market)

    return _result_with_ext(safe_data, ext_values)


def _as_query_str(value: Any) -> str | None:
    """规范化 Optional 查询参数。

    直接调用端点函数时 FastAPI 不会注入 Query 默认值, 形参拿到的是 Query 对象本身,
    str() 会得到 "Query(None)" 这类无意义字符串 —— 于是 None 判断失效、查库路径跑偏。
    真实请求下这里永远是 str 或 None, 所以按类型收口即可。
    """
    return value if isinstance(value, str) and value else None


def _cached_with_realtime(request: Request, market: str = "cn", as_of: str | None = None) -> dict:
    """读取指定日期的盘后缓存，并用监控引擎的实时结果覆盖同策略。

    实时结果仅由 A 股监控引擎产出, 港美股只返回盘后缓存(不叠加实时)。
    as_of=None 时取最近写入的日期 (旧行为)。
    """
    data_dir = request.app.state.repo.store.data_dir
    cached = strategy_cache.read_cache(data_dir, market, as_of)
    if cached is None:
        cached = {"as_of": None, "results": {}, "updated_at": None}

    # 叠加监控引擎内存里的实时结果 (若有), 用新鲜数据覆盖同策略的盘后结果
    # 仅 A 股: 监控引擎的实时结果基于 A 股实时行情算出, 叠加到港美股缓存上会
    # 把 A 股个股混进港美股结果里。
    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    if market == "cn" and monitor_engine is not None:
        realtime_results = monitor_engine.latest_strategy_results()
        # 实时结果的 as_of 恒为「今天」, 与历史日期槽对不上 —— 请求历史日期时只
        # 叠加同日的条目, 否则今天的实时命中会混进用户正在看的历史日期结果里。
        if realtime_results and as_of is not None:
            realtime_results = {
                sid: item
                for sid, item in realtime_results.items()
                if isinstance(item, dict) and str(item.get("as_of")) == as_of
            }
        if realtime_results:
            results = dict(cached.get("results") or {})
            results.update(realtime_results)
            cached = dict(cached)
            cached["results"] = results
            # 有实时数据时, 以最新时间戳为准
            import time as _time
            cached["updated_at"] = int(_time.time() * 1000)

    return cached


@router.get("/cached")
def get_cached(
    request: Request,
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
    market: str = Query("cn", description="cn|hk|us"),
    date: Optional[str] = Query(None, description="YYYY-MM-DD, 省略时取最近写入的日期"),
):
    """读取指定日期的策略结果缓存, 并叠加监控引擎本轮实时算出的结果。

    - 盘后缓存 (strategy_cache.json): 非监控策略 / 页面秒加载用, run_all 写入。
      每个交易日期独立成槽, 切回已算过的日期直接命中, 不重算。
    - 监控引擎内存结果 (latest_strategy_results): 实时行情每轮对「加入监控的策略」算出,
      不落盘 (避免与 read_cache 的 mtime 校验冲突), 在此直接叠加覆盖盘后结果。
      被监控的策略拿到新鲜数据, 非监控策略仍用盘后缓存。
    - 缓存按 market 隔离 (A 股沿用原文件名, 港美股各自独立文件)。
    - market 未显式传参时可能是 Query 默认对象而非字符串, 先规范化再做比较。
    """
    market = str(market) or "cn"
    as_of = _as_query_str(date)
    cached = _cached_with_realtime(request, market, as_of)

    # 无任何数据 (盘后缓存空 + 无实时结果) → 返回空标记, 前端据此提示
    if not cached.get("results") and cached.get("as_of") is None:
        return {"as_of": None, "results": {}, "updated_at": None}

    ext_values = _load_ext_value_maps(request.app.state.repo, ext_columns)
    return _cache_payload_with_ext(cached, ext_values)


@router.get("/cached-summary")
def get_cached_summary(
    request: Request,
    market: str = Query("cn", description="cn|hk|us"),
    date: Optional[str] = Query(None, description="YYYY-MM-DD, 省略时取最近写入的日期"),
):
    """返回指定日期策略卡片所需的轻量摘要，不序列化股票明细。

    前端据 results[sid].as_of 与当前选中日期比对来判断哪些策略还没算过,
    因此这里必须只回该日期的槽, 混入其他日期会让「未算过」被判成「已算过」。
    """
    market = str(market) or "cn"
    as_of = _as_query_str(date)
    cached = _cached_with_realtime(request, market, as_of)
    results = cached.get("results") or {}
    summary = {
        sid: {
            "total": int(result.get("total") or 0),
            "as_of": result.get("as_of"),
        }
        for sid, result in results.items()
        if isinstance(result, dict)
    }

    cached_as_of = cached.get("as_of")
    ever_rows = cached.get("today_ever_rows") or {}
    ever_counts = {}
    for sid, result in results.items():
        if not isinstance(result, dict) or result.get("as_of") != cached_as_of:
            continue
        current_symbols = {
            str(row["symbol"])
            for row in result.get("rows") or []
            if isinstance(row, dict) and row.get("symbol")
        }
        ever_counts[sid] = len(set((ever_rows.get(sid) or {}).keys()) | current_symbols)
    return {
        "as_of": cached_as_of,
        "results": summary,
        "today_ever_counts": ever_counts,
        "updated_at": cached.get("updated_at"),
    }


@router.get("/cached-result/{strategy_id}")
def get_cached_result(
    strategy_id: str,
    request: Request,
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
    market: str = Query("cn", description="cn|hk|us"),
    date: Optional[str] = Query(None, description="YYYY-MM-DD, 省略时取最近写入的日期"),
):
    """按需返回指定日期单个策略的完整明细及其该日失效行。"""
    market = str(market) or "cn"
    cached = _cached_with_realtime(request, market, _as_query_str(date))
    raw_result = (cached.get("results") or {}).get(strategy_id)
    if not isinstance(raw_result, dict):
        return {
            "result": None,
            "today_ever_rows": None,
            "strategy_ids_by_symbol": {},
            "updated_at": cached.get("updated_at"),
        }

    ext_values = _load_ext_value_maps(request.app.state.repo, ext_columns)
    result = {
        "as_of": raw_result.get("as_of"),
        "strategy": strategy_id,
        "rows": _rows_with_ext(raw_result.get("rows") or [], ext_values),
        "total": int(raw_result.get("total") or 0),
        "elapsed_ms": 0.0,
    }

    ever_rows = None
    if cached.get("as_of") == result["as_of"]:
        strategy_ever_rows = (cached.get("today_ever_rows") or {}).get(strategy_id)
        if isinstance(strategy_ever_rows, dict):
            ever_rows = {
                symbol: _row_with_ext(row, ext_values, symbol=symbol)
                for symbol, row in strategy_ever_rows.items()
                if isinstance(row, dict)
            }

    selected_symbols = {
        str(row["symbol"])
        for row in raw_result.get("rows") or []
        if isinstance(row, dict) and row.get("symbol")
    }
    strategy_ids_by_symbol: dict[str, list[str]] = {symbol: [] for symbol in selected_symbols}
    for sid, cached_result in (cached.get("results") or {}).items():
        if not isinstance(cached_result, dict) or cached_result.get("as_of") != result["as_of"]:
            continue
        for row in cached_result.get("rows") or []:
            symbol = str(row.get("symbol")) if isinstance(row, dict) and row.get("symbol") else None
            if symbol in strategy_ids_by_symbol:
                strategy_ids_by_symbol[symbol].append(sid)

    return {
        "result": result,
        "today_ever_rows": ever_rows,
        "strategy_ids_by_symbol": strategy_ids_by_symbol,
        "updated_at": cached.get("updated_at"),
    }


@router.get("/market-snapshot")
def market_snapshot(request: Request):
    """最新全市场轻量行情快照，供板块/概念聚合分析使用。"""
    import polars as pl

    repo = request.app.state.repo
    svc = ScreenerService(repo)
    as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "rows": []}

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "rows": []}

    if "close" in df.columns and "total_shares" in df.columns and "market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("total_shares")).alias("market_cap"))
    if "close" in df.columns and "float_shares" in df.columns and "float_market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("float_shares")).alias("float_market_cap"))

    cols = [
        "symbol", "name", "close", "change_pct", "amount", "volume",
        "turnover_rate", "vol_ratio_5d", "total_shares", "float_shares",
        "market_cap", "float_market_cap", "consecutive_limit_ups",
    ]
    df = df.select([c for c in cols if c in df.columns])
    rows = df.to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None

    return {"as_of": str(as_of), "rows": rows}


@router.post("/run_all")
def run_all(request: Request, body: Optional[dict] = None):
    """批量运行指定策略；注册、路由和执行均由 StrategyEngine 负责。"""
    from datetime import date as date_type

    t_total = time.perf_counter()

    body = body or {}
    repo = request.app.state.repo
    asset_type = str(body.get("asset_type") or "stock")
    timeframe = str(body.get("timeframe") or "1d")
    # 多市场: 前端一直在请求体里发 market, 但此处此前从未读取 —— 切到港股跑全部
    # 策略实际返回的是 A 股结果。必须传入 ScreenerService, 否则 enriched 目录
    # 与最新日期都会落到 A 股。
    market = str(body.get("market") or "cn")
    svc = ScreenerService(repo, asset_type=asset_type, market=market)
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="策略引擎未初始化")

    # 解析日期
    raw_date = body.get("as_of")
    if raw_date:
        as_of = date_type.fromisoformat(str(raw_date)) if isinstance(raw_date, str) else raw_date
    else:
        as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "results": {}}

    data_dir = request.app.state.repo.store.data_dir

    requested_ids = body.get("strategy_ids")
    if requested_ids and isinstance(requested_ids, list):
        all_ids = [str(sid) for sid in requested_ids]
        unknown = [
            sid
            for sid in all_ids
            if not engine.has(sid) or engine.get(sid).meta.get("research_only")
        ]
        if unknown:
            raise HTTPException(status_code=404, detail=f"unknown strategies: {unknown}")
        # 显式指定的策略同样要做市场兼容过滤: 前端策略池可能含依赖涨跌停/连板的
        # A 股专属策略, 在港美股上跑只会产出无意义结果。这里静默跳过而不报错 ——
        # 策略本身是合法的, 只是不适用于当前市场。
        if market != "cn":
            meta_by_id = {m["id"]: m for m in engine.list_strategies()}
            all_ids = [
                sid for sid in all_ids
                if _market_compatible_strategy(meta_by_id.get(sid) or {}, market)
            ]
    else:
        all_ids = [
            meta["id"]
            for meta in engine.list_strategies()
            if not meta.get("research_only")
            and asset_type in meta.get("asset_types", ["stock"])
            and timeframe in meta.get("timeframes", ["1d"])
            and _market_compatible_strategy(meta, market)
        ]

    if not all_ids:
        return {"as_of": str(as_of), "results": {}}

    # 批量预加载所有 override 配置
    t0 = time.perf_counter()
    all_overrides = strategy_config.list_overrides(data_dir)
    logger.info("run_all: list_overrides took %.1fms (%d overrides)", (time.perf_counter() - t0) * 1000, len(all_overrides))

    params_map = {
        sid: dict((all_overrides.get(sid) or {}).get("params") or {})
        for sid in all_ids
    }
    overrides_map = {sid: all_overrides.get(sid, {}) for sid in all_ids}
    try:
        context = svc.build_strategy_context(
            engine,
            as_of,
            all_ids,
            timeframe=timeframe,
            params_map=params_map,
            overrides_map=overrides_map,
        )
        engine_results = engine.run_all(
            context,
            params_map=params_map,
            overrides_map=overrides_map,
            strategy_ids=all_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    results: dict[str, dict] = {}
    for sid, result in engine_results.items():
        safe_rows = _safe(asdict(result)).get("rows", [])
        results[sid] = {
            "total": result.total,
            "as_of": str(as_of),
            "rows": safe_rows,
        }

    elapsed = (time.perf_counter() - t_total) * 1000
    logger.info("run_all: total took %.1fms (%d strategies)", elapsed, len(all_ids))

    # 写入策略缓存 (供页面秒加载)
    if results:
        try:
            strategy_cache.write_cache(data_dir, str(as_of), results, market)
        except Exception:  # noqa: BLE001
            pass

    if body.get("summary_only"):
        return {
            "as_of": str(as_of),
            "results": {
                sid: {"total": result["total"], "as_of": result["as_of"]}
                for sid, result in results.items()
            },
        }

    ext_values = _load_ext_value_maps(repo, body.get("ext_columns"))
    return {"as_of": str(as_of), "results": _results_with_ext(results, ext_values)}


@router.get("/limit-ladder")
def limit_ladder(
    request: Request,
    as_of: Optional[date] = None,
    direction: str = Query("up", description="up=涨停梯队 | down=跌停梯队"),
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
    market: str = Query("cn", description="cn|hk|us（多市场扩展）"),
):
    """连板/连跌梯队 — 按连板数分组, 含三状态。
    返回: tiers = [{ boards, count, stocks: [{symbol,name,change_pct,status,...}] }]

    direction=up (默认):
      status: limit_up=涨停 | broken=炸板(摸板未封) | failed=断板(晋级失败)
    direction=down:
      status: limit_down=跌停 | recovery=翘板(跌停后回升,含收阳条件) | failed=止跌(昨日跌停今日未跌停也未翘板)

    ext_columns: 动态 JOIN 扩展数据, 如 "concept.concept,industry.industry"

    market=hk|us（多市场扩展）: 返回与 A 股同构的 tiers——
      boards = 20日动量档位(0-5), status = high(60日新高)/momentum(强动量)/volume(放量),
      counts.up/down = 60日新高/新低数。前端复用 A 股连板梯队 UI。
    """
    if market in ("hk", "us"):
        return _limit_ladder_market(request, market, as_of)
    import polars as pl

    is_down = direction == "down"

    # 按 direction 参数化字段映射
    if is_down:
        sig_col = "signal_limit_down"
        consec_col = "consecutive_limit_downs"
        broken_col = "signal_limit_down_recovery"
        status_main, status_broken, status_failed = "limit_down", "recovery", "failed"
    else:
        sig_col = "signal_limit_up"
        consec_col = "consecutive_limit_ups"
        broken_col = "signal_broken_limit_up"
        status_main, status_broken, status_failed = "limit_up", "broken", "failed"

    repo = request.app.state.repo
    svc = ScreenerService(repo)
    as_of = as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "tiers": [], "counts": {"up": 0, "down": 0}}

    # 双方向涨跌停计数(不论当前 direction, 前端始终同时显示)
    count_up_raw = int(df.filter(pl.col("signal_limit_up").fill_null(False)).height) if "signal_limit_up" in df.columns else 0
    count_down_raw = int(df.filter(pl.col("signal_limit_down").fill_null(False)).height) if "signal_limit_down" in df.columns else 0

    # 双方向 sealed 修正: 减去各自的假涨停(假涨停已归炸板, 不计入涨停数)
    depth_svc_global = getattr(request.app.state, "depth_service", None)
    fake_up = 0
    fake_down = 0
    sealed_up_ready = False
    sealed_down_ready = False
    if depth_svc_global:
        up_map = depth_svc_global.get_sealed_map(as_of, is_down=False)
        down_map = depth_svc_global.get_sealed_map(as_of, is_down=True)
        sealed_up_ready = bool(up_map) and depth_svc_global.is_sealed_ready(as_of)
        sealed_down_ready = bool(down_map) and depth_svc_global.is_sealed_ready(as_of)
        if up_map:
            fake_up = sum(1 for v in up_map.values() if v.get("sealed") is False)
        if down_map:
            fake_down = sum(1 for v in down_map.values() if v.get("sealed") is False)
    count_up = count_up_raw - fake_up if sealed_up_ready else count_up_raw
    count_down = count_down_raw - fake_down if sealed_down_ready else count_down_raw

    # 双方向 sealed 明细(供前端弹窗同时显示涨跌停)
    def _count_sealed(m: dict, ready: bool):
        if not m or not ready:
            return {"real": 0, "fake": 0, "pending": 0}
        real = sum(1 for v in m.values() if v.get("sealed") is True)
        fake = sum(1 for v in m.values() if v.get("sealed") is False)
        pending = sum(1 for v in m.values() if v.get("sealed") is None)
        return {"real": real, "fake": fake, "pending": pending}
    sealed_counts_up = _count_sealed(up_map, sealed_up_ready)
    sealed_counts_down = _count_sealed(down_map, sealed_down_ready)

    # 加载前一日的 prev consecutive_limit_ups/downs
    # 窄读: 仅取前一交易日的 [symbol, consec_col] 两列 (存储列, 直接谓词下推读 parquet),
    # 替代旧的 range(1,10) 循环逐日 _load_enriched_for_date 全量指标重算 (最坏 9× 全市场重算)。
    prev_consec: pl.DataFrame = svc.load_prior_consecutive(as_of, consec_col)

    if not prev_consec.is_empty():
        df = df.join(prev_consec, on="symbol", how="left")
    else:
        df = df.with_columns(pl.lit(0).cast(pl.UInt32).alias("prev_consec"))

    # 表达式
    is_limit = pl.col(sig_col).fill_null(False) if sig_col in df.columns else pl.lit(False)
    is_broken = pl.col(broken_col).fill_null(False) if broken_col in df.columns else pl.lit(False)
    consec = pl.col(consec_col).fill_null(0) if consec_col in df.columns else pl.lit(0)
    prev_c = pl.col("prev_consec").fill_null(0)

    # 计算 status + boards (结构涨跌停对称, 仅字段与字面量不同)
    is_failed = ~is_limit & ~is_broken & (prev_c > 0)
    df = df.with_columns([
        pl.when(is_limit).then(pl.lit(status_main))
        .when(is_broken).then(pl.lit(status_broken))
        .when(is_failed).then(pl.lit(status_failed))
        .otherwise(None).alias("status"),
        pl.when(is_limit).then(consec)
        .when(is_broken | is_failed).then(prev_c + 1)
        .otherwise(0).cast(pl.UInt32).alias("boards"),
    ])

    df = df.filter(pl.col("status").is_not_null() & (pl.col("boards") > 0))

    # ── 五档 sealed 叠加(独立旁路, 不改 signal_limit_up) ──
    # 假涨停(收盘价=涨停价但卖一有量)从 limit 降级为 broken(归炸板视图)
    # 真涨停保留 + 附封单量; sealed=null(待确认/降级)保持原状
    depth_svc = getattr(request.app.state, "depth_service", None)
    sealed_ready = False
    sealed_age: float | None = None
    if depth_svc:
        # 复用上方双方向计数已读取的 sealed map: 同一请求、同一 as_of、同一对象,
        # 不再第三次读取 (内存路径含全量浅拷贝, parquet 路径含整文件读)。
        sealed_map = down_map if is_down else up_map
        sealed_ready = bool(sealed_map) and depth_svc.is_sealed_ready(as_of)
        sealed_age = depth_svc.get_sealed_age(as_of) if sealed_ready else None

        if sealed_map:
            # 构建 sealed 列(symbol → sealed bool, vol)
            sym_sealed = {s: v.get("sealed") for s, v in sealed_map.items()}
            sym_vol = {s: v.get("vol") for s, v in sealed_map.items()}

            # JOIN sealed: 对每只 status=main 的票, 看 sealed 值
            sealed_rows = pl.DataFrame({
                "symbol": list(sym_sealed.keys()),
                "_sealed": list(sym_sealed.values()),
                "_sealed_vol": list(sym_vol.values()),
            }) if sym_sealed else pl.DataFrame()

            if not sealed_rows.is_empty():
                df = df.join(sealed_rows, on="symbol", how="left")
                # 假涨停(main 状态但 sealed=False)→ 降级为 broken
                df = df.with_columns(
                    pl.when(
                        (pl.col("status") == status_main)
                        & pl.col("_sealed").is_not_null()
                        & (pl.col("_sealed") == False)  # noqa: E712
                    ).then(pl.lit(status_broken))
                    .otherwise(pl.col("status")).alias("status"),
                    # sealed_status: real/fake/pending/null
                    pl.when(
                        (pl.col("status") == status_main)
                        & (pl.col("_sealed") == True)  # noqa: E712
                    ).then(pl.lit("real"))
                    .when(
                        (pl.col("_sealed") == False)  # noqa: E712
                    ).then(pl.lit("fake"))
                    .when(
                        (pl.col("status") == status_main)
                        & pl.col("_sealed").is_null()
                    ).then(pl.lit("pending"))
                    .otherwise(None).alias("sealed_status"),
                    pl.col("_sealed_vol").alias("sealed_vol"),
                ).drop(["_sealed", "_sealed_vol"])
            else:
                df = df.with_columns(
                    pl.lit(None).alias("sealed_status"),
                    pl.lit(None).alias("sealed_vol"),
                )
        else:
            df = df.with_columns(
                pl.lit(None).alias("sealed_status"),
                pl.lit(None).alias("sealed_vol"),
            )
    else:
        df = df.with_columns(
            pl.lit(None).alias("sealed_status"),
            pl.lit(None).alias("sealed_vol"),
        )

    df = df.with_columns(_one_word_limit_expr(status_main, df.columns).alias("is_one_word"))

    # 动态 JOIN 扩展数据
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    ext_col_names: list[str] = []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.services.ext_data import ExtConfigStore

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                ext_df = pl.from_arrow(db.query(
                    f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = ext_df.rename({field_name: ext_col_name})
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
                    ext_col_names.append(ext_col_name)
            except Exception:
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        from app.api.ext_data import _parquet_glob
                        glob = _parquet_glob(cfg, data_dir)
                        ext_df = pl.read_parquet(glob)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = ext_df.select(["symbol", field_name]).rename({field_name: ext_col_name})
                            df = df.join(ext_df, on="symbol", how="left")
                            ext_col_names.append(ext_col_name)
                    except Exception:
                        pass

    # 选择输出列
    cols = ["symbol", "name", "close", "change_pct", "boards", "status", consec_col, "sealed_status", "sealed_vol", "is_one_word"] + ext_col_names
    df = df.select([c for c in cols if c in df.columns])
    # 排序: boards 降序, status 按主状态→炸/翘→断/止
    status_order = pl.when(pl.col("status") == status_main).then(0)
    status_order = status_order.when(pl.col("status") == status_broken).then(1)
    status_order = status_order.otherwise(2).alias("_status_order")
    df = df.with_columns(status_order).sort(["boards", "_status_order"], descending=[True, False]).drop("_status_order")

    rows = df.to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None

    # 按 boards 分组
    tiers: dict[int, list] = {}
    for r in rows:
        n = int(r.get("boards") or 0)
        tiers.setdefault(n, []).append(r)

    tier_list = [
        {"boards": n, "count": len(stocks), "stocks": stocks}
        for n, stocks in sorted(tiers.items(), key=lambda x: -x[0])
    ]

    return {
        "as_of": str(as_of),
        "tiers": tier_list,
        "counts": {"up": count_up, "down": count_down},
        "counts_raw": {"up": count_up_raw, "down": count_down_raw},
        "sealed_ready": sealed_ready,
        "sealed_age": round(sealed_age, 0) if sealed_age is not None else None,
        "sealed_counts": {
            "real": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "real"),
            "fake": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "fake"),
            "pending": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "pending"),
        },
        "sealed_counts_up": sealed_counts_up,
        "sealed_counts_down": sealed_counts_down,
    }


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 'config_id1.field1,config_id2.field2' 为 [(config_id, field_name), ...]。"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if not config_id or not field_name:
            continue
        if not is_valid_ext_ident(config_id) or "\x00" in field_name:
            continue
        result.append((config_id, field_name))
    return result


# ================================================================
# 港美股强度梯队（多市场扩展）— 与 A 股连板梯队同构返回
# ================================================================
def _limit_ladder_market(request: Request, market: str, as_of: date | None) -> dict:
    """港美股强度梯队：复用 A 股连板梯队 UI，语义替换。

    - boards = 20日动量档位: ≥25%→5, ≥15%→4, ≥8%→3, ≥3%→2, 其余不显示
    - status = high(60日新高突破) | momentum(强动量) | volume(放量)
    - counts.up/down = 60日新高/新低数
    """
    import polars as pl

    repo = request.app.state.repo
    svc = ScreenerService(repo, market=market)
    as_of = as_of or svc.latest_date()
    if not as_of:
        return {"as_of": None, "tiers": [], "counts": {"up": 0, "down": 0},
                "counts_raw": {"up": 0, "down": 0}, "sealed_ready": False,
                "sealed_age": None, "sealed_counts": {"real": 0, "fake": 0, "pending": 0},
                "sealed_counts_up": None, "sealed_counts_down": None, "market": market}

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "tiers": [], "counts": {"up": 0, "down": 0},
                "counts_raw": {"up": 0, "down": 0}, "sealed_ready": False,
                "sealed_age": None, "sealed_counts": {"real": 0, "fake": 0, "pending": 0},
                "sealed_counts_up": None, "sealed_counts_down": None, "market": market}

    need = ["symbol", "name", "close", "change_pct", "amount", "momentum_20d",
            "vol_ratio_5d", "high_60d", "low_60d", "signal_n_day_high", "signal_n_day_low"]
    df = df.select([c for c in need if c in df.columns])

    # 60日新高/新低计数（涨跌切换语义：up=新高榜 down=新低榜）
    count_up = 0
    count_down = 0
    if "signal_n_day_high" in df.columns:
        count_up = int(df.filter(pl.col("signal_n_day_high").fill_null(False)).height)
    elif "high_60d" in df.columns and "close" in df.columns:
        count_up = int((df["close"] >= df["high_60d"].fill_null(0) * _NEAR_HIGH_RATIO).sum())
    if "signal_n_day_low" in df.columns:
        count_down = int(df.filter(pl.col("signal_n_day_low").fill_null(False)).height)
    elif "low_60d" in df.columns and "close" in df.columns:
        count_down = int((df["close"] <= df["low_60d"].fill_null(0) * _NEAR_LOW_RATIO).sum())

    # 状态计算
    is_high = pl.lit(False)
    if "high_60d" in df.columns and "close" in df.columns:
        is_high = (pl.col("close") >= pl.col("high_60d").fill_null(0) * _NEAR_HIGH_RATIO)
    if "signal_n_day_high" in df.columns:
        is_high = is_high | pl.col("signal_n_day_high").fill_null(False)
    is_volume = pl.lit(False)
    if "vol_ratio_5d" in df.columns and "change_pct" in df.columns:
        is_volume = (pl.col("vol_ratio_5d").fill_null(0) >= 1.5) & (pl.col("change_pct").fill_null(0) > 0)
    is_momentum = pl.lit(False)
    if "momentum_20d" in df.columns:
        is_momentum = (pl.col("momentum_20d").fill_null(0) >= 0.03)

    # boards = 20日动量档位
    # 缺 momentum_20d 时降级为 0（等价于全部落到 otherwise(1)，随后被 boards>=2 过滤掉），
    # 与上面 is_momentum 的列存在性保护保持一致，避免直接抛 ColumnNotFoundError。
    mom = pl.col("momentum_20d").fill_null(0) if "momentum_20d" in df.columns else pl.lit(0.0)
    boards = (pl.when(mom >= 0.25).then(5)
              .when(mom >= 0.15).then(4)
              .when(mom >= 0.08).then(3)
              .when(mom >= 0.03).then(2)
              .otherwise(1))
    status = (pl.when(is_high).then(pl.lit("high"))
              .when(is_volume).then(pl.lit("volume"))
              .when(is_momentum).then(pl.lit("momentum"))
              .otherwise(None))

    df = df.with_columns([
        boards.alias("boards"),
        status.alias("status"),
        boards.alias("consecutive_limit_ups"),
        pl.lit(0).cast(pl.UInt32).alias("consecutive_limit_downs"),
        pl.lit(None).alias("sealed_status"),
        pl.lit(None).alias("sealed_vol"),
    ])
    # 只有动量档 >=2 的标的进入梯队
    df = df.filter(pl.col("boards") >= 2)

    rows = df.to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None

    tiers: dict[int, list] = {}
    for r in rows:
        n = int(r.get("boards") or 0)
        tiers.setdefault(n, []).append(r)
    tier_list = [
        {"boards": n, "count": len(stocks), "stocks": stocks}
        for n, stocks in sorted(tiers.items(), key=lambda x: -x[0])
    ]

    return {
        "as_of": str(as_of),
        "tiers": tier_list,
        "counts": {"up": count_up, "down": count_down},
        "counts_raw": {"up": count_up, "down": count_down},
        "sealed_ready": False,
        "sealed_age": None,
        "sealed_counts": {"real": 0, "fake": 0, "pending": 0},
        "sealed_counts_up": None,
        "sealed_counts_down": None,
        "market": market,
    }
