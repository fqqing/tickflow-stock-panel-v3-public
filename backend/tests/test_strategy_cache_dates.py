from __future__ import annotations

import json
from types import SimpleNamespace

from app.api import screener as screener_api
from app.services import strategy_cache


def _result(as_of: str, *symbols: str) -> dict:
    return {
        "total": len(symbols),
        "as_of": as_of,
        "rows": [{"symbol": symbol, "close": index + 1.0} for index, symbol in enumerate(symbols)],
    }


class _MonitorEngine:
    def __init__(self, results=None):
        self.results = results or {}

    def latest_strategy_results(self):
        return self.results


def _request(tmp_path, monitor_results=None):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, monitor_engine=_MonitorEngine(monitor_results))
    return SimpleNamespace(app=SimpleNamespace(state=state))


# --- 多日期槽: 核心修复行为 -------------------------------------------------


def test_write_new_date_keeps_previous_date_slot(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_b": _result("2026-07-21", "600000.SH")})

    day_one = strategy_cache.read_cache(tmp_path, "cn", "2026-07-20")
    day_two = strategy_cache.read_cache(tmp_path, "cn", "2026-07-21")

    # 关键: 写 07-21 不能把 07-20 的槽冲掉 (单槽时这里会是 None, 用户切回即重算)
    assert day_one is not None
    assert set(day_one["results"]) == {"strategy_a"}
    assert day_one["results"]["strategy_a"]["rows"][0]["symbol"] == "000001.SZ"
    assert set(day_two["results"]) == {"strategy_b"}


def test_read_cache_by_date_returns_none_for_uncached_date(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})

    # 没算过的日期必须老实返回 None, 前端据此触发重跑
    assert strategy_cache.read_cache(tmp_path, "cn", "2026-07-19") is None
    assert strategy_cache.read_cache(tmp_path, "cn", "2026-07-21") is None
    assert strategy_cache.read_cache(tmp_path, "cn", "2026-07-20") is not None


def test_read_cache_without_date_returns_latest_written(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-22", {"strategy_c": _result("2026-07-22", "300001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_b": _result("2026-07-21", "600000.SH")})

    cached = strategy_cache.read_cache(tmp_path, "cn")

    # 最近写入的是 07-21 (不是日期最大的 07-22)
    assert cached["as_of"] == "2026-07-21"
    assert set(cached["results"]) == {"strategy_b"}


def test_ever_rows_are_isolated_per_date(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_a": _result("2026-07-21", "600000.SH")})

    day_one = strategy_cache.read_cache(tmp_path, "cn", "2026-07-20")
    day_two = strategy_cache.read_cache(tmp_path, "cn", "2026-07-21")

    # 各日期的「曾命中」集合不能互相污染
    assert set(day_one["today_ever_rows"]["strategy_a"]) == {"000001.SZ"}
    assert set(day_two["today_ever_rows"]["strategy_a"]) == {"600000.SH"}


def test_same_date_rewrites_still_merge_within_slot(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_b": _result("2026-07-20", "600000.SH")})

    cached = strategy_cache.read_cache(tmp_path, "cn", "2026-07-20")

    assert set(cached["results"]) == {"strategy_a", "strategy_b"}
    assert set(cached["today_ever_rows"]) == {"strategy_a", "strategy_b"}


# --- 旧格式兼容与淘汰 -------------------------------------------------------


def test_legacy_flat_cache_is_readable_and_migrated_on_write(tmp_path):
    path = tmp_path / "user_data" / "strategy_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "as_of": "2026-07-20",
            "results": {"strategy_a": _result("2026-07-20", "000001.SZ")},
            "today_ever_matched": {"strategy_a": ["000001.SZ"]},
            "today_ever_rows": {"strategy_a": {"000001.SZ": {"symbol": "000001.SZ"}}},
            "enriched_mtime": 1.5,
            "updated_at": 111,
        }),
        encoding="utf-8",
    )

    # 旧版平铺格式照常读出
    cached = strategy_cache.read_cache(tmp_path, "cn")
    assert cached["as_of"] == "2026-07-20"
    assert set(cached["results"]) == {"strategy_a"}
    assert set(cached["today_ever_rows"]["strategy_a"]) == {"000001.SZ"}
    assert strategy_cache.read_cache(tmp_path, "cn", "2026-07-20") is not None

    # 写入一次后落成新版结构, 且旧日期的内容没丢
    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_b": _result("2026-07-21", "600000.SH")})
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw["by_date"]) == {"2026-07-20", "2026-07-21"}
    assert set(strategy_cache.read_cache(tmp_path, "cn", "2026-07-20")["results"]) == {"strategy_a"}


def test_slot_count_is_capped_and_keeps_newest_dates(tmp_path):
    dates = [f"2026-07-{day:02d}" for day in range(1, strategy_cache._MAX_CACHE_DATES + 3)]
    for as_of in dates:
        strategy_cache.write_cache(tmp_path, as_of, {"strategy_a": _result(as_of, "000001.SZ")})

    kept = strategy_cache.read_cache(tmp_path, "cn", dates[-1])
    assert kept is not None
    # 最早的日期被淘汰
    assert strategy_cache.read_cache(tmp_path, "cn", dates[0]) is None
    assert strategy_cache.read_cache(tmp_path, "cn", dates[-2]) is not None


def test_corrupt_cache_returns_none(tmp_path):
    path = tmp_path / "user_data" / "strategy_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert strategy_cache.read_cache(tmp_path, "cn", "2026-07-20") is None


# --- 端点行为 ---------------------------------------------------------------


def test_as_query_str_normalizes_query_object_default():
    from fastapi import Query

    # 直接调用端点时形参是 Query 对象而非 None, 必须被收口成 None
    assert screener_api._as_query_str(Query(None)) is None
    assert screener_api._as_query_str(None) is None
    assert screener_api._as_query_str("") is None
    assert screener_api._as_query_str("2026-07-20") == "2026-07-20"


def test_cached_summary_scopes_to_requested_date(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_a": _result("2026-07-21", "600000.SH")})

    payload = screener_api.get_cached_summary(_request(tmp_path), market="cn", date="2026-07-20")
    assert payload["results"] == {"strategy_a": {"total": 1, "as_of": "2026-07-20"}}

    missing = screener_api.get_cached_summary(_request(tmp_path), market="cn", date="2026-07-19")
    assert missing["results"] == {}
    assert missing["as_of"] is None


def test_cached_endpoint_scopes_to_requested_date(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_a": _result("2026-07-21", "600000.SH")})

    # ext_columns 显式传 None: 直接调用端点时 FastAPI 不注入 Query 默认值
    payload = screener_api.get_cached(_request(tmp_path), ext_columns=None, market="cn", date="2026-07-20")
    assert payload["as_of"] == "2026-07-20"
    assert payload["results"]["strategy_a"]["rows"][0]["symbol"] == "000001.SZ"

    # 未缓存日期返回空标记, 前端据此触发 run_all
    empty = screener_api.get_cached(_request(tmp_path), ext_columns=None, market="cn", date="2026-07-19")
    assert empty == {"as_of": None, "results": {}, "updated_at": None}


def test_realtime_overlay_only_applies_to_matching_date(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("2026-07-20", "000001.SZ")})
    realtime = {"strategy_a": _result("2026-07-21", "600000.SH")}

    # 看历史日期时不能把今天的实时命中混进来
    historical = screener_api.get_cached(
        _request(tmp_path, realtime), ext_columns=None, market="cn", date="2026-07-20",
    )
    assert historical["results"]["strategy_a"]["rows"][0]["symbol"] == "000001.SZ"

    # 请求实时结果自身所属的日期时才叠加
    today = screener_api.get_cached_result(
        "strategy_a", _request(tmp_path, realtime), ext_columns=None, market="cn", date="2026-07-21",
    )
    assert today["result"]["rows"][0]["symbol"] == "600000.SH"
