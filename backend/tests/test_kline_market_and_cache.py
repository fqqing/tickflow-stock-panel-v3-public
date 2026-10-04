"""``/api/kline/daily`` 的 market 透传契约 + 周/月K 聚合缓存。

背景: 三市日K分目录存储, 而 ``repo.get_daily_asset`` 的 market 默认是 cn。
API 层若不透传, 港美股 K 线恒返回空 —— 既不报错也不提示, 排查时极易误判成
「没同步」。这里用替身 repo 把「market 一定被传下去」这个契约锁住。

只测透传与缓存, 不碰真实数据: 行情用固定构造的 DataFrame。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import kline as kline_api


def _request(repo=None, headers=None):
    # headers 必须有: 响应现在带 ETag, 端点会读 request.headers['if-none-match']
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=repo or MagicMock(),
                capabilities=MagicMock(),
            )
        ),
        headers=headers or {},
    )


def _daily_df(n: int = 120, symbol: str = "600000.SH") -> pl.DataFrame:
    # 末根对齐今天: get_daily 默认区间是 [today - days, today], 固定日期会整体落空
    end = date.today()
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "date": [end - timedelta(days=n - 1 - i) for i in range(n)],
            "open": [10.0 + i * 0.01 for i in range(n)],
            "high": [11.0 + i * 0.01 for i in range(n)],
            "low": [9.0 + i * 0.01 for i in range(n)],
            "close": [10.0 + (i % 7) * 0.1 for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
        }
    )


def _repo(rows: pl.DataFrame | None = None) -> MagicMock:
    repo = MagicMock()
    repo.resolve_asset_type.return_value = "stock"
    repo.get_instruments.return_value = pl.DataFrame(
        {"symbol": [], "name": [], "total_shares": [], "float_shares": []}
    )
    repo.get_daily_asset.return_value = rows if rows is not None else pl.DataFrame()
    return repo


def _call_daily(repo, symbol: str, **overrides):
    """直调端点函数: FastAPI 的 Query 默认值只在 HTTP 入口生效, 直调时必须显式给全。

    端点现在返回带 ETag 的 JSONResponse (条件请求改造), 这里解回 dict 供断言。
    """
    kwargs = {
        "start_date": None,
        "end_date": None,
        "ext_columns": None,
        "indicators": None,
        "fields": None,
        "period": "day",
        "adjust": "qfq",
        "market": "",
    }
    kwargs.update(overrides)
    resp = kline_api.get_daily(_request(repo), symbol, days=30, **kwargs)
    return json.loads(resp.body) if hasattr(resp, "body") else resp


# ===== market 透传 =====


def test_daily_auto_derives_market_from_symbol_suffix():
    """不传 market 时按 symbol 后缀推导: 港股符号必须走 hk 目录。"""
    repo = _repo(_daily_df(30, "00700.HK"))
    _call_daily(repo, "00700.HK")
    assert repo.get_daily_asset.call_args.kwargs["market"] == "hk"


def test_daily_explicit_market_wins_over_suffix():
    """显式 market 优先: 同一 symbol 可以被强制换市场读。"""
    repo = _repo(_daily_df(30, "000001.SZ"))
    resp = _call_daily(repo, "000001.SZ", market="us")
    assert repo.get_daily_asset.call_args.kwargs["market"] == "us"
    assert resp["market"] == "us"


def test_daily_rejects_unknown_market():
    with pytest.raises(HTTPException) as exc:
        _call_daily(_repo(_daily_df(30)), "600000.SH", market="jp")
    assert exc.value.status_code == 400


def test_daily_reports_market_even_when_rows_empty(monkeypatch):
    """空结果也要回显 market, 否则前端无法区分「没数据」和「取错市场」。"""
    # 空 df 会触发 sync_daily_batch 兜底, 桩掉它避免测试真的打网络
    monkeypatch.setattr(kline_api.kline_sync, "sync_daily_batch", lambda *a, **k: pl.DataFrame())
    repo = _repo(pl.DataFrame())
    resp = _call_daily(repo, "00700.HK")
    assert resp["rows"] == []
    assert resp["market"] == "hk"


# ===== 周/月K 聚合缓存 =====


def test_week_aggregation_is_computed_once_per_fingerprint(monkeypatch):
    """同标的同周期同复权 -> 第二次直接命中缓存。"""
    kline_api.clear_period_cache()
    calls: list[str] = []
    real = kline_api._aggregate_period

    def counting(df, period):
        calls.append(period)
        return real(df, period)

    monkeypatch.setattr(kline_api, "_aggregate_period", counting)

    df = _daily_df(120)
    kline_api._aggregate_period_cached(df, "week", "600000.SH", "qfq")
    kline_api._aggregate_period_cached(df, "week", "600000.SH", "qfq")
    assert calls == ["week"]


def test_week_cache_key_includes_adjust_and_data_boundary(monkeypatch):
    """复权口径与数据边界都进 key: 任一变化都要重算。"""
    kline_api.clear_period_cache()
    calls: list[str] = []
    real = kline_api._aggregate_period

    def counting(df, period):
        calls.append(period)
        return real(df, period)

    monkeypatch.setattr(kline_api, "_aggregate_period", counting)

    df = _daily_df(120)
    kline_api._aggregate_period_cached(df, "week", "600000.SH", "qfq")
    # 复权在聚合之前做, 三种口径行数完全相同 —— 不进 key 就会串味
    kline_api._aggregate_period_cached(df, "week", "600000.SH", "none")
    # 数据边界变了 (少 20 根)
    kline_api._aggregate_period_cached(df.head(100), "week", "600000.SH", "qfq")
    assert calls == ["week", "week", "week"]
