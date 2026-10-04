"""五档盘口查询 API.

优先使用 stock-sdk 插件(免费, 含 bid/ask 五档), 无 tickflow Pro+ 也可用.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.data_providers.custom import loader as custom_loader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/depth", tags=["depth"])


def _get_stocksdk_provider():
    """获取 stocksdk provider, 未安装/不可用时返回 None."""
    try:
        return custom_loader.get_provider("stocksdk")
    except Exception as e:
        logger.debug("stocksdk provider 不可用: %s", e)
        return None


def _normalize_depth_row(row: dict) -> dict[str, Any]:
    """把 stock-sdk FullQuote 归一化成前端盘口面板需要的结构."""
    bid = row.get("bid") or []
    ask = row.get("ask") or []
    # 补齐到 5 档(不足时补空)
    bid_levels = [
        {"price": b.get("price"), "volume": b.get("volume")}
        for b in bid[:5]
    ]
    ask_levels = [
        {"price": a.get("price"), "volume": a.get("volume")}
        for a in ask[:5]
    ]
    while len(bid_levels) < 5:
        bid_levels.append({"price": None, "volume": None})
    while len(ask_levels) < 5:
        ask_levels.append({"price": None, "volume": None})

    # 委比 = (买五档总量 - 卖五档总量) / (买五档总量 + 卖五档总量) * 100
    bid_vol = sum(b.get("volume") or 0 for b in bid[:5])
    ask_vol = sum(a.get("volume") or 0 for a in ask[:5])
    total = bid_vol + ask_vol
    wbi = ((bid_vol - ask_vol) / total * 100.0) if total > 0 else None
    wdiff = bid_vol - ask_vol if total > 0 else None

    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "last_price": row.get("last_price"),
        "prev_close": row.get("prev_close"),
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "volume": row.get("volume"),
        "amount": row.get("amount"),
        "change_pct": row.get("change_pct"),
        "turnover_rate": row.get("turnover_rate"),
        "pe": row.get("pe"),
        "pb": row.get("pb"),
        "total_market_cap": row.get("total_market_cap"),
        "circulating_market_cap": row.get("circulating_market_cap"),
        "limit_up": row.get("limit_up"),
        "limit_down": row.get("limit_down"),
        "volume_ratio": row.get("volume_ratio"),
        "avg_price": row.get("avg_price"),
        "bid": bid_levels,
        "ask": ask_levels,
        "wb_ratio": wbi,      # 委比 %
        "wb_diff": wdiff,     # 委差(手)
        "time": row.get("time"),
        "timestamp": row.get("timestamp"),
    }


@router.get("/{symbol}")
def get_depth(symbol: str) -> dict:
    """查询单只股票五档盘口.

    返回: 最新价, 涨跌, 五档 bid/ask, 委比委差, 换手率, 市值等.
    """
    provider = _get_stocksdk_provider()
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="stock-sdk 插件不可用, 无法获取盘口数据",
        )
    rows = provider.get_depth([symbol])
    if not rows:
        raise HTTPException(status_code=404, detail=f"未获取到 {symbol} 的盘口数据")
    return _normalize_depth_row(rows[0])


@router.get("/batch")
def get_depth_batch(
    symbols: str = Query(..., description="逗号分隔的股票代码, 如 600519.SH,000001.SZ"),
) -> dict:
    """批量查询多只股票五档盘口."""
    provider = _get_stocksdk_provider()
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="stock-sdk 插件不可用, 无法获取盘口数据",
        )
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if not syms:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    rows = provider.get_depth(syms)
    return {row["symbol"]: _normalize_depth_row(row) for row in rows}
