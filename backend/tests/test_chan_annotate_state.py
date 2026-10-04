"""annotate 端点的失效检查与「当前状态」合成逻辑 (纯函数, 无需 repo)。

判断口径 (2026-09-18 与用户确认):
- 两列各是「最近一次」信号, 不是同一时刻 => 谁近谁主导 (买后/卖后状态)。
- 失效检查: 最新收盘 < 买点信号价 => 买点作废; > 卖点信号价 => 卖点作废 (转强)。
- 买点距离: <=5% 贴近可介入; >=15% 已涨离 (追高风险)。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.chan import _side_payload, _state_payload


def _snap(kind: str | None, bars: int | None, price: float | None, label: str = "一买"):
    """造一个快照桩; kind=None 表示该侧无信号。"""
    if kind is None:
        return SimpleNamespace(
            kind=None, label="无买点", bars_since=None, price=None,
            trend=None, text="当前无缠论买点信号",
        )
    return SimpleNamespace(
        kind=kind, label=label, bars_since=bars, price=price,
        trend="up", text=f"{label} @ {price}, 距今 {bars} 根",
    )


class TestSidePayloadInvalid:
    def test_buy_invalid_when_close_below_price(self):
        snap = _snap("1buy", 4, 14.41)
        row = _side_payload("", snap, 60, "买点", last_close=13.90, is_buy=True)
        assert row["invalid"] is True

    def test_buy_valid_when_close_above_price(self):
        snap = _snap("3buy", 4, 14.41, label="三买")
        row = _side_payload("", snap, 60, "买点", last_close=14.60, is_buy=True)
        assert row["invalid"] is False

    def test_sell_invalid_when_close_above_price(self):
        snap = _snap("1sell", 14, 10.07, label="一卖")
        row = _side_payload("sell_", snap, 60, "卖点", last_close=10.20, is_buy=False)
        assert row["sell_invalid"] is True

    def test_sell_valid_when_close_below_price(self):
        snap = _snap("1sell", 14, 10.07, label="一卖")
        row = _side_payload("sell_", snap, 60, "卖点", last_close=9.80, is_buy=False)
        assert row["sell_invalid"] is False

    def test_stale_side_never_invalid(self):
        snap = _snap("1buy", 100, 10.0)
        row = _side_payload("", snap, 60, "买点", last_close=5.0, is_buy=True)
        assert row["stale"] is True
        assert row["kind"] is None
        assert row["invalid"] is False

    def test_missing_price_or_close_never_invalid(self):
        snap = _snap("1buy", 4, None)
        assert _side_payload("", snap, 60, "买点", last_close=9.0)["invalid"] is False
        snap2 = _snap("1buy", 4, 10.0)
        assert _side_payload("", snap2, 60, "买点", last_close=None)["invalid"] is False


class TestStatePayload:
    def test_no_signal_on_both_sides(self):
        buy = _side_payload("", _snap(None, None, None), 60, "买点")
        sell = _side_payload("sell_", _snap(None, None, None), 60, "卖点", is_buy=False)
        state = _state_payload(buy, sell, 10.0)
        assert state["state_side"] is None
        assert state["state_label"] == "无信号"

    def test_fresher_side_dominates_buy(self):
        # 截图实测例: 三买 4 根前 vs 一卖 23 根前 => 买后状态
        buy = _side_payload("", _snap("3buy", 4, 14.41, "三买"), 60, "买点",
                            last_close=14.60)
        sell = _side_payload("sell_", _snap("1sell", 23, 18.16, "一卖"), 60, "卖点",
                             last_close=14.60, is_buy=False)
        state = _state_payload(buy, sell, 14.60)
        assert state["state_side"] == "buy"
        assert state["state_label"] == "买后4天·贴近买点价"
        assert state["state_invalid"] is False

    def test_fresher_side_dominates_sell(self):
        # 截图实测例: 二买 29 根前 vs 一卖 14 根前 => 卖后状态
        buy = _side_payload("", _snap("2buy", 29, 7.68, "二买"), 60, "买点",
                            last_close=9.80)
        sell = _side_payload("sell_", _snap("1sell", 14, 10.07, "一卖"), 60, "卖点",
                             last_close=9.80, is_buy=False)
        state = _state_payload(buy, sell, 9.80)
        assert state["state_side"] == "sell"
        assert state["state_label"] == "卖后14天"

    def test_sell_invalidated_by_new_high(self):
        sell = _side_payload("sell_", _snap("1sell", 14, 10.07, "一卖"), 60, "卖点",
                             last_close=10.20, is_buy=False)
        state = _state_payload(_side_payload("", _snap(None, None, None), 60, "买点"),
                               sell, 10.20)
        assert state["state_side"] == "sell"
        assert state["state_label"] == "卖后14天·已新高失效"
        assert state["state_invalid"] is True

    def test_buy_invalidated_by_breakdown(self):
        buy = _side_payload("", _snap("1buy", 6, 14.41), 60, "买点", last_close=13.90)
        state = _state_payload(buy, _side_payload("sell_", _snap(None, None, None), 60,
                                                  "卖点", is_buy=False), 13.90)
        assert state["state_label"] == "买后6天·已失效"
        assert state["state_invalid"] is True

    def test_buy_distance_buckets(self):
        # 贴近 (<=5%) / 中间 / 已涨离 (>=15%)
        for close, expect in ((14.60, "贴近买点价"), (15.50, "买后4天"), (16.80, "已涨离")):
            buy = _side_payload("", _snap("3buy", 4, 14.41, "三买"), 60, "买点",
                                last_close=close)
            state = _state_payload(buy, _side_payload("sell_", _snap(None, None, None),
                                                      60, "卖点", is_buy=False), close)
            assert expect in state["state_label"], (close, state["state_label"])

    def test_stale_side_ignored_in_dominance(self):
        # 买点超窗 (stale) 后视为无信号, 由有效卖点主导
        buy = _side_payload("", _snap("1buy", 100, 10.0), 60, "买点", last_close=11.0)
        sell = _side_payload("sell_", _snap("1sell", 58, 12.0, "一卖"), 60, "卖点",
                             last_close=11.0, is_buy=False)
        state = _state_payload(buy, sell, 11.0)
        assert state["state_side"] == "sell"
