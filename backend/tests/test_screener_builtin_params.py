from __future__ import annotations

import types
from datetime import date
from typing import ClassVar

from app.api import screener as screener_api
from app.services.screener import ScreenerResult


class _CapturingScreenerService:
    calls: ClassVar[list[dict]] = []

    def __init__(self, repo, asset_type="stock", market="cn"):
        self.repo = repo
        self.asset_type = asset_type
        self.market = market

    def latest_date(self):
        return date(2026, 7, 15)

    def build_strategy_context(
        self,
        engine,
        as_of,
        strategy_ids,
        *,
        timeframe="1d",
        params_map=None,
        overrides_map=None,
    ):
        self.calls.append({
            "kind": "context",
            "strategy_ids": strategy_ids,
            "timeframe": timeframe,
            "params_map": params_map,
            "overrides_map": overrides_map,
        })
        return types.SimpleNamespace(as_of=as_of)


class _CapturingStrategyEngine:
    calls: ClassVar[list[dict]] = []

    def has(self, strategy_id):
        return strategy_id == "builtin_strategy"

    def list_strategies(self):
        # run_all 在港美股模式下会调 list_strategies 做市场兼容过滤
        return [{"id": "builtin_strategy", "asset_types": ["stock"], "timeframes": ["1d"]}]

    def get(self, strategy_id):
        if not self.has(strategy_id):
            raise ValueError(f"unknown strategy: {strategy_id}")
        return types.SimpleNamespace(meta={"id": strategy_id})

    def run(self, strategy_id, context, *, pool=None, params=None, overrides=None):
        self.calls.append({
            "kind": "run",
            "strategy_id": strategy_id,
            "pool": pool,
            "params": params,
            "overrides": overrides,
        })
        return ScreenerResult(as_of=context.as_of, strategy=strategy_id)

    def run_all(self, context, *, params_map=None, overrides_map=None, strategy_ids=None):
        self.calls.append({
            "kind": "run_all",
            "params_map": params_map,
            "overrides_map": overrides_map,
            "strategy_ids": strategy_ids,
        })
        return {
            strategy_id: ScreenerResult(as_of=context.as_of, strategy=strategy_id)
            for strategy_id in strategy_ids or []
        }


def _api_request(tmp_path, engine):
    repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))
    state = types.SimpleNamespace(repo=repo, strategy_engine=engine)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _install_api_fakes(monkeypatch):
    _CapturingScreenerService.calls = []
    _CapturingStrategyEngine.calls = []
    monkeypatch.setattr(screener_api, "ScreenerService", _CapturingScreenerService)
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *_args: {})
    monkeypatch.setattr(screener_api, "_update_cache_strategy", lambda *_args: None)
    monkeypatch.setattr(screener_api.strategy_cache, "write_cache", lambda *_args: None)


def test_single_run_passes_saved_params_to_strategy_engine(monkeypatch, tmp_path):
    engine = _CapturingStrategyEngine()
    request = _api_request(tmp_path, engine)
    _install_api_fakes(monkeypatch)
    saved = {"params": {"threshold": 3.0, "enabled": False}}
    monkeypatch.setattr(screener_api.strategy_config, "load_override", lambda *_args: saved)

    screener_api.run_preset(
        screener_api.PresetRequest(
            strategy_id="builtin_strategy",
            as_of=date(2026, 7, 15),
        ),
        request,
    )

    context_call = _CapturingScreenerService.calls[0]
    run_call = _CapturingStrategyEngine.calls[0]
    assert context_call["params_map"] == {"builtin_strategy": saved["params"]}
    assert context_call["overrides_map"] == {"builtin_strategy": saved}
    assert run_call["params"] == saved["params"]
    assert run_call["overrides"] == saved


def test_batch_run_passes_saved_params_to_strategy_engine(monkeypatch, tmp_path):
    engine = _CapturingStrategyEngine()
    request = _api_request(tmp_path, engine)
    _install_api_fakes(monkeypatch)
    saved = {"params": {"threshold": 3.0, "enabled": False}}
    monkeypatch.setattr(
        screener_api.strategy_config,
        "list_overrides",
        lambda *_args: {"builtin_strategy": saved},
    )

    screener_api.run_all(
        request,
        body={"as_of": "2026-07-15", "strategy_ids": ["builtin_strategy"]},
    )

    context_call = _CapturingScreenerService.calls[0]
    run_all_call = _CapturingStrategyEngine.calls[0]
    expected_params = {"builtin_strategy": saved["params"]}
    expected_overrides = {"builtin_strategy": saved}
    assert context_call["params_map"] == expected_params
    assert context_call["overrides_map"] == expected_overrides
    assert run_all_call["params_map"] == expected_params
    assert run_all_call["overrides_map"] == expected_overrides


def test_batch_run_passes_market_from_body_to_service(monkeypatch, tmp_path):
    """run_all 必须把请求体里的 market 传给 ScreenerService。

    回归: 此前 run_all 从不读 body 里的 market, ScreenerService 恒为默认
    "cn" —— 切到港股跑全部策略实际返回的是 A 股结果, 前端却按港美股展示。
    """
    engine = _CapturingStrategyEngine()
    request = _api_request(tmp_path, engine)
    _install_api_fakes(monkeypatch)
    monkeypatch.setattr(screener_api.strategy_config, "list_overrides", lambda *_args: {})
    # 记录构造参数: run_all 创建后即调用 latest_date(), 无法从 calls 反推实例。
    created = {}
    monkeypatch.setattr(
        screener_api, "ScreenerService",
        lambda repo, asset_type="stock", market="cn": (
            created.update(market=market, asset_type=asset_type),
            _CapturingScreenerService(repo, asset_type, market),
        )[1],
    )

    screener_api.run_all(
        request,
        body={"as_of": "2026-07-15", "strategy_ids": ["builtin_strategy"], "market": "hk"},
    )

    assert created.get("market") == "hk"
    # 未传 market 时回落 cn
    screener_api.run_all(request, body={"as_of": "2026-07-15", "strategy_ids": ["builtin_strategy"]})
    assert created.get("market") == "cn"


def test_batch_summary_response_still_writes_full_cache(monkeypatch, tmp_path):
    engine = _CapturingStrategyEngine()
    request = _api_request(tmp_path, engine)
    _install_api_fakes(monkeypatch)
    written = []
    monkeypatch.setattr(screener_api.strategy_config, "list_overrides", lambda *_args: {})
    monkeypatch.setattr(screener_api.strategy_cache, "write_cache", lambda *args: written.append(args))

    payload = screener_api.run_all(
        request,
        body={
            "as_of": "2026-07-15",
            "strategy_ids": ["builtin_strategy"],
            "summary_only": True,
        },
    )

    assert payload == {
        "as_of": "2026-07-15",
        "results": {"builtin_strategy": {"total": 0, "as_of": "2026-07-15"}},
    }
    assert written[0][2]["builtin_strategy"]["rows"] == []
