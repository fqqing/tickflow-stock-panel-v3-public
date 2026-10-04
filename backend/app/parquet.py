"""Polars parquet helpers."""
from __future__ import annotations

from typing import Any

import polars as pl

DAILY_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "quote_ts": pl.Int64,
}

ENRICHED_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "raw_close": pl.Float64,
    "raw_high": pl.Float64,
    "raw_low": pl.Float64,
    "turnover_rate": pl.Float64,
    "consecutive_limit_ups": pl.UInt32,
    "consecutive_limit_downs": pl.UInt32,
    "quote_ts": pl.Int64,
}


def scan_parquet_compat(source: Any, **kwargs: Any) -> pl.LazyFrame:
    """Scan partitioned parquet while tolerating additive schema changes."""
    kwargs.setdefault("missing_columns", "insert")
    kwargs.setdefault("extra_columns", "ignore")
    return pl.scan_parquet(source, **kwargs)


def scan_daily_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("schema", DAILY_STORAGE_SCHEMA)
    kwargs.setdefault("cast_options", pl.ScanCastOptions(integer_cast="allow-float"))
    return scan_parquet_compat(source, **kwargs)


def scan_enriched_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("schema", ENRICHED_STORAGE_SCHEMA)
    kwargs.setdefault("cast_options", pl.ScanCastOptions(integer_cast="allow-float"))
    return scan_parquet_compat(source, **kwargs)


def market_symbol_filter(market: str) -> pl.Expr | None:
    """按市场 symbol 后缀收窄读取范围的表达式; 市场未知时返回 None (不过滤)。

    A 股 enriched 目录 (kline_daily_enriched) 早期写入过港美股行 —— 实测 180 天
    窗口 237 万行里 71% 是 .US/.HK, 而 A 股策略只需要其中 68 万行。不过滤会把
    3 倍无关标的算进指标 (读盘、指标、涨跌停信号全线变慢, 内存同步放大)。
    港美股虽走独立目录, 同样过滤一次可防同类污染。
    """
    from app.markets import suffixes_for

    suffixes = suffixes_for(market)
    if not suffixes:
        return None
    expr = pl.col("symbol").str.ends_with(suffixes[0])
    for suffix in suffixes[1:]:
        expr = expr | pl.col("symbol").str.ends_with(suffix)
    return expr
