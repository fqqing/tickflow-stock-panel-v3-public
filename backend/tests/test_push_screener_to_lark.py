"""push_screener_to_lark 记录映射单测 (纯逻辑, 不发网络请求)。"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import push_screener_to_lark as ps  # noqa: E402


def _row(**kw):
    base = {"symbol": "600354.SH", "name": "敦煌种业", "close": 11.45, "change_pct": 0.0306}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# 异动预警映射
# --------------------------------------------------------------------------

def test_abnormal_basic_mapping():
    rows = [{
        "symbol": "600354.SH", "name": "敦煌种业", "board": "主板", "close": 11.45,
        "windows": {
            "3d": {"value": 0.0535, "threshold": 0.20, "closeness": 0.2675},
            "10d": {"value": 0.5349, "threshold": 1.00, "closeness": 0.5349},
            "30d": {"value": 1.9694, "threshold": 2.00, "closeness": 0.9847},
        },
        "max_closeness": 0.9847, "status": "edge",
    }]
    recs = ps._abnormal_records(rows, "2026-09-30")
    assert len(recs) == 1
    r = recs[0]
    assert r["代码"] == "sh600354"
    assert r["名称"] == "敦煌种业"
    assert r["日期"] == "2026-09-30"
    assert r["收盘价"] == 11.45
    # 目标 = 所需涨幅最小的未触发窗口 (30d: 2.00 - 1.9694 = 0.0306)
    assert r["目标等级"] == "30日异动"
    assert abs(r["所需最小涨幅"] - 0.0306) < 1e-6
    assert r["预警信息"] == "明日若涨 3.06% 将触发30日异动"
    # 30 日需涨 3.06% <= 主板涨停 10% -> True
    assert r["下一日可能触发"] == "True"
    assert r["触发信号次数"] == 0
    assert r["是否异动类型"] is None


def test_abnormal_triggered_window():
    """已触发窗口计入 触发信号次数/是否异动类型, 且不再作为目标。"""
    rows = [{
        "symbol": "300820.SZ", "name": "英杰电气", "board": "创业板/科创板", "close": 68.5,
        "windows": {
            "3d": {"value": 0.3210, "threshold": 0.30, "closeness": 1.07},
            "10d": {"value": 0.60, "threshold": 1.00, "closeness": 0.60},
            "30d": {"value": 0.90, "threshold": 2.00, "closeness": 0.45},
        },
        "status": "triggered",
    }]
    r = ps._abnormal_records(rows, "2026-09-30")[0]
    assert r["代码"] == "sz300820"
    assert r["触发信号次数"] == 1
    assert r["是否异动类型"] == "3日涨跌幅异常(32.10%)"
    # 3d 已触发被排除, 目标落在 need 最小的 10d (1.00-0.60=0.40)
    assert r["目标等级"] == "10日异动"
    assert abs(r["所需最小涨幅"] - 0.40) < 1e-6
    # 创业板涨停 20% < 40% -> False
    assert r["下一日可能触发"] == "False"


def test_abnormal_all_triggered_falls_back_to_max():
    """全部窗口已触发时, 目标取偏离最大的窗口, 所需涨幅 0。"""
    rows = [{
        "symbol": "920001.BJ", "name": "北交样本", "board": "北交所", "close": 20.0,
        "windows": {
            "3d": {"value": 0.45, "threshold": 0.40, "closeness": 1.125},
            "30d": {"value": 2.5, "threshold": 2.00, "closeness": 1.25},
        },
    }]
    r = ps._abnormal_records(rows, "2026-09-30")[0]
    assert r["代码"] == "bj920001"
    assert r["触发信号次数"] == 2
    assert r["目标等级"] == "30日异动"
    assert r["所需最小涨幅"] == 0.0
    assert r["预警信息"] is None
    assert r["下一日可能触发"] == "True"  # 0 <= 上限


def test_abnormal_drops_unreachable_by_default():
    """还需涨 48% 且未触发过的行, 默认被过滤掉(无行动价值)。"""
    rows = [{
        "symbol": "000678.SZ", "name": "襄阳轴承", "board": "主板", "close": 12.89,
        "windows": {
            "3d": {"value": 0.05, "threshold": 0.20, "closeness": 0.25},
            "10d": {"value": 0.5116, "threshold": 1.00, "closeness": 0.5116},
        },
    }]
    # 主板涨停 10%, 而最近的目标窗口还差 15% -> 明日不可能触发, 过滤掉
    assert ps._abnormal_records(rows, "2026-09-30") == []
    # only_actionable=False 时保留, 目标取 need 最小的 3d (0.20 - 0.05)
    kept = ps._abnormal_records(rows, "2026-09-30", only_actionable=False)
    assert len(kept) == 1
    assert kept[0]["目标等级"] == "3日异动"
    assert abs(kept[0]["所需最小涨幅"] - 0.15) < 1e-6
    assert kept[0]["触发信号次数"] == 0


# --------------------------------------------------------------------------
# 选股映射
# --------------------------------------------------------------------------

def test_trend_dragon_pct_and_bias():
    rows = [_row(ma5=11.0, ma20=10.5888, macd_dif=0.1)]
    recs = ps.build_records(
        "trend_dragon", rows, "2026-09-30",
        momentum_map={"600354.SH": 0.6414},
    )
    r = recs[0]
    # change_pct 小数 -> 百分数
    assert abs(r["涨跌幅%"] - 3.06) < 1e-6
    # (11.45 / 10.5888 - 1) * 100
    assert abs(r["乖离MA20%"] - 8.1361) < 0.01
    assert r["资金动能"] == 0.6414
    assert r["MA5"] == 11.0
    assert r["MA13"] is None  # enriched 无 ma13


def test_bottom_structure_fields():
    rows = [_row(ma5=11.0, ma20=10.5, macd_dif=0.12, macd_dea=0.08)]
    r = ps.build_records("bottom_structure", rows, "2026-09-30")[0]
    assert r["DIF"] == 0.12 and r["DEA"] == 0.08
    assert r["MA5"] == 11.0 and r["MA20"] == 10.5
    assert r["信号状态"] == "" and r["钝化类型"] == ""


def test_invalid_numbers_become_none():
    rows = [_row(close=None, change_pct="-", ma20=0)]
    r = ps.build_records("trend_dragon", rows, "2026-09-30")[0]
    assert r["收盘价"] is None
    assert r["涨跌幅%"] is None
    assert r["乖离MA20%"] is None  # ma20=0 不参与除零
