"""K 线叠加层的两个外部事件源: 监控触发 / 回测买卖点。

这两个端点是「K 线图上标出外部事件」的数据通道, 测试的重点不是字段齐全,
而是三个容易静默出错的语义:
  1. 触发记录按**北京时间**归日 (ts 是 UTC epoch, 直接取 UTC date 会错一天)
  2. 同一天多次触发合并成一个点 (否则密集触发会叠出一堆重复标记)
  3. 回测交易记录是**覆盖写** (只保留最近一次, 避免"图上这些点来自哪次回测"无法回答)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app.api import alerts, backtest
from app.market_time import CN_TZ


def _alert_request(data_dir: Path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
            )
        )
    )


def _write_alerts(data_dir: Path, events: list[dict]) -> None:
    p = data_dir / "user_data" / "alerts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )


def _by_symbol(data_dir: Path, symbol: str, **overrides):
    """直调端点: FastAPI 的 Query 默认值只在 HTTP 入口生效, 直调时必须显式给全。"""
    kwargs = {"days": 7, "limit": 200}
    kwargs.update(overrides)
    return alerts.alerts_by_symbol(_alert_request(data_dir), symbol, **kwargs)


def _trades(symbol: str, **overrides):
    kwargs = {"limit": 200}
    kwargs.update(overrides)
    return backtest.last_trades_by_symbol(symbol, **kwargs)


def _ts(day: str, hh: int, mm: int) -> int:
    """北京时间某时刻对应的 epoch 毫秒。"""
    d = datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(tzinfo=CN_TZ)
    return int(d.timestamp() * 1000)


def _ev(symbol: str, ts: int, *, severity="info", rule_name="规则A", signals=None):
    return {
        "ts": ts, "symbol": symbol, "severity": severity,
        "rule_name": rule_name, "message": "msg", "signals": signals or [],
    }


# ══════════════════════════════════════════════════════════════
# 监控触发 (GET /api/alerts/by-symbol)
# ══════════════════════════════════════════════════════════════

def test_alerts_by_symbol_groups_by_beijing_date(tmp_path: Path):
    """UTC 23:30 (北京次日 07:30) 必须归到北京那一天, 否则整点错位一天。"""
    # 北京时间 2026-09-30 07:30 = UTC 2026-09-29 23:30
    _write_alerts(tmp_path, [_ev("600519.SH", _ts("2026-09-30", 7, 30))])
    resp = _by_symbol(tmp_path, "600519.SH", days=30)
    assert [p["date"] for p in resp["points"]] == ["2026-09-30"]


def test_alerts_by_symbol_merges_same_day(tmp_path: Path):
    """同一天多次触发合并成一个点, 标签去重、severity 取最高。"""
    _write_alerts(tmp_path, [
        _ev("600519.SH", _ts("2026-09-30", 10, 0), severity="info", rule_name="规则A"),
        _ev("600519.SH", _ts("2026-09-30", 13, 30), severity="critical", rule_name="规则B"),
        _ev("600519.SH", _ts("2026-09-30", 14, 0), severity="warn", rule_name="规则A"),
    ])
    resp = _by_symbol(tmp_path, "600519.SH", days=30)
    assert len(resp["points"]) == 1
    p = resp["points"][0]
    assert p["count"] == 3
    assert p["severity"] == "critical"
    assert p["labels"] == ["规则A", "规则B"]


def test_alerts_by_symbol_filters_other_symbols(tmp_path: Path):
    _write_alerts(tmp_path, [
        _ev("600519.SH", _ts("2026-09-30", 10, 0)),
        _ev("000001.SZ", _ts("2026-09-30", 10, 0)),
    ])
    resp = _by_symbol(tmp_path, "600519.SH", days=30)
    assert len(resp["points"]) == 1
    assert resp["points"][0]["count"] == 1


def test_alerts_by_symbol_ascending_and_limit(tmp_path: Path):
    _write_alerts(tmp_path, [
        _ev("600519.SH", _ts("2026-09-29", 10, 0)),
        _ev("600519.SH", _ts("2026-09-30", 10, 0)),
        _ev("600519.SH", _ts("2026-10-01", 10, 0)),
    ])
    resp = _by_symbol(tmp_path, "600519.SH", days=30, limit=2)
    assert [p["date"] for p in resp["points"]] == ["2026-09-30", "2026-10-01"]


def test_alerts_by_symbol_empty_file(tmp_path: Path):
    resp = _by_symbol(tmp_path, "600519.SH")
    assert resp == {"symbol": "600519.SH", "days": 7, "points": []}


def test_alerts_by_symbol_skips_missing_ts(tmp_path: Path):
    """没有 ts 的历史记录不该归到 1970 年。"""
    _write_alerts(tmp_path, [{"symbol": "600519.SH", "message": "m"}])
    resp = _by_symbol(tmp_path, "600519.SH", days=30)
    assert resp["points"] == []


# ══════════════════════════════════════════════════════════════
# 回测买卖点 (落盘 + GET /api/backtest/trades/by-symbol)
# ══════════════════════════════════════════════════════════════

def test_persist_last_trades_writes_slim_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    backtest._persist_last_trades(
        {
            "trades": [{
                "symbol": "600519.SH", "entry_date": "2026-01-05", "exit_date": "2026-02-10",
                "entry_price": 1500.0, "exit_price": 1600.0, "pnl_pct": 0.0667,
                "exit_reason": "止盈", "duration": 25, "shares": 100,
            }],
            "stats": {"n_trades": 1},
        },
        strategy_id="trend_breakout",
    )
    payload = json.loads(
        (tmp_path / "user_data" / backtest._LAST_TRADES_FILE).read_text(encoding="utf-8")
    )
    assert payload["strategy_id"] == "trend_breakout"
    assert payload["n_trades"] == 1
    t = payload["trades"][0]
    # 只留叠加层需要的字段: duration/shares 这类明细不落盘
    assert set(t) == {
        "symbol", "entry_date", "exit_date", "entry_price",
        "exit_price", "pnl_pct", "exit_reason",
    }
    assert t["exit_reason"] == "止盈"


def test_persist_last_trades_overwrites(tmp_path: Path, monkeypatch):
    """覆盖写: 只保留最近一次回测, 否则无法回答"图上这些点来自哪次回测"。"""
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    backtest._persist_last_trades(
        {"trades": [{"symbol": "A", "entry_date": "2026-01-01", "exit_date": "2026-01-02"}]},
        strategy_id="s1",
    )
    backtest._persist_last_trades(
        {"trades": [{"symbol": "B", "entry_date": "2026-02-01", "exit_date": "2026-02-02"}]},
        strategy_id="s2",
    )
    payload = json.loads(
        (tmp_path / "user_data" / backtest._LAST_TRADES_FILE).read_text(encoding="utf-8")
    )
    assert [t["symbol"] for t in payload["trades"]] == ["B"]
    assert payload["strategy_id"] == "s2"


def test_persist_last_trades_skips_non_dict(tmp_path: Path, monkeypatch):
    """optimize / walkforward 的结果结构不同, 静默跳过而不是报错。"""
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    backtest._persist_last_trades(None)
    backtest._persist_last_trades({"trades": []})
    backtest._persist_last_trades([{"symbol": "A"}])
    assert not (tmp_path / "user_data" / backtest._LAST_TRADES_FILE).exists()


def test_persist_last_trades_drops_incomplete(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    backtest._persist_last_trades({"trades": [
        {"symbol": "600519.SH", "entry_date": "2026-01-01", "exit_date": "2026-01-02"},
        {"symbol": "600519.SH", "entry_date": "2026-01-01"},          # 未平仓
        {"symbol": None, "entry_date": "x", "exit_date": "y"},
    ]})
    payload = json.loads(
        (tmp_path / "user_data" / backtest._LAST_TRADES_FILE).read_text(encoding="utf-8")
    )
    assert payload["n_trades"] == 1


def test_trades_by_symbol_filters_and_limits(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    backtest._persist_last_trades({"trades": [
        {"symbol": "600519.SH", "entry_date": "2026-01-01", "exit_date": "2026-01-02", "pnl_pct": 0.01},
        {"symbol": "000001.SZ", "entry_date": "2026-01-01", "exit_date": "2026-01-02", "pnl_pct": 0.02},
        {"symbol": "600519.SH", "entry_date": "2026-03-01", "exit_date": "2026-03-05", "pnl_pct": -0.03},
    ]}, strategy_id="s1")
    resp = _trades("600519.SH")
    assert resp["strategy_id"] == "s1"
    assert len(resp["trades"]) == 2
    assert all(t["symbol"] == "600519.SH" for t in resp["trades"])
    assert len(_trades("600519.SH", limit=1)["trades"]) == 1


def test_trades_by_symbol_no_file_is_empty(tmp_path: Path, monkeypatch):
    """没跑过回测是正常状态, 不是错误。"""
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    resp = _trades("600519.SH")
    assert resp["trades"] == []
    assert resp["strategy_id"] is None
    assert resp["finished_at"] is None
