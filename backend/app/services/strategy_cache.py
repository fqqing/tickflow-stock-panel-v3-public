"""策略结果缓存 — 写入本地文件, 供策略页面秒加载。

缓存结构 (多日期槽): 每个交易日期一个独立槽位, 切回已算过的日期直接命中,
不再把上一个日期的结果覆盖掉。实测日志里 23 次写入只对应 4 个日期, 单槽时
其中 19 次是完全重复的全量重算。

  {
    "as_of": "2026-09-17",       // 最近写入的日期 (标量, 供读侧取「最新」)
    "updated_at": 1705324800000, // Unix ms
    "by_date": {
      "2026-09-17": {
        "results": { strategy_id: { total, as_of, rows } },
        "today_ever_matched": { strategy_id: [symbol, ...] },     // 该日曾命中并集
        "today_ever_rows": { strategy_id: { symbol: row_data } }, // 该日曾命中行数据
        "enriched_mtime": 1705324800.0,
        "updated_at": 1705324800000
      },
      "2026-09-16": { ... }
    }
  }

旧版单槽格式 (顶层直接放 results / today_ever_rows) 仍可读: 读到即迁移, 下次写入
就落成新版。read_cache 不论新旧都把结果展开成上面 slot 的平铺形态返回, 调用方
无需感知格式差异。

文件路径: data/user_data/strategy_cache.json
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _json_default(obj: Any) -> Any:
    """处理 date/datetime 等 JSON 不认识的类型。"""
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


logger = logging.getLogger(__name__)

_CACHE_FILENAME = "strategy_cache.json"

# 保留的日期槽上限。实测单个日期约 0.56MB (5 个策略 x 132 行, 同日重跑的
# 「曾命中」并集几乎没有增长 —— 日志里 23 次写入的命中数与曾命中数基本相等,
# 最大 179 行), 10 个槽约合两周交易日、不到 10MB。超出后淘汰最久未写入的日期。
_MAX_CACHE_DATES = 10


def _normalize_slots(raw: dict | None) -> dict[str, dict]:
    """把磁盘内容统一成 {as_of: slot} 形态, 兼容旧版单槽格式。

    旧文件没有 by_date 键, 顶层就是单个日期 —— 直接把它包装成一个槽,
    下一次 write_cache 就会落成新版结构, 无需单独的迁移步骤。
    """
    if not raw:
        return {}
    by_date = raw.get("by_date")
    if isinstance(by_date, dict):
        slots = {
            str(key): value
            for key, value in by_date.items()
            if isinstance(value, dict) and isinstance(value.get("results"), dict)
        }
        if slots:
            return slots
    as_of = raw.get("as_of")
    if isinstance(as_of, str) and isinstance(raw.get("results"), dict):
        return {
            as_of: {
                "results": raw.get("results") or {},
                "today_ever_matched": raw.get("today_ever_matched") or {},
                "today_ever_rows": raw.get("today_ever_rows") or {},
                "enriched_mtime": raw.get("enriched_mtime"),
                "updated_at": raw.get("updated_at"),
            }
        }
    return {}


def _slot_view(slot: dict, as_of: str) -> dict:
    """把单个日期槽展开成旧版平铺结构, 供既有端点原样消费。"""
    return {
        "as_of": as_of,
        "results": slot.get("results") or {},
        "today_ever_matched": slot.get("today_ever_matched") or {},
        "today_ever_rows": slot.get("today_ever_rows") or {},
        "enriched_mtime": slot.get("enriched_mtime"),
        "updated_at": slot.get("updated_at"),
    }


def _latest_as_of(raw: dict | None, slots: dict[str, dict]) -> str | None:
    """取最近写入的日期: 优先用顶层 as_of, 回退到 updated_at 最大的槽。"""
    top = raw.get("as_of") if raw else None
    if isinstance(top, str) and top in slots:
        return top
    if not slots:
        return None
    # updated_at 同毫秒时用日期串兜底, 保证结果确定 (字典序即时间序)
    return max(slots, key=lambda key: (slots[key].get("updated_at") or 0, key))


# 读写同一 JSON 文件的进程内锁: write_cache 的 read-modify-write 与并发 read_cache
# 无锁会丢更新/读到半写文件。read_cache 与 write_cache 共用此锁; write 内部复用
# _read_cache_unlocked 避免自死锁。写入用临时文件 + os.replace 做到原子替换。
#
# 注: 该锁按模块全局共享, 不按市场细分。不同市场写不同文件, 理论上可并行, 但
# 策略缓存写入是低频操作 (盘后批量/单策略重跑), 细分锁带来的收益不足以抵偿
# 死锁风险, 故保持单锁。
_file_lock = threading.Lock()


def _cache_path(data_dir: Path, market: str = "cn") -> Path:
    """策略缓存文件路径, 按市场隔离。

    A 股沿用原文件名 strategy_cache.json —— 既向后兼容已存在的用户缓存,
    也避免升级后 A 股策略页首屏突然读不到数据。港美股各自独立成文件。

    此前所有市场共用一个文件: 港股跑完 run_all 会覆盖 A 股结果, 切回 A 股
    看到的是港股命中数, 属实际的数据正确性问题。
    """
    if market and market != "cn":
        return data_dir / "user_data" / f"strategy_cache_{market}.json"
    return data_dir / "user_data" / _CACHE_FILENAME


def _enriched_parquet_path(data_dir: Path, as_of: str, market: str = "cn") -> Path:
    """返回 enriched parquet 文件路径。"""
    from app.tickflow.repository import enriched_dirname
    return data_dir / enriched_dirname("stock", market) / f"date={as_of}" / "part.parquet"


def _get_enriched_mtime(data_dir: Path, as_of: str, market: str = "cn") -> float | None:
    """返回 enriched parquet 文件的 mtime (秒)。文件不存在返回 None。"""
    p = _enriched_parquet_path(data_dir, as_of, market)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def read_cache(data_dir: Path, market: str = "cn", as_of: str | None = None) -> dict | None:
    """读取策略缓存。返回 None 表示该日期无缓存或读取失败。

    as_of 为 None 时取最近写入的日期 (等价于旧版单槽行为); 指定日期时只在该日期
    的槽存在才返回 —— 前端据此判断要不要重跑, 因此「没算过」必须老实返回 None。

    说明: 原先有 enriched mtime 过期校验 (数据文件变化 → 判过期返回 None),
    但在有实时行情的系统里, enriched parquet 每轮被刷新 → mtime 必然变化 →
    缓存被永久判死, 策略页读不到数据。且判过期后不触发重算, 只能让用户手动重跑,
    保护价值有限。故移除: 盘后缓存总能读出, 实时新鲜度由 /api/screener/cached
    端点叠加监控引擎的内存实时结果 (latest_strategy_results) 来保证。
    """
    with _file_lock:
        raw = _read_cache_unlocked(data_dir, market)
        slots = _normalize_slots(raw)
        target = as_of or _latest_as_of(raw, slots)
        if target is None or target not in slots:
            return None
        return _slot_view(slots[target], target)


def clear_cache(data_dir: Path, market: str | None = None) -> None:
    """删除策略结果缓存；策略代码 reload 后避免继续展示旧公式结果。

    market=None 时清空全部市场: 策略代码/公式变了, 所有市场的既有结果都失效,
    只清 A 股会让港美股继续展示旧公式算出的结果。
    """
    from app.markets import ALL_MARKETS
    targets = ALL_MARKETS if market is None else [market]
    with _file_lock:
        for m in targets:
            path = _cache_path(data_dir, m)
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)



def _read_cache_unlocked(data_dir: Path, market: str = "cn") -> dict | None:
    """实际读取逻辑 (不持锁)。供 read_cache 与 write_cache 复用, 避免重入死锁。"""
    path = _cache_path(data_dir, market)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        cached = json.loads(text)
    except Exception as e:
        logger.warning("读取策略缓存失败: %s", e)
        return None

    return cached


def _rows_to_symbol_map(rows: list[dict]) -> dict[str, dict]:
    """将 rows 列表转为 {symbol: row_data} 映射。"""
    result: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            result[sym] = row
    return result


def write_cache(
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
    market: str = "cn",
) -> None:
    """将指定日期的策略结果写入缓存文件, 同时更新该日曾命中集合。

    - 每个交易日期独立成槽: 写新日期不会覆盖其他日期的结果
    - 同一日期内合并 (并集) 之前曾命中的 symbol, 并用最新行数据更新
    - 按 market 写入独立文件, 不同市场互不覆盖
    """
    path = _cache_path(data_dir, market)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 整个 read-modify-write 持锁: 避免并发 write 丢更新, 也避免与 read_cache 撕裂
    with _file_lock:
        _write_cache_locked(path, data_dir, as_of, results, market)


def _write_cache_locked(
    path: Path,
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
    market: str = "cn",
) -> None:
    """持 _file_lock 后的实际写入逻辑 (read-merge-write + 原子替换)。

    只更新 as_of 这一个槽位, 其他日期的槽原样保留 —— 用户切回旧日期时直接命中。
    """
    # 读取旧缓存 (已持锁, 走不重入的 _read_cache_unlocked)
    raw = _read_cache_unlocked(data_dir, market)
    slots = _normalize_slots(raw)
    old_slot = slots.get(as_of) or {}
    old_ever_rows: dict[str, dict[str, dict]] = old_slot.get("today_ever_rows") or {}

    # 同一日期内多次运行: 合并 (并集) 之前算出的结果, 保留本轮未重跑的策略。
    # 不同日期各自独立成槽, 不再互相覆盖。
    merged_results = {**(old_slot.get("results") or {}), **results}

    # 当前命中的行数据 → symbol 映射
    current_row_maps: dict[str, dict[str, dict]] = {}
    for sid, r in results.items():
        current_row_maps[sid] = _rows_to_symbol_map(r.get("rows", []))

    if old_ever_rows:
        # 同一天内重跑: 合并 — 用当前行数据更新旧数据 (保持最新价格等)
        merged_rows: dict[str, dict[str, dict]] = {}
        all_keys = set(old_ever_rows.keys()) | set(current_row_maps.keys())
        for sid in all_keys:
            # 以旧数据为基础, 用当前数据覆盖 (当前数据更新鲜)
            merged_rows[sid] = {**(old_ever_rows.get(sid) or {}), **(current_row_maps.get(sid) or {})}
        today_ever_rows = merged_rows
    else:
        # 该日期首次写入
        today_ever_rows = current_row_maps

    # 从 ever_rows 提取 symbol 列表 (用于快速计数)
    today_ever_matched = {sid: sorted(maps.keys()) for sid, maps in today_ever_rows.items()}

    # enriched_mtime: 盘后缓存写入时记录 (向后兼容旧字段)。read_cache 已不再用它
    # 做过期校验, 实时新鲜度改由 /cached 端点叠加监控引擎内存结果保证。
    enriched_mtime = _get_enriched_mtime(data_dir, as_of, market)
    now_ms = int(time.time() * 1000)

    slots[as_of] = {
        "results": merged_results,
        "today_ever_matched": today_ever_matched,
        "today_ever_rows": today_ever_rows,
        "enriched_mtime": enriched_mtime,
        "updated_at": now_ms,
    }
    # 超出上限淘汰最久未写入的日期 (按各槽 updated_at 降序保留最新 N 个)。
    # updated_at 同毫秒时用日期串兜底排序, 保证淘汰结果确定 (日期串字典序即时间序)。
    if len(slots) > _MAX_CACHE_DATES:
        keep = sorted(
            slots,
            key=lambda key: (slots[key].get("updated_at") or 0, key),
            reverse=True,
        )[:_MAX_CACHE_DATES]
        slots = {key: slots[key] for key in keep}

    payload = {
        "as_of": as_of,
        "by_date": slots,
        "enriched_mtime": enriched_mtime,
        "updated_at": now_ms,
    }
    try:
        # 原子写: 先写临时文件再 os.replace, 避免读侧读到半写的 JSON
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
        os.replace(tmp, path)
        total_rows = sum(len(r.get("rows", [])) for r in merged_results.values())
        total_ever = sum(len(v) for v in today_ever_matched.values())
        logger.info(
            "策略缓存已写入: %s, %d 策略, %d 命中, %d 曾命中, 共 %d 个日期槽",
            as_of, len(merged_results), total_rows, total_ever, len(slots),
        )
    except Exception as e:
        logger.warning("写入策略缓存失败: %s", e)
