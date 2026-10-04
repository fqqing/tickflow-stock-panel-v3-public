"""/api/signallab 的端到端测试(用桩替换昂贵的复盘计算)。

复盘本身要跑全区间矩阵信号, 单测里跑不动, 这里只验证 **API 契约**:
参数校验、分页、404/400、台账里 NaN 能否安全过 JSON、汇总与归因的列是否对得上。
复盘的算法由 test_signallab_lab.py / test_signallab_outcome.py 覆盖。
"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import signallab as api
from app.signallab.lab import save_ledger

STRATEGY_ID = "bottom_structure"
START = date(2026, 1, 5)
END = date(2026, 2, 5)


# ===== 桩 =====


class _Strategy:
    """模拟 StrategyDef: 标识只在 meta 里, 没有 .id 属性(真机就是如此)。"""

    def __init__(self, sid: str, name: str, backend: str = "matrix_native") -> None:
        self.execution_backend = backend
        self.meta = {"id": sid, "name": name, "category": "底部"}
        self.warmup_bars = 60


class _Engine:
    def __init__(self, strategies: list[_Strategy]) -> None:
        self._items = {s.meta["id"]: s for s in strategies}

    def has(self, sid: str) -> bool:
        return sid in self._items

    def get(self, sid: str) -> _Strategy:
        if sid not in self._items:
            raise ValueError(f"unknown strategy: {sid}")
        return self._items[sid]

    def strategy_definitions(self) -> tuple[_Strategy, ...]:
        return tuple(self._items.values())

    def required_history_bars(self, ids, params_map=None) -> int:
        return 60


class _Repo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    @staticmethod
    def get_enriched_latest():
        return (None, date(2026, 2, 5))

    @staticmethod
    def get_instruments() -> pl.DataFrame:
        return pl.DataFrame({"symbol": ["000001.SZ", "000002.SZ"], "asset_type": ["stock", "stock"]})


def _ledger(rows: int = 40) -> pl.DataFrame:
    rng = np.random.default_rng(7)
    return pl.DataFrame({
        "symbol": [f"{600000 + i}.SH" for i in range(rows)],
        "signal_date": [f"2026-01-{5 + (i % 20):02d}" for i in range(rows)],
        "fill_date": [f"2026-01-{6 + (i % 20):02d}" for i in range(rows)],
        "entry_price": np.round(rng.uniform(5, 50, rows), 3),
        "filled": [True] * (rows - 3) + [False] * 3,
        "truncated": [False] * rows,
        "mfe": rng.uniform(0, 0.2, rows),
        "mae": -rng.uniform(0, 0.2, rows),
        "mfe_bar": rng.integers(0, 20, rows),
        "mae_bar": rng.integers(0, 20, rows),
        "entry_signal_code": [i % 2 for i in range(rows)],
        "entry_signal_name": ["底部结构" if i % 2 == 0 else "钝化加低九" for i in range(rows)],
        "ctx_drawdown_from_high": -rng.uniform(0, 0.5, rows),
        "ctx_rally_from_low": rng.uniform(0, 0.5, rows),
        "ctx_vol_ratio": rng.uniform(0.2, 5, rows),
        "ctx_atr_pct": rng.uniform(0.01, 0.08, rows),
        "ctx_ma_bias": rng.uniform(-0.2, 0.3, rows),
        "ret_5d": rng.uniform(-0.1, 0.15, rows),
        "ret_20d": rng.uniform(-0.2, 0.3, rows),
        "exc_5d": rng.uniform(-0.1, 0.1, rows),
        "exc_20d": rng.uniform(-0.2, 0.2, rows),
    })


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """起一个只挂 signallab 路由的 app, 复盘计算换成写桩数据的假实现。"""
    rows_holder = {"n": 40}
    seen: list = []

    def fake_run_lab(repo, engine, config, *, data_dir=None, on_progress=None):
        seen.append(config)
        frame = _ledger(rows_holder["n"])
        if on_progress:
            on_progress("signals", 40, "桩数据")
        if config.write and data_dir is not None:
            save_ledger(frame, data_dir, config.strategy_id, config.start, config.end)
        return frame


    monkeypatch.setattr(api, "run_lab", fake_run_lab)

    # 让后台任务同步跑完: 在独立线程里开新事件循环, 避免 asyncio.run 撞上正在运行的 loop。
    def spawn_sync(coro):
        with _cf.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, coro).result()

    monkeypatch.setattr(api, "_spawn", spawn_sync)

    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = _Repo(tmp_path)
    app.state.strategy_engine = _Engine([
        _Strategy(STRATEGY_ID, "底部结构"),
        _Strategy("trend_dragon", "趋势擒龙"),
        _Strategy("legacy_expr", "老表达式策略", backend="expression"),
    ])
    test_client = TestClient(app)
    test_client.seen = seen      # 让断言能看到实际传给 run_lab 的 config
    return test_client


# ===== 元信息 =====


def test_strategies_marks_supported_backend(client) -> None:
    payload = client.get("/api/signallab/strategies").json()
    items = {item["id"]: item for item in payload["strategies"]}
    assert items[STRATEGY_ID]["supported"] is True
    assert items["legacy_expr"]["supported"] is False
    assert items[STRATEGY_ID]["warmup_bars"] == 60
    assert "ctx_drawdown_from_high" in payload["context_features"]


def test_datasets_empty_before_run(client) -> None:
    assert client.get("/api/signallab/datasets").json()["datasets"] == []


# ===== 复盘任务 =====


def test_run_then_query_endpoints(client) -> None:
    response = client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID,
        "start": START.isoformat(),
        "end": END.isoformat(),
        "horizons": [5, 20],
    })
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    run = client.get(f"/api/signallab/runs/{run_id}").json()
    assert run["status"] == "succeeded"
    assert run["result"]["rows"] == 40
    assert run["result"]["n_filled"] == 37

    datasets = client.get("/api/signallab/datasets").json()["datasets"]
    assert len(datasets) == 1
    assert datasets[0]["horizons"] == [5, 20]

    outcomes = client.get("/api/signallab/outcomes", params={
        "strategy_id": STRATEGY_ID, "limit": 10,
    }).json()
    assert outcomes["total"] == 37          # filled_only 默认 True, 去掉 3 条未成交
    assert len(outcomes["rows"]) == 10
    assert outcomes["rows"][0]["signal_date"].startswith("2026-01-")

    summary = client.get("/api/signallab/summary", params={"strategy_id": STRATEGY_ID}).json()
    assert summary["horizons"] == [5, 20]
    assert summary["n_signals"] == 40
    assert summary["overall"]["n_signals"] == 40
    # 汇总列的命名是 ret{horizon}_* (与台账的 ret_{horizon}d 不同, 沿用 summary 既有约定)
    assert 0 < summary["overall"]["ret5_win_rate"] < 1
    assert summary["overall"]["ret20_n"] == 40

    attribution = client.get("/api/signallab/attribution", params={
        "strategy_id": STRATEGY_ID, "horizon": 20, "min_samples": 5,
    }).json()
    assert attribution["horizon"] == 20
    features = {row["feature"] for row in attribution["rows"]}
    assert "entry_signal_name" in features
    assert "ctx_drawdown_from_high" in features
    # 字符串特征按取值分组, 不参与分位切桶
    names = {row["bucket"] for row in attribution["rows"] if row["feature"] == "entry_signal_name"}
    assert names == {"底部结构", "钝化加低九"}


def test_run_passes_resolved_symbol_pool(client) -> None:
    """limit 必须真的把标的池截短 —— 否则会按 symbols=None 跑全市场。"""
    client.post("/api/signallab/runs", json={"strategy_id": STRATEGY_ID, "limit": 2})
    config = client.seen[-1]
    assert config.symbols == ("000001.SZ", "000002.SZ")

    client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID, "symbols": ["600519.SH"],
    })
    assert client.seen[-1].symbols == ("600519.SH",)


def test_run_rejects_unknown_and_non_matrix(client) -> None:
    assert client.post("/api/signallab/runs", json={"strategy_id": "nope"}).status_code == 404
    response = client.post("/api/signallab/runs", json={"strategy_id": "legacy_expr"})
    assert response.status_code == 400


def test_run_rejects_bad_params(client) -> None:
    for payload in ({"horizons": [0, -3]}, {"horizons": []}):
        response = client.post("/api/signallab/runs", json={
            "strategy_id": STRATEGY_ID, **payload,
        })
        assert response.status_code == 422, payload
    # 止损/止盈方向反了也是入参错误
    assert client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID, "stop_loss": 0.05,
    }).status_code == 422
    assert client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID, "take_profit": -0.05,
    }).status_code == 422


def test_runs_list_and_missing(client) -> None:
    assert client.get("/api/signallab/runs").json()["active_id"] is None
    assert client.get("/api/signallab/runs/deadbeef").status_code == 404


# ===== 查询端点的边界 =====


def test_query_without_dataset_is_404(client) -> None:
    for path in ("outcomes", "summary", "attribution"):
        response = client.get(f"/api/signallab/{path}", params={"strategy_id": STRATEGY_ID})
        assert response.status_code == 404, path


def test_outcomes_filters_and_pagination(client) -> None:
    client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID, "start": START.isoformat(), "end": END.isoformat(),
    })
    base = client.get("/api/signallab/outcomes", params={
        "strategy_id": STRATEGY_ID, "limit": 500, "filled_only": False,
    }).json()
    assert base["total"] == 40

    filtered = client.get("/api/signallab/outcomes", params={
        "strategy_id": STRATEGY_ID, "limit": 500, "symbol": "600000.SH",
    }).json()
    assert filtered["total"] == 1

    page2 = client.get("/api/signallab/outcomes", params={
        "strategy_id": STRATEGY_ID, "limit": 5, "offset": 5, "sort": "symbol",
    }).json()
    assert len(page2["rows"]) == 5
    first = client.get("/api/signallab/outcomes", params={
        "strategy_id": STRATEGY_ID, "limit": 5, "offset": 0, "sort": "symbol",
    }).json()
    assert page2["rows"][0]["symbol"] != first["rows"][0]["symbol"]


def test_summary_grouping_and_bad_columns(client) -> None:
    client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID, "start": START.isoformat(), "end": END.isoformat(),
    })
    grouped = client.get("/api/signallab/summary", params={
        "strategy_id": STRATEGY_ID, "group_by": "entry_signal_name",
    }).json()
    assert len(grouped["groups"]) == 2
    assert {g["entry_signal_name"] for g in grouped["groups"]} == {"底部结构", "钝化加低九"}

    bad = client.get("/api/signallab/summary", params={
        "strategy_id": STRATEGY_ID, "group_by": "not_a_column",
    })
    assert bad.status_code == 400

    missing_horizon = client.get("/api/signallab/summary", params={
        "strategy_id": STRATEGY_ID, "horizons": "99",
    })
    assert missing_horizon.status_code == 400


def test_attribution_falls_back_to_longest_horizon(client) -> None:
    client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID, "start": START.isoformat(), "end": END.isoformat(),
    })
    payload = client.get("/api/signallab/attribution", params={
        "strategy_id": STRATEGY_ID, "horizon": 60, "min_samples": 5,
    }).json()
    assert payload["requested_horizon"] == 60
    assert payload["horizon"] == 20        # 台账只有 5/20, 退回最长

    bad = client.get("/api/signallab/attribution", params={
        "strategy_id": STRATEGY_ID, "features": "not_a_column",
    })
    assert bad.status_code == 400


def test_outcomes_rows_carry_name_column(client) -> None:
    """台账行必须带 name: 前端要显示中文名称, 而策略输出只有代码。"""
    client.post("/api/signallab/runs", json={
        "strategy_id": STRATEGY_ID, "start": START.isoformat(), "end": END.isoformat(),
    })
    payload = client.get("/api/signallab/outcomes", params={
        "strategy_id": STRATEGY_ID, "limit": 5, "filled_only": False,
    }).json()
    assert "name" in payload["columns"]
    assert all("name" in row for row in payload["rows"])


def test_attach_names_is_idempotent_and_skips_existing(monkeypatch) -> None:
    """已有 name 列不能被覆盖(上游带出来的名称优先); 无 symbol 列则原样返回。"""
    monkeypatch.setattr(api, "market_meta", lambda: {"600000.SH": {"name": "浦发银行"}})

    tagged = api._attach_names(pl.DataFrame({"symbol": ["600000.SH"], "name": ["自定义名称"]}))
    assert tagged["name"][0] == "自定义名称", "已有 name 列必须原样保留"

    filled = api._attach_names(pl.DataFrame({"symbol": ["600000.SH", "999999.XX"]}))
    assert filled["name"].to_list() == ["浦发银行", None]

    empty = api._attach_names(pl.DataFrame({"symbol": []}))
    assert empty.height == 0 and "name" not in empty.columns

    no_symbol = api._attach_names(pl.DataFrame({"x": [1]}))
    assert no_symbol.columns == ["x"]


def test_fill_names_only_patches_missing(monkeypatch) -> None:
    monkeypatch.setattr(api, "market_meta", lambda: {"600000.SH": {"name": "浦发银行"}})
    rows = api._fill_names([
        {"symbol": "600000.SH"},
        {"symbol": "600000.SH", "name": "上游名称"},
        {"symbol": "000001.SZ"},
    ])
    assert rows[0]["name"] == "浦发银行"
    assert rows[1]["name"] == "上游名称", "上游已有名称不能被维表覆盖"
    assert rows[2]["name"] is None
