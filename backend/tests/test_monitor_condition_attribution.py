"""Tests for per-condition attribution in monitor alerts (app/strategy/monitor.py).

Regression cover for the defect where an OR rule that fired on ONE condition
pushed a message listing EVERY configured condition, so the user could not tell
which condition actually triggered. Two separate root causes:

  1. _build_condition_mask collapsed N conditions into a single boolean via
     any_horizontal, discarding per-condition attribution at filter time.
  2. _match_conditions re-derived hits only for op="truth" boolean signals, so
     threshold conditions (change_pct >= 0.05) were never attributable at all.

The engine is driven through its real public entry point (evaluate) so the
assertions cover the message text that actually reaches the Feishu webhook,
which reads ev["message"] verbatim.
"""
from __future__ import annotations

import polars as pl

from app.strategy.monitor import MonitorRuleEngine


def _rule(conditions: list[dict], logic: str = "or", **over) -> dict:
    base = {
        "id": "r1", "name": "多条件监控", "enabled": True,
        "type": "signal", "asset_type": "stock", "scope": "all",
        "logic": logic, "conditions": conditions,
        "cooldown_seconds": 0,
    }
    base.update(over)
    return base


def _quote(**cols) -> pl.DataFrame:
    """单只股票的行情快照; 未给出的列使用不触发任何条件的中性值。"""
    row = {"symbol": ["600000.SH"], "name": ["浦发银行"], "close": [10.0], "change_pct": [0.0]}
    row.update({k: [v] for k, v in cols.items()})
    return pl.DataFrame(row)


def _fire(rule: dict, df: pl.DataFrame) -> list[dict]:
    engine = MonitorRuleEngine()
    engine.set_rules([rule])
    return engine.evaluate(df)


# ---------------------------------------------------------------------------
# OR logic: message must name only the condition that actually fired
# ---------------------------------------------------------------------------
def test_or_rule_message_names_only_the_matched_threshold_condition() -> None:
    """核心缺陷: 三条件 OR 只命中一条时, 文案不得列出另外两条。"""
    rule = _rule([
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 80},
        {"field": "vol_ratio_5d", "op": ">=", "value": 3},
    ])
    # 只有涨跌幅达标, RSI 与量比都远未触发
    df = _quote(change_pct=0.06, rsi_14=50.0, vol_ratio_5d=1.0)

    events = _fire(rule, df)

    assert len(events) == 1
    msg = events[0]["message"]
    assert "change_pct" in msg or "涨跌幅" in msg
    # 未命中的条件不得出现在推送文案里
    assert "rsi" not in msg.lower()
    assert "vol_ratio" not in msg and "量比" not in msg


def test_or_rule_matched_conditions_covers_threshold_ops() -> None:
    """阈值类条件必须可归因 —— 旧实现只归因 op=truth, 这里会拿到空列表。"""
    rule = _rule([
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 80},
    ])
    events = _fire(rule, _quote(change_pct=0.06, rsi_14=50.0))

    matched = events[0]["matched_conditions"]
    assert matched == [{"field": "change_pct", "op": ">=", "value": 0.05}]


def test_or_rule_attributes_boolean_signal_without_dragging_in_threshold() -> None:
    rule = _rule([
        {"field": "signal_ma20_cross_up", "op": "truth"},
        {"field": "change_pct", "op": ">=", "value": 0.09},
    ])
    df = _quote(signal_ma20_cross_up=True, change_pct=0.01)

    events = _fire(rule, df)

    assert events[0]["matched_conditions"] == [
        {"field": "signal_ma20_cross_up", "op": "truth"},
    ]
    assert events[0]["signals"] == ["signal_ma20_cross_up"]


def test_or_rule_reports_both_when_two_conditions_genuinely_fire() -> None:
    """归因是「真正命中的子集」, 不是「只取第一条」。"""
    rule = _rule([
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 70},
        {"field": "vol_ratio_5d", "op": ">=", "value": 9},
    ])
    df = _quote(change_pct=0.06, rsi_14=75.0, vol_ratio_5d=1.0)

    matched = _fire(rule, df)[0]["matched_conditions"]

    assert [c["field"] for c in matched] == ["change_pct", "rsi_14"]


# ---------------------------------------------------------------------------
# AND logic and the full-snapshot field must keep their existing meaning
# ---------------------------------------------------------------------------
def test_and_rule_matches_every_condition_by_definition() -> None:
    rule = _rule([
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 70},
    ], logic="and")
    df = _quote(change_pct=0.06, rsi_14=75.0)

    events = _fire(rule, df)

    assert len(events) == 1
    assert [c["field"] for c in events[0]["matched_conditions"]] == ["change_pct", "rsi_14"]


def test_conditions_field_still_carries_the_full_rule_snapshot() -> None:
    """conditions 保留「规则配了什么」, matched_conditions 表达「触发了什么」。"""
    conds = [
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 80},
    ]
    events = _fire(_rule(conds), _quote(change_pct=0.06, rsi_14=50.0))

    assert events[0]["conditions"] == conds          # 全量快照不变
    assert len(events[0]["matched_conditions"]) == 1  # 命中子集


def test_custom_message_still_overrides_generated_text() -> None:
    rule = _rule(
        [{"field": "change_pct", "op": ">=", "value": 0.05}],
        message="自定义文案",
    )
    assert _fire(rule, _quote(change_pct=0.06))[0]["message"] == "自定义文案"


# ---------------------------------------------------------------------------
# Matching semantics must be unchanged by the attribution refactor
# ---------------------------------------------------------------------------
def test_or_rule_does_not_fire_when_no_condition_holds() -> None:
    rule = _rule([
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 80},
    ])
    assert _fire(rule, _quote(change_pct=0.01, rsi_14=50.0)) == []


def test_and_rule_does_not_fire_on_partial_match() -> None:
    rule = _rule([
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 80},
    ], logic="and")
    assert _fire(rule, _quote(change_pct=0.06, rsi_14=50.0)) == []


def test_missing_field_yields_no_event() -> None:
    """字段缺失时整条规则判空 —— 保持既有的安全降级行为。"""
    rule = _rule([{"field": "not_a_column", "op": ">=", "value": 1}])
    assert _fire(rule, _quote(change_pct=0.06)) == []


def test_null_values_do_not_produce_spurious_attribution() -> None:
    """null 不得被当成命中 (fill_null(False) 的保护)。"""
    rule = _rule([
        {"field": "rsi_14", "op": ">=", "value": 70},
        {"field": "change_pct", "op": ">=", "value": 0.05},
    ])
    df = pl.DataFrame({
        "symbol": ["600000.SH"], "name": ["浦发银行"], "close": [10.0],
        "change_pct": [0.06], "rsi_14": [None],
    }, schema_overrides={"rsi_14": pl.Float64})

    matched = _fire(rule, df)[0]["matched_conditions"]

    assert [c["field"] for c in matched] == ["change_pct"]


def test_internal_flag_columns_never_leak_into_events() -> None:
    """逐条件标记列是评估中间态, 不得出现在事件里。"""
    rule = _rule([{"field": "change_pct", "op": ">=", "value": 0.05}])
    ev = _fire(rule, _quote(change_pct=0.06))[0]
    assert not [k for k in ev if k.startswith("__cond_hit_")]


# ---------------------------------------------------------------------------
# SSE payload contract: the whitelist must forward the attribution
# ---------------------------------------------------------------------------
def test_sse_alert_payload_forwards_matched_conditions() -> None:
    """SSE 载荷是显式白名单 —— 归因字段漏登记就到不了前端 (与文案分开的第二个漏点)。"""
    from app.services.quote_service import rule_event_to_alert

    rule = _rule([
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 80},
    ])
    ev = _fire(rule, _quote(change_pct=0.06, rsi_14=50.0))[0]

    alert = rule_event_to_alert(ev)

    assert [c["field"] for c in alert["matched_conditions"]] == ["change_pct"]
    assert len(alert["conditions"]) == 2      # 全量快照仍在
    assert alert["message"] == ev["message"]  # 飞书正文同源


def test_sse_alert_payload_tolerates_events_without_attribution() -> None:
    """历史/策略类事件没有该字段时不得 KeyError, 退化为空列表。"""
    from app.services.quote_service import rule_event_to_alert

    legacy = {
        "source": "signal", "type": "signal", "symbol": "600000.SH",
        "name": "浦发银行", "message": "旧记录", "price": 10.0,
        "change_pct": 0.01, "signals": [],
    }
    alert = rule_event_to_alert(legacy)

    assert alert["matched_conditions"] == []
    assert alert["conditions"] == []
