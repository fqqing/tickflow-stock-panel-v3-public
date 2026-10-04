"""AI 形态归因解读 (app.signallab.insight) 单测 — 只测提示词装配, 不真调 LLM。

核心保证: 喂给 LLM 的**每一个数字都来自台账统计**, 提示词里没有模型自己算的余地。
因此这里锁的是「事实装配」而不是生成效果。
"""
from __future__ import annotations

import json

import numpy as np
import polars as pl

from app.signallab.insight import (
    attribution_markdown,
    build_user_prompt,
    collect_attribution_facts,
    overall_markdown,
    params_markdown,
)


def _ledger(n: int = 200) -> pl.DataFrame:
    rng = np.random.default_rng(11)
    vol_ratio = rng.uniform(0.5, 3.0, n)
    return pl.DataFrame({
        "symbol": [f"{i:06d}" for i in range(n)],
        "entry_signal_name": ["底部结构"] * n,
        "ctx_vol_ratio": vol_ratio,
        "ctx_atr_pct": rng.uniform(0.01, 0.05, n),
        "ret_5d": (vol_ratio - 1.75) * 0.05,
        "ret_20d": (vol_ratio - 1.75) * 0.08,
    })


def test_collect_attribution_facts_returns_only_bucket_stats():
    rows, overall, horizons = collect_attribution_facts(_ledger(), 5, min_samples=20)
    assert horizons == [5, 20]
    assert rows, "有足够样本时应产出分桶行"
    assert set(rows[0]) == {
        "feature", "bucket", "ret_n", "ret_win_rate", "ret_mean", "ret_profit_factor",
    }
    assert "ret5_mean" in overall, "整体战绩用汇总口径的列名"


def test_collect_attribution_facts_empty_when_all_buckets_thin():
    rows, _, _ = collect_attribution_facts(_ledger(n=40), 5, min_samples=1000)
    assert rows == []


def test_markdown_tables_contain_the_numbers():
    rows, overall, horizons = collect_attribution_facts(_ledger(), 5, min_samples=20)
    table = attribution_markdown(rows, 5)
    assert table.startswith("| 特征 | 档位 | 样本数 | 胜率 | 平均收益 | 盈亏比 |")
    assert "%" in table, "收益与胜率应以百分数呈现, 避免模型按小数理解"
    overall_table = overall_markdown(overall, horizons)
    assert "5 日" in overall_table and "20 日" in overall_table


def test_params_markdown_lists_ids_and_defaults():
    text = params_markdown([
        {"id": "require_gap", "label": "需要跳空", "type": "bool", "default": True},
    ])
    assert "require_gap" in text and "需要跳空" in text
    assert "该策略没有声明可调参数" in params_markdown(None)


def test_user_prompt_carries_dataset_range_and_focus():
    rows, overall, horizons = collect_attribution_facts(_ledger(), 5, min_samples=20)
    prompt = build_user_prompt(
        strategy_name="底部结构",
        strategy_desc="测试策略",
        dataset={"start": "2026-03-01", "end": "2026-09-30", "rows": 123},
        horizons=horizons,
        overall=overall,
        rows=rows,
        params=[{"id": "p1", "label": "参数1", "type": "float", "default": 1.5}],
        horizon=5,
        focus="只看创业板",
    )
    assert "2026-03-01" in prompt and "123" in prompt
    assert "只看创业板" in prompt
    assert "p1" in prompt


def test_insight_stream_emits_error_when_no_bucket(monkeypatch):
    """档位全被样本量门槛剔掉时, 必须回一条 error 而不是空流。"""
    import asyncio

    from app.signallab import insight

    async def run() -> list[dict]:
        out = []
        async for chunk in insight.analyze_attribution_stream(
            _ledger(n=40), {"start": None, "end": None},
            strategy_name="x", horizon=5, min_samples=1000,
        ):
            out.append(json.loads(chunk))
        return out

    events = asyncio.run(run())
    assert events[0]["type"] == "error"


_PARAMS = [
    {"id": "atr_window", "label": "ATR 窗口", "type": "int", "default": 14, "min": 5, "max": 60, "step": 1},
    {"id": "vol_mult", "label": "量能倍数", "type": "float", "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
]


def _run_stream(
    monkeypatch,
    chunks: list[str],
    params: list[dict] | None = None,
    follow_up: str = "",
) -> list[dict]:
    """用假 LLM 跑一遍 analyze_attribution_stream, 收集事件。

    follow_up 模拟"二次追问"的返回(正文没带出建议块时才会触发), 默认空串 ——
    单测不得真的联网调 LLM。
    """
    import asyncio

    from app.services import ai_provider
    from app.signallab import insight

    async def _fake_stream(messages, **kwargs):
        """假流: 忽略入参, 只吐 chunks。"""
        for c in chunks:
            yield c

    async def _fake_generate(messages, **kwargs):
        """假补全: 忽略入参, 只返回 follow_up。"""
        return follow_up

    monkeypatch.setattr(ai_provider, "ai_configured", lambda: True)
    monkeypatch.setattr(ai_provider, "stream_ai_text", _fake_stream)
    monkeypatch.setattr(ai_provider, "generate_ai_text", _fake_generate)

    async def run() -> list[dict]:
        out = []
        async for chunk in insight.analyze_attribution_stream(
            _ledger(), {"start": None, "end": None},
            strategy_id="bottom_structure",
            strategy_name="底部结构",
            params=params if params is not None else _PARAMS,
            horizon=5,
            min_samples=20,
        ):
            out.append(json.loads(chunk))
        return out

    return asyncio.run(run())


def test_stream_emits_suggestions_and_keeps_prose_clean(monkeypatch):
    """正文照常流式下发, 建议块被扣下并单独作为 suggestions 事件发出。"""
    events = _run_stream(monkeypatch, [
        "## 关键发现\n",
        "- 高量比档位表现更好\n",
        "```json\n",
        '{"suggestions":[{"param_id":"atr_window","direction":"down",'
        '"min":6,"max":20,"step":2,"reason":"高 ATR 档表现差"}]}\n',
        "```",
    ])
    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert "suggestions" in kinds

    prose = "".join(e.get("content", "") for e in events if e["type"] == "delta")
    assert "suggestions" not in prose, "JSON 建议块不能漏进正文"
    assert "高量比档位表现更好" in prose

    sug = next(e for e in events if e["type"] == "suggestions")
    assert sug["strategy_id"] == "bottom_structure"
    assert sug["combos"] >= 1
    item = sug["items"][0]
    assert item["param_id"] == "atr_window"
    assert item["reason"] == "高 ATR 档表现差"
    g = item["grid"]
    # 8 档 (6~20 / step 2) 超过 5 档上限 -> 步长按整数倍加粗到 4, 区间端点随之后移
    assert g == {"min": 6.0, "max": 18.0, "step": 4.0}
    assert item["levels"] == [6, 10, 14, 18]
    assert sug["combos"] == 4


def test_stream_falls_back_to_explore_grid_when_model_invents_params(monkeypatch):
    """模型编了不存在的参数 + 追问也给不出 -> 退化为标注来源的探索网格, 不冒充 AI 结论。"""
    events = _run_stream(monkeypatch, [
        "正文\n",
        "```json\n",
        '{"suggestions":[{"param_id":"magic_param","direction":"up"}]}\n',
        "```",
    ])
    sug = next((e for e in events if e["type"] == "suggestions"), None)
    assert sug is not None, "有可调参数时总该给出一个可跑的网格"
    assert sug["source"] == "explore"
    assert all(i["param_id"] != "magic_param" for i in sug["items"])


def test_stream_uses_follow_up_when_prose_has_no_json(monkeypatch):
    """正文没带建议块 -> 二次追问的 JSON 生效, 来源标 ai。"""
    events = _run_stream(
        monkeypatch,
        ["只有正文\n"],
        follow_up='{"suggestions":[{"param_id":"vol_mult","direction":"up",'
                  '"min":1.0,"max":3.0,"step":0.5,"reason":"高量比档更好"}]}',
    )
    sug = next(e for e in events if e["type"] == "suggestions")
    assert sug["source"] == "ai"
    assert sug["items"][0]["param_id"] == "vol_mult"
    assert sug["items"][0]["reason"] == "高量比档更好"


def test_stream_without_params_and_suggestions_has_no_event(monkeypatch):
    events = _run_stream(monkeypatch, ["只有正文\n"], params=[])
    assert [e["type"] for e in events] == ["meta", "delta", "done"]
