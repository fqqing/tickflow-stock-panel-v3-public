"""行业归属摄取服务回归测试。

覆盖: 长表落盘契约、新鲜度跳过、强制重抓、板块完整率闸门、无数据时不清空旧表、数据源路由。
"""
from __future__ import annotations

from datetime import timedelta

import polars as pl

from app.market_time import cn_today
from app.services import industry_sync


def _rows(n_boards: int = 2, n_symbols: int = 2) -> list[dict]:
    out = []
    for b in range(n_boards):
        for s in range(n_symbols):
            out.append({
                "board_code": f"BK{1300 + b}",
                "board_name": f"板块{b}",
                "symbol": f"60000{s}.SH",
                "code": f"60000{s}",
                "name": f"股票{s}",
                "board_change_pct": 1.0 * b,
                "price": 10.0,
                "change_pct": 0.5,
                "turnover_rate": 1.0,
                "pe": 20.0,
                "pb": 2.0,
            })
    return out


def _ok_payload(n_boards: int = 2, n_symbols: int = 2, **meta) -> dict:
    """完整的抓取结果(默认板块完整率 100%)。"""
    full_meta = {"boards_total": n_boards, "boards_requested": n_boards, "boards_ok": n_boards}
    full_meta.update(meta)
    return {"rows": _rows(n_boards, n_symbols), "meta": full_meta, "errors": {}}


def test_sync_writes_long_table_with_as_of(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: _ok_payload())
    n = industry_sync.sync_industry_members(tmp_path)

    assert n == 4
    df = industry_sync.load_industry_members(tmp_path)
    assert df.height == 4
    # 长表契约: 消费方按这些列名取值。
    for col in industry_sync.INDUSTRY_COLUMNS:
        assert col in df.columns
    assert "as_of" in df.columns
    assert df["as_of"].max() == cn_today()
    # 一只票可以命中多个板块 —— 这是刻意的(东财板块分层), 不能被去重掉。
    assert df["symbol"].n_unique() == 2
    assert df["board_code"].n_unique() == 2
    assert df.height == df["symbol"].n_unique() * df["board_code"].n_unique()


def test_sync_dedupes_same_symbol_board_pair(tmp_path, monkeypatch):
    payload = _ok_payload()
    payload["rows"].append(dict(payload["rows"][0]))  # 上游偶发重复行
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: payload)

    assert industry_sync.sync_industry_members(tmp_path) == 4


def test_sync_skips_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: _ok_payload())
    industry_sync.sync_industry_members(tmp_path)

    called = []
    monkeypatch.setattr(
        industry_sync, "_fetch_via_provider", lambda: called.append(1) or _ok_payload()
    )
    # 刚写完 → as_of 是今天 → 默认应跳过, 且不产生任何网络请求。
    assert industry_sync.sync_industry_members(tmp_path) == 0
    assert called == []


def test_sync_force_bypasses_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: _ok_payload())
    industry_sync.sync_industry_members(tmp_path)

    called = []
    monkeypatch.setattr(
        industry_sync, "_fetch_via_provider", lambda: called.append(1) or _ok_payload()
    )
    assert industry_sync.sync_industry_members(tmp_path, force=True) == 4
    assert called == [1]


def test_sync_refreshes_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: _ok_payload())
    industry_sync.sync_industry_members(tmp_path)

    stale = cn_today() - timedelta(days=industry_sync.INDUSTRY_REFRESH_DAYS + 1)
    # 手动把 as_of 改旧, 模拟表放了一段时间。
    out = industry_sync.industry_members_path(tmp_path)
    (
        industry_sync.load_industry_members(tmp_path)
        .with_columns(pl.lit(stale).alias("as_of"))
        .write_parquet(out)
    )

    assert (
        industry_sync.industry_members_age_days(tmp_path)
        == industry_sync.INDUSTRY_REFRESH_DAYS + 1
    )
    assert industry_sync.sync_industry_members(tmp_path) == 4


def test_sync_keeps_existing_when_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: _ok_payload())
    industry_sync.sync_industry_members(tmp_path)
    before = industry_sync.industry_members_path(tmp_path).read_bytes()

    # 无数据源 → 不能用空表覆盖既有数据。
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: None)
    assert industry_sync.sync_industry_members(tmp_path, force=True) == 0
    assert industry_sync.industry_members_path(tmp_path).read_bytes() == before

    # rows 为空(桥接整体失败)同理。
    monkeypatch.setattr(
        industry_sync, "_fetch_via_provider", lambda: {"rows": [], "meta": {}, "errors": {}}
    )
    assert industry_sync.sync_industry_members(tmp_path, force=True) == 0
    assert industry_sync.industry_members_path(tmp_path).read_bytes() == before


def test_sync_rejects_low_board_coverage(tmp_path, monkeypatch):
    """完整率不达标 → 不覆盖旧表。

    这是本模块最关键的一道闸门: 部分板块抓失败产出的表**看不出缺口**, 却带当天
    as_of, 会被下游当成新鲜完整数据用下去。
    """
    monkeypatch.setattr(industry_sync, "_fetch_via_provider", lambda: _ok_payload())
    industry_sync.sync_industry_members(tmp_path)
    before = industry_sync.industry_members_path(tmp_path).read_bytes()

    monkeypatch.setattr(
        industry_sync,
        "_fetch_via_provider",
        lambda: _ok_payload(boards_requested=100, boards_ok=50),
    )
    assert industry_sync.sync_industry_members(tmp_path, force=True) == 0
    assert industry_sync.industry_members_path(tmp_path).read_bytes() == before


def test_sync_accepts_coverage_at_threshold(tmp_path, monkeypatch):
    # 90/100 = 90% == 阈值(闸门用 <), 应放行。
    monkeypatch.setattr(
        industry_sync,
        "_fetch_via_provider",
        lambda: _ok_payload(boards_requested=100, boards_ok=90),
    )
    assert industry_sync.sync_industry_members(tmp_path) == 4


def test_sync_without_meta_still_writes(tmp_path, monkeypatch):
    """老桥接不回报 meta → 无法校验完整率, 仍落盘(否则功能不可用), 但需留痕。"""
    monkeypatch.setattr(
        industry_sync,
        "_fetch_via_provider",
        lambda: {"rows": _rows(), "meta": {}, "errors": {}},
    )
    assert industry_sync.sync_industry_members(tmp_path) == 4


def test_sync_skips_when_upstream_rows_lack_key_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(
        industry_sync,
        "_fetch_via_provider",
        lambda: {"rows": [{"name": "缺列"}], "meta": {}, "errors": {}},
    )
    assert industry_sync.sync_industry_members(tmp_path) == 0
    assert not industry_sync.industry_members_path(tmp_path).exists()


def test_load_missing_file_returns_empty(tmp_path):
    assert industry_sync.load_industry_members(tmp_path).is_empty()
    assert industry_sync.industry_members_age_days(tmp_path) is None


def test_fetch_via_provider_returns_none_for_tickflow(monkeypatch):
    """日K数据源是 tickflow 时不走 provider 分支(该源不提供行业数据)。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    assert industry_sync._fetch_via_provider() is None


def test_fetch_via_provider_uses_provider_when_available(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    class FakeProvider:
        def fetch_industry_members(self, boards=None):
            return _ok_payload()

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "stocksdk")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: name == "stocksdk")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: FakeProvider())

    result = industry_sync._fetch_via_provider()
    assert result is not None
    assert len(result["rows"]) == 4
    assert result["meta"]["boards_ok"] == 2


def test_fetch_via_provider_none_when_provider_lacks_method(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    class NoIndustry:
        pass

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "somehttp")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: NoIndustry())

    assert industry_sync._fetch_via_provider() is None
