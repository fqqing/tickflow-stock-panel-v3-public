"""app/signallab.lab 的定向测试。

重点盯三件事:
1. 滚动窗口算子忽略 NaN(停牌日既不能污染极值, 也不能进均值分母)。
2. 形态特征**严格按 np.nonzero(entry) 的行序对齐**, 且只用信号日及之前的数据 ——
   特征错行或掺入未来信息会让归因结论彻底失真, 而这两类错误在接口层完全看不出来。
3. 台账仓储(落盘 / 列举 / 定位 / 读回)的往返一致。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.signallab.lab import (
    CONTEXT_FEATURES,
    LabRunConfig,
    attach_context_features,
    list_datasets,
    load_ledger,
    resolve_dataset,
    save_ledger,
    signal_name_column,
)
from app.signallab.runs import SignalLabRunStore


def test_rolling_max_min_ignore_nan() -> None:
    from app.signallab.lab import _rolling_max, _rolling_min

    values = np.array([[1.0, np.nan], [3.0, 2.0], [np.nan, 5.0], [2.0, 1.0]])
    highs = _rolling_max(values, 2)
    # col0 窗口 2: [1]->1, [1,3]->3, [3,NaN]->3, [NaN,2]->2; col1: NaN, 2, 5, 5
    assert highs[:, 0].tolist() == [1.0, 3.0, 3.0, 2.0]
    assert np.isnan(highs[0, 1])
    assert highs[1, 1] == 2.0
    assert highs[3, 1] == 5.0

    lows = _rolling_min(values, 2)
    assert lows[:, 0].tolist() == [1.0, 1.0, 3.0, 2.0]
    assert lows[3, 1] == 1.0


def test_rolling_mean_excludes_nan_from_denominator() -> None:
    from app.signallab.lab import _rolling_mean

    values = np.array([[2.0], [np.nan], [4.0], [6.0]])
    means = _rolling_mean(values, 2)
    # 窗口不足的头部是 NaN; 第 2 行窗口含一个 NaN, 均值只算有效值 = 4
    assert np.isnan(means[0, 0])
    assert means[1, 0] == 2.0
    assert means[2, 0] == 4.0
    assert means[3, 0] == 5.0


def _market(n_rows: int = 30, n_cols: int = 2) -> SimpleNamespace:
    index = np.arange(n_rows, dtype=np.float64)[:, None]
    offset = np.arange(n_cols, dtype=np.float64)[None, :] * 0.5
    close = 10.0 + index + offset
    return SimpleNamespace(
        close=close,
        high=close + 1.0,
        low=close - 1.0,
        volume=100.0 + 10.0 * index + offset,
    )


def test_context_features_are_aligned_and_causal() -> None:
    market = _market()
    entry = np.zeros((30, 2), dtype=bool)
    entry[20, 0] = True
    entry[22, 1] = True
    entry[25, 0] = True

    rows, cols = np.nonzero(entry)
    frame = pl.DataFrame({"symbol": [f"S{i}" for i in range(rows.size)]})
    enriched = attach_context_features(frame, market, entry)

    assert set(CONTEXT_FEATURES).issubset(set(enriched.columns))
    for i, (row, col) in enumerate(zip(rows, cols, strict=True)):
        # 只用 [0..row] 的数据: 价格单调递增, 所以窗口高点就是当日最高
        window = slice(row - 19, row + 1)
        close_now = market.close[row, col]
        assert enriched["ctx_drawdown_from_high"][i] == pytest.approx(
            close_now / (close_now + 1.0) - 1.0
        )
        assert enriched["ctx_rally_from_low"][i] == pytest.approx(
            close_now / market.low[0, col] - 1.0
        )
        assert enriched["ctx_vol_ratio"][i] == pytest.approx(
            market.volume[row, col] / market.volume[window, col].mean()
        )
        assert enriched["ctx_ma_bias"][i] == pytest.approx(
            close_now / market.close[window, col].mean() - 1.0
        )
        # TR = max(high-low, |high-prev_close|, |low-prev_close|) 恒为 2
        assert enriched["ctx_atr_pct"][i] == pytest.approx(2.0 / close_now)


def test_context_features_skipped_on_shape_mismatch() -> None:
    """行序对不上时宁可缺特征, 也不能错行。"""
    market = _market()
    entry = np.zeros((30, 2), dtype=bool)
    entry[20, 0] = True
    frame = pl.DataFrame({"symbol": ["A", "B"]})   # 2 行 vs 1 个信号
    assert attach_context_features(frame, market, entry) is frame


def test_signal_name_column_maps_code_to_cn() -> None:
    series = pl.Series("entry_signal_code", [0, 1, 2, None], dtype=pl.Int16)
    names = signal_name_column(series, ("signal_bottom_structure", "signal_stale_nine_turn"))
    assert names.to_list() == ["底部结构", "钝化加低九", "signal#2", None]


def test_signal_name_column_without_signal_ids() -> None:
    series = pl.Series("entry_signal_code", [0], dtype=pl.Int16)
    assert signal_name_column(series, ()).to_list() == [None]


def test_config_rejects_bad_range() -> None:
    with pytest.raises(ValueError, match="end"):
        LabRunConfig(strategy_id="s", start=date(2026, 1, 10), end=date(2026, 1, 5))
    with pytest.raises(ValueError, match="horizons"):
        LabRunConfig(strategy_id="s", start=date(2026, 1, 5), end=date(2026, 1, 10), horizons=(0,))
    config = LabRunConfig(strategy_id="s", start=date(2026, 1, 5), end=date(2026, 1, 10),
                          horizons=(5, 1, 5))
    assert config.horizons == (1, 5)
    assert config.dataset_name == "events_2026-01-05_2026-01-10.parquet"


def test_dataset_roundtrip(tmp_path) -> None:
    frame = pl.DataFrame({"symbol": ["A", "B"], "ret_5d": [0.01, -0.02]})
    path = save_ledger(frame, tmp_path, "bottom_structure", date(2026, 1, 5), date(2026, 2, 5))
    assert path.exists()

    datasets = list_datasets(tmp_path)
    assert len(datasets) == 1
    assert datasets[0]["strategy_id"] == "bottom_structure"
    assert datasets[0]["start"] == "2026-01-05"
    assert datasets[0]["rows"] == 2

    # 精确命中 / 只给 start / 都不给(取最新)
    assert resolve_dataset(tmp_path, "bottom_structure", date(2026, 1, 5), date(2026, 2, 5)) == path
    assert resolve_dataset(tmp_path, "bottom_structure", date(2026, 1, 5), None) == path
    assert resolve_dataset(tmp_path, "bottom_structure") == path
    assert resolve_dataset(tmp_path, "other") is None

    loaded = load_ledger(tmp_path, "bottom_structure")
    assert loaded is not None and loaded.height == 2
    assert load_ledger(tmp_path, "unknown") is None


def test_list_datasets_empty_dir(tmp_path) -> None:
    assert list_datasets(tmp_path) == []


def test_run_store_single_flight_and_lifecycle() -> None:
    store = SignalLabRunStore()
    run_id, is_new = store.create({"strategy_id": "s"})
    assert is_new
    reused_id, is_new = store.create({"strategy_id": "s"})
    assert not is_new and reused_id == run_id
    assert store.active_id() == run_id

    store.start(run_id)
    store.progress(run_id, "signals", 40, "计算中")
    snapshot = store.get(run_id)
    assert snapshot["status"] == "running"
    assert snapshot["stage"] == "signals"
    assert snapshot["log"][-1]["msg"] == "计算中"

    store.succeed(run_id, {"rows": 12})
    done = store.get(run_id)
    assert done["status"] == "succeeded"
    assert done["result"] == {"rows": 12}
    assert done["duration_s"] is not None
    assert store.active_id() is None

    # 结束后单飞解除
    next_id, is_new = store.create({"strategy_id": "s"})
    assert is_new and next_id != run_id
    store.fail(next_id, "boom")
    assert store.get(next_id)["error"] == "boom"
    assert store.list_recent(limit=5)[0]["id"] == next_id
    assert store.get("missing") is None
