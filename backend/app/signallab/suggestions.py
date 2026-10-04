"""把 LLM 的形态归因结论转成可直接喂给网格搜索的参数范围。

为什么单独一个模块
------------------
``insight.py`` 负责"让 LLM 说话", 本模块负责"把话变成能执行的东西", 两者分开是因为
后者的正确性可以被单测锁死, 而 LLM 的输出不可控:

- **流式切分** (:class:`FenceSplitter`): 模型要把结构化建议放在正文之后的 ```` ```json ````
  块里, 但这个块不能出现在用户看到的正文里(一堆 JSON 没意义)。按行切分是因为 delta
  边界是任意的, 只有以换行结尾的行才是完整的, 围栏标记不会被切成 "``" + "`json"。
- **校验与收口** (:func:`normalize_suggestions`): 模型给的范围必须落回
  ``StrategyDef.meta["params"]`` 声明的类型/边界/步长上:
    * 参数 id 不在策略里 -> 丢弃(模型会编参数名);
    * min/max clamp 到声明区间, step 保证整除 (max-min), 否则后端
      ``optimizer._candidates_for`` 会直接拒掉整个网格;
    * 档位数与参数条数双重封顶 -> 组合数不会爆 (3 个参数 x 5 档 = 125 组上限)。

产出直接对齐前端 ``pages/backtest/components/paramSweep.tsx`` 的 Sweep 结构:
数值型给 ``{min, max, step}``, bool/select 给候选值列表。
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

logger = logging.getLogger(__name__)

#: 建议条数上限 / 单参数档位上限 —— 两者相乘就是最坏组合数 (3 x 5 -> 125 组)。
MAX_SUGGESTED_PARAMS = 3
MAX_LEVELS = 5

#: 行首围栏(允许最多 3 个前导空格, 语言标记可选): "```json" / "```" / "```python"
_FENCE_RE = re.compile(r"^[ \t]{0,3}```(.*)$")

#: 语言标记是否为 json 家族(jsonc / json5 也算, 模型偶尔会这么写)
_JSON_LANG_RE = re.compile(r"^[ \t]*[jJ][sS][oO][nN][a-zA-Z0-9]*[ \t]*$")

#: 浮点尾巴收敛位数
_NDIGITS = 6


class FenceSplitter:
    """把 LLM 输出流切成「正文」与「第一个 ```json 块」。

    状态机: text(正文) / code(普通代码块, 内容仍当正文) / json(建议块, 扣下)。
    只有以 ``\\n`` 结尾的行会被处理, 其余留在缓冲区等下一片 delta —— 这样围栏标记
    永远不会被 delta 边界切开。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._state = "text"
        self._json: list[str] = []
        self.text = ""

    @property
    def state(self) -> str:
        return self._state

    def feed(self, delta: str) -> str:
        """喂入一片 delta, 返回可以推给前端的正文增量(建议块部分已被扣下)。"""
        self._buf += delta
        out: list[str] = []
        while True:
            idx = self._buf.find("\n")
            if idx < 0:
                break
            line = self._buf[: idx + 1]
            self._buf = self._buf[idx + 1 :]
            chunk = self._feed_line(line)
            if chunk:
                out.append(chunk)
        text = "".join(out)
        self.text += text
        return text

    def _feed_line(self, line: str) -> str:
        m = _FENCE_RE.match(line)
        if m:
            if self._state == "text":
                if _JSON_LANG_RE.match(m.group(1) or ""):
                    self._state = "json"
                    return ""
                # 普通代码块: 围栏行与内容都还给正文(提示词已要求模型别用, 兜个底)
                self._state = "code"
                return line
            prev, self._state = self._state, "text"
            return "" if prev == "json" else line
        if self._state == "json":
            self._json.append(line)
            return ""
        return line

    def flush(self) -> str:
        """流结束时冲刷不足一行的残留, 返回正文增量。"""
        rest = self._buf
        self._buf = ""
        if not rest:
            return ""
        if self._state == "json":
            # 收尾的 ``` 常常不带换行(流到此结束), 剥掉再入库, 否则 JSON 解析会带尾巴
            self._json.append(_FENCE_RE.sub("", rest, count=1))
            return ""
        self.text += rest
        return rest

    def json_text(self) -> str:
        """扣下的建议块原文(不含围栏行)。"""
        return "".join(self._json)


def parse_suggestion_payload(text: str) -> dict[str, Any] | None:
    """从建议块原文里抠出 JSON 对象。

    模型可能在块内先写一句说明, 或在 JSON 后补半个围栏 —— 取首个 ``{`` 到最后一个
    ``}`` 之间再解析, 比要求"整块必须是纯 JSON"宽容得多, 且不会误吃正文。
    """
    body = (text or "").strip()
    if not body:
        return None
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(body[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        logger.debug("参数建议块不是合法 JSON, 已忽略: %.200s", body)
        return None
    return payload if isinstance(payload, dict) else None


def _json_object_at(text: str, start: int) -> dict | None:
    """从 start(必须是 ``{``) 起做括号配对扫描, 返回完整对象; 不成形返回 None。

    手扫而不是正则, 是因为建议里可能含中文与引号, 正则很容易在字符串内部误判边界。
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def extract_suggestions(prose: str = "", json_text: str = "") -> dict | None:
    """取模型给出的建议对象: 先找扣下的 json 块, 再退回正文里手写的块。

    为什么要退回正文: 模型并不总按围栏输出 —— 有时写 ```` ``` ````(无语言标记)、
    有时直接把 JSON 混在正文里。这两种情况下 :class:`FenceSplitter` 都当正文放行了,
    但内容仍然是可用建议, 丢掉可惜。
    """
    for source in (json_text, prose):
        if not source:
            continue
        # 从后往前找, 最后一次出现的才是模型的最终结论
        idx = source.rfind('"suggestions"')
        if idx < 0:
            continue
        start = source.rfind("{", 0, idx)
        if start < 0:
            continue
        obj = _json_object_at(source, start)
        if obj is not None:
            return obj
    return parse_suggestion_payload(json_text)


def _as_float(value: Any) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _r6(value: float) -> float:
    return round(float(value), _NDIGITS)


def _default_window(default: float, lo_bound: float, hi_bound: float, direction: str) -> tuple[float, float]:
    """模型没给范围时, 按"调参方向"以默认值为锚点推出一个窗口。

    down -> 扫默认值下方(收紧该参数), up -> 扫上方, hold -> 默认值两侧各一点。
    默认值为 0 或贴着边界时无锚点, 退化为按声明区间的比例切一段。
    """
    span = hi_bound - lo_bound
    if span <= 0:
        return lo_bound, hi_bound
    if default <= 0:
        if direction == "down":
            return lo_bound, lo_bound + span * 0.5
        if direction == "up":
            return lo_bound + span * 0.5, hi_bound
        return lo_bound + span * 0.3, lo_bound + span * 0.7
    if direction == "down":
        lo, hi = max(lo_bound, default * 0.6), min(hi_bound, default)
    elif direction == "up":
        lo, hi = max(lo_bound, default), min(hi_bound, default * 1.6)
    else:
        lo, hi = max(lo_bound, default * 0.8), min(hi_bound, default * 1.2)
    if hi - lo <= span * 1e-6:  # 默认值贴边界 -> 向内侧让出 20% 区间
        lo = max(lo_bound, min(lo, hi_bound - span * 0.2))
        hi = min(hi_bound, max(hi, lo_bound + span * 0.2))
    return lo, hi


def _numeric_grid(
    raw: dict,
    pmeta: dict,
    direction: str,
    max_levels: int,
    *,
    coarse: bool = False,
) -> tuple[float, float, float, list] | None:
    """coarse=True 时按"铺满区间"而不是"保留步长粒度"来加粗步长(兜底探索网格用)。"""
    """数值型参数 -> (min, max, step, levels)。

    三步收口: 区间 clamp 到声明边界 -> 步长兜底 + 档位封顶 -> 重算端点保证 step 整除
    (max-min), 否则优化器展开候选值时末档会越界被拒。
    """
    is_int = pmeta.get("type") == "int"
    default = _as_float(pmeta.get("default")) or 0.0
    lo_bound = _as_float(pmeta.get("min"))
    hi_bound = _as_float(pmeta.get("max"))
    if lo_bound is None:
        lo_bound = default * 0.5 if default > 0 else 0.0
    if hi_bound is None:
        hi_bound = default * 1.5 if default > 0 else 1.0
    if hi_bound - lo_bound <= 0:
        return None

    # 1) 区间
    lo = _as_float(raw.get("min"))
    hi = _as_float(raw.get("max"))
    if lo is None or hi is None:
        dlo, dhi = _default_window(default, lo_bound, hi_bound, direction)
        lo = dlo if lo is None else lo
        hi = dhi if hi is None else hi
    if lo > hi:
        lo, hi = hi, lo
    lo = min(max(lo, lo_bound), hi_bound)
    hi = min(max(hi, lo_bound), hi_bound)

    # 2) 步长: 模型给的 > 参数声明的 > 区间四等分
    step = _as_float(raw.get("step")) or _as_float(pmeta.get("step"))
    if step is None or step <= 0:
        step = (hi - lo) / 4.0
        if is_int:
            step = float(max(1, round(float(step))))
    if step <= 0:
        return None

    # 3) 档位封顶: 档太多就按整数倍加粗步长(保住区间宽度, 只牺牲分辨率)。
    #    取整数倍而不是重算 (hi-lo)/(n-1), 是为了让步长仍落在模型/声明的粒度上
    #    (给 0.1 的粒度不会被改成 0.37 这种数)。
    steps_needed = (hi - lo) / step
    if steps_needed + 1 > max_levels:
        if coarse:
            # 铺满区间优先: 均分后再让对齐流程向下收, 保证末档不越界
            even = (hi - lo) / (max_levels - 1)
            step = float(math.floor(even)) if is_int else even
        else:
            factor = math.ceil(steps_needed / (max_levels - 1))
            step = step * max(1, factor)

    # 4) 对齐端点
    if is_int:
        step = float(max(1, round(float(step))))
        lo = float(math.floor(lo))
        hi = float(math.ceil(hi))
    else:
        step = _r6(step)
        lo, hi = _r6(lo), _r6(hi)
    n = math.floor((hi - lo) / step + 1e-9)
    while n >= 1 and lo + n * step > hi_bound + 1e-9:
        n -= 1
    if n < 1:
        # 区间容不下一档: 以 default 为锚往能放下的方向挪, 仍放不下就放弃这条建议
        anchor = min(max(default, lo_bound), hi_bound)
        lo = min(max(anchor - step / 2, lo_bound), hi_bound - step)
        hi = lo + step
        if lo < lo_bound - 1e-9 or hi > hi_bound + 1e-9:
            return None
        n = 1
    else:
        hi = lo + n * step

    levels: list = []
    for i in range(n + 1):
        v = lo + i * step
        levels.append(round(float(v)) if is_int else _r6(v))
    lo_v = levels[0]
    hi_v = levels[-1]
    return (
        float(lo_v),
        float(hi_v),
        float(round(float(step)) if is_int else step),
        levels,
    )


def _normalize_one(raw: dict, pid: str, pmeta: dict, max_levels: int, *, coarse: bool = False) -> dict | None:
    ptype = pmeta.get("type")
    label = str(pmeta.get("label") or pid)
    direction = str(raw.get("direction") or "").strip().lower()
    if direction not in ("up", "down", "hold"):
        direction = "hold"
    reason = str(raw.get("reason") or raw.get("why") or "").strip()

    if ptype == "bool":
        return {
            "param_id": pid, "label": label, "type": "bool", "direction": direction,
            "reason": reason, "grid": [True, False], "levels": [True, False], "n_levels": 2,
        }
    if ptype == "select":
        options = list(pmeta.get("options") or [])
        wanted = raw.get("values")
        values = [v for v in wanted if v in options] if isinstance(wanted, list) else []
        if not values:
            values = list(options)
        if not values:
            return None
        return {
            "param_id": pid, "label": label, "type": "select", "direction": direction,
            "reason": reason, "grid": values, "levels": values, "n_levels": len(values),
        }
    if ptype not in ("float", "int"):
        return None
    grid = _numeric_grid(raw, pmeta, direction, max_levels, coarse=coarse)
    if grid is None:
        return None
    lo, hi, step, levels = grid
    return {
        "param_id": pid, "label": label, "type": ptype, "direction": direction,
        "reason": reason, "grid": {"min": lo, "max": hi, "step": step},
        "levels": levels, "n_levels": len(levels),
    }


def normalize_suggestions(
    payload: dict | None,
    params_meta: list[dict] | None,
    *,
    max_params: int = MAX_SUGGESTED_PARAMS,
    max_levels: int = MAX_LEVELS,
) -> list[dict]:
    """把模型的建议块规范成可直接用的网格条目; 非法/编造的一律丢弃。"""
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("suggestions")
    if not isinstance(raw_items, list):
        return []
    by_id = {str(p.get("id")): p for p in (params_meta or [])}
    out: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("param_id") or raw.get("id") or "").strip()
        pmeta = by_id.get(pid)
        if pmeta is None:
            logger.debug("建议的参数 '%s' 不在策略参数表里, 已丢弃", pid)
            continue
        item = _normalize_one(raw, pid, pmeta, max_levels)
        if item is not None:
            out.append(item)
        if len(out) >= max_params:
            break
    return out


def explore_suggestions(
    params_meta: list[dict] | None,
    *,
    max_params: int = 2,
    max_levels: int = MAX_LEVELS,
) -> list[dict]:
    """AI 没给出可执行建议时的兜底: 按参数声明等距铺一个探索网格。

    ⚠️ 这不是 AI 的结论 —— 前端必须标注来源, 否则用户会以为"AI 建议扫这个范围"。
    只铺前 max_params 个参数, 避免把组合数放大到几千组。
    """
    out: list[dict] = []
    for p in params_meta or []:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        raw: dict = {}
        if p.get("type") in ("float", "int"):
            raw = {"min": p.get("min"), "max": p.get("max")}
        item = _normalize_one(raw, pid, p, max_levels, coarse=True)
        if item is not None:
            item["reason"] = "AI 未给出可执行建议, 按参数声明范围等距铺开(仅供探索)"
            out.append(item)
        if len(out) >= max_params:
            break
    return out


def suggest_combos(items: list[dict]) -> int:
    """建议网格的组合数(各参数档位数相乘)。"""
    total = 1
    for item in items:
        total *= max(1, int(item.get("n_levels") or 1))
    return total
