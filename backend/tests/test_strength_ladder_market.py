"""Tests for the HK/US market strength ladder (app/api/screener.py).

Covers _limit_ladder_market, the HK/US re-interpretation of the A-share
limit-up ladder (boards = 20d momentum tier, status = high/momentum/volume,
counts = 60d new-high/new-low counts). This is the endpoint the HK/US ladder
page actually consumes, via GET /api/screener/limit-ladder?market=hk|us.

The data store is stubbed with a minimal repo that returns a prepared
enriched frame through get_enriched_latest_market (the same cache path the
service uses for the latest trading date), so the tests exercise the real
ladder logic without recomputing indicators from raw parquet.
"""
from __future__ import annotations

import types
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.api.screener import _limit_ladder_market


class _FakeRepo:
    """Minimal repo stub: only the market-enriched read surface is needed."""

    def __init__(
        self,
        enriched: pl.DataFrame,
        latest: date | None,
        data_dir: Path,
    ) -> None:
        self.store = types.SimpleNamespace(data_dir=data_dir)
        self._enriched = enriched
        self._latest = latest

    def latest_enriched_date_market(self, market: str) -> date | None:
        return self._latest

    def get_enriched_latest_market(
        self, market: str, symbols: list[str] | None = None
    ) -> tuple[pl.DataFrame, date | None]:
        return self._enriched, self._latest


def _make_request(repo: _FakeRepo) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(repo=repo))
    )


def _base_row(symbol: str, **overrides: object) -> dict[str, object]:
    """One enriched row with neutral values; override per test."""
    row: dict[str, object] = {
        "symbol": symbol,
        "name": f"name-{symbol}",
        "close": 10.0,
        "change_pct": 0.01,
        "amount": 1_000_000.0,
        "momentum_20d": 0.05,
        "vol_ratio_5d": 0.5,
        "high_60d": 10.0,
        "low_60d": 9.0,
        "signal_n_day_high": False,
        "signal_n_day_low": False,
    }
    row.update(overrides)
    return row


def _run_ladder(
    repo: _FakeRepo,
    market: str = "hk",
    as_of: date | None = None,
) -> dict:
    return _limit_ladder_market(_make_request(repo), market, as_of)


def _flatten_tiers(result: dict) -> dict[str, dict]:
    """symbol -> stock dict, flattened over every tier."""
    flat: dict[str, dict] = {}
    for tier in result["tiers"]:
        for stock in tier["stocks"]:
            flat[str(stock["symbol"])] = stock
    return flat


# ---------------------------------------------------------------------------
# Momentum tier boundaries (boards)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("momentum", "expected_boards"),
    [
        (0.30, 5),
        (0.25, 5),            # inclusive upper boundary of tier 5
        (0.25 - 1e-9, 4),     # just below 25% falls to tier 4
        (0.15, 4),            # inclusive upper boundary of tier 4
        (0.15 - 1e-9, 3),     # just below 15% falls to tier 3
        (0.08, 3),            # inclusive upper boundary of tier 3
        (0.08 - 1e-9, 2),     # just below 8% falls to tier 2
        (0.03, 2),            # inclusive lower boundary of tier 2
    ],
)
def test_momentum_tier_boundaries(tmp_path, momentum, expected_boards) -> None:
    df = pl.DataFrame([_base_row("A", momentum_20d=momentum)])
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    stocks = _flatten_tiers(result)
    assert stocks["A"]["boards"] == expected_boards
    assert [t["boards"] for t in result["tiers"]] == [expected_boards]


@pytest.mark.parametrize("momentum", [0.03 - 1e-9, 0.02, 0.0, -0.1])
def test_momentum_below_three_percent_is_excluded(
    tmp_path, momentum: float
) -> None:
    df = pl.DataFrame([_base_row("A", momentum_20d=momentum)])
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    assert result["tiers"] == []


def test_null_momentum_is_treated_as_zero_and_excluded(tmp_path) -> None:
    df = pl.DataFrame([_base_row("A", momentum_20d=None)])
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    assert result["tiers"] == []


# ---------------------------------------------------------------------------
# Status classification and precedence (high > volume > momentum)
# ---------------------------------------------------------------------------
def test_status_precedence_high_over_volume_over_momentum(tmp_path) -> None:
    rows = [
        # high only: close at the 60d high (>= 99.5%) but weak volume
        _base_row("H", close=10.0, high_60d=10.0, vol_ratio_5d=0.5),
        # high AND volume: high must win the precedence
        _base_row("HV", close=10.0, high_60d=10.0, vol_ratio_5d=2.0),
        # volume only: no new high, strong volume, positive change
        _base_row("V", close=8.0, high_60d=9.0, vol_ratio_5d=2.0, change_pct=0.02),
        # volume AND momentum: volume must win over momentum
        _base_row(
            "VM", close=8.0, high_60d=9.0, vol_ratio_5d=2.0,
            momentum_20d=0.06, change_pct=0.02,
        ),
        # momentum only: no new high, no volume surge
        _base_row(
            "M", close=8.0, high_60d=9.0, vol_ratio_5d=0.5,
            momentum_20d=0.06, change_pct=-0.02,
        ),
        # volume ratio >= 1.5 but flat close: volume requires change_pct > 0
        _base_row(
            "VZ", close=8.0, high_60d=9.0, vol_ratio_5d=2.0,
            momentum_20d=0.06, change_pct=0.0,
        ),
        # not a close-based new high, but the signal column marks it
        _base_row(
            "SH", close=8.0, high_60d=9.0, vol_ratio_5d=0.5,
            signal_n_day_high=True,
        ),
    ]
    df = pl.DataFrame(rows)
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    statuses = {sym: stock["status"] for sym, stock in _flatten_tiers(result).items()}
    assert statuses == {
        "H": "high",
        "HV": "high",     # high beats volume
        "V": "volume",
        "VM": "volume",   # volume beats momentum
        "M": "momentum",
        "VZ": "momentum",  # volume needs change_pct > 0
        "SH": "high",      # signal_n_day_high also triggers high
    }


# ---------------------------------------------------------------------------
# 60d new-high / new-low counts
# ---------------------------------------------------------------------------
def test_counts_use_signal_columns_over_all_rows_including_excluded(
    tmp_path,
) -> None:
    rows = [
        _base_row("s1", momentum_20d=0.30, signal_n_day_high=True),
        _base_row("s2", momentum_20d=0.20, signal_n_day_low=True),
        # below 3%: excluded from tiers but still counted by signal columns
        _base_row("s3", momentum_20d=0.01, signal_n_day_high=True),
        # null signals: fill_null(False) -> not counted
        _base_row("s4", signal_n_day_high=None, signal_n_day_low=None),
        _base_row("s5"),
    ]
    df = pl.DataFrame(rows)
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    assert result["counts"] == {"up": 2, "down": 1}
    assert result["counts_raw"] == result["counts"]
    tier_symbols = [s["symbol"] for t in result["tiers"] for s in t["stocks"]]
    assert "s3" not in tier_symbols


def test_counts_fallback_to_high_low_60d_without_signal_columns(tmp_path) -> None:
    rows = [
        # close >= high_60d * 0.995 -> counts as new high
        _base_row("a", close=10.0, high_60d=10.0, low_60d=9.0),
        # close below 99.5% of high_60d -> not a new high
        _base_row("b", close=9.5, high_60d=10.0, low_60d=9.0),
        # close <= low_60d * 1.005 -> counts as new low
        _base_row("c", close=8.0, high_60d=9.0, low_60d=8.5),
        # close above 100.5% of low_60d -> not a new low
        _base_row("d", close=8.0, high_60d=9.0, low_60d=7.0),
    ]
    df = pl.DataFrame(rows).drop(["signal_n_day_high", "signal_n_day_low"])
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    assert result["counts"] == {"up": 1, "down": 1}


# ---------------------------------------------------------------------------
# Graceful degradation with missing optional columns
# ---------------------------------------------------------------------------
def test_missing_vol_ratio_5d_degrades_gracefully(tmp_path) -> None:
    df = pl.DataFrame([_base_row("A", close=10.0, high_60d=10.0)]).drop(
        "vol_ratio_5d"
    )
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    stocks = _flatten_tiers(result)
    # no volume statuses possible; the close-based high still shows
    assert stocks["A"]["status"] == "high"


def test_missing_signal_columns_degrades_gracefully(tmp_path) -> None:
    df = pl.DataFrame(
        [
            # close-based new high
            _base_row("A", close=10.0, high_60d=10.0, low_60d=9.0),
            # close-based new low
            _base_row("B", close=8.0, high_60d=9.0, low_60d=8.5),
        ]
    ).drop(["signal_n_day_high", "signal_n_day_low"])
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    stocks = _flatten_tiers(result)
    assert stocks["A"]["status"] == "high"
    assert result["counts"] == {"up": 1, "down": 1}


def test_missing_name_and_amount_are_tolerated(tmp_path) -> None:
    df = pl.DataFrame([_base_row("A")]).drop(["name", "amount"])
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    stock = next(iter(_flatten_tiers(result).values()))
    assert "name" not in stock
    assert "amount" not in stock


def test_missing_momentum_column_degrades_to_empty_ladder(tmp_path) -> None:
    # momentum_20d is now guarded like the neighbouring is_momentum block:
    # without it every row falls to boards=1 and is filtered out by
    # boards >= 2, yielding an empty ladder instead of ColumnNotFoundError.
    df = pl.DataFrame([_base_row("A")]).drop("momentum_20d")
    assert "vol_ratio_5d" in df.columns
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    assert result["tiers"] == []
    # up/down counts come from the 60-day signals and stay available.
    assert set(result["counts"]) == {"up", "down"}


def test_missing_change_pct_degrades_without_crash(tmp_path) -> None:
    # is_volume now requires BOTH vol_ratio_5d and change_pct to be present,
    # so a frame missing change_pct degrades instead of crashing. The row
    # still qualifies on momentum, so the ladder is not empty.
    df = pl.DataFrame([_base_row("A")]).drop("change_pct")
    assert "vol_ratio_5d" in df.columns
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path))
    stock = next(iter(_flatten_tiers(result).values()))
    assert stock["status"] != "volume"


# ---------------------------------------------------------------------------
# Empty / no-data paths and response shape
# ---------------------------------------------------------------------------
def test_empty_frame_returns_skeleton_with_date(tmp_path) -> None:
    result = _run_ladder(
        _FakeRepo(pl.DataFrame(), date(2025, 1, 1), tmp_path),
        as_of=date(2025, 1, 1),
    )
    assert result == {
        "as_of": "2025-01-01",
        "tiers": [],
        "counts": {"up": 0, "down": 0},
        "counts_raw": {"up": 0, "down": 0},
        "sealed_ready": False,
        "sealed_age": None,
        "sealed_counts": {"real": 0, "fake": 0, "pending": 0},
        "sealed_counts_up": None,
        "sealed_counts_down": None,
        "market": "hk",
    }


def test_no_latest_date_returns_skeleton_with_none(tmp_path) -> None:
    result = _run_ladder(_FakeRepo(pl.DataFrame(), None, tmp_path))
    assert result["as_of"] is None
    assert result["tiers"] == []
    assert result["counts"] == {"up": 0, "down": 0}


def test_us_market_is_reflected_in_response(tmp_path) -> None:
    df = pl.DataFrame([_base_row("A")])
    result = _run_ladder(_FakeRepo(df, date(2025, 1, 1), tmp_path), market="us")
    assert result["market"] == "us"
    assert result["as_of"] == "2025-01-01"


def test_tier_structure_order_and_alias_fields(tmp_path) -> None:
    rows = [
        _base_row("t5", momentum_20d=0.30),
        _base_row("t4", momentum_20d=0.20),
        _base_row("t3", momentum_20d=0.10),
        _base_row("t2", momentum_20d=0.04),
        _base_row("gone", momentum_20d=0.01),  # below 3%: not shown
    ]
    result = _run_ladder(_FakeRepo(pl.DataFrame(rows), date(2025, 1, 1), tmp_path))
    assert [t["boards"] for t in result["tiers"]] == [5, 4, 3, 2]
    assert [t["count"] for t in result["tiers"]] == [1, 1, 1, 1]
    for stock in _flatten_tiers(result).values():
        # A-share ladder aliases: consecutive_limit_ups mirrors boards
        assert stock["consecutive_limit_ups"] == stock["boards"]
        assert stock["consecutive_limit_downs"] == 0
        assert stock["sealed_status"] is None
        assert stock["sealed_vol"] is None
