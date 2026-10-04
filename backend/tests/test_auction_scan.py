"""全市场竞价扫描的单测(不联网)。

只测纯函数部分: 涨停阈值判定与 rank_auction 的排序/筛选语义。
``fetch_auction_scan`` 本身要连 eltdx, 交给真机验证。
"""
from __future__ import annotations

from app.pulse.auction import _limit_up_threshold, rank_auction


def _row(symbol: str, open_pct: float, score: float | None = None,
         repair: float | None = None) -> dict:
    return {
        "symbol": symbol,
        "open_change_pct": open_pct,
        "score": score,
        "repair_pct": repair,
    }


def test_limit_up_threshold_by_board():
    """涨停阈值按板块区分: 科创/创业 20%, 北交所 30%, 主板 10%。"""
    assert _limit_up_threshold("688001.SH") == 0.198
    assert _limit_up_threshold("300750.SZ") == 0.198
    assert _limit_up_threshold("920002.BJ") == 0.298
    assert _limit_up_threshold("600519.SH") == 0.098
    assert _limit_up_threshold("000001.SZ") == 0.098


def test_rank_score_mode_puts_missing_score_last():
    """score=None(竞价点数不足)一律垫底, 不能因为 None 参与比较而崩或乱序。"""
    rows = [
        _row("A", 0.01, score=None),
        _row("B", 0.01, score=80.0),
        _row("C", 0.01, score=50.0),
    ]
    out = rank_auction(rows, mode="score")
    assert [r["symbol"] for r in out] == ["B", "C", "A"]


def test_rank_score_mode_descending_by_default():
    rows = [_row("A", 0.01, score=10.0), _row("B", 0.01, score=90.0)]
    assert [r["symbol"] for r in rank_auction(rows, mode="score")] == ["B", "A"]
    assert [r["symbol"] for r in rank_auction(rows, mode="score", ascending=True)] == ["A", "B"]


def test_rank_repair_mode_only_keeps_low_open():
    """低开走强榜必须只留竞价低开的行 —— 否则高开高走会盖住真正的低开修复。"""
    rows = [
        _row("A", 0.05, repair=0.10),   # 高开 + 继续涨: 修复幅度最大, 但不是低开
        _row("B", -0.03, repair=0.08),  # 低开 + 强修复
        _row("C", -0.01, repair=0.02),  # 低开 + 弱修复
        _row("D", 0.0, repair=0.05),    # 平开: 不算低开
    ]
    out = rank_auction(rows, mode="repair")
    assert [r["symbol"] for r in out] == ["B", "C"], "高开/平开必须被排除"


def test_rank_open_pct_range_filter():
    """开盘涨幅区间筛选(圈定「小幅低开」「深跌低开」这类范围)。"""
    rows = [
        _row("A", -0.09, score=1.0),   # 深跌低开
        _row("B", -0.02, score=2.0),   # 小幅低开
        _row("C", 0.07, score=3.0),    # 高开
    ]
    out = rank_auction(rows, mode="score", min_open_pct=-0.05, max_open_pct=0.0)
    assert [r["symbol"] for r in out] == ["B"]


def test_rank_limit():
    rows = [_row(f"S{i}", 0.01, score=float(i)) for i in range(10)]
    assert len(rank_auction(rows, mode="score", limit=3)) == 3


def test_rank_empty():
    assert rank_auction([], mode="score") == []
    assert rank_auction([], mode="repair") == []
