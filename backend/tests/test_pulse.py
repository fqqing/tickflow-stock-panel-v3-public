"""盘中脉搏(app.pulse)单测: M1 资金流 / M2 竞价 / M3 力道 / M4 题材 / M5 梯队 / M6 逐笔。

上游是网络接口(通达信 7709), 这里全部用**替身**注入: 每个模块都从
``app.pulse.gateway`` 导入 ``client``, monkeypatch 掉就能离线测口径。

测的重点是**量纲与口径**(这块没有任何机器校验, 全靠注释和用例守着):
百分数 -> 小数、主力=超大+大、撤单只统计可撤单期、买卖分档、POC/VWAP。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import polars as pl
import pytest

from app.pulse import auction, gateway, ladder, moneyflow, strength, tick, topic


# ---------------------------------------------------------------- M1 资金流


class _FakeMoneyFlow:
    """money_flow.daily 的替身: 按代码返回若干天的 MoneyFlowDaily。"""

    def __init__(self, codes: dict[str, list[dict]]):
        self.codes = codes

    def daily(self, codes):
        blocks = []
        for c in codes:
            recs = self.codes.get(c)
            if recs is None:
                continue
            blocks.append(SimpleNamespace(
                exchange=c[:2], code=c[2:],
                records=[SimpleNamespace(**r) for r in recs],
            ))
        return SimpleNamespace(blocks=blocks)


def _mf_record(**kw) -> dict:
    base = {
        "date_raw": 20260930, "date": "2026-09-30",
        "total_amount": 1_000_000.0,
        "main_net": 150_000.0, "main_ratio": 15.0,
        "main_super_large_net": 100_000.0, "main_large_net": 50_000.0,
        "main_medium_net": -80_000.0, "main_small_net": -20_000.0,
        "main_buy_net": 120_000.0, "main_buy_ratio": 12.0,
        "buckets": tuple(range(16)),
    }
    base.update(kw)
    return base


def test_moneyflow_units_and_main_definition(monkeypatch):
    """main_net 必须等于 超大单 + 大单(通达信"主力"口径), 比率百分数 -> 小数。"""
    cli = SimpleNamespace(money_flow=_FakeMoneyFlow({
        "sh600519": [_mf_record()], "sz000001": [_mf_record(main_net=-50_000.0, main_ratio=-5.0)],
    }))
    monkeypatch.setattr(moneyflow, "client", lambda: cli)
    rows = moneyflow.fetch_moneyflow(["600519.SH", "000001.SZ"])
    assert len(rows) == 2
    first = next(r for r in rows if r["symbol"] == "600519.SH")
    # 100000 + 50000 == 150000, 与上游 main_net 一致(口径自洽性)
    assert first["super_large_net"] + first["large_net"] == pytest.approx(first["main_net"])
    # main_ratio 上游给百分数 15.0 => 契约小数 0.15
    assert first["main_ratio"] == pytest.approx(0.15)
    assert first["main_net"] / first["total_amount"] == pytest.approx(first["main_ratio"])
    assert first["buckets"] == list(range(16))
    out = next(r for r in rows if r["symbol"] == "000001.SZ")
    assert out["main_ratio"] == pytest.approx(-0.05)


def test_moneyflow_skips_unknown_market(monkeypatch):
    cli = SimpleNamespace(money_flow=_FakeMoneyFlow({"sh600519": [_mf_record()]}))
    monkeypatch.setattr(moneyflow, "client", lambda: cli)
    assert moneyflow.fetch_moneyflow(["600519.SH", "00700.HK"]) != []
    assert all(r["symbol"] == "600519.SH" for r in moneyflow.fetch_moneyflow(["600519.SH", "00700.HK"]))


def test_moneyflow_summarize_ranks_by_latest_net(monkeypatch):
    cli = SimpleNamespace(money_flow=_FakeMoneyFlow({
        "sh600519": [_mf_record(), _mf_record(date="2026-09-29", main_net=10_000.0)],
        "sz000001": [_mf_record(main_net=900_000.0)],
    }))
    monkeypatch.setattr(moneyflow, "client", lambda: cli)
    out = moneyflow.summarize(moneyflow.fetch_moneyflow(["600519.SH", "000001.SZ"], days=2))
    assert out["rows"][0]["symbol"] == "000001.SZ"   # 净额大的排前
    m = next(r for r in out["rows"] if r["symbol"] == "600519.SH")
    assert m["sum_main_net"] == pytest.approx(160_000.0)  # 150000 + 10000
    assert m["days"] == 2 and m["inflow_days"] == 2


# ---------------------------------------------------------------- M2 竞价


def _auction_points(n=30) -> list[SimpleNamespace]:
    """构造 09:15~09:25 的竞价序列: 匹配量递增, 未匹配先冲高后回落。"""
    out = []
    for i in range(n):
        minute = 15 + (i * 10) // n
        second = (i * 7) % 60
        out.append(SimpleNamespace(
            index=i, time_label=f"09:{minute:02d}:{second:02d}",
            price=10.0 + (i % 3) * 0.01,
            matched_volume=i * 10.0,
            unmatched_volume=100.0 - i * 3.0,
        ))
    return out


def _auction_cli():
    return SimpleNamespace(helpers=SimpleNamespace(auction_data=lambda code: SimpleNamespace(
        code=code, trading_date="2026-09-30",
        series=SimpleNamespace(points=_auction_points()),
        pre_close_price=10.0, open_price=10.02, open_volume=500.0,
        open_amount=5_010_000.0, open_change_pct=0.2,
    )))


def test_auction_percent_to_ratio_and_score(monkeypatch):
    monkeypatch.setattr(auction, "client", _auction_cli)
    monkeypatch.setattr(auction, "_float_shares", lambda sym: 1_000_000.0)
    data = auction.fetch_auction("600519.SH")
    # 上游 open_change_pct=0.2(百分数) => 0.002
    assert data["open_change_pct"] == pytest.approx(0.002)
    assert len(data["points"]) == 30
    score = data["score"]
    assert score is not None
    assert 0 <= score["total"] <= 100
    # 分项之和 == 总分(别出现权重算错却看不出来)
    assert sum(score["parts"].values()) == pytest.approx(score["total"], abs=0.2)


def test_auction_cancel_only_counts_revocable_window(monkeypatch):
    """撤单率只统计 09:20 之前。全时段算会得到"人人都在撤单"的荒谬结论。"""
    points = _auction_points()
    # 把 09:20 之后的未匹配量全部改成 0(撮合完成), 末端必然归零。
    for p in points:
        if p.time_label >= "09:20":
            p.unmatched_volume = 0.0
    score = auction._score(
        [{"time": p.time_label, "price": p.price, "matched": p.matched_volume,
          "unmatched": p.unmatched_volume} for p in points],
        pre_close=10.0, open_price=10.02, open_amount=5_010_000.0, float_shares=1_000_000.0,
    )
    # 可撤单期(09:15~09:19)内从 100 回落到 ~85 => 撤单率约 0.15, 绝不是 1.0。
    assert score["cancel_rate"] < 0.5


def test_auction_too_few_points_returns_none():
    assert auction._score(
        [{"time": "09:15:01", "price": 10.0, "matched": 1.0, "unmatched": 1.0}],
        pre_close=10.0, open_price=10.0, open_amount=1.0, float_shares=1.0,
    ) is None


# ---------------------------------------------------------------- M3 买卖力道


def test_strength_summary(monkeypatch):
    pts = [SimpleNamespace(time_label=f"{9 + i // 60:02d}:{31 + i % 60:02d}",
                           series_a=10.0 + i, series_b=5.0,
                           buy_commission=10.0 + i, sell_commission=5.0)
           for i in range(120)]
    cli = SimpleNamespace(helpers=SimpleNamespace(
        buy_sell_strength=lambda code: SimpleNamespace(points=pts)))
    monkeypatch.setattr(strength, "client", lambda: cli)
    data = strength.fetch_strength("600519.SH")
    assert len(data["points"]) == 120
    assert data["points"][0]["cum_delta"] == pytest.approx(5.0)
    s = data["summary"]
    assert s["net"] == pytest.approx(sum(5.0 + i for i in range(120)))
    assert -1 <= s["strength_ratio"] <= 1


def test_strength_empty_when_no_points(monkeypatch):
    cli = SimpleNamespace(helpers=SimpleNamespace(
        buy_sell_strength=lambda code: SimpleNamespace(points=[])))
    monkeypatch.setattr(strength, "client", lambda: cli)
    assert strength.fetch_strength("600519.SH")["points"] == []


# ---------------------------------------------------------------- M4 题材


def test_topic_pseudo_filter():
    """上游"昨日涨停/最近情绪指数"是统计标签不是题材, 默认必须过滤掉。"""
    assert topic._is_pseudo("昨日涨停") is True
    assert topic._is_pseudo("最近情绪指数") is True
    assert topic._is_pseudo("白酒概念") is False


def test_topic_rank_filters_pseudo(monkeypatch):
    payload = {"trade_date": "2026-09-30", "rows": [
        {"topic_name": "昨日涨停", "rank": 1},
        {"topic_name": "白酒概念", "rank": 2},
    ]}
    monkeypatch.setattr(topic, "cached_slow", lambda key, loader, ttl: (payload, False, time.time()))
    out = topic.fetch_topic_rank()
    assert [r["topic_name"] for r in out["rows"]] == ["白酒概念"]
    assert out["filtered_pseudo"] is True
    assert topic.fetch_topic_rank(include_pseudo=True)["rows"] == payload["rows"]


def test_full_code_to_symbol():
    assert topic._full_code_to_symbol("sh600825") == "600825.SH"
    assert topic._full_code_to_symbol("sz000001") == "000001.SZ"
    assert topic._full_code_to_symbol("") is None


# ---------------------------------------------------------------- M5 涨停梯队


def test_ladder_stats_and_filters(monkeypatch):
    rows = [
        {"symbol": "A", "ladder_level": 7, "limit_status": "sealed", "seal_amount": 1e9},
        {"symbol": "B", "ladder_level": 3, "limit_status": "sealed", "seal_amount": 2e8},
        {"symbol": "C", "ladder_level": 1, "limit_status": "open", "seal_amount": 0.0},
    ]
    monkeypatch.setattr(ladder, "cached_slow",
                        lambda key, loader, ttl: ({"trade_date": "d", "rows": rows}, False, time.time()))
    out = ladder.fetch_ladder(min_level=2)
    assert [r["symbol"] for r in out["rows"]] == ["A", "B"]   # 按连板高度倒序
    assert ladder.fetch_ladder(only_sealed=True)["total"] == 3
    stats = ladder.ladder_stats(rows)
    assert stats["count"] == 3 and stats["sealed"] == 2 and stats["max_level"] == 7
    assert stats["by_level"] == {1: 1, 3: 1, 7: 1}
    assert stats["seal_total_amount"] == pytest.approx(1.2e9)


# ---------------------------------------------------------------- M6 逐笔


def _tick_df() -> pl.DataFrame:
    return pl.DataFrame({
        "index": list(range(6)),
        "time": ["09:25", "09:31", "09:31", "10:05", "10:05", "14:30"],
        "price": [10.0, 10.1, 10.2, 10.3, 10.15, 10.0],
        "volume": [100.0, 10.0, 20.0, 30.0, 5.0, 8.0],
        "order_count": [50, 5, 8, 12, 3, 4],
        "side": ["neutral", "buy", "sell", "buy", "sell", "buy"],
        "kind": ["opening_match"] + ["trade"] * 5,
    })


def test_orderflow_delta_and_cumdelta():
    data = tick.orderflow(_tick_df())
    s = data["summary"]
    assert s["buy_volume"] == pytest.approx(48.0)     # 10 + 30 + 8
    assert s["sell_volume"] == pytest.approx(25.0)    # 20 + 5
    assert s["neutral_volume"] == pytest.approx(100.0)  # 竞价撮合单列
    assert s["delta"] == pytest.approx(23.0)
    assert s["delta_ratio"] == pytest.approx(23.0 / 73.0)
    # CumDelta 是逐分钟累加, 末点应等于总 delta
    assert data["points"][-1]["cum_delta"] == pytest.approx(23.0)
    assert data["points"][0]["minute"] == "09:25"


def test_footprint_grid_poc_and_vwap():
    fp = tick.footprint(_tick_df(), rows=5, bucket_minutes=30)
    assert fp["low"] == pytest.approx(10.0) and fp["high"] == pytest.approx(10.3)
    assert len(fp["price_levels"]) == 5
    # 时间桶用可读标签, 前端直接当横轴刻度
    assert "09:00" in fp["time_buckets"] or "09:30" in fp["time_buckets"]
    assert fp["poc"] is not None
    notional = 10.0 * 100 + 10.1 * 10 + 10.2 * 20 + 10.3 * 30 + 10.15 * 5 + 10.0 * 8
    assert fp["vwap"] == pytest.approx(notional / 173.0)
    # 每格的买卖量必须守恒
    buy = sum(c["buy"] for c in fp["cells"])
    sell = sum(c["sell"] for c in fp["cells"])
    assert buy + sell == pytest.approx(73.0)


def test_footprint_empty():
    fp = tick.footprint(pl.DataFrame(), rows=10)
    assert fp["cells"] == [] and fp["poc"] is None


# ---------------------------------------------------------------- S4 分价表


def test_price_dist_bins_and_units():
    """档位按 tick 整数倍取步长; 成交量/笔数守恒, amount=元。"""
    d = tick.price_distribution(_tick_df(), max_rows=5)
    rows = d["rows"]
    assert rows, "应有档位"
    # span=0.30 / 5 档 -> 目标步长 0.06, 已是 tick(0.01) 整数倍
    assert d["step"] == pytest.approx(0.06)
    assert abs(d["step"] / 0.01 - round(d["step"] / 0.01)) < 1e-9, "步长必须是 tick 整数倍"
    # 总量守恒
    assert sum(r["volume"] for r in rows) == pytest.approx(173.0)
    assert sum(r["trades"] for r in rows) == 6
    # amount = price(元) x volume(手) x 100
    expect = (10.0 * 100 + 10.1 * 10 + 10.2 * 20 + 10.3 * 30 + 10.15 * 5 + 10.0 * 8) * 100
    assert d["total_amount"] == pytest.approx(expect)
    assert sum(r["amount"] for r in rows) == pytest.approx(expect)
    # 累计占比从最低档往上累加, 末档为 1
    assert rows[-1]["cum_ratio"] == pytest.approx(1.0)
    assert rows == sorted(rows, key=lambda r: r["price"])


def test_price_dist_low_price_degrades_to_tick():
    """低价股: 目标步长 < tick 时退化为逐价位(不硬分 60 档)。"""
    df = pl.DataFrame({
        "index": list(range(4)),
        "time": ["09:31"] * 4,
        "price": [3.00, 3.01, 3.02, 3.10],
        "volume": [10.0, 20.0, 30.0, 40.0],
        "order_count": [1, 2, 3, 4],
        "side": ["buy", "sell", "buy", "sell"],
        "kind": ["trade"] * 4,
    })
    d = tick.price_distribution(df, max_rows=60)
    assert d["step"] == pytest.approx(0.01), "步长不得小于 tick"
    # 3.00/3.01/3.02/3.10 各自独立成档(空档不占位, 故是 4 档而不是 11 档)
    assert len(d["rows"]) == 4
    assert [r["price"] for r in d["rows"]] == pytest.approx([3.005, 3.015, 3.025, 3.105])


def test_price_dist_poc_and_current():
    """POC = 成交量最大的档; 当前价取第一条(df 倒序, index 0 = 最新)。"""
    d = tick.price_distribution(_tick_df(), max_rows=5)
    # 10.00 档有 100(neutral) + 8(buy) = 108 手, 是最大档
    poc_row = max(d["rows"], key=lambda r: r["volume"])
    assert d["poc"] == pytest.approx(poc_row["price"])
    assert poc_row["volume"] == pytest.approx(108.0)
    assert d["current"] == pytest.approx(10.0), "df 倒序, 第一行就是最新价"
    assert d["vwap"] == pytest.approx(
        (10.0 * 100 + 10.1 * 10 + 10.2 * 20 + 10.3 * 30 + 10.15 * 5 + 10.0 * 8) / 173.0
    )


def test_price_dist_buy_sell_conservation():
    """主买/主卖与 orderflow 口径一致(neutral 单列, 不计入买卖)。"""
    d = tick.price_distribution(_tick_df(), max_rows=5)
    assert d["buy_volume"] == pytest.approx(48.0)
    assert d["sell_volume"] == pytest.approx(25.0)
    assert d["neutral_volume"] == pytest.approx(100.0)
    assert d["buy_volume"] + d["sell_volume"] + d["neutral_volume"] == pytest.approx(173.0)


def test_price_dist_single_price_no_division_by_zero():
    df = pl.DataFrame({
        "index": [0, 1], "time": ["09:31", "09:31"], "price": [10.0, 10.0],
        "volume": [5.0, 7.0], "order_count": [1, 1],
        "side": ["buy", "sell"], "kind": ["trade", "trade"],
    })
    d = tick.price_distribution(df, max_rows=60)
    assert d["step"] == pytest.approx(0.01)
    assert len(d["rows"]) == 1
    assert d["rows"][0]["volume"] == pytest.approx(12.0)


def test_price_dist_empty():
    d = tick.price_distribution(pl.DataFrame(), max_rows=60)
    assert d["rows"] == [] and d["poc"] is None and d["current"] is None


def test_resolve_day_prefers_preferred():
    assert str(tick.resolve_day("2026-01-01")) == "2026-01-01"


# ---------------------------------------------------------------- M0 缓存基建


def test_gateway_ttl_cache_roundtrip(monkeypatch, tmp_path):
    """TTL 内命中不重复加载; 过期后立刻返回旧值并后台刷新(stale-while-revalidate)。"""
    monkeypatch.setattr(gateway, "cache_dir", lambda: tmp_path)
    gateway._INDEX.pop("ut", None)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"v": calls["n"]}

    got, stale, stamp = gateway.cached_slow("ut", loader, ttl=60)
    assert got == {"v": 1} and stale is False and stamp is not None
    got2, stale2, _ = gateway.cached_slow("ut", loader, ttl=60)
    assert got2 == {"v": 1} and stale2 is False and calls["n"] == 1  # 未重新加载

    # 过期: 立刻给旧值, 后台线程刷新
    got3, stale3, _ = gateway.cached_slow("ut", loader, ttl=-1)
    assert stale3 is True and got3 in ({"v": 1}, {"v": 2})
    for _ in range(50):
        if calls["n"] >= 2:
            break
        time.sleep(0.05)
    assert calls["n"] == 2
    # 刷新完成后再取就是新值
    got4, stale4, _ = gateway.cached_slow("ut", loader, ttl=60)
    assert got4 == {"v": 2} and stale4 is False

    gateway.invalidate("ut")
    gateway._INDEX.pop("ut", None)
    assert gateway._read_disk("ut") is None
