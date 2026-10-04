"""eltdx(通达信)分钟K provider 纯逻辑测试(不依赖网络)。

网络相关的部分一律 monkeypatch 掉模块级的 ``_client``, 只验证:
代码映射 / 交易日展开 / 结果字段与量纲 / 非本市场过滤 / 不支持周期回落 /
进度回调 / 数据集声明 / 容错。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.plugins.eltdx import provider as eltdx_mod
from app.plugins.eltdx.provider import (
    _BARS_PER_DAY,
    EltdxMinuteProvider,
    _trading_days,
    app_to_eltdx,
)


class _FakeBar:
    def __init__(self, time, o, h, low, c, vol, amt):
        self.time = time
        self.open = o
        self.high = h
        self.low = low
        self.close = c
        self.volume_lots = vol
        self.amount = amt


class _FakeSeries:
    def __init__(self, bars):
        self.bars = bars


class _FakeBars:
    """记录调用参数, 按构造时的映射返回结果。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[dict] = []

    def get(self, codes, *, period, count, anchor_date):
        self.calls.append(
            {"codes": list(codes), "period": period, "count": count, "anchor_date": anchor_date}
        )
        out = {}
        for c in codes:
            series = self.mapping.get(c)
            if series is not None:
                out[c] = series
        return out


class _FakeClient:
    def __init__(self, mapping):
        self.bars = _FakeBars(mapping)


@pytest.fixture
def provider():
    return EltdxMinuteProvider()


# ---------------- 代码映射 ----------------

def test_app_to_eltdx_maps_a_share_markets():
    assert app_to_eltdx("600519.SH") == "sh600519"
    assert app_to_eltdx("000001.SZ") == "sz000001"
    assert app_to_eltdx("920002.BJ") == "bj920002"


def test_app_to_eltdx_rejects_foreign_markets():
    """港美股必须被过滤, 否则会白烧请求且污染结果。"""
    assert app_to_eltdx("AAPL.US") is None
    assert app_to_eltdx("00700.HK") is None
    assert app_to_eltdx("BTC.US") is None


def test_app_to_eltdx_rejects_malformed():
    assert app_to_eltdx("") is None
    assert app_to_eltdx("600519") is None
    assert app_to_eltdx(".SH") is None


# ---------------- 交易日展开 ----------------

def test_trading_days_skips_weekend():
    # 2026-09-26 是周六, 2026-09-27 周日; 窗口 09-25(周五) ~ 09-28(周一)
    days = _trading_days(datetime(2026, 9, 25), datetime(2026, 9, 28))
    assert days  # 非空
    assert all(d.weekday() < 5 for d in days)


def test_trading_days_newest_first():
    days = _trading_days(datetime(2026, 9, 1), datetime(2026, 9, 10))
    assert days == sorted(days, reverse=True)


def test_trading_days_respects_max_days():
    days = _trading_days(datetime(2026, 1, 1), datetime(2026, 9, 30), max_days=5)
    assert len(days) == 5


def test_trading_days_defaults_to_today_when_none():
    days = _trading_days(None, None)
    assert len(days) == 1
    assert days[0] <= date.today()


# ---------------- 数据字段与量纲 ----------------

def _make_mapping(day: date, codes=("sh600519",)) -> dict:
    bars = [
        _FakeBar(datetime(day.year, day.month, day.day, 9, 31), 10.0, 10.5, 9.9, 10.2, 100.0, 102000.0),
        _FakeBar(datetime(day.year, day.month, day.day, 9, 32), 10.2, 10.4, 10.1, 10.3, 50.0, 51500.0),
    ]
    return {c: _FakeSeries(list(bars)) for c in codes}


def test_get_minute_schema_and_units(provider, monkeypatch):
    day = date(2026, 9, 29)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient(_make_mapping(day)))
    df = provider.get_minute(
        ["600519.SH"],
        datetime(2026, 9, 29, 9, 25),
        datetime(2026, 9, 29, 15, 5),
    )
    assert not df.is_empty()
    for col in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount"):
        assert col in df.columns
    row = df.row(0, named=True)
    # volume 用 volume_lots(手), amount 用上游真实成交额(元), 不做换算
    assert row["volume"] == 100.0
    assert row["amount"] == 102000.0
    assert row["close"] == 10.2


def test_get_minute_datetime_is_naive_beijing(provider, monkeypatch):
    """与腾讯 provider 一致: 落成北京墙钟的 naive datetime, 下游不再换算时区。"""
    day = date(2026, 9, 29)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient(_make_mapping(day)))
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    dt = df["datetime"][0]
    assert dt.tzinfo is None
    assert dt.hour == 9 and dt.minute == 31


def test_get_minute_uses_anchor_date_and_per_day_count(provider, monkeypatch):
    """必须按天用 anchor_date 取窗口(不能用 all_pages, 会抛 RuntimeError)。"""
    day = date(2026, 9, 29)
    fake = _FakeClient(_make_mapping(day))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: fake)
    provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert fake.bars.calls, "应至少发起一次请求"
    call = fake.bars.calls[0]
    assert call["period"] == "1m"
    assert call["count"] == _BARS_PER_DAY["1m"]
    assert call["anchor_date"] == day


# ---------------- 过滤与回落 ----------------

def test_get_minute_filters_foreign_symbols(provider, monkeypatch):
    day = date(2026, 9, 29)
    fake = _FakeClient(_make_mapping(day))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: fake)
    df = provider.get_minute(
        ["AAPL.US", "00700.HK"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5)
    )
    assert df.is_empty()
    assert not fake.bars.calls, "全部被过滤时不应发起任何请求"


def test_get_minute_callbacks_once_when_all_filtered(provider, monkeypatch):
    """全滤空仍要回调一次, 否则前端进度条卡在 0。"""
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient({}))
    seen: list[tuple[int, int]] = []
    provider.get_minute(["AAPL.US"], None, None, on_chunk_done=lambda c, t: seen.append((c, t)))
    assert seen == [(1, 1)]


def test_get_minute_returns_empty_for_unsupported_freq(provider, monkeypatch):
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient({}))
    for freq in ("90m", "120m", "1d"):
        df = provider.get_minute(["600519.SH"], None, None, freq=freq)
        assert df.is_empty(), f"{freq} 应返回空交由调用方回落"


def test_get_minute_empty_symbols(provider):
    assert provider.get_minute([], None, None).is_empty()


def test_get_minute_drops_bars_of_other_days(provider, monkeypatch):
    """非交易日的 anchor 会拿到前一交易日尾部, 必须按日历日过滤掉。"""
    day = date(2026, 9, 29)
    prev = date(2026, 9, 28)
    bars = [
        _FakeBar(datetime(prev.year, prev.month, prev.day, 14, 59), 1.0, 1.0, 1.0, 1.0, 1.0, 100.0),
        _FakeBar(datetime(day.year, day.month, day.day, 9, 31), 10.0, 10.5, 9.9, 10.2, 100.0, 102000.0),
    ]
    monkeypatch.setattr(
        eltdx_mod, "_client", lambda: _FakeClient({"sh600519": _FakeSeries(bars)})
    )
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert df.height == 1
    assert df["datetime"][0].date() == day


# ---------------- 容错 ----------------

def test_get_minute_survives_upstream_error(provider, monkeypatch):
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    class _Cli:
        bars = _Boom()

    monkeypatch.setattr(eltdx_mod, "_client", lambda: _Cli())
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert df.is_empty()


def test_get_minute_falls_back_amount_when_missing(provider, monkeypatch):
    """上游缺 amount 时用 成交量(手) x 100 x 收盘价 估算, 不能整行丢弃。"""
    bars = [_FakeBar(datetime(2026, 9, 29, 9, 31), 10.0, 10.5, 9.9, 10.2, 100.0, None)]
    monkeypatch.setattr(
        eltdx_mod, "_client", lambda: _FakeClient({"sh600519": _FakeSeries(bars)})
    )
    df = provider.get_minute(["600519.SH"], datetime(2026, 9, 29, 9, 25), datetime(2026, 9, 29, 15, 5))
    assert df.height == 1
    assert df["amount"][0] == pytest.approx(100.0 * 100 * 10.2)


# ---------------- 契约 ----------------

def test_dataset_declaration(provider):
    assert "minute" in provider.config.datasets
    assert provider.name == "eltdx"
    assert provider.builtin is True


def test_close_is_noop(provider):
    assert provider.close() is None


def test_test_dataset_minute(provider, monkeypatch):
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeClient(_make_mapping(date.today())))
    info = provider.test_dataset("minute", ["600519.SH"])
    assert info["provider"] == "eltdx"
    assert info["dataset"] == "minute"
    assert info["rows"] >= 0


def test_test_dataset_rejects_unknown(provider):
    """daily 已支持(见下节), 未声明的才应报错。"""
    with pytest.raises(ValueError):
        provider.test_dataset("financial")


def test_bars_per_day_matches_a_share_session():
    """A股一天 4 小时连续竞价: 1m=240, 5m=48, 15m=16, 30m=8, 60m=4。"""
    assert _BARS_PER_DAY["1m"] == 240
    assert _BARS_PER_DAY["5m"] == 48
    assert _BARS_PER_DAY["15m"] == 16
    assert _BARS_PER_DAY["30m"] == 8
    assert _BARS_PER_DAY["60m"] == 4


# ---- realtime (quotes.get_snapshots) ----

class _FakeSnap:
    """QuoteSnapshot 的最小替身。change_pct 按**百分数**给(上游实测口径)。"""

    def __init__(self, exchange, code, **kw):
        self.exchange = exchange
        self.code = code
        self.last_price = kw.get("last_price", 10.0)
        self.pre_close_price = kw.get("pre_close_price", 10.0)
        self.open_price = kw.get("open_price", 10.0)
        self.high_price = kw.get("high_price", 10.2)
        self.low_price = kw.get("low_price", 9.8)
        self.total_hand = kw.get("total_hand", 1000)
        self.amount = kw.get("amount", 10.0 * 1000 * 100)
        self.change_pct = kw.get("change_pct", 1.5)
        self.change = kw.get("change", 0.15)
        self.time_raw = kw.get("time_raw", 15174239)


class _FakeQuotes:
    """记录每次批量调用; 含 fail 集合里的代码时整批抛错(模拟老三板拖垮整批)。"""

    def __init__(self, snaps, fail=()):
        self.snaps = {f"{s.exchange}{s.code}": s for s in snaps}
        self.fail = set(fail)
        self.calls: list[list[str]] = []

    def get_snapshots(self, codes):
        self.calls.append(list(codes))
        if any(c in self.fail for c in codes):
            raise RuntimeError("snapshot record marker not found")
        return [self.snaps[c] for c in codes if c in self.snaps]


class _FakeSnapClient:
    def __init__(self, quotes):
        self.quotes = quotes


def test_datasets_declare_realtime():
    assert "realtime" in eltdx_mod._DATASETS
    assert "minute" in eltdx_mod._DATASETS
    assert EltdxMinuteProvider.supports_index_realtime is True


def test_parse_snap_normalizes_units():
    """change_pct 上游是百分数(1.5) => 契约要小数(0.015); volume=手 / amount=元。"""
    snap = _FakeSnap("sh", "600519", last_price=10.0, total_hand=1000, amount=1_000_000.0)
    row = eltdx_mod._parse_snap(snap, {"name": "贵州茅台", "float_shares": 100_000_000})
    assert row is not None
    assert row["symbol"] == "600519.SH"
    assert row["name"] == "贵州茅台"
    assert row["change_pct"] == pytest.approx(0.015)
    assert row["volume"] == 1000.0
    assert row["amount"] == 1_000_000.0
    # 换手率: volume(手) x 100 / 流通股本(股)
    assert row["turnover_rate"] == pytest.approx(1000 * 100 / 100_000_000)
    assert row["amplitude"] == pytest.approx((10.2 - 9.8) / 10.0)
    assert row["timestamp"] > 0


def test_parse_snap_suspended_and_invalid():
    """停牌(last=0)用前收补; 前收也为 0 时整行丢弃。"""
    row = eltdx_mod._parse_snap(_FakeSnap("sz", "000001", last_price=0.0, pre_close_price=8.0))
    assert row is not None and row["last_price"] == 8.0
    assert eltdx_mod._parse_snap(_FakeSnap("sz", "000001", last_price=0.0, pre_close_price=0.0)) is None
    assert eltdx_mod._parse_snap(_FakeSnap("xx", "000001")) is None  # 未知市场


def test_snap_timestamp_bounds():
    assert eltdx_mod._snap_timestamp(15174239) is not None
    assert eltdx_mod._snap_timestamp(0) is None
    assert eltdx_mod._snap_timestamp(None) is None
    assert eltdx_mod._snap_timestamp("x") is None
    assert eltdx_mod._snap_timestamp(99999999) is None  # 小时越界


def test_fetch_snap_batch_splits_around_bad_code():
    """整批失败时二分降级: 4 只里有 1 只坏代码, 其余 3 只必须都拿到。

    实测 bj830799(老三板)会让整批 ProtocolError —— 无法预先枚举黑名单, 只能二分。
    """
    snaps = [_FakeSnap("sh", f"60000{i}") for i in range(4)]
    quotes = _FakeQuotes(snaps, fail={"sh600002"})
    monkeypatch_client = _FakeSnapClient(quotes)
    import app.plugins.eltdx.provider as mod

    original = mod._client
    mod._client = lambda: monkeypatch_client
    try:
        got = mod._fetch_snap_batch([f"sh60000{i}" for i in range(4)])
    finally:
        mod._client = original
    assert len(got) == 3
    assert {"sh600000", "sh600001", "sh600003"} == {f"{s.exchange}{s.code}" for s in got}


def test_get_realtime_excludes_index(monkeypatch):
    """get_realtime 枚举全市场时不能混入指数(指数由 get_index_realtime 按码单拉)。"""
    snaps = [_FakeSnap("sh", "600519"), _FakeSnap("sh", "000001")]
    quotes = _FakeQuotes(snaps)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeSnapClient(quotes))
    monkeypatch.setattr(
        eltdx_mod,
        "_local_market_meta",
        lambda: {
            "600519.SH": {"name": "贵州茅台", "float_shares": 1.0, "is_index": False},
            "000001.SH": {"name": "上证指数", "float_shares": None, "is_index": True},
        },
    )
    rows = EltdxMinuteProvider().get_realtime()
    assert [r["symbol"] for r in rows] == ["600519.SH"]


def test_get_realtime_empty_without_dim_table(monkeypatch):
    monkeypatch.setattr(eltdx_mod, "_local_market_meta", lambda: {})
    assert EltdxMinuteProvider().get_realtime() == []


def test_index_realtime_by_code_list(monkeypatch):
    quotes = _FakeQuotes([_FakeSnap("sh", "000001"), _FakeSnap("sz", "399001")])
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeSnapClient(quotes))
    monkeypatch.setattr(
        eltdx_mod, "_local_market_meta",
        lambda: {"000001.SH": {"name": "上证指数", "is_index": True},
                 "399001.SZ": {"name": "深证成指", "is_index": True}},
    )
    rows = EltdxMinuteProvider().get_index_realtime(["000001.SH", "399001.SZ"])
    assert {r["symbol"] for r in rows} == {"000001.SH", "399001.SZ"}
    assert all(r["name"] for r in rows)
    assert EltdxMinuteProvider().get_index_realtime([]) == []


def test_test_dataset_realtime(monkeypatch):
    quotes = _FakeQuotes([_FakeSnap("sh", "600519"), _FakeSnap("sh", "000001")])
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeSnapClient(quotes))
    monkeypatch.setattr(eltdx_mod, "_local_market_meta", lambda: {})
    info = EltdxMinuteProvider().test_dataset("realtime")
    assert info["dataset"] == "realtime"
    assert info["rows"] == 2
    assert info["columns"] == eltdx_mod._RT_COLUMNS


# ---- daily (bars.get period="day") ----

def test_datasets_declare_daily():
    assert "daily" in eltdx_mod._DATASETS
    assert "daily" in EltdxMinuteProvider().config.datasets


def test_is_index_symbol():
    """上证 000xxx / 深证 399xxx 是指数; 股票与 ETF 不得误判。"""
    assert eltdx_mod._is_index_symbol("000001.SH") is True
    assert eltdx_mod._is_index_symbol("399001.SZ") is True
    assert eltdx_mod._is_index_symbol("600519.SH") is False
    assert eltdx_mod._is_index_symbol("000001.SZ") is False  # 平安银行
    assert eltdx_mod._is_index_symbol("510300.SH") is False  # ETF


def test_estimate_day_bars():
    end = date(2026, 9, 30)
    # 无起点: 只取一页
    assert eltdx_mod._estimate_day_bars(None, end) == eltdx_mod._DAY_PAGE_MAX
    # 一年约 243 个交易日, 按 0.70 估 => 约 256, 留足余量
    est = eltdx_mod._estimate_day_bars(date(2025, 9, 30), end)
    assert 243 <= est <= 300
    # 超长窗口被段数上限封顶
    assert eltdx_mod._estimate_day_bars(date(1990, 1, 1), end) == (
        eltdx_mod._DAY_PAGE_MAX * eltdx_mod._DAY_MAX_SEGMENTS
    )


def test_page_size_cap_is_800():
    """上游硬限制: count > 800 直接 ValueError, 长历史只能分段。"""
    assert eltdx_mod._DAY_PAGE_MAX == 800


class _FakeDayBars:
    """按 mapping 返回**固定**日K(不管 anchor), 记录调用参数。

    故意不做 anchor 过滤, 以便验证 provider 自己会丢弃 anchor 之后的 bar。
    """

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[dict] = []

    def get(self, codes, *, period, count, anchor_date):
        self.calls.append(
            {"codes": list(codes), "period": period, "count": count, "anchor_date": anchor_date}
        )
        return {c: self.mapping[c] for c in codes if c in self.mapping}


class _FakeDayBarsStream:
    """按 anchor **动态生成**一段日K(模拟上游有连续历史), 用于验证分段。

    per_code=1 时 earliest == anchor, 用来模拟"上游已到历史尽头"。
    """

    def __init__(self, codes, per_code: int = 3, price: float = 10.0):
        self.codes = set(codes)
        self.per_code = per_code
        self.price = price
        self.calls: list[dict] = []

    def get(self, codes, *, period, count, anchor_date):
        self.calls.append(
            {"codes": list(codes), "period": period, "count": count, "anchor_date": anchor_date}
        )
        out = {}
        n = min(self.per_code, count)
        for c in codes:
            if c not in self.codes:
                continue
            bars = []
            for i in reversed(range(n)):
                d = anchor_date - timedelta(days=i)
                bars.append(
                    _FakeBar(
                        datetime(d.year, d.month, d.day, 15, 0),
                        self.price, self.price + 0.5, self.price - 0.5, self.price,
                        100.0, 100.0 * 100 * self.price,
                    )
                )
            out[c] = _FakeSeries(bars)
        return out


class _FakeDayClient:
    def __init__(self, bars):
        self.bars = bars


def _day_series(code: str, days: list[date], base_price: float = 10.0):
    """构造日K series; volume 用 100 手, amount = 100*100*price。"""
    bars = [
        _FakeBar(
            datetime(d.year, d.month, d.day, 15, 0),
            base_price, base_price + 0.5, base_price - 0.5, base_price,
            100.0, 100.0 * 100 * base_price,
        )
        for d in days
    ]
    return {code: _FakeSeries(bars)}


def test_get_daily_schema_and_units(monkeypatch):
    """日K schema = DAILY_COLS, date 是 Date, volume=手 / amount=元。"""
    day = date(2026, 9, 30)
    fake = _FakeDayBars(_day_series("sh600519", [day]))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    df = EltdxMinuteProvider().get_daily(
        ["600519.SH"], datetime(2026, 9, 29), datetime(2026, 9, 30), "stock"
    )
    assert not df.is_empty()
    for col in ("symbol", "date", "open", "high", "low", "close", "volume", "amount"):
        assert col in df.columns
    row = df.row(0, named=True)
    assert row["date"] == day
    assert row["volume"] == 100.0
    assert row["amount"] == pytest.approx(100.0 * 100 * 10.0)
    assert df.schema["date"] == pl.Date


def test_get_daily_index_volume_scaled_by_100(monkeypatch):
    """指数: 上游是「手」, 存量口径是「股」 => 必须 x100。"""
    day = date(2026, 9, 30)
    fake = _FakeDayBars(_day_series("sh000001", [day]))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    df = EltdxMinuteProvider().get_daily(
        ["000001.SH"], datetime(2026, 9, 29), datetime(2026, 9, 30), "index"
    )
    assert df["volume"][0] == pytest.approx(100.0 * 100)
    # amount 不换算
    assert df["amount"][0] == pytest.approx(100.0 * 100 * 10.0)


def test_get_daily_etf_not_scaled(monkeypatch):
    """ETF 与股票同口径(手), 不得误用指数的 x100。"""
    day = date(2026, 9, 30)
    fake = _FakeDayBars(_day_series("sh510300", [day]))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    df = EltdxMinuteProvider().get_daily(
        ["510300.SH"], datetime(2026, 9, 29), datetime(2026, 9, 30), "etf"
    )
    assert df["volume"][0] == pytest.approx(100.0)


def test_get_daily_filters_out_of_window(monkeypatch):
    """窗口外的 bar 必须被过滤(上游按 count 返回, 不认 start/end)。"""
    days = [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)]
    fake = _FakeDayBars(_day_series("sh600519", days))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    df = EltdxMinuteProvider().get_daily(
        ["600519.SH"], datetime(2026, 9, 28), datetime(2026, 9, 29), "stock"
    )
    got = sorted(df["date"].to_list())
    assert got == [date(2026, 9, 28), date(2026, 9, 29)]


def test_get_daily_segments_long_history(monkeypatch):
    """count 上限 800 => 长历史必须分段, anchor 逐段前移。"""
    fake = _FakeDayBarsStream(["sh600519"], per_code=3)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    EltdxMinuteProvider().get_daily(["600519.SH"], datetime(2020, 1, 1), datetime(2026, 9, 30), "stock")
    assert len(fake.calls) >= 2, "长窗口应触发多段请求"
    anchors = [c["anchor_date"] for c in fake.calls]
    assert anchors == sorted(anchors, reverse=True), "anchor 必须逐段前移"
    assert len(set(anchors)) == len(anchors), "anchor 不得重复(否则死循环)"
    assert all(c["count"] <= eltdx_mod._DAY_PAGE_MAX for c in fake.calls)
    assert all(c["period"] == "day" for c in fake.calls)


def test_get_daily_stops_when_upstream_exhausted(monkeypatch):
    """上游不再往前给数据(earliest == anchor)时必须停止, 不能空转到段数上限。"""
    fake = _FakeDayBarsStream(["sh600519"], per_code=1)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    EltdxMinuteProvider().get_daily(["600519.SH"], datetime(1990, 1, 1), datetime(2026, 9, 30), "stock")
    assert len(fake.calls) == 1


def test_get_daily_progress_callback_completes(monkeypatch):
    """提前结束时必须补一次进度到 total, 否则前端进度条卡住。"""
    fake = _FakeDayBarsStream(["sh600519"], per_code=1)
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    seen: list[tuple[int, int]] = []
    EltdxMinuteProvider().get_daily(
        ["600519.SH"], datetime(1990, 1, 1), datetime(2026, 9, 30), "stock",
        on_chunk_done=lambda c, t: seen.append((c, t)),
    )
    assert seen
    assert seen[-1][0] == seen[-1][1], "最后一个进度必须是 100%"


def test_get_daily_filters_foreign_and_empty(monkeypatch):
    provider = EltdxMinuteProvider()
    assert provider.get_daily([], None, None).is_empty()
    fake = _FakeDayBars({})
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    assert provider.get_daily(["AAPL.US", "00700.HK"], None, None).is_empty()
    assert not fake.calls, "全部被过滤时不应发起任何请求"


def test_get_daily_survives_upstream_error(monkeypatch):
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    class _Cli:
        bars = _Boom()

    monkeypatch.setattr(eltdx_mod, "_client", lambda: _Cli())
    df = EltdxMinuteProvider().get_daily(["600519.SH"], datetime(2026, 9, 1), datetime(2026, 9, 30))
    assert df.is_empty()


def test_get_daily_drops_future_bars(monkeypatch):
    """anchor 之后的 bar 必须丢弃, 否则会引入未来数据。"""
    future = date(2026, 10, 5)
    day = date(2026, 9, 30)
    fake = _FakeDayBars(_day_series("sh600519", [day, future]))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    df = EltdxMinuteProvider().get_daily(
        ["600519.SH"], datetime(2026, 9, 29), datetime(2026, 9, 30), "stock"
    )
    assert date(2026, 10, 5) not in df["date"].to_list()


def test_test_dataset_daily(monkeypatch):
    day = date(2026, 9, 30)
    fake = _FakeDayBars(_day_series("sh600519", [day]))
    monkeypatch.setattr(eltdx_mod, "_client", lambda: _FakeDayClient(fake))
    info = EltdxMinuteProvider().test_dataset("daily", ["600519.SH"])
    assert info["dataset"] == "daily"
    assert info["rows"] == 1
    assert "close" in info["columns"]
