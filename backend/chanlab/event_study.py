"""缠论买卖点事件研究 (N13 第一步).

回答的问题不是「这条策略曲线赚多少」(那是 :mod:`chanlab.backtest` 撮合回测的
事), 而是**「这类买点历史上出现之后, N 日后涨跌的概率分布长什么样」**:

    每类买点 (1/1p/2/2s/3a/3b) 在回溯窗口内每次出现 -> 记一个事件 ->
    取确认后 N in {5,10,20,60} 根 K 线的收益 -> 按类型聚合出
    样本数 / 胜率 / 均值 / 中位数 / 分位数。

锚点口径 (关键, 别改错):

- **bsp.klu 是笔端点 K 线, 不是确认点** —— vendor 的 ``CBS_Point.__init__`` 里
  ``self.klu = bi.get_end_klu()``, 即笔的最低/最高那根。站在那根 K 线收盘时你
  根本不知道笔已经结束 (笔要被后续走势确认), 直接拿它当买入锚就是未来函数,
  实测 buy:1 的 5 日胜率会虚高到 94%。
- 所以锚 = ``end_klu.idx + delay`` 的收盘价, ``delay`` 默认 1 (v1 chan_structure
  的「确认延迟 +1」口径) —— 这是**最低限度**的去未来化; 一类买点的真实确认往往
  要等反向笔走完好几根, 想要更严的口径把 ``--delay`` 调大即可, 参数在 CLI 上。
- N 日 = **N 根有效 K 线**。自有日线库只有交易日, 停牌日不占行, 所以
  ``closes[anchor + n]`` 天然就是「N 个交易日后」, 不需要交易日历。
- 收益 = ``(close[n] / close[anchor] - 1) * 100``, 前复权口径, 与图表一致。

产物是 JSON 落 ``data/backtest/event_study_{level}.json``, 由
``backend/scripts/event_study.py`` 离线生成 (分钟级重活不进请求路径),
接口只读文件。选股页的「历史胜率」列与回测页的事件研究卡共用它。
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from chanlab.engine import build_chan, level_elements
from chanlab.loader import load_symbol_daily, load_symbol_minute

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "backtest"

#: 统计的持有期 (有效 K 线根数)
HORIZONS = (5, 10, 20, 60)
#: 低于该样本数的类型不产出统计 (两三笔算出的胜率是噪声)
MIN_SAMPLES = 5
MIN_BARS = 60

MINUTE_FREQUENCIES = ("1m", "5m", "15m", "30m", "60m")


def _is_minute(level: str) -> bool:
    return level.strip().lower() in MINUTE_FREQUENCIES


def load_frame(symbol: str, level: str, lookback: int):
    if _is_minute(level):
        return load_symbol_minute(symbol, freq=level.strip().lower(), lookback=lookback)
    return load_symbol_daily(symbol, lookback=lookback)


def _bsp_type_names(bsp) -> list[str]:
    out: list[str] = []
    for t in bsp.type:
        name = t.value if hasattr(t, "value") else str(t)
        if name not in out:
            out.append(name)
    return out


def study_symbol(
    symbol: str,
    *,
    level: str = "1d",
    lookback: int = 1200,
    profile: str = "v1",
    horizons: tuple[int, ...] = HORIZONS,
    delay: int = 1,
) -> list[dict[str, Any]] | None:
    """单标的全部买卖点事件。数据不足返回 None, 失败也返回 None (调用方计数).

    ``delay``: 确认延迟 (有效 K 线根数), 见模块 docstring 的未来函数说明。
    """
    frame = load_frame(symbol, level, lookback)
    if frame is None or frame.is_empty() or frame.height < MIN_BARS:
        return None

    time_col = "datetime" if _is_minute(level) else "date"
    try:
        chan = build_chan(symbol, start=str(frame[time_col].min()), lv_list=[level], profile=profile)
        elements = level_elements(chan)
    except Exception:
        return None

    closes = frame["close"].to_list()
    n_bars = len(closes)
    events: list[dict[str, Any]] = []
    for bsp in elements["bsp"]:
        end_idx = int(getattr(bsp.klu, "idx", -1))
        idx = end_idx + delay
        if end_idx < 0 or idx >= n_bars:
            continue
        base = float(closes[idx])
        if base <= 0 or base != base:
            continue
        fwd: dict[int, float] = {}
        for n in horizons:
            j = idx + n
            if j < n_bars:
                ret = (float(closes[j]) / base - 1.0) * 100.0
                if not math.isfinite(ret):
                    continue
                fwd[n] = round(ret, 4)
        if not fwd:
            continue
        side = "buy" if bsp.is_buy else "sell"
        for name in _bsp_type_names(bsp):
            events.append({
                "symbol": symbol,
                "key": f"{side}:{name}",
                "idx": idx,
                "fwd": fwd,
            })
    return events


# --------------------------------------------------------------------------
# 聚合 (纯函数, 独立可测)
# --------------------------------------------------------------------------


def _quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数. 空序列不会被传进来 (调用方保证)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def aggregate(
    events: list[dict[str, Any]],
    horizons: tuple[int, ...] = HORIZONS,
    min_samples: int = MIN_SAMPLES,
) -> dict[str, dict[str, Any]]:
    """事件列表 -> 按类型键聚合的统计。

    返回 ``{key: {samples, horizons: {n: {samples, win_rate, mean, median, p10, p90}}}}``。
    样本数按类型全量计 (某个 N 上够不到窗口的事件只在那一档少一个样本)。
    """
    by_key: dict[str, list[dict]] = {}
    for e in events:
        by_key.setdefault(str(e["key"]), []).append(e)

    out: dict[str, dict[str, Any]] = {}
    for key, group in sorted(by_key.items()):
        stats: dict[str, Any] = {"samples": len(group), "horizons": {}}
        for n in horizons:
            vals = sorted(e["fwd"][n] for e in group if n in e["fwd"])
            if len(vals) < min_samples:
                continue
            wins = sum(1 for v in vals if v > 0)
            stats["horizons"][str(n)] = {
                "samples": len(vals),
                "win_rate": round(wins / len(vals), 4),
                "mean": round(sum(vals) / len(vals), 4),
                "median": round(_quantile(vals, 0.5), 4),
                "p10": round(_quantile(vals, 0.1), 4),
                "p90": round(_quantile(vals, 0.9), 4),
            }
        if stats["horizons"]:
            out[key] = stats
    return out


# --------------------------------------------------------------------------
# 产物读写
# --------------------------------------------------------------------------


def artifact_path(level: str) -> Path:
    return OUT_DIR / f"event_study_{level.strip().lower()}.json"


def write_artifact(payload: dict, level: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = artifact_path(level)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


#: (路径, mtime, size) -> payload; 接口高频读, 文件一天才变一次
_ARTIFACT_CACHE: dict[Path, tuple[float, int, dict | None]] = {}


def read_artifact(level: str = "1d") -> dict:
    """读事件研究产物。不存在返回 ``{"available": False}``, 页面据此提示。"""
    path = artifact_path(level)
    if not path.is_file():
        return {"available": False, "level": level}
    stat = path.stat()
    hit = _ARTIFACT_CACHE.get(path)
    if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"available": False, "level": level}
    if len(_ARTIFACT_CACHE) > 4:
        _ARTIFACT_CACHE.clear()
    _ARTIFACT_CACHE[path] = (stat.st_mtime, stat.st_size, payload)
    return payload


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
