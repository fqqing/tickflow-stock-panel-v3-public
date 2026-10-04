"""参数建议提取与收口 — 流式切分 / 边界校验 / 档位对齐。

重点验证三件事:
1. 建议块不会漏进正文(且 delta 被任意切碎也不会漏);
2. 模型编的参数名 / 越界范围 / 不整除的步长都不会原样下发;
3. 产出的网格能被 ``optimizer.expand_param_grid`` 接受(与前端 sweepError 同口径)。
"""
from __future__ import annotations

import math

import pytest

from app.signallab.suggestions import (
    FenceSplitter,
    explore_suggestions,
    extract_suggestions,
    normalize_suggestions,
    parse_suggestion_payload,
    suggest_combos,
)

PARAMS = [
    {"id": "atr_window", "label": "ATR 窗口", "type": "int", "default": 14, "min": 5, "max": 60, "step": 1},
    {"id": "vol_mult", "label": "量能倍数", "type": "float", "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    {"id": "use_filter", "label": "启用过滤", "type": "bool", "default": True},
    {"id": "mode", "label": "模式", "type": "select", "default": "a", "options": ["a", "b", "c"]},
]


def test_splitter_strips_json_block_from_prose():
    sp = FenceSplitter()
    out = "".join([
        sp.feed("正文第一行\n"),
        sp.feed("正文第二行\n"),
        sp.feed("```json\n"),
        sp.feed('{"suggestions": []}\n'),
        sp.feed("```"),
        sp.flush(),
    ])
    assert out == "正文第一行\n正文第二行\n", "建议块不能出现在正文里"
    assert sp.json_text().strip() == '{"suggestions": []}'


def test_splitter_survives_fragmented_fence():
    """围栏标记被 delta 切成碎片时不得漏进正文。"""
    sp = FenceSplitter()
    chunks = ["正文\n", "`", "`", "`j", "son\n", '{"suggestions":[]}', "\n", "```"]
    out = "".join(sp.feed(c) for c in chunks)
    assert out == "正文\n"
    assert sp.json_text().strip() == '{"suggestions":[]}'


def test_splitter_keeps_plain_code_block_as_prose():
    sp = FenceSplitter()
    out = "".join([sp.feed("说明\n"), sp.feed("```python\nx=1\n"), sp.feed("```\n"), sp.flush()])
    assert "x=1" in out, "非 json 代码块属于正文, 不应被扣下"
    assert sp.json_text() == ""


def test_extract_suggestions_from_prose_when_fence_unlabeled():
    """模型写了无语言标记的 ``` 块 -> 内容进了正文, 仍要能抠出建议。"""
    sp = FenceSplitter()
    prose = "".join([
        sp.feed("结论\n"),
        sp.feed("```\n"),
        sp.feed('{"suggestions":[{"param_id":"atr_window","direction":"down",'
                '"min":6,"max":20,"step":2}]}\n'),
        sp.feed("```\n"),
        sp.flush(),
    ])
    assert sp.json_text() == "", "无语言标记的块不算建议块"
    payload = extract_suggestions(prose=prose, json_text=sp.json_text())
    items = normalize_suggestions(payload, PARAMS)
    assert [i["param_id"] for i in items] == ["atr_window"]


def test_extract_suggestions_with_braces_and_quotes_in_reason():
    """reason 里带花括号/引号时不能把 JSON 截断。"""
    prose = '正文\n{"suggestions":[{"param_id":"vol_mult","direction":"up",' \
            '"reason":"区间 {1.0, 3.0} 内试探\\"更严\\"的阈值","min":1.0,"max":3.0,"step":0.5}]}'
    items = normalize_suggestions(extract_suggestions(prose=prose), PARAMS)
    assert len(items) == 1
    assert "更严" in items[0]["reason"]
    assert items[0]["grid"]["max"] == 3.0


def test_extract_suggestions_returns_none_without_key():
    assert extract_suggestions(prose="只有结论", json_text="") is None
    assert extract_suggestions() is None


def test_parse_payload_tolerates_surrounding_text():
    assert parse_suggestion_payload("好的: {\"suggestions\": [1]}\n如上") == {"suggestions": [1]}
    assert parse_suggestion_payload("没有 JSON") is None
    assert parse_suggestion_payload("{[bad json}") is None


def test_missing_param_id_is_dropped():
    items = normalize_suggestions(
        {"suggestions": [{"param_id": "not_a_param", "direction": "up"}]}, PARAMS
    )
    assert items == []


def test_numeric_range_is_clamped_and_step_aligned():
    """模型给越界范围 + 不整除步长 -> clamp 到声明区间且 step 整除 (max-min)。"""
    items = normalize_suggestions(
        {"suggestions": [
            {"param_id": "atr_window", "direction": "down", "min": 1, "max": 999, "step": 3},
        ]},
        PARAMS,
    )
    assert len(items) == 1
    g = items[0]["grid"]
    assert g["min"] >= 5 and g["max"] <= 60, "必须落在声明区间内"
    span = round(g["max"] - g["min"], 6)
    assert span / g["step"] == pytest.approx(round(span / g["step"]), abs=1e-9), "步长必须整除区间"
    assert items[0]["levels"][0] == g["min"] and items[0]["levels"][-1] == g["max"]
    assert all(float(v).is_integer() for v in items[0]["levels"]), "int 型档位必须是整数"


def test_float_param_levels_are_finite_and_capped():
    items = normalize_suggestions(
        {"suggestions": [{"param_id": "vol_mult", "direction": "up", "min": 0.5, "max": 5.0, "step": 0.01}]},
        PARAMS,
    )
    assert len(items) == 1
    levels = items[0]["levels"]
    assert len(levels) <= 5, "档位数必须封顶"
    assert all(0.5 <= v <= 5.0 for v in levels)


def test_range_defaults_from_direction_when_absent():
    """模型只给方向没给范围 -> 按方向从默认值推窗口, 且 still 合法。"""
    down = normalize_suggestions({"suggestions": [{"param_id": "atr_window", "direction": "down"}]}, PARAMS)
    up = normalize_suggestions({"suggestions": [{"param_id": "atr_window", "direction": "up"}]}, PARAMS)
    assert down[0]["grid"]["max"] <= 14, "down 应扫默认值下方"
    assert up[0]["grid"]["min"] >= 14, "up 应扫默认值上方"
    for item in (down[0], up[0]):
        g = item["grid"]
        assert g["min"] >= 5 and g["max"] <= 60
        assert (g["max"] - g["min"]) % g["step"] < 1e-9


def test_bool_and_select_fall_back_to_declared_options():
    items = normalize_suggestions(
        {"suggestions": [
            {"param_id": "use_filter", "direction": "hold"},
            {"param_id": "mode", "direction": "hold", "values": ["b", "zzz"]},
        ]},
        PARAMS,
    )
    assert items[0]["grid"] == [True, False]
    assert items[1]["grid"] == ["b"], "不在 options 里的候选值要被剔除"


def test_suggestion_count_and_combos_are_capped():
    payload = {"suggestions": [
        {"param_id": "atr_window", "direction": "down"},
        {"param_id": "vol_mult", "direction": "up"},
        {"param_id": "use_filter", "direction": "hold"},
        {"param_id": "mode", "direction": "hold"},
    ]}
    items = normalize_suggestions(payload, PARAMS)
    assert len(items) == 3, "条数必须封顶"
    assert suggest_combos(items) == math.prod(i["n_levels"] for i in items)
    assert suggest_combos(items) <= 125, "最坏组合数要可控"


def test_explore_grid_spans_declared_range_and_is_capped():
    """兜底网格: 铺声明区间(不是默认值附近), 且参数条数封顶。"""
    items = explore_suggestions(PARAMS)
    assert len(items) == 2, "兜底只铺前 2 个参数, 避免组合爆炸"
    g = items[0]["grid"]
    assert g["min"] == 5, "从声明下限起步"
    assert g["max"] >= 55 and g["max"] <= 60, "尽量铺满声明区间(步长取整数后末档会略收)"
    assert items[0]["n_levels"] == 5
    assert all("仅供探索" in i["reason"] for i in items), "必须说明这不是 AI 结论"


def test_explore_grid_empty_when_no_numeric_params():
    assert explore_suggestions([]) == []
    assert explore_suggestions([{"id": "x", "type": "unknown"}]) == []


def test_invalid_payload_shapes_return_empty():
    assert normalize_suggestions(None, PARAMS) == []
    assert normalize_suggestions({"suggestions": []}, PARAMS) == []
    assert normalize_suggestions({"foo": 1}, PARAMS) == []
    assert normalize_suggestions({"suggestions": ["not-a-dict"]}, PARAMS) == []


def test_normalized_grid_accepted_by_optimizer():
    """收口后的网格必须能过 optimizer 的展开校验(否则一键填入后会被后端拒)。"""
    from app.backtest.optimizer import expand_param_grid

    items = normalize_suggestions(
        {"suggestions": [
            {"param_id": "atr_window", "direction": "down", "min": 6, "max": 30, "step": 2},
            {"param_id": "vol_mult", "direction": "up", "min": 1.0, "max": 3.0, "step": 0.5},
        ]},
        PARAMS,
    )
    grid = {i["param_id"]: i["grid"] for i in items}
    combos = expand_param_grid(PARAMS, grid)
    assert combos, "网格必须能展开出组合"
    assert {c["atr_window"] for c in combos} == set(items[0]["levels"])
