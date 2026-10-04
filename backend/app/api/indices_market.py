"""港美股主要指数 API（多市场扩展）。

数据来自腾讯免费接口（TickFlow 免费模式无港美股指数），由
services/index_sync_market.sync_market_indices 同步到本地。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import index_sync_market

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/indices/market", tags=["indices-market"])


@router.get("")
def market_indices(request: Request, market: str = Query("hk", description="hk|us")):
    """港美股指数清单 + 最新收盘/涨跌幅。"""
    if market not in ("hk", "us"):
        raise HTTPException(status_code=400, detail="market 必须为 hk|us")
    items = index_sync_market.list_market_indices(market)
    data_dir = request.app.state.repo.store.data_dir
    daily_dir = Path(data_dir) / f"kline_index_daily_{market}"
    out = []
    for item in items:
        symbol = item["symbol"]
        latest = None
        if daily_dir.exists():
            try:
                import polars as pl
                # 实时快照优先（美股指数 latest_{symbol}.parquet 含 change_pct）
                snap_path = daily_dir / f"latest_{symbol}.parquet"
                if snap_path.exists():
                    snap = pl.read_parquet(snap_path)
                    if not snap.is_empty():
                        r = snap.to_dicts()[0]
                        latest = {
                            "date": str(r.get("date", "")),
                            "close": float(r.get("close", 0)),
                            "change_pct": float(r.get("change_pct") or 0),
                            "snapshot": True,
                        }
                if latest is None:
                    lf = pl.scan_parquet((daily_dir / "date=*" / "*.parquet").as_posix())
                    row = lf.filter(pl.col("symbol") == symbol).sort("date", descending=True).head(2).collect()
                    if row.height >= 2:
                        prev = float(row["close"][-1])
                        cur = float(row["close"][0])
                        latest = {
                            "date": str(row["date"][0]),
                            "close": cur,
                            "change_pct": (cur / prev - 1) if prev else 0.0,
                        }
                    elif row.height == 1:
                        latest = {"date": str(row["date"][0]), "close": float(row["close"][0]), "change_pct": 0.0}
            except Exception as e:  # noqa: BLE001
                logger.warning("index %s latest failed: %s", symbol, e)
        out.append({**item, "latest": latest})
    return {"market": market, "items": out}


@router.get("/kline")
def market_index_kline(
    request: Request,
    market: str = Query("hk"),
    symbol: str = Query(...),
    days: int = Query(250, ge=30, le=800),
):
    """港美股指数日K（含未复权 OHLCV）。"""
    daily_dir = Path(request.app.state.repo.store.data_dir) / f"kline_index_daily_{market}"
    if not daily_dir.exists():
        return {"rows": []}
    import polars as pl
    try:
        lf = pl.scan_parquet((daily_dir / "**" / "*.parquet").as_posix())
        df = (
            lf.filter(pl.col("symbol") == symbol)
            .sort("date", descending=True)
            .head(days)
            .sort("date")
            .collect()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("index kline failed: %s", e)
        return {"rows": []}
    rows = [
        {"date": str(r["date"]), "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "volume": r["volume"]}
        for r in df.to_dicts()
    ]
    return {"symbol": symbol, "market": market, "rows": rows}


# ================================================================
# 与 A 股 /api/index/* 同构的端点（前端复用 A 股 Indices 页渲染）
# ================================================================

@router.get("/list")
def market_indices_list(request: Request, market: str = Query("hk", description="hk|us")):
    """港美股指数清单（对齐 A 股 /api/index/list 返回格式）。"""
    items = index_sync_market.list_market_indices(market)
    return {"results": items, "count": len(items)}


@router.get("/quotes")
def market_indices_quotes(request: Request, market: str = Query("hk", description="hk|us")):
    """港美股指数最新行情（对齐 A 股 /api/index/quotes 返回格式）。"""
    if market not in ("hk", "us"):
        raise HTTPException(status_code=400, detail="market 必须为 hk|us")
    import polars as pl

    data_dir = request.app.state.repo.store.data_dir
    daily_dir = Path(data_dir) / f"kline_index_daily_{market}"
    rows = []
    for item in index_sync_market.list_market_indices(market):
        symbol = item["symbol"]
        latest = None
        if daily_dir.exists():
            try:
                snap = daily_dir / f"latest_{symbol}.parquet"
                if snap.exists():
                    s = pl.read_parquet(snap)
                    if not s.is_empty():
                        r = s.to_dicts()[0]
                        latest = {
                            "date": str(r.get("date", "")),
                            "close": float(r.get("close", 0)),
                            "change_pct": float(r.get("change_pct") or 0),
                            "last_price": float(r.get("close", 0)),
                            "pct": float(r.get("change_pct") or 0),
                        }
                if latest is None:
                    lf = pl.scan_parquet((daily_dir / "date=*" / "*.parquet").as_posix())
                    df = lf.filter(pl.col("symbol") == symbol).sort("date", descending=True).head(2).collect()
                    if df.height >= 2:
                        prev = float(df["close"][-1]); cur = float(df["close"][0])
                        latest = {"date": str(df["date"][0]), "close": cur, "last_price": cur,
                                  "change_pct": (cur / prev - 1) if prev else 0.0, "pct": (cur / prev - 1) if prev else 0.0}
                    elif df.height == 1:
                        cur = float(df["close"][0])
                        latest = {"date": str(df["date"][0]), "close": cur, "last_price": cur, "change_pct": 0.0, "pct": 0.0}
            except Exception as e:  # noqa: BLE001
                logger.warning("index quote %s failed: %s", symbol, e)
        if latest:
            rows.append({"symbol": symbol, "name": item["name"], **latest})
    return {"rows": rows, "count": len(rows)}


@router.get("/daily")
def market_indices_daily(
    request: Request,
    market: str = Query("hk"),
    symbol: str = Query(...),
    days: int = Query(180, ge=30, le=800),
    start: str | None = None,
    end: str | None = None,
):
    """港美股指数日K（对齐 A 股 /api/index/daily 返回格式）。"""
    daily_dir = Path(request.app.state.repo.store.data_dir) / f"kline_index_daily_{market}"
    if not daily_dir.exists():
        return {"symbol": symbol, "rows": [], "index_info": None}
    import polars as pl
    try:
        lf = pl.scan_parquet((daily_dir / "date=*" / "*.parquet").as_posix())
        lf = lf.filter(pl.col("symbol") == symbol)
        if start and end:
            lf = lf.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        else:
            lf = lf.sort("date", descending=True).head(days)
        df = lf.sort("date").collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("index daily failed: %s", e)
        return {"symbol": symbol, "rows": [], "index_info": None}
    rows = [
        {"date": str(r["date"]), "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "volume": r["volume"], "amount": 0}
        for r in df.to_dicts()
    ]
    name = next((i["name"] for i in index_sync_market.list_market_indices(market) if i["symbol"] == symbol), symbol)
    return {"symbol": symbol, "rows": rows, "index_info": {"symbol": symbol, "name": name}}


@router.get("/minute")
def market_indices_minute(request: Request, market: str = Query("hk"), symbol: str = Query(...)):
    """港美股指数分时（免费数据源无，返回空）。"""
    return {"symbol": symbol, "rows": [], "index_info": None}


@router.post("/sync")
def market_indices_sync(request: Request, market: str = Query("hk", description="hk|us")):
    """手动同步港美股指数（对齐 A 股 /api/index/sync_daily 语义）。"""
    from app.services.index_sync_market import sync_market_indices

    data_dir = request.app.state.repo.store.data_dir
    n = sync_market_indices(market, Path(data_dir))
    return {"ok": True, "rows": n, "market": market}
