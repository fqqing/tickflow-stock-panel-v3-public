"""策略池: 把「一组筛选条件」存成命名条目, 之后一键重跑。

一个策略就是一串查询参数 —— 扫描产物本身是离线生成的, 策略不持有任何数据。
所以这里只做三件事: 存/取/跑。

存储用 JSON 而不是 parquet 或 sqlite: 条目数是十位数, 而且用户会想直接打开
看一眼改一下; 引入数据库带来的依赖与锁语义在这里都用不上。

⚠️ 所有入参都过一遍白名单。策略是从前端来的自由 JSON, 直接喂给 polars 表达式
等于把「任意表达式执行」开了个口子 (``sort`` 尤其危险)。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from chanlab.scan_store import BSP_TYPES, SORTABLE, load_frame, resolve_file
from chanlab.scan_store import query as scan_query
from chanlab.scan_store import resonance as resonance_query
from chanlab.strategies import STRATEGIES, clean_params
from chanlab.strategies import scan as strategy_scan

MODE_SINGLE = "single"
MODE_RESONANCE = "resonance"
#: v1 同源策略: 条件是「策略 id + 一组开关」, 在自有日线库上现算 (不需要扫描产物)
MODE_STRATEGY = "strategy"

ROOT = Path(__file__).resolve().parents[2]
POOL_DIR = ROOT / "data" / "pool"
POOL_FILE = POOL_DIR / "strategies.json"

#: 文件版本号. 结构变了就 +1, 读取时按版本决定是否迁移
SCHEMA_VERSION = 1

MODES = (MODE_SINGLE, MODE_RESONANCE, MODE_STRATEGY)
LEVELS = ("1d", "60m", "30m", "15m", "5m", "1m")
ZS_POS = ("", "above", "inside", "below")
DIVERGE = ("", "bottom", "top")
DIRECTION = ("", "up", "down")
MATCHES = ("last", "window")

#: 单级别一侧的可选字段与默认值
SINGLE_KEYS = {
    "level": "1d",
    "lookback": 500,
    "types": [],
    "match": "last",
    "ago_max": 0,
    "zs_pos": "",
    "diverge": "",
    "seg_dir": "",
    "bi_dir": "",
    "sort": "buy_ago",
    "desc": False,
}

#: 共振模式: 主/次级别各一份条件 (字段名带 p_ / s_ 前缀, 与 /api/scan/resonance 一致)
_RESONANCE_PREFIXES = ("p", "s")
_SIDE_KEYS = ("level", "lookback", "types", "ago_max", "zs_pos", "diverge", "seg_dir", "bi_dir")


class PoolError(Exception):
    """策略池的可预期错误 (参数不合法 / 条目不存在)。

    单独一个类型是为了让 server.py 能把它翻译成 4xx 而不是 500 —— 「你填错了」
    和「服务挂了」必须分开报。
    """


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_types(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [x.strip() for x in value.split(",")]
    elif isinstance(value, list | tuple | set):
        items = [str(x).strip() for x in value]
    else:
        raise PoolError(f"types 只能是字符串或数组, 收到 {type(value).__name__}")
    bad = [t for t in items if t and t not in BSP_TYPES]
    if bad:
        raise PoolError(f"未知买卖点类型 {bad}, 可选 {list(BSP_TYPES)}")
    return [t for t in items if t]


def _pick(value: Any, allowed: tuple[str, ...], field: str, default: str) -> str:
    """枚举字段: 空值回落 default, 不在白名单里就报错而不是静默吞掉。"""
    text = "" if value is None else str(value).strip()
    if not text:
        return default
    if text not in allowed:
        raise PoolError(f"{field} 只能是 {'/'.join(x or '(空)' for x in allowed)}, 收到 {text!r}")
    return text


def _pick_level(value: Any, field: str) -> str:
    text = "" if value is None else str(value).strip().lower()
    if not text:
        return "1d"
    if text in ("d", "day"):
        text = "1d"
    if text not in LEVELS:
        raise PoolError(f"{field} 只能是 {'/'.join(LEVELS)}, 收到 {text!r}")
    return text


def _int(value: Any, field: str, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise PoolError(f"{field} 必须是整数, 收到 {value!r}") from exc
    if not low <= n <= high:
        raise PoolError(f"{field} 必须在 {low}~{high} 之间, 收到 {n}")
    return n


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _sort_key(value: Any, default: str, prefixed: bool = False) -> str:
    """排序键白名单。共振模式额外允许 ``s_`` 前缀的次级别列。"""
    text = "" if value is None else str(value).strip()
    if not text:
        return default
    if text in SORTABLE:
        return text
    if prefixed and text.startswith("s_") and text[2:] in SORTABLE:
        return text
    raise PoolError(f"sort 只能是 {'/'.join(SORTABLE)}" + ("(或 s_ 前缀)" if prefixed else ""))


def _clean_side(payload: Any, prefix: str = "") -> dict:
    """清洗一侧的筛选条件。``prefix`` 非空时键名带 ``p_`` / ``s_`` 前缀。"""
    src = payload if isinstance(payload, dict) else {}

    def get(key: str) -> Any:
        return src.get(f"{prefix}_{key}" if prefix else key)

    out: dict[str, Any] = {
        "level": _pick_level(get("level"), f"{prefix}_level" if prefix else "level"),
        "lookback": _int(get("lookback"), "lookback", 500, 50, 20000),
        "types": _clean_types(get("types")),
        "ago_max": _int(get("ago_max"), "ago_max", 0, 0, 500),
        "zs_pos": _pick(get("zs_pos"), ZS_POS, "zs_pos", ""),
        "diverge": _pick(get("diverge"), DIVERGE, "diverge", ""),
        "seg_dir": _pick(get("seg_dir"), DIRECTION, "seg_dir", ""),
        "bi_dir": _pick(get("bi_dir"), DIRECTION, "bi_dir", ""),
    }
    return out


def normalize(payload: dict, *, base: dict | None = None) -> dict:
    """把前端传来的任意 JSON 洗成一个合法策略条目。

    ``base`` 给定时保留它的 id / 时间戳 (更新场景), 否则生成新的。
    刻意做成「缺字段补默认」而不是「缺字段报错」: 前端只会传它关心的那几项。
    """
    if not isinstance(payload, dict):
        raise PoolError("策略内容必须是 JSON 对象")

    mode = _pick(payload.get("mode"), MODES, "mode", "single")
    name = str(payload.get("name") or "").strip()
    if not name:
        name = (base or {}).get("name") or "未命名策略"
    if len(name) > 60:
        raise PoolError("策略名最长 60 个字符")

    item: dict[str, Any] = {
        "id": (base or {}).get("id") or uuid.uuid4().hex[:8],
        "name": name,
        "mode": mode,
        "note": str(payload.get("note") or "")[:500],
        "created_at": (base or {}).get("created_at") or _now(),
        "updated_at": _now(),
    }

    if mode == MODE_STRATEGY:
        spec_id = str((payload.get("strategy") or {}).get("id") or "").strip()
        if spec_id not in STRATEGIES:
            raise PoolError(f"未知策略 {spec_id!r}, 可选 {list(STRATEGIES)}")
        raw = (payload.get("strategy") or {}).get("params")
        item["strategy"] = {
            "id": spec_id,
            "params": clean_params(spec_id, raw if isinstance(raw, dict) else {}),
        }
        item["single"] = {**SINGLE_KEYS}
        item["resonance"] = _default_resonance()
    elif mode == MODE_SINGLE:
        side = _clean_side(payload.get("single"))
        side["match"] = _pick((payload.get("single") or {}).get("match"), MATCHES, "match", "last")
        side["sort"] = _sort_key((payload.get("single") or {}).get("sort"), "buy_ago")
        side["desc"] = _bool((payload.get("single") or {}).get("desc"))
        item["single"] = side
        item["resonance"] = _default_resonance()
    else:
        reso: dict[str, Any] = {}
        for prefix in _RESONANCE_PREFIXES:
            for key, value in _clean_side(payload.get("resonance"), prefix).items():
                reso[f"{prefix}_{key}"] = value
        src = payload.get("resonance") if isinstance(payload.get("resonance"), dict) else {}
        reso["sort"] = _sort_key(src.get("sort"), "s_buy_ago", prefixed=True)
        reso["desc"] = _bool(src.get("desc"))
        item["resonance"] = reso
        item["single"] = {**SINGLE_KEYS}

    lark_src = payload.get("lark") if isinstance(payload.get("lark"), dict) else {}
    base_lark = (base or {}).get("lark") if isinstance((base or {}).get("lark"), dict) else {}
    default_token, default_table = _lark_defaults(item)
    item["lark"] = {
        "base_token": str(lark_src.get("base_token") or base_lark.get("base_token") or default_token).strip(),
        "table_id": str(lark_src.get("table_id") or base_lark.get("table_id") or default_table).strip(),
    }
    return item


def _lark_defaults(item: dict) -> tuple[str, str]:
    """v1 同源策略自带目标表 —— 建策略时直接带上, 省得再去翻飞书后台找 token。

    只认 :data:`chanlab.strategies.STRATEGIES` 里登记的那几个 id; 缠论类策略
    (single/resonance) 没有默认表, 返回空串由用户自己填。
    """
    spec = STRATEGIES.get(str((item.get("strategy") or {}).get("id") or ""))
    if spec is None:
        return "", ""
    return spec.lark_base_token, spec.lark_table_id


def _default_resonance() -> dict:
    out: dict[str, Any] = {}
    for prefix, level in zip(_RESONANCE_PREFIXES, ("1d", "30m"), strict=True):
        for key in _SIDE_KEYS:
            out[f"{prefix}_{key}"] = SINGLE_KEYS[key]
        out[f"{prefix}_level"] = level
    out["sort"] = "s_buy_ago"
    out["desc"] = False
    return out


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------


def load() -> list[dict]:
    """读全部策略。文件坏了返回空列表而不是抛异常: 池子空了用户重建就是了,
    但整个页面打不开是事故。"""
    if not POOL_FILE.is_file():
        return []
    try:
        data = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    # 兼容早期 {"items": [...]} 形态
    items = data.get("items") or [] if isinstance(data, dict) else data
    return [x for x in items if isinstance(x, dict) and x.get("id")]


def save(items: list[dict]) -> None:
    """原子写盘: 先写临时文件再 replace, 避免写一半崩掉留下半个 JSON。"""
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": SCHEMA_VERSION, "updated_at": _now(), "items": items},
        ensure_ascii=False,
        indent=2,
    )
    fd, tmp = tempfile.mkstemp(dir=POOL_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, POOL_FILE)
    finally:
        # replace 成功后 tmp 已经不存在, 这里只是异常路径的兜底
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def list_items() -> list[dict]:
    return load()


def get(item_id: str) -> dict | None:
    return next((x for x in load() if x.get("id") == item_id), None)


def create(payload: dict) -> dict:
    items = load()
    item = normalize(payload)
    items.append(item)
    save(items)
    return item


def update(item_id: str, payload: dict) -> dict:
    items = load()
    idx = next((i for i, x in enumerate(items) if x.get("id") == item_id), -1)
    if idx < 0:
        raise PoolError(f"策略不存在: {item_id}")
    merged = {**items[idx], **{k: v for k, v in payload.items() if k not in ("id",)}}
    items[idx] = normalize(merged, base=items[idx])
    save(items)
    return items[idx]


def delete(item_id: str) -> bool:
    items = load()
    left = [x for x in items if x.get("id") != item_id]
    if len(left) == len(items):
        return False
    save(left)
    return True


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def run(item: dict, *, limit: int = 100, offset: int = 0) -> dict:
    """按策略跑一次筛选, 返回命中数与当前页。

    与 /api/scan 同源: 走的是同一个 :func:`scan_store.query`, 不存在「策略池里
    跑出来和选股页不一样」的口径错配。
    """
    started = time.perf_counter()
    mode = item.get("mode", MODE_SINGLE)

    if mode == MODE_STRATEGY:
        return _run_strategy(item, limit=limit, offset=offset, started=started)

    if mode == MODE_SINGLE:
        side = item.get("single") or {}
        path, matched = resolve_file(level=side.get("level", "1d"), lookback=side.get("lookback", 500))
        if path is None:
            raise PoolError("没有找到扫描结果, 先跑 backend/scripts/scan_market.py")
        total, rows = scan_query(
            load_frame(path),
            types=side.get("types") or None,
            match=side.get("match", "last"),
            ago_max=side.get("ago_max") or None,
            zs_pos=side.get("zs_pos", ""),
            diverge=side.get("diverge", ""),
            seg_dir=side.get("seg_dir", ""),
            bi_dir=side.get("bi_dir", ""),
            sort=side.get("sort", "buy_ago"),
            desc=bool(side.get("desc")),
            limit=limit,
            offset=offset,
        )
        meta = _meta(path, side.get("level", "1d"), matched)
    else:
        reso = item.get("resonance") or {}
        p_path, p_matched = resolve_file(
            level=reso.get("p_level", "1d"), lookback=reso.get("p_lookback", 500)
        )
        s_path, s_matched = resolve_file(
            level=reso.get("s_level", "30m"), lookback=reso.get("s_lookback", 500)
        )
        if p_path is None or s_path is None:
            missing = [n for n, p in (("大级别", p_path), ("小级别", s_path)) if p is None]
            raise PoolError(f"{'/'.join(missing)}扫描结果不存在, 先跑 scan_market.py")
        total, rows = resonance_query(
            load_frame(p_path),
            load_frame(s_path),
            primary_filters=_side(reso, "p"),
            secondary_filters=_side(reso, "s"),
            sort=reso.get("sort", "s_buy_ago"),
            desc=bool(reso.get("desc")),
            limit=limit,
            offset=offset,
        )
        meta = {
            "primary": _meta(p_path, reso.get("p_level", "1d"), p_matched),
            "secondary": _meta(s_path, reso.get("s_level", "30m"), s_matched),
        }

    return {
        "mode": mode,
        "total": total,
        "rows": rows,
        "meta": meta,
        "cost_sec": round(time.perf_counter() - started, 3),
    }


def _run_strategy(item: dict, *, limit: int, offset: int, started: float) -> dict:
    """v1 同源策略: 直接在日线库上现算。与 :func:`chanlab.strategies.scan` 同源。

    不走 ``scan_store`` 是因为这些策略看的不是缠论买卖点 —— 它们是 MACD/均线/
    量价判链, 产物里没有对应的列。
    """
    spec = item.get("strategy") or {}
    try:
        result = strategy_scan(
            str(spec.get("id") or ""),
            spec.get("params") or {},
            limit=limit,
            offset=offset,
        )
    except KeyError as exc:
        raise PoolError(str(exc).strip("'\"")) from exc
    except RuntimeError as exc:  # 日线库为空这类环境级问题, 不该报成 500
        raise PoolError(str(exc)) from exc
    return {
        "mode": MODE_STRATEGY,
        "total": result["total"],
        "rows": result["rows"],
        "meta": {"strategy": result["strategy"], "strategy_name": result["strategy_name"]},
        "signal_date": result["signal_date"],
        "cost_sec": round(time.perf_counter() - started, 3),
    }


def _side(reso: dict, prefix: str) -> dict:
    """共振的一侧 -> :func:`scan_store.apply_filters` 入参 (无前缀键名)。"""
    return {
        "types": reso.get(f"{prefix}_types") or None,
        "ago_max": reso.get(f"{prefix}_ago_max") or None,
        "zs_pos": reso.get(f"{prefix}_zs_pos", ""),
        "diverge": reso.get(f"{prefix}_diverge", ""),
        "seg_dir": reso.get(f"{prefix}_seg_dir", ""),
        "bi_dir": reso.get(f"{prefix}_bi_dir", ""),
    }


def _meta(path: Path, level: str, matched: bool) -> dict:
    """复用 scan_store 的元信息, 顺带把数据截止日带出去 (推送时要当信号日期)。"""
    from chanlab.scan_store import scan_meta

    return scan_meta(path, wanted_level=level, matched=matched)


def signal_date(result: dict) -> str:
    """从 :func:`run` 的结果里取「信号日期」: 扫描产物最后一根 K 线的日期。

    不用 ``generated_at``: 流水线可以在没有新数据的日子重跑, 时间戳很新但内容
    是上周的, 拿它当信号日期会把同一批结果当成不同的信号重复推进飞书 —— 而飞书
    侧的去重键正是 (代码, 信号日期), 用错日期等于去重失效。
    """
    if declared := str(result.get("signal_date") or "").strip():
        return declared[:10]
    meta = result["meta"]
    if result["mode"] == "single":
        return str(meta.get("data_last_date") or "")[:10]
    return str(meta.get("secondary", {}).get("data_last_date") or "")[:10]
