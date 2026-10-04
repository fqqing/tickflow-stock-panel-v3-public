"""P1 离线验证: 除权因子 affected 精确化 (enriched 伪增量的根因修复)。

核心诉求: 「不能把真变化判成无变化」(否则 enriched 漏更新, 是正确性事故),
同时「不能把所有拉回来的都算变化」(否则 enriched 全量重算, 430s 的元凶)。

运行:
  cd backend && .venv/Scripts/python.exe scripts/verify_p1_adj_diff.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.kline_sync import (  # noqa: E402
    _EX_FACTOR_EPS,
    _diff_affected_symbols,
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


def df(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": [r[0] for r in rows], "trade_date": [r[1] for r in rows], "ex_factor": [r[2] for r in rows]},
        schema={"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64},
    )


D1, D2, D3 = date(2024, 6, 14), date(2025, 6, 14), date(2026, 6, 14)

print("\n=== P1 除权因子 diff (正确性优先) ===")

# --- 正确性: 真变化必须被识别 ---
base = df([("A.SZ", D1, 1.5), ("B.SZ", D1, 2.0)])

# 1. 完全相同 -> 零受影响(这正是原来 4623 只的场景)
out = _diff_affected_symbols(base, base)
ok("数据完全没变 -> affected 为空", out == [], f"got {out}")

# 2. 新增一行 -> 该 symbol 受影响
new = df([("A.SZ", D1, 1.5), ("B.SZ", D1, 2.0), ("C.SZ", D1, 1.1)])
out = _diff_affected_symbols(base, new)
ok("新增标的 -> 只它受影响", sorted(out) == ["C.SZ"], f"got {out}")

# 3. 新增某只的新日期 -> 该 symbol
new = df([("A.SZ", D1, 1.5), ("B.SZ", D1, 2.0), ("A.SZ", D2, 1.9)])
out = _diff_affected_symbols(base, new)
ok("已有标的新增除权日 -> 它受影响", sorted(out) == ["A.SZ"], f"got {out}")

# 4. 值真的变了 -> 必须识别(漏判 = enriched 不更新, 是事故)
new = df([("A.SZ", D1, 1.8), ("B.SZ", D1, 2.0)])
out = _diff_affected_symbols(base, new)
ok("★ 因子值变更 -> 必须识别", sorted(out) == ["A.SZ"], f"got {out}")

# 5. 大幅变更
new = df([("A.SZ", D1, 1.5), ("B.SZ", D1, 99.0)])
out = _diff_affected_symbols(base, new)
ok("因子大幅变更 -> 识别", sorted(out) == ["B.SZ"], f"got {out}")

# 6. 浮点噪声 -> 不算变化(否则每次都全市场)
eps_noise = _EX_FACTOR_EPS / 10
new = df([("A.SZ", D1, 1.5 + eps_noise), ("B.SZ", D1, 2.0 - eps_noise)])
out = _diff_affected_symbols(base, new)
ok("容差内浮点噪声 -> 不算变化", out == [], f"got {out}")

# 7. 超过容差的最小变化 -> 要识别
over = _EX_FACTOR_EPS * 10
new = df([("A.SZ", D1, 1.5 + over), ("B.SZ", D1, 2.0)])
out = _diff_affected_symbols(base, new)
ok("超容差变化 -> 识别", sorted(out) == ["A.SZ"], f"got {out}")

# 8. 行消失(de-dup keep=last 语义) -> 不崩且只报变化
short = df([("A.SZ", D1, 1.5)])
out = _diff_affected_symbols(base, short)
ok("行数减少 -> 不崩, 无假阳性", out == [], f"got {out}")

# 9. existing 为空 -> 全部算受影响(首次写入语义)
out = _diff_affected_symbols(base.clear(), new)
ok("首次(existing 空) -> 全量受影响", set(out) == {"A.SZ", "B.SZ"}, f"got {out}")

# 10. 缺 ex_factor 列 -> 安全退化, 不抛异常
weird = pl.DataFrame({"symbol": ["A.SZ"], "trade_date": [D1]}, schema={"symbol": pl.Utf8, "trade_date": pl.Date})
try:
    out = _diff_affected_symbols(weird, weird)
    ok("缺列 -> 安全退化不抛异常", isinstance(out, list), f"got {out}")
except Exception as e:
    ok("缺列 -> 安全退化不抛异常", False, f"raised {e}")

print("\n=== 真实数据对拍 (本机 data/adj_factor) ===")
real = _BACKEND.parent / "data" / "adj_factor" / "all.parquet"
if real.exists():
    existing = pl.read_parquet(real)
    total_syms = existing["symbol"].n_unique()
    # 场景: 数据源又把全部除权因子拉了回来, 内容与本地完全一致
    out = _diff_affected_symbols(existing, existing)
    print(f"  本地行数        : {existing.height:,}")
    print(f"  本地标的数      : {total_syms:,}")
    print(f"  「无任何变化」次数的 affected: {len(out)} 只")
    print(f"  旧行为(unique)  : {total_syms} 只")
    ok(
        "★ 无变化时真实数据 affected=0 (旧行为是全市场)",
        len(out) == 0,
        f"got {len(out)}",
    )

    # 场景: 模拟某只真发生了除权
    import random

    rng = random.Random(42)
    victim = rng.choice(existing["symbol"].unique().to_list())
    maxrow = existing.filter(pl.col("symbol") == victim).sort("trade_date").tail(1)
    bumped = maxrow.with_columns((pl.col("ex_factor") * 1.05).alias("ex_factor"))
    merged2 = pl.concat([existing.filter(pl.col("symbol") != victim), bumped]).sort(["symbol", "trade_date"])
    out2 = _diff_affected_symbols(existing, merged2)
    ok(
        "★ 真实数据里某只真变化 -> 精确命中该只",
        out2 == [victim],
        f"expected [{victim}], got {out2}",
    )
    print(f"  模拟变更标的    : {victim} -> affected={out2}")
else:
    print(f"  (跳过: {real} 不存在)")

print(f"\n{'=' * 46}")
print(f"结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
