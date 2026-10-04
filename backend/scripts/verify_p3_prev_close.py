"""P3 离线验证: 昨收兜底逻辑 (不需要起服务, 直接 import + mock)。

覆盖「实时链路与历史同步解耦」的核心改动:
  local     — 本地日K有昨收时优先用本地(前复权口径, 最准)
  realtime  — 本地缺失时用实时快照兜底(盘后没同步也能看盘的关键)
  none      — 两者都没有时返回 None, 而不是 0(避免前端把未知当成 0 基准)

运行:
  cd backend && .venv/Scripts/python.exe scripts/verify_p3_prev_close.py
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import patch

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.api.kline import (  # noqa: E402
    _prev_close_with_fallback,
    _realtime_prev_close,
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


class FakeRepo:
    """最小 repo 替身: 只提供 _get_previous_closes 需要的 get_daily_asset。"""

    def __init__(self, rows: list[tuple[date, float]] | None = None):
        self.rows = rows or []
        self.raise_error = False

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None, market="cn"):
        import polars as pl

        if self.raise_error:
            raise RuntimeError("boom")
        return pl.DataFrame(
            {"date": [r[0] for r in self.rows], "close": [r[1] for r in self.rows]},
            schema={"date": pl.Date, "close": pl.Float64},
        )


TODAY = date(2026, 9, 28)
YESTERDAY = date(2026, 9, 25)  # 上一个交易日(26/27 为周末)

print("\n=== P3 昨收兜底 ===")

# 1. 本地日K有昨收 -> local, 且不打实时源
repo = FakeRepo([(date(2026, 9, 24), 10.0), (YESTERDAY, 11.5)])
with patch("app.api.kline._realtime_prev_close", wraps=_realtime_prev_close) as spy:
    values, sources = _prev_close_with_fallback(repo, "000001.SZ", [TODAY], "stock")
ok("本地有昨收 -> 值 11.5", values[TODAY] == 11.5, f"got {values[TODAY]}")
ok("本地有昨收 -> source=local", sources[TODAY] == "local", f"got {sources[TODAY]}")
ok("本地有昨收 -> 不打实时源(零开销)", spy.call_count == 0, f"called {spy.call_count}")

# 2. 本地无数据 -> realtime 兜底
repo = FakeRepo([])
with patch("app.api.kline._realtime_prev_close", return_value=12.3):
    values, sources = _prev_close_with_fallback(repo, "000001.SZ", [TODAY], "stock")
ok("本地无 -> 用实时值 12.3", values[TODAY] == 12.3, f"got {values[TODAY]}")
ok("本地无 -> source=realtime", sources[TODAY] == "realtime", f"got {sources[TODAY]}")

# 3. 本地异常 + 实时也失败 -> none, 值为 None(不是 0)
repo = FakeRepo([])
repo.raise_error = True
with patch("app.api.kline._realtime_prev_close", return_value=None):
    values, sources = _prev_close_with_fallback(repo, "000001.SZ", [TODAY], "stock")
ok("两者都无 -> 值 None", values[TODAY] is None, f"got {values[TODAY]}")
ok("两者都无 -> source=none", sources[TODAY] == "none", f"got {sources[TODAY]}")

# 4. 本地日K全为 None/0/inf -> 视为缺失, 走兜底
import polars as pl  # noqa: E402

repo = FakeRepo()
repo.rows = [(YESTERDAY, None), (date(2026, 9, 24), 0.0)]
with patch("app.api.kline._realtime_prev_close", return_value=9.9):
    values, sources = _prev_close_with_fallback(repo, "000001.SZ", [TODAY], "stock")
ok("本地值无效(None/0) -> 走兜底", values[TODAY] == 9.9, f"got {values[TODAY]}")
ok("本地值无效 -> source=realtime", sources[TODAY] == "realtime", f"got {sources[TODAY]}")

# 5. 多交易日批量: 每个日期独立判定
repo = FakeRepo([(YESTERDAY, 11.5)])
with patch("app.api.kline._realtime_prev_close", return_value=8.8):
    d1, d2 = date(2026, 9, 25), date(2026, 9, 28)
    values, sources = _prev_close_with_fallback(repo, "000001.SZ", [d1, d2], "stock")
ok("批量: 首日无更早数据 -> realtime", sources[d1] == "realtime", f"got {sources[d1]}")
ok("批量: 次日有昨收 -> local", sources[d2] == "local" and values[d2] == 11.5, f"got {sources[d2]}/{values[d2]}")

# 6. _realtime_prev_close 的防御: 脏数据与多返回值
class FakeProvider:
    def __init__(self, rows):
        self.rows = rows

    def get_depth(self, symbols):
        return self.rows


print("\n=== _realtime_prev_close 防御 ===")


def _patch_provider(rows):
    import app.api.kline as k

    class FakeLoader:
        @staticmethod
        def get_provider(name):
            return FakeProvider(rows)

    return patch.object(k, "__dict__", {**k.__dict__})


import app.data_providers.custom.loader as real_loader  # noqa: E402

with patch.object(real_loader, "get_provider", return_value=FakeProvider([{"symbol": "000001.SZ", "prev_close": 20.0}])):
    ok("正常取值", _realtime_prev_close("000001.SZ") == 20.0)

with patch.object(real_loader, "get_provider", return_value=FakeProvider([])):
    ok("空列表 -> None", _realtime_prev_close("000001.SZ") is None)

with patch.object(real_loader, "get_provider", side_effect=RuntimeError("plugin down")):
    ok("provider 抛异常 -> None(不冒泡)", _realtime_prev_close("000001.SZ") is None)

with patch.object(real_loader, "get_provider", return_value=FakeProvider([{"symbol": "600000.SH", "prev_close": 30.0}, {"symbol": "000001.SZ", "prev_close": 21.0}])):
    ok("多返回值 -> 取匹配的 symbol", _realtime_prev_close("000001.SZ") == 21.0)

with patch.object(real_loader, "get_provider", return_value=FakeProvider([{"symbol": "000001.SZ", "prev_close": 0}])):
    ok("脏值 0 -> None", _realtime_prev_close("000001.SZ") is None)

with patch.object(real_loader, "get_provider", return_value=FakeProvider([{"symbol": "000001.SZ", "prev_close": -5}])):
    ok("脏值负数 -> None", _realtime_prev_close("000001.SZ") is None)

with patch.object(real_loader, "get_provider", return_value=FakeProvider([{"symbol": "000001.SZ", "prev_close": "abc"}])):
    ok("脏值非数字 -> None", _realtime_prev_close("000001.SZ") is None)

print(f"\n{'=' * 46}")
print(f"结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
