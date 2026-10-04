"""策略加载窗口与缓存行为测试。

背景 (2026-09-17 实测):
  1. A 股 enriched 目录 (kline_daily_enriched) 里混着 71% 的港美股行 ——
     180 天窗口扫出 237 万行, 而 A 股只需要其中 68 万行。
  2. 慢路径窗口被硬截在 180 个日历日, lookback=201 时只剩 124 个交易日 ——
     策略声明的 200 根 bar 被静默砍掉四成 (upward_trend_breakout 选出 46 只,
     而给足窗口后是 42 只), 且与走预计算缓存的路径结果不一致。
  3. repo 预计算缓存的覆盖校验按 (lookback+60)x2 日历日估算, 本地数据只有
     245 个交易日, 于是永远判不覆盖 —— 预计算白跑, 每次策略运行都全市场重算。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.markets import MARKET_CN, MARKET_HK, MARKET_US
from app.parquet import market_symbol_filter
from app.services import screener as sc
from app.services.screener import ScreenerService
from app.tickflow.repository import _HISTORY_WARMUP_BARS, _REFRESH_HISTORY_DAYS, KlineRepository

# ── 市场过滤 ──────────────────────────────────────────────────────────


def test_symbol_filter_cn_keeps_only_a_share_suffixes():
    expr = market_symbol_filter(MARKET_CN)
    df = pl.DataFrame({"symbol": ["600000.SH", "000001.SZ", "920344.BJ", "00700.HK", "AAPL.US"]})
    assert df.filter(expr)["symbol"].to_list() == ["600000.SH", "000001.SZ", "920344.BJ"]


def test_symbol_filter_hk_and_us():
    df = pl.DataFrame({"symbol": ["600000.SH", "00700.HK", "AAPL.US"]})
    assert df.filter(market_symbol_filter(MARKET_HK))["symbol"].to_list() == ["00700.HK"]
    assert df.filter(market_symbol_filter(MARKET_US))["symbol"].to_list() == ["AAPL.US"]


def test_symbol_filter_unknown_market_returns_none():
    """未知市场不过滤 —— 宁可多算也不能漏标的。"""
    assert market_symbol_filter("jp") is None


# ── 窗口起点按交易日反推 ──────────────────────────────────────────────


def _weekdays(end: date, count: int) -> list[date]:
    days: list[date] = []
    cur = end
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
    return sorted(days)


def _service_with_dates(tmp_path: Path, days: list[date]) -> ScreenerService:
    for day in days:
        (tmp_path / "kline_daily_enriched" / f"date={day.isoformat()}").mkdir(parents=True)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return ScreenerService(repo, asset_type="stock", market="cn")  # type: ignore[arg-type]


def test_history_window_start_counts_trading_days_not_calendar_days(tmp_path):
    days = _weekdays(date(2026, 9, 17), 260)
    svc = _service_with_dates(tmp_path, days)
    start = svc._history_window_start(days[-1], 202)
    assert start == days[-202]
    # 交易日计数 ≠ 自然日: 202 个交易日跨约 280 个日历日
    assert (days[-1] - start).days > 202


def test_history_window_start_returns_earliest_when_not_enough_history(tmp_path):
    days = _weekdays(date(2026, 9, 17), 5)
    svc = _service_with_dates(tmp_path, days)
    assert svc._history_window_start(days[-1], 202) == days[0]


def test_history_window_start_falls_back_to_calendar_estimate(tmp_path):
    """目录不可读时回退到 交易日 x 2 的日历日估算, 不能抛异常。"""
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "missing"))
    svc = ScreenerService(repo, asset_type="stock", market="cn")  # type: ignore[arg-type]
    target = date(2026, 9, 17)
    assert svc._history_window_start(target, 202) == target - timedelta(days=404)


# ── 慢路径读盘时过滤其他市场 ──────────────────────────────────────────


class _FakeRepo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    def get_enriched_history(self, target_date, lookback_days):
        return None

    def get_instruments_asset(self, asset_type, market=None):
        return pl.DataFrame()

    def get_historical_shares(self):
        return pl.DataFrame()


def _write_partition(
    root: Path, day: date, symbols: list[str], dirname: str = "kline_daily_enriched"
) -> None:
    target = root / dirname / f"date={day.isoformat()}"
    target.mkdir(parents=True, exist_ok=True)
    n = len(symbols)
    pl.DataFrame({
        "symbol": symbols,
        "date": [day] * n,
        "open": [10.0] * n,
        "high": [10.5] * n,
        "low": [9.5] * n,
        "close": [10.2] * n,
        "volume": [1000.0] * n,
        "amount": [1_000_000.0] * n,
        "raw_close": [10.2] * n,
        "raw_high": [10.5] * n,
        "raw_low": [9.5] * n,
    }).write_parquet(target / "part.parquet")


def test_load_enriched_history_drops_other_markets(tmp_path):
    """A 股目录里混着港美股行时, 慢路径不能把它们算进指标。"""
    target = date(2026, 1, 6)
    _write_partition(tmp_path, target - timedelta(days=1), ["600000.SH", "00700.HK", "AAPL.US"])
    _write_partition(tmp_path, target, ["600000.SH", "00700.HK", "AAPL.US"])

    svc = ScreenerService(_FakeRepo(tmp_path), asset_type="stock", market="cn")
    df = svc._load_enriched_history(target, 1)
    svc.clear_history_cache()

    assert df["symbol"].unique().to_list() == ["600000.SH"]
    assert df["date"].max() == target


def test_load_enriched_history_keeps_rows_for_hk_market(tmp_path):
    """港美股路径同样过滤, 但保留自己市场的标的。"""
    target = date(2026, 1, 6)
    _write_partition(
        tmp_path, target, ["600000.SH", "00700.HK", "AAPL.US"], dirname="kline_daily_enriched_hk"
    )

    svc = ScreenerService(_FakeRepo(tmp_path), asset_type="stock", market="hk")
    df = svc._load_enriched_history(target, 1)
    svc.clear_history_cache()

    assert df["symbol"].unique().to_list() == ["00700.HK"]


# ── repo 预计算缓存的覆盖校验 (按交易日) ──────────────────────────────


def _repo_with_cache(days: list[date]) -> KlineRepository:
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_history_cache = pl.DataFrame({
        "symbol": ["600000.SH"] * len(days),
        "date": days,
    })
    return repo


def test_repo_cache_hit_returns_lookback_window():
    days = _weekdays(date(2026, 9, 17), 300)
    repo = _repo_with_cache(days)

    out = repo.get_enriched_history(days[-1], 201)

    assert out is not None
    # lookback + 1: 多给一根作为滚动窗口的前值
    assert out["date"].n_unique() == 202
    assert out["date"].min() == days[-202]
    assert out["date"].max() == days[-1]


def test_repo_cache_miss_when_lookback_exceeds_available_history():
    days = _weekdays(date(2026, 9, 17), 300)
    repo = _repo_with_cache(days)
    assert repo.get_enriched_history(days[-1], 301) is None


def test_repo_cache_miss_when_target_not_in_cache():
    """目标日期不是缓存里的交易日 (如周末) → 不覆盖。"""
    days = _weekdays(date(2026, 9, 17), 300)
    repo = _repo_with_cache(days)
    saturday = date(2026, 9, 19)
    assert saturday.weekday() >= 5
    assert repo.get_enriched_history(saturday, 1) is None


def test_repo_cache_miss_when_target_after_cache():
    days = _weekdays(date(2026, 9, 17), 300)
    repo = _repo_with_cache(days)
    assert repo.get_enriched_history(days[-1] + timedelta(days=7), 1) is None


def test_repo_cache_empty_or_missing_returns_none():
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_history_cache = None
    assert repo.get_enriched_history(date(2026, 9, 17), 1) is None
    repo._enriched_history_cache = pl.DataFrame()
    assert repo.get_enriched_history(date(2026, 9, 17), 1) is None


# ── 进程级 history 缓存: TTL 与容量 ───────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_history_cache():
    sc.ScreenerService.clear_history_cache()
    yield
    sc.ScreenerService.clear_history_cache()


def _frame(marker: int) -> pl.DataFrame:
    return pl.DataFrame({"marker": [marker]})


def test_history_cache_serves_within_ttl_and_expires_after():
    key = ("stock", date(2026, 9, 17), 201)
    sc._store_history_cache(key, _frame(1), now=1000.0)

    assert sc._load_history_cache(key, now=1000.0 + sc._HISTORY_CACHE_TTL - 1) is not None
    assert sc._load_history_cache(key, now=1000.0 + sc._HISTORY_CACHE_TTL + 1) is None
    # 过期条目应被顺手清掉
    assert key not in sc._history_cache


def test_history_cache_evicts_oldest_entry_beyond_capacity():
    limit = sc._HISTORY_CACHE_MAX
    for i in range(limit + 2):
        sc._store_history_cache(("stock", date(2026, 9, 1 + i), 201), _frame(i), now=1000.0 + i)

    assert len(sc._history_cache) == limit
    # 最久未写入的两条被淘汰
    assert ("stock", date(2026, 9, 1), 201) not in sc._history_cache
    assert ("stock", date(2026, 9, 2), 201) not in sc._history_cache
    assert ("stock", date(2026, 9, 1 + limit + 1), 201) in sc._history_cache


def test_history_cache_ttl_is_long_enough_to_survive_date_hopping():
    """切日期来回看时不该每 2 分钟就重付一次全市场重算 (旧值 120 秒)。"""
    assert sc._HISTORY_CACHE_TTL >= 900.0


# ── 常量契约 ──────────────────────────────────────────────────────────


def test_refresh_window_covers_longest_strategy_lookback():
    """预计算窗口必须覆盖「最长 lookback + 指标 warmup」, 否则缓存永远判不覆盖。"""
    longest_lookback = 261  # upward_trend_breakout: 实测 EMA89 收敛点 + 1 根余量
    needed_bars = longest_lookback + _HISTORY_WARMUP_BARS
    approx_trading_days = _REFRESH_HISTORY_DAYS / 7 * 5
    assert approx_trading_days >= needed_bars
