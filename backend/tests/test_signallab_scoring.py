"""当日候选打分 (app.signallab.scoring) 单测。

重点锁三件事:
1. 档位切点来自台账, 今日打分必须复用同一套切点(否则档位不同义);
2. 打分是样本量加权, 不是等权 -- 小样本档位不能和大样本一样有话语权;
3. 特征缺失(次新股窗口不足)不参与打分, 而不是当 0 分。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.signallab.scoring import (
    build_bucket_model,
    context_features_for,
    score_candidates,
)

_LAST = date(2026, 1, 1) + timedelta(days=79)


def _hist(n_days: int = 80, symbols: tuple[str, ...] = ("A", "B")) -> pl.DataFrame:
    """构造 enriched 日线: 每天 close 递增 0.1, high=close+1, low=close-1。"""
    rows = []
    for symbol in symbols:
        for i in range(n_days):
            close = 10.0 + i * 0.1
            rows.append({
                "symbol": symbol,
                "date": date(2026, 1, 1) + timedelta(days=i),
                "open": close,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 100.0 + i,
            })
    return pl.DataFrame(rows)


def _ledger(n: int = 200) -> pl.DataFrame:
    """构造台账: ctx_vol_ratio 越大收益越高(单调), 便于断言排序。"""
    rng = np.random.default_rng(7)
    vol_ratio = rng.uniform(0.5, 3.0, n)
    return pl.DataFrame({
        "symbol": [f"{i:06d}" for i in range(n)],
        "ctx_vol_ratio": vol_ratio,
        "ctx_atr_pct": rng.uniform(0.01, 0.05, n),
        "ret_5d": (vol_ratio - 1.75) * 0.05,
    })


def test_context_features_computes_known_windows():
    ctx = context_features_for(_hist(), _LAST)
    assert ctx.height == 2
    row = ctx.filter(pl.col("symbol") == "A").to_dicts()[0]
    # 单边上行: 最后一根 close 是窗口内最高价 -> 回撤接近 0(相对窗口最高 high 略微为负)
    assert row["ctx_drawdown_from_high"] <= 0
    assert row["ctx_drawdown_from_high"] > -0.2
    assert row["ctx_rally_from_low"] > 0       # 距 60 日低点已上涨
    assert row["ctx_ma_bias"] > 0              # 站上 MA20
    assert row["ctx_atr_pct"] > 0


def test_context_features_empty_when_columns_missing():
    assert context_features_for(pl.DataFrame({"symbol": ["A"]}), _LAST).is_empty()


def test_context_features_empty_when_date_absent():
    assert context_features_for(_hist(n_days=5), date(2030, 1, 1)).is_empty()


def test_bucket_model_reuses_ledger_edges_and_orders_by_mean():
    model, specs = build_bucket_model(_ledger(), ["ctx_vol_ratio", "ctx_atr_pct"], 5)
    assert set(model) == {"ctx_vol_ratio", "ctx_atr_pct"}
    assert specs["ctx_vol_ratio"], "切点必须回传, 今日打分要复用"
    buckets = model["ctx_vol_ratio"]["buckets"]
    assert len(buckets) >= 2
    assert buckets[0]["mean"] > buckets[-1]["mean"]     # 单调关系: 高档位更好
    assert all(b["n"] >= 20 for b in buckets)


def test_bucket_model_min_samples_drops_thin_buckets():
    model, _ = build_bucket_model(_ledger(n=60), ["ctx_vol_ratio"], 5, min_samples=100)
    assert model == {}, "样本量门槛高于总样本时应得到空模型"


def test_bucket_model_raises_on_missing_return_column():
    with pytest.raises(ValueError, match="缺少收益列"):
        build_bucket_model(_ledger().drop("ret_5d"), ["ctx_vol_ratio"], 5)


def test_score_candidates_orders_by_historical_bucket_mean():
    ledger = _ledger()
    model, specs = build_bucket_model(ledger, ["ctx_vol_ratio"], 5)
    today = pl.DataFrame({
        "symbol": ["LOW", "HIGH"],
        "ctx_vol_ratio": [0.6, 2.9],        # 分别落在历史最差档与最好档
    })
    scored = score_candidates(today, model, specs, ["ctx_vol_ratio"])
    assert [r["symbol"] for r in scored] == ["HIGH", "LOW"]
    assert scored[0]["score"] > 0 > scored[-1]["score"]
    assert scored[0]["reasons"][0]["feature"] == "ctx_vol_ratio"


def test_score_candidates_skips_missing_feature_instead_of_zero():
    """特征为 null(次新股窗口不足)时不参与打分, 而不是拉低到 0。"""
    ledger = _ledger()
    model, specs = build_bucket_model(ledger, ["ctx_vol_ratio"], 5)
    today = pl.DataFrame({
        "symbol": ["NOCTX", "HIGH"],
        "ctx_vol_ratio": [None, 2.9],
    })
    scored = score_candidates(today, model, specs, ["ctx_vol_ratio"])
    assert [r["symbol"] for r in scored] == ["HIGH"], "缺特征的行应被丢弃而不是记 0 分"


def test_score_candidates_is_sample_weighted_not_equal_weight():
    """样本量加权: (0.10*100 + (-0.10)*10) / 110 = 0.0818, 等权会得到 0。"""
    def _model(n: int, mean: float) -> dict:
        return {
            "label": "f", "edges": [],
            "buckets": [{"bucket": "a", "n": n, "mean": mean, "win_rate": None,
                         "profit_factor": None}],
        }

    model = {"big": _model(100, 0.10), "small": _model(10, -0.10)}
    # 非数值特征直接按取值分档, 不需要切点
    today = pl.DataFrame({"symbol": ["X"], "big": ["a"], "small": ["a"]})
    scored = score_candidates(today, model, {}, ["big", "small"])
    assert scored[0]["score"] == pytest.approx((0.10 * 100 - 0.10 * 10) / 110, abs=1e-6)
