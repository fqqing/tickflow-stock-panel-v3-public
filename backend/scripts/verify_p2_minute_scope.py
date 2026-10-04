"""P2 离线验证: 分钟同步分层 (focus 模式)。

不依赖 TickFlow key, 用本地 enriched 真实数据算 focus 规模, 并覆盖各级降级逻辑。

运行:
  cd backend && .venv/Scripts/python.exe scripts/verify_p2_minute_scope.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import duckdb  # noqa: E402

from app.jobs.daily_pipeline import (  # noqa: E402
    _CN_SYMBOL_SUFFIXES,
    _MINUTE_FOCUS_TOP_N,
    apply_minute_scope,
)

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


DATA_DIR = _BACKEND.parent / "data"


class FakeStore:
    data_dir = DATA_DIR


class FakeRepo:
    """最小 repo 替身: 提供 _top_active_symbols 需要的 store.data_dir 与 db。"""

    store = FakeStore()
    db = duckdb.connect()


UNIVERSE = [
    "600519.SH", "000001.SZ", "300750.SZ", "603986.SH",
    "300308.SZ", "300502.SZ", "600176.SH", "002384.SZ",
    "830799.BJ", "999999.SZ",
]
CN_ONLY = [s for s in UNIVERSE if s.upper().endswith(_CN_SYMBOL_SUFFIXES)]

print("\n=== P2 分钟同步分层 ===")

# 1. scope=all -> 原样返回
with patch("app.jobs.daily_pipeline._prefs") as prefs:
    prefs.get_minute_sync_scope.return_value = "all"
    out = apply_minute_scope(list(UNIVERSE), FakeRepo())
ok("scope=all -> 原样返回(不改行为)", out == UNIVERSE, f"got {len(out)}")

# 2. 非法 scope -> 当 all 处理
with patch("app.jobs.daily_pipeline._prefs") as prefs:
    prefs.get_minute_sync_scope.return_value = "FOCUS!!"
    out = apply_minute_scope(list(UNIVERSE), FakeRepo())
ok("非法 scope -> 退回全量(不冒险)", out == UNIVERSE, f"got {len(out)}")

# 3. focus + 本地成交额榜(真实数据, TickFlow 池不可用)
with patch("app.jobs.daily_pipeline._prefs") as prefs:
    prefs.get_minute_sync_scope.return_value = "focus"
    with patch("app.jobs.daily_pipeline.get_pool", side_effect=RuntimeError("no tickflow key")):
        out = apply_minute_scope(list(UNIVERSE), FakeRepo())
local_focus = out
ok(
    "focus + 池全挂 -> 本地成交额榜仍生效",
    0 < len(out) < len(UNIVERSE),
    f"got {len(out)} / {len(UNIVERSE)}",
)
ok("focus 结果全部在 A 股 universe 内", set(out) <= set(UNIVERSE), f"extra={set(out) - set(UNIVERSE)}")

# 4. 保险1: 成交额榜与 TickFlow 池都拿不到 -> 退回原样
with patch("app.jobs.daily_pipeline._prefs") as prefs:
    prefs.get_minute_sync_scope.return_value = "focus"
    with patch("app.jobs.daily_pipeline.get_pool", return_value=[]):
        with patch("app.jobs.daily_pipeline._top_active_symbols", return_value=set()):
            out = apply_minute_scope(list(UNIVERSE), FakeRepo())
ok("保险1: focus 池为空 -> 退回全量", out == UNIVERSE, f"got {len(out)}")

# 5. 保险2: focus 池与 universe 无交集 -> 退回原样
with patch("app.jobs.daily_pipeline._prefs") as prefs:
    prefs.get_minute_sync_scope.return_value = "focus"
    with patch("app.jobs.daily_pipeline.get_pool", return_value=["000000.XX"]):
        with patch("app.jobs.daily_pipeline._top_active_symbols", return_value={"111111.YY"}):
            out = apply_minute_scope(list(UNIVERSE), FakeRepo())
ok("保险2: 无交集 -> 退回全量", out == UNIVERSE, f"got {len(out)}")

# 6. repo=None 时不崩(成交额榜跳过)
with patch("app.jobs.daily_pipeline._prefs") as prefs:
    prefs.get_minute_sync_scope.return_value = "focus"
    with patch("app.jobs.daily_pipeline.get_pool", return_value=["000001.SZ"]):
        out = apply_minute_scope(list(UNIVERSE), None)
ok("repo=None -> 只用 tickflow 池, 不崩", "000001.SZ" in out, f"got {out}")

print("\n=== 真实规模 (本机 data/) ===")
try:
    only_cn = apply_minute_scope([], FakeRepo())  # 空列表快速探路
except Exception:
    only_cn = []

pattern = f"{DATA_DIR.as_posix()}/kline_daily_enriched/**/*.parquet"
try:
    con = duckdb.connect()
    total = con.execute(
        f"SELECT count(*) FROM read_parquet('{pattern}')"
    ).fetchone()[0]
    latest = con.execute(f"SELECT max(date) FROM read_parquet('{pattern}')").fetchone()[0]
    top = con.execute(
        f"SELECT count(*) FROM (SELECT symbol FROM read_parquet('{pattern}') "
        f"WHERE date = DATE '{latest}' AND amount > 0 ORDER BY amount DESC LIMIT {_MINUTE_FOCUS_TOP_N})"
    ).fetchone()[0]
    # 全市场 A 股数(与 P0 口径一致)
    inst = f"{DATA_DIR.as_posix()}/instruments/instruments.parquet"
    all_syms = [r[0] for r in con.execute(f"SELECT symbol FROM read_parquet('{inst}')").fetchall()]
    cn_syms = [s for s in all_syms if str(s).upper().endswith(_CN_SYMBOL_SUFFIXES)]

    print(f"  enriched 总行数     : {total:,}")
    print(f"  enriched 最新交易日 : {latest}")
    print(f"  成交额 TOP{_MINUTE_FOCUS_TOP_N}      : {top:,} 只")
    print(f"  A 股全量(P0 后)     : {len(cn_syms):,} 只")

    # 用真实 A 股 universe 跑一次 focus
    with patch("app.jobs.daily_pipeline._prefs") as prefs:
        prefs.get_minute_sync_scope.return_value = "focus"
        with patch("app.jobs.daily_pipeline.get_pool", side_effect=RuntimeError("no key")):
            real_focus = apply_minute_scope(cn_syms, FakeRepo())
    ratio = len(cn_syms) / max(len(real_focus), 1)
    print(f"  focus 实际标的数    : {len(real_focus):,} 只")
    print(f"  相比全量缩减        : {ratio:.2f}x")

    ok("focus 规模明显小于全量", len(real_focus) < len(cn_syms) * 0.4, f"{len(real_focus)} vs {len(cn_syms)}")
    ok("focus 规模非空", len(real_focus) > 500, f"got {len(real_focus)}")
    ok("focus 结果均为 A 股", all(s.upper().endswith(_CN_SYMBOL_SUFFIXES) for s in real_focus))
except Exception as e:
    print(f"  (跳过真实规模校验: {e})")

print(f"\n{'=' * 46}")
print(f"结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
