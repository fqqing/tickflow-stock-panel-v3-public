"""em_xdxr 复权因子插件测试。

不依赖网络: 事件拉取与本地日K读取都被 monkeypatch, 只验证
符号/日期归一化、通达信乘法复权倍率的推导、护栏、以及插件注册接线。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from app.data_providers import custom as cs
from app.data_providers.custom.loader import plugin_manifest
from app.indicators.pipeline import _apply_adj_factor
from app.plugins.em_xdxr import provider as ep


def _raw_frame(symbol: str, closes: list[float]) -> pl.DataFrame:
    base = dt.date(2026, 1, 5)
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "date": [base + dt.timedelta(days=i) for i in range(len(closes))],
            "close": closes,
        }
    )


def _events_frame(rows: list[tuple[str, dt.date, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.Utf8, "trade_date": pl.Date, "fh": pl.Float64, "sg": pl.Float64},
        orient="row",
    )


# ---------- 注册接线 ----------


def test_plugin_is_registered_and_declares_only_adj_factor():
    cs.load_all()
    assert "em_xdxr" in cs.names()
    # 只声明 adj_factor: 日K 由日K数据源提供, 多声明会误导设置页的分流
    assert cs.provider_has_dataset("em_xdxr", "adj_factor")
    assert not cs.provider_has_dataset("em_xdxr", "daily")
    manifest = plugin_manifest("em_xdxr")
    assert manifest is not None
    assert manifest["datasets"] == ["adj_factor"]
    status = {p["name"]: p for p in cs.list_plugins()}["em_xdxr"]
    assert status["available"] is True


# ---------- 归一化 ----------


def test_norm_symbol_prefers_secucode():
    assert ep._norm_symbol({"SECUCODE": "600519.SH", "SECURITY_CODE": "600519"}) == "600519.SH"
    assert ep._norm_symbol({"SECUCODE": "000858.SZ"}) == "000858.SZ"


def test_norm_symbol_falls_back_to_code_prefix():
    assert ep._norm_symbol({"SECURITY_CODE": "600519"}) == "600519.SH"
    assert ep._norm_symbol({"SECURITY_CODE": "000858"}) == "000858.SZ"
    assert ep._norm_symbol({"SECURITY_CODE": "300750"}) == "300750.SZ"
    assert ep._norm_symbol({"SECURITY_CODE": "920001"}) == "920001.BJ"
    assert ep._norm_symbol({"SECURITY_CODE": "abc"}) is None
    assert ep._norm_symbol({}) is None


def test_norm_date_truncates_timestamp():
    assert ep._norm_date("2026-09-17 00:00:00") == "2026-09-17"
    assert ep._norm_date(None) == ""


# ---------- 倍率推导 ----------


def test_build_factors_uses_price_before_ex_date():
    """纯派息: 除权日前一根收盘价 P 代入 (P - 派息)/P, ex_factor 取倒数。"""
    raw = _raw_frame("600519.SH", [10.0, 10.2, 10.4, 10.6, 10.8])
    ev = _events_frame([("600519.SH", dt.date(2026, 1, 8), 0.5, 0.0)])
    out = ep._build_factors(raw, ev)
    assert out.height == 1
    assert out["trade_date"].to_list() == [dt.date(2026, 1, 8)]
    assert out["ex_factor"][0] == pytest.approx(10.4 / (10.4 - 0.5))


def test_build_factors_handles_songzhuan():
    """纯送转: ratio = P / (P x (1 + 每股送转))。"""
    raw = _raw_frame("000001.SZ", [20.0, 21.0, 22.0, 23.0])
    ev = _events_frame([("000001.SZ", dt.date(2026, 1, 7), 0.0, 1.0)])
    out = ep._build_factors(raw, ev)
    assert out["ex_factor"][0] == pytest.approx(2.0)


def test_build_factors_skips_event_at_or_before_first_bar():
    """首个 bar 之前的事件没有「前一根」可用, 也无需复权 → 跳过而不是报错。"""
    raw = _raw_frame("600519.SH", [10.0, 10.2, 10.4])
    ev = _events_frame(
        [
            ("600519.SH", dt.date(2026, 1, 1), 1.0, 0.0),  # 早于首个 bar
            ("600519.SH", dt.date(2026, 1, 5), 1.0, 0.0),  # 就是首个 bar, 无 idx-1
        ]
    )
    assert ep._build_factors(raw, ev).height == 0


def test_build_factors_drops_out_of_range_ratio():
    """派息大于价格 → ratio <= 0, 属脏数据, 必须丢弃 (护栏与用户工具一致)。"""
    raw = _raw_frame("600519.SH", [5.0, 5.0])
    ev = _events_frame([("600519.SH", dt.date(2026, 1, 6), 9.0, 0.0)])
    assert ep._build_factors(raw, ev).height == 0


def test_build_factors_ignores_unknown_symbol():
    raw = _raw_frame("600519.SH", [10.0, 10.2])
    ev = _events_frame([("999999.SZ", dt.date(2026, 1, 6), 0.1, 0.0)])
    assert ep._build_factors(raw, ev).height == 0


def test_factors_produce_forward_adjustment_semantics():
    """端到端口径: 最新价不变, 历史价按 (P - 派息)/P 下调。"""
    raw = _raw_frame("600519.SH", [10.0, 10.2, 10.4, 9.9, 10.0]).with_columns(
        pl.col("close").alias("open"), pl.col("close").alias("high"), pl.col("close").alias("low")
    )
    ev = _events_frame([("600519.SH", dt.date(2026, 1, 8), 0.5, 0.0)])
    adj = _apply_adj_factor(raw, ep._build_factors(raw, ev)).sort("date")
    close = adj["close"].to_list()
    ratio = (10.4 - 0.5) / 10.4
    assert close[:3] == pytest.approx([10.0 * ratio, 10.2 * ratio, 10.4 * ratio])
    # 除权日及之后保持原价(前复权以最新价为锚)
    assert close[3:] == pytest.approx([9.9, 10.0])
    # ex_rights: 无因子的 bar 为 null(join_asof 未命中), 除权日当天为 True
    flags = adj["ex_rights"].to_list()
    assert flags[3] is True
    assert all(f is not True for i, f in enumerate(flags) if i != 3)


# ---------- provider 接线 ----------


def test_get_adj_factors_empty_input_returns_empty():
    p = ep.EmXdxrAdjProvider()
    assert p.get_adj_factors([], None, None).is_empty()
    assert p.get_adj_factors(["600519.SH"], None, None, asset_type="etf").is_empty()


def test_get_adj_factors_returns_contract_schema(monkeypatch):
    """表结构必须是 normalize_adj_factors / _apply_adj_factor 期望的三列。"""
    raw = _raw_frame("600519.SH", [10.0, 10.2, 10.4, 9.9])
    ev = _events_frame([("600519.SH", dt.date(2026, 1, 8), 0.5, 0.0)])
    monkeypatch.setattr(ep, "_load_raw_closes", lambda symbols: raw)
    monkeypatch.setattr(ep, "_fetch_events", lambda start, end: ev)
    out = ep.EmXdxrAdjProvider().get_adj_factors(["600519.SH"], None, None)
    assert out.columns == ["symbol", "trade_date", "ex_factor"]
    assert out.schema["symbol"] == pl.Utf8
    assert out.schema["trade_date"] == pl.Date
    assert out.schema["ex_factor"] == pl.Float64
    assert out.height == 1


def test_get_adj_factors_uses_local_kline_window(monkeypatch):
    """事件窗口必须覆盖本地K线全区间, 不能只用 start_time(否则历史 bar 因子残缺)。"""
    captured: dict = {}
    raw = _raw_frame("600519.SH", [10.0, 10.2, 10.4])

    def fake_fetch(start, end):
        captured["start"] = start
        captured["end"] = end
        return _events_frame([])

    monkeypatch.setattr(ep, "_load_raw_closes", lambda symbols: raw)
    monkeypatch.setattr(ep, "_fetch_events", fake_fetch)
    out = ep.EmXdxrAdjProvider().get_adj_factors(
        ["600519.SH"], dt.datetime(2026, 1, 7), dt.datetime(2026, 1, 8)
    )
    assert out.is_empty()
    assert captured["start"] == raw["date"].min()
    assert captured["end"] == raw["date"].max()


def test_get_adj_factors_without_local_kline_returns_empty(monkeypatch):
    monkeypatch.setattr(ep, "_load_raw_closes", lambda symbols: pl.DataFrame())
    assert ep.EmXdxrAdjProvider().get_adj_factors(["600519.SH"], None, None).is_empty()
