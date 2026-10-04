"""AI 形态归因解读 — 把分桶统计交给 LLM 提炼结论与调参建议。

为什么需要这一步
----------------
``/api/signallab/attribution`` 给出的是**事实**(哪个档位样本多少, 平均收益多少),
但要把它变成行动还需要两跳: 哪些档位值得加权 / 该避开, 以及这些形态分别对应策略
的哪个参数。这一步把两跳交给 LLM, 事实部分仍由后端算好塞进提示词 —— LLM 只做
解读与建议, 不负责算数(让它算数会编)。

设计约束
--------
- **只读事实**: 提示词里的每个数字都来自台账统计, 要求模型不得自行估算或补充
  表外数字; 台账样本量小的时候要明确说"结论不可靠"。
- **流式**: 与概念轮动 / RPS 分析同一套 NDJSON 协议(meta/delta/error/done),
  前端可以逐字渲染, 长时间等待不白屏。
- **参数建议要落到具体参数**: 提示词里带上策略的 params 声明(id/label/default),
  让模型把"避开高 ATR 档"翻译成"调哪个参数、往哪个方向", 否则建议无法执行。
- **建议要出机器可读的一份**: 正文给人类看, 末尾再输出一个 ```json 建议块给程序用
  (见 ``suggestions.py``)。这份会被 :class:`FenceSplitter` 从流里扣下来, 不进正文,
  且每条都要过参数声明的边界/步长校验后才下发给前端 —— 模型编的参数名直接丢。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

import polars as pl

from app.signallab.lab import DEFAULT_ATTRIBUTION_FEATURES
from app.signallab.outcome import ret_column
from app.signallab.suggestions import (
    FenceSplitter,
    explore_suggestions,
    extract_suggestions,
    normalize_suggestions,
    suggest_combos,
)
from app.signallab.summary import attribute_outcomes, summarize_outcomes

logger = logging.getLogger(__name__)

#: 提示词里最多放多少行分桶结果(特征数 x 档位数, 实际一般 20~40 行)
_MAX_ROWS = 60

#: 台账收益列名 ret_{N}d
_RET_COL_RE = re.compile(r"^ret_(\d+)d$")


def _pct(value: Any, digits: int = 2) -> str:
    """小数 -> "x.xx%"; 无效值 -> "--"。"""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "--"
    if v != v:
        return "--"
    return f"{v * 100:.{digits}f}%"


def _num(value: Any, digits: int = 2) -> str:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "--"
    if v != v:
        return "--"
    return f"{v:.{digits}f}"


def attribution_markdown(rows: Sequence[dict[str, Any]], horizon: int) -> str:
    """分桶结果 -> Markdown 表格(喂给 LLM 的事实部分)。"""
    head = "| 特征 | 档位 | 样本数 | 胜率 | 平均收益 | 盈亏比 |"
    split = "| --- | --- | --- | --- | --- | --- |"
    lines = [head, split]
    for row in list(rows)[:_MAX_ROWS]:
        lines.append(
            f"| {row.get('feature', '--')} | {row.get('bucket', '--')} "
            f"| {row.get('ret_n', '--')} | {_pct(row.get('ret_win_rate'), 1)} "
            f"| {_pct(row.get('ret_mean'))} | {_num(row.get('ret_profit_factor'))} |"
        )
    return "\n".join(lines)


def overall_markdown(overall: dict[str, Any], horizons: Sequence[int]) -> str:
    """整体战绩 -> Markdown 表格。"""
    lines = ["| 持有期 | 样本 | 胜率 | 平均收益 | 盈亏比 |", "| --- | --- | --- | --- | --- |"]
    for horizon in horizons:
        lines.append(
            f"| {horizon} 日 | {overall.get(f'ret{horizon}_n', '--')} "
            f"| {_pct(overall.get(f'ret{horizon}_win_rate'), 1)} "
            f"| {_pct(overall.get(f'ret{horizon}_mean'))} "
            f"| {_num(overall.get(f'ret{horizon}_profit_factor'))} |"
        )
    return "\n".join(lines)


def params_markdown(params: Sequence[dict[str, Any]] | None) -> str:
    """策略可调参数 -> Markdown 列表(让建议能落到具体参数)。"""
    if not params:
        return "(该策略没有声明可调参数)"
    return "\n".join(
        f"- `{p.get('id')}` ({p.get('label') or p.get('id')})"
        f" 类型 {p.get('type')} 当前默认 {p.get('default')}"
        for p in params
    )


def _system_prompt() -> str:
    return (
        "你是一名资深的 A 股量化研究员, 负责解读一个策略的「信号形态归因」结果。\n"
        "归因的含义: 把策略在历史上产生的每一条信号, 按信号发生时**已经知道**的形态特征"
        "(距 60 日高点回撤 / 距 60 日低点涨幅 / 20 日量比 / 14 日 ATR% / MA20 乖离)分档, "
        "统计各档位持有 N 个交易日后的表现。\n"
        "硬性要求:\n"
        "1. 只使用下面给出的数据, 不得自行估算、补充或编造任何表外数字;\n"
        "2. 样本数少于 30 的档位必须明确标注「样本不足, 结论仅作参考」;\n"
        "3. 结论要能落地: 说清楚该加权/该避开哪些形态, 并对应到具体可调参数;\n"
        "4. 用中文, 结构化输出(小标题 + 要点), 不要复述表格, 总长控制在 800 字以内;\n"
        "5. 如果数据整体不支持任何结论(样本太少 / 各档差异不显著), 直接说明并给出"
        "下一步该补什么数据, 不要硬凑建议;\n"
        "6. 正文写完后, 最后单独输出一个 ```json 代码块 (正文里不要出现其他代码块), "
        "把参数建议写成机器可读的形式, 用于直接生成参数网格:\n"
        '{"suggestions":[{"param_id":"<参数 id, 必须来自上面列表>",'
        '"direction":"up|down|hold","reason":"一句话说明为什么",'
        '"min":<数字>,"max":<数字>,"step":<数字>}]}\n'
        "要求: 最多 3 条, 只写有把握的; min/max 必须落在该参数声明的范围内, "
        "step 要能把 (max-min) 整除; 没有可调参数或数据不支持时输出 "
        '{"suggestions":[]}。'
    )


_SUGGEST_SYSTEM = (
    "你是量化策略调参助手, 只做一件事: 把形态归因结论翻译成参数扫描范围。\n"
    "只输出一个 JSON 对象, 不要任何解释、不要代码块围栏、不要前后缀文字:\n"
    '{"suggestions":[{"param_id":"<下面参数列表里的 id>","direction":"up|down|hold",'
    '"reason":"一句话","min":<数字>,"max":<数字>,"step":<数字>}]}\n'
    "规则: 最多 3 条; min/max 必须落在该参数声明的 [min,max] 内; step 必须能把 "
    "(max-min) 整除; 没有把握就返回 {\"suggestions\":[]}, 不要硬凑。"
)


def _suggest_user_prompt(
    params: Sequence[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
    horizon: int,
) -> str:
    """二次追问的提示词: 只带参数声明 + 分桶事实, 不要正文(省 token 也更聚焦)。"""
    return "\n\n".join([
        f"## 可调参数\n{params_markdown(params)}",
        f"## 形态归因 (持有 {horizon} 日, 按平均收益降序)\n{attribution_markdown(rows, horizon)}",
        "请给出建议扫描的参数范围。",
    ])


async def _request_param_suggestions(
    params: Sequence[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
    horizon: int,
) -> list[dict]:
    """正文没带出建议块时的二次追问 —— 一次短调用, 只求 JSON。

    为什么不直接把这段塞进主提示词: 主调用要流式输出长正文, 模型在长输出末尾常常
    省略结构化尾巴(实测 bottom_structure 两次都只在正文里说"无法给出建议", 没有
    输出 json 块)。短调用 + temperature=0 的输出稳定得多。
    """
    if not params:
        return []
    try:
        from app.services.ai_provider import generate_ai_text

        text = await generate_ai_text(
            [
                {"role": "system", "content": _SUGGEST_SYSTEM},
                {"role": "user", "content": _suggest_user_prompt(params, rows, horizon)},
            ],
            temperature=0.0,
            max_tokens=800,
        )
    except Exception as e:  # 追问失败不该让整条解读失败, 只是没有建议而已
        logger.warning("参数建议二次追问失败: %r", e)
        return []
    return normalize_suggestions(extract_suggestions(prose=text or ""), list(params))


def build_user_prompt(
    *,
    strategy_name: str,
    strategy_desc: str,
    dataset: dict[str, Any],
    horizons: Sequence[int],
    overall: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    params: Sequence[dict[str, Any]] | None,
    horizon: int,
    focus: str,
) -> str:
    """组装用户提示词: 策略背景 + 整体战绩 + 分桶事实 + 可调参数 + 关注点。"""
    parts = [
        f"## 策略\n名称: {strategy_name}\n描述: {strategy_desc or '(无)'}",
        f"## 台账\n区间: {dataset.get('start')} ~ {dataset.get('end')}"
        f", 共 {dataset.get('rows') or '未知'} 条信号",
        f"## 整体战绩 (T+1 开盘成交口径)\n{overall_markdown(overall, horizons)}",
        f"## 形态归因 (持有 {horizon} 日, 按平均收益降序)\n"
        f"{attribution_markdown(rows, horizon)}",
        f"## 可调参数\n{params_markdown(params)}",
    ]
    if focus:
        parts.append(f"## 用户追加关注点\n{focus}")
    parts.append(
        "请按系统提示的要求输出: 关键发现 / 值得加权与应当规避的形态 / "
        "对应的参数调整建议 / 结论的可靠性与风险提示。\n"
        "最后**务必**另起一段输出一个 ```json 代码块, 内含 suggestions 数组: 从上面的可调"
        '参数里挑最多 3 个, 给 min/max/step (须落在该参数声明范围内); 确实无可调或数据不'
        '支持时输出 {"suggestions":[]} —— 这一段是给程序读的, 不能省略。'
    )
    return "\n\n".join(parts)


def collect_attribution_facts(
    frame: pl.DataFrame,
    horizon: int,
    *,
    features: Sequence[str] | None = None,
    buckets: int = 4,
    min_samples: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[int]]:
    """算好要喂给 LLM 的事实: (分桶行, 整体战绩, 持有期列表)。"""
    wanted = list(features) if features else list(DEFAULT_ATTRIBUTION_FEATURES)
    usable = [f for f in wanted if f in frame.columns]
    result = attribute_outcomes(
        frame,
        usable,
        horizon,
        column=ret_column(horizon),
        buckets=buckets,
        min_samples=min_samples,
    )
    # 收益列名形如 ret_5d / ret_20d (注意不是汇总口径的 ret5_mean)
    horizons = sorted({
        int(m.group(1))
        for m in (_RET_COL_RE.match(name) for name in frame.columns)
        if m
    })
    overall_frame = summarize_outcomes(
        frame, horizons=horizons or [horizon], with_excess=False, with_path=False
    )
    overall = overall_frame.to_dicts()[0] if not overall_frame.is_empty() else {}
    rows: list[dict[str, Any]] = []
    for row in result.to_dicts():
        rows.append({
            "feature": row.get("feature"),
            "bucket": row.get("bucket"),
            "ret_n": row.get("ret_n"),
            "ret_win_rate": row.get("ret_win_rate"),
            "ret_mean": row.get("ret_mean"),
            "ret_profit_factor": row.get("ret_profit_factor"),
        })
    return rows, overall, horizons


async def analyze_attribution_stream(
    frame: pl.DataFrame,
    dataset: dict[str, Any],
    *,
    strategy_id: str = "",
    strategy_name: str,
    strategy_desc: str = "",
    params: Sequence[dict[str, Any]] | None = None,
    horizon: int,
    features: Sequence[str] | None = None,
    buckets: int = 4,
    min_samples: int = 20,
    focus: str = "",
) -> AsyncIterator[str]:
    """流式形态归因解读, yield 每个 NDJSON 事件字符串。

    协议与概念轮动分析一致: meta / delta / error / done, 额外多一个 ``suggestions``
    —— 正文流完后再下发模型给的参数建议(已按参数声明收口), 供前端一键送进网格搜索。
    """
    rows, overall, horizons = collect_attribution_facts(
        frame, horizon, features=features, buckets=buckets, min_samples=min_samples
    )
    if not rows:
        yield json.dumps(
            {"type": "error", "message": "台账里没有样本量足够的档位, 换个持有期或放宽 min_samples 再试"},
            ensure_ascii=False,
        )
        return

    yield json.dumps({
        "type": "meta",
        "horizon": horizon,
        "n_buckets": len(rows),
        "n_signals": frame.height,
        "summary": f"{len(rows)} 个档位 · {frame.height} 条信号 · 持有 {horizon} 日",
    }, ensure_ascii=False)

    try:
        from app.services.ai_provider import ai_configured, stream_ai_text

        if not ai_configured():
            yield json.dumps(
                {"type": "error", "message": "AI 未配置, 请在「设置」页填写 API Key 与接口地址"},
                ensure_ascii=False,
            )
            return

        user_prompt = build_user_prompt(
            strategy_name=strategy_name,
            strategy_desc=strategy_desc,
            dataset=dataset,
            horizons=horizons,
            overall=overall,
            rows=rows,
            params=params,
            horizon=horizon,
            focus=focus,
        )
        splitter = FenceSplitter()
        got = False
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=None,
        ):
            got = True
            # 建议块(```json ... ```)被 splitter 扣下, 只把正文推给前端逐字渲染。
            chunk = splitter.feed(delta)
            if chunk:
                yield json.dumps({"type": "delta", "content": chunk}, ensure_ascii=False)
        tail = splitter.flush()
        if tail:
            yield json.dumps({"type": "delta", "content": tail}, ensure_ascii=False)
        if not got:
            yield json.dumps(
                {"type": "error", "message": "AI 未返回正文(输出被截断), 请重试"},
                ensure_ascii=False,
            )
            return
    except Exception as e:  # 流式接口要把失败推给前端, 不能变成 500
        logger.exception("AI attribution insight failed: %s", e)
        yield json.dumps({"type": "error", "message": f"AI 形态归因失败: {e}"}, ensure_ascii=False)
        return

    meta_list = list(params or [])
    items = normalize_suggestions(
        extract_suggestions(prose=splitter.text, json_text=splitter.json_text()),
        meta_list,
    )
    source = "ai"
    if not items:
        items = await _request_param_suggestions(meta_list, rows, horizon)
    if not items:
        items = explore_suggestions(meta_list)
        source = "explore" if items else "none"
    if items:
        yield json.dumps({
            "type": "suggestions",
            "strategy_id": strategy_id,
            "horizon": horizon,
            "source": source,
            "items": items,
            "combos": suggest_combos(items),
        }, ensure_ascii=False)
    yield json.dumps({"type": "done"}, ensure_ascii=False)
