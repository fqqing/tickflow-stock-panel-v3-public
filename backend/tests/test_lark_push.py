"""/api/lark 推送契约测试 (不发真实飞书请求)。

面板推送是**显式触发**的, 单测重点: 参数透传、未配置表必须安全跳过(不能误推到
别人的表)、取数失败要回 error 而不是 500。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import lark as api
from app.services import lark_bitable as lb
from app.services import lark_screener as ls


def _client() -> TestClient:
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir="/tmp/nonexistent"))
    app.state.strategy_engine = object()
    app.state.quote_service = None
    app.include_router(api.router)
    return TestClient(app)


def test_tables_endpoint_lists_all_with_config_flag():
    """前端靠 configured 决定能不能选 —— 未配置的表要显式标出来。"""
    resp = _client().get("/api/lark/tables")
    assert resp.status_code == 200
    tables = {t["id"]: t for t in resp.json()["tables"]}
    assert set(tables) == {
        "bottom_structure", "upward_trend_breakout", "trend_dragon", "abnormal", "startup_surge",
    }
    assert tables["bottom_structure"]["label"] == "底部结构"
    assert tables["startup_surge"]["configured"] is True, "启动策略 2026-10-01 已建独立表"
    assert tables["abnormal"]["configured"] is True


def test_status_endpoint_reports_cli():
    body = _client().get("/api/lark/status").json()
    assert "cli" in body and isinstance(body["available"], bool)


def test_push_unknown_strategy():
    body = _client().post("/api/lark/push", json={"strategy_id": "nope"}).json()
    assert body["ok"] is False
    assert "未知策略" in body["error"]


def test_push_unconfigured_table_skips_without_writing(monkeypatch):
    """未配置表必须在写表之前拦下 —— 否则会推进别人的表造成脏数据。

    五个目标表 2026-10-01 起全部已配置, 因此这里临时把 startup_surge 抹空来
    复现「未配置」分支, 而不是依赖某张表永远为空。
    """
    called = []

    def _boom(*a, **kw):  # pragma: no cover - 真被调用就说明拦截失败
        called.append((a, kw))
        raise AssertionError("未配置的表不应触发写表")

    monkeypatch.setattr(lb, "push_records", _boom)
    cfg = dict(ls.STRATEGY_TABLES["startup_surge"])
    unconfigured = {**cfg, "base_token": None, "table_id": None}
    monkeypatch.setitem(ls.STRATEGY_TABLES, "startup_surge", unconfigured)
    body = _client().post("/api/lark/push", json={"strategy_id": "startup_surge"}).json()
    assert body["ok"] is False
    assert "未配置" in body["error"]
    assert called == []


def test_push_passes_params_through(monkeypatch):
    captured: dict = {}

    def _fake(sid, **kw):
        captured["sid"] = sid
        captured.update(kw)
        return {"ok": True, "strategy_id": sid, "pushed": 3}

    monkeypatch.setattr(ls, "run_push", _fake)
    body = _client().post("/api/lark/push", json={
        "strategy_id": "bottom_structure",
        "as_of": "2026-09-30",
        "force": True,
        "dry_run": False,
        "market": "cn",
        "with_momentum": False,
    }).json()
    assert body["pushed"] == 3
    assert captured["sid"] == "bottom_structure"
    assert captured["as_of"] == "2026-09-30"
    assert captured["force"] is True
    assert captured["with_momentum"] is False
    assert captured["market"] == "cn"


# ===== service 层 =====

def test_run_push_rejects_unknown_strategy():
    out = ls.run_push("nope", repo=None)
    assert out["ok"] is False
    assert "未知策略" in out["error"]


def test_run_push_returns_error_instead_of_raising(monkeypatch):
    """面板要看到失败原因, 不能让端点抛异常变 500。"""

    def _boom(*a, **kw):
        raise RuntimeError("后端未就绪")

    monkeypatch.setattr(ls, "fetch_strategy_rows", _boom)
    out = ls.run_push("bottom_structure", repo=object(), engine=object(), data_dir="/tmp")
    assert out["ok"] is False
    assert "取数失败" in out["error"]


def test_run_push_dry_run_does_not_write(monkeypatch):
    monkeypatch.setattr(
        ls, "fetch_strategy_rows",
        lambda *a, **kw: ("2026-09-30", [
            {"symbol": "600354.SH", "name": "敦煌种业", "close": 11.45, "change_pct": 0.0306},
        ]),
    )
    out = ls.run_push("bottom_structure", repo=object(), engine=object(), data_dir="/tmp", dry_run=True)
    assert out["ok"] is True
    assert out["selected"] == 1
    assert out["pushed"] == 0
    assert out["sample"][0]["代码"] == "600354"


def test_run_push_empty_selection(monkeypatch):
    """选不出股要给出明确提示, 不能当成成功推送 0 条。"""
    monkeypatch.setattr(ls, "fetch_strategy_rows", lambda *a, **kw: ("2026-09-30", []))
    out = ls.run_push("bottom_structure", repo=object(), engine=object(), data_dir="/tmp")
    assert out["ok"] is False
    assert "无选中个股" in out["error"]


def test_fetch_strategy_rows_requires_engine():
    with pytest.raises(RuntimeError, match="策略引擎未初始化"):
        ls.fetch_strategy_rows(repo=object(), engine=None, data_dir="/tmp", strategy_id="bottom_structure")


def test_fetch_strategy_rows_rejects_unknown():
    class _Engine:
        def has(self, sid):
            return False

    with pytest.raises(RuntimeError, match="unknown strategy"):
        ls.fetch_strategy_rows(
            repo=object(), engine=_Engine(), data_dir="/tmp", strategy_id="nope", as_of="2026-09-30",
        )


def test_service_abnormal_mapping_matches_script():
    """service 与 scripts 是同一份实现, 这里锁住异常分支的行为。"""
    rows = [{
        "symbol": "600354.SH", "name": "敦煌种业", "board": "主板", "close": 11.45,
        "windows": {"30d": {"value": 1.9694, "threshold": 2.00, "closeness": 0.9847}},
    }]
    r = ls._abnormal_records(rows, "2026-09-30")[0]
    assert r["目标等级"] == "30日异动"
    assert r["下一日可能触发"] == "True"


def test_startup_surge_record_fields():
    """启动策略走独立表: 量比/换手/动量/评分这组选股字段, 不带底部结构的 DIF/DEA。"""
    rows = [{
        "symbol": "600354.SH", "name": "敦煌种业", "close": 11.45, "change_pct": 0.0306,
        "vol_ratio_5d": 1.8734, "turnover_rate": 5.2136, "momentum_20d": 0.1234,
        "ma5": 11.2, "ma20": 10.9, "score": 48.6217,
    }]
    r = ls.build_records("startup_surge", rows, "2026-09-30")[0]
    assert r["代码"] == "600354"
    assert r["市场"] == "SH"
    assert r["信号日期"] == "2026-09-30"
    assert r["涨跌幅%"] == 3.06          # 小数 -> 百分数
    assert r["20日动量%"] == 12.34
    assert r["量比5日"] == 1.87          # 抹到 2 位, 不留浮点尾巴
    assert r["换手率%"] == 5.21          # enriched 已是百分数, 不换算
    assert r["评分"] == 48.62
    assert "DIF" not in r and "钝化类型" not in r
