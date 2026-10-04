"""缠论告警 (N12): 新买点 / 突破中枢 / 跌破中枢 -> 飞书。

数据从哪来: 每日流水线的 ``scan_market.py`` 产物 (``data/scan/scan_*.parquet``)
已经带全了判断所需的状态 —— 买点类型/日期/距今、中枢上下沿与相对位置。告警
不再重算一遍缠论, 而是拿**当前扫描 vs 上次确认状态**做差分:

- ``new_buy``    新买点: 最近一根 K 线上出现买点 (buy_ago == 0), 且这个买点
  (按 buy_date) 上次没报过。
- ``zs_up``      突破中枢: 上次收盘在中枢内/下方, 这次到了上方。
- ``zs_down``    跌破中枢: 上次在中枢内/上方, 这次到了下方。

为什么自己存状态而不去 diff 两个扫描文件: ``scan_market.py`` 每天覆盖同一个
文件名, 历史根本不存在; 就算改成按日期命名, 「哪两个文件该比」也是个新问题。
本地状态文件 (``data/alerts/chan_state_*.parquet``) 把语义定死成「上次处理到
哪」—— 检测是纯函数 (只读), 推飞书成功或点「忽略」才落盘。这样:

- 页面刷新 N 次看到的是同一批告警 (不会看一眼就消失);
- 未处理的告警跨天仍然挂着 (今天没处理, 明天扫描后差分的基准还是上上次);
- 去重交给飞书表的 (代码, 信号日期, 告警类型) 业务键, 手抖点两次也不灌重。

首次运行没有状态文件: 只有新买点能报 (它不依赖上次状态), 中枢类事件要等
下一次确认后才有效 —— 这是特性不是缺陷, 没有基准就无法定义「突破」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from chanlab import scan_store
from chanlab.lark import _BSP_LABEL, PushResult, _r2
from chanlab.lark import push as lark_push

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "data" / "alerts"

KIND_NEW_BUY = "new_buy"
KIND_ZS_UP = "zs_up"
KIND_ZS_DOWN = "zs_down"

#: 告警类型 -> 中文标签 (飞书表与前端共用)
KIND_LABELS = {
    KIND_NEW_BUY: "新买点",
    KIND_ZS_UP: "突破中枢",
    KIND_ZS_DOWN: "跌破中枢",
}

#: 告警飞书表的去重键 —— 同一天同一只票可以既出买点又突破中枢, 只用
#: (代码, 信号日期) 会把后一种挤掉
ALERT_KEY_FIELDS = ("代码", "信号日期", "告警类型")

#: 状态文件里保留的列。刻意只存判断需要的最小集: 全市场 5000 行 x 每天一份,
#: 存全列纯属浪费
_STATE_COLS = (
    "symbol", "last_date", "buy_type", "buy_date", "buy_ago", "zs_pos",
)


def _state_path(level: str, profile: str, lookback: int) -> Path:
    return STATE_DIR / f"chan_state_{level}_{profile}_{lookback}.parquet"


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        df = pl.read_parquet(path)
    except Exception:  # 状态坏了当作没有, 检测还能跑
        return {}
    return {r["symbol"]: r for r in df.to_dicts()}


def _current(path: Path) -> tuple[pl.DataFrame, str]:
    """读当前扫描产物, 返回 (frame, 数据截止日)。"""
    df = scan_store.load_frame(path)
    as_of = ""
    if "last_date" in df.columns and df.height:
        newest = df["last_date"].max()
        as_of = str(newest)[:10] if newest is not None else ""
    return df, as_of


def detect(
    level: str = "1d",
    profile: str = "v1",
    lookback: int = 500,
) -> dict:
    """差分出当前告警。**纯函数**: 不写状态, 不碰飞书。

    返回 ``{alerts, as_of, scanned, has_state, note}``。扫描产物不存在时
    ``alerts=[]`` 且 note 说明原因, 不抛异常 —— 页面不该因为没跑过扫描而 500。
    """
    path, matched = scan_store.resolve_file(level=level, profile=profile, lookback=lookback)
    if path is None:
        return {
            "alerts": [], "as_of": "", "scanned": 0, "has_state": False,
            "matched": False,
            "note": "没有扫描结果, 先跑 backend/scripts/scan_market.py 或每日流水线",
        }

    df, as_of = _current(path)
    cur = {r["symbol"]: r for r in df.to_dicts()}
    state = _load_state(_state_path(level, profile, lookback))

    alerts: list[dict[str, Any]] = []
    for symbol, row in cur.items():
        prev = state.get(symbol)
        buy_ago = row.get("buy_ago")
        buy_date = str(row.get("buy_date") or "")
        zs_pos = str(row.get("zs_pos") or "")

        # 新买点: 买点就在最后一根 K 线上, 且这个日期上次没报过
        if buy_ago is not None and buy_ago == 0 and buy_date and (
            prev is None or str(prev.get("buy_date") or "") != buy_date
        ):
            alerts.append(_alert(symbol, row, KIND_NEW_BUY, None))

        # 中枢事件必须两侧都有状态才能定义「变化」
        if prev is None:
            continue
        prev_zs = str(prev.get("zs_pos") or "")
        if prev_zs in ("inside", "below") and zs_pos == "above":
            alerts.append(_alert(symbol, row, KIND_ZS_UP, prev_zs))
        elif prev_zs in ("inside", "above") and zs_pos == "below":
            alerts.append(_alert(symbol, row, KIND_ZS_DOWN, prev_zs))

    order = {KIND_NEW_BUY: 0, KIND_ZS_UP: 1, KIND_ZS_DOWN: 2}
    alerts.sort(key=lambda a: (a["chg_pct"] if a["chg_pct"] is not None else -999), reverse=True)
    alerts.sort(key=lambda a: order[a["kind"]])

    note = ""
    if not state:
        note = "首次检测, 只有新买点 (中枢突破/跌破要等下一次确认建立基准)"
    return {
        "alerts": alerts,
        "as_of": as_of,
        "scanned": df.height,
        "has_state": bool(state),
        "matched": matched,
        "note": note,
    }


def _alert(symbol: str, row: dict, kind: str, prev_zs_pos: str | None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": row.get("name"),
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "buy_type": row.get("buy_type"),
        "buy_date": row.get("buy_date"),
        "close": row.get("close"),
        "chg_pct": row.get("chg_pct"),
        "zs_pos": row.get("zs_pos"),
        "zs_dist": row.get("zs_dist"),
        "prev_zs_pos": prev_zs_pos,
        "last_date": str(row.get("last_date") or "")[:10],
    }


def ack(level: str = "1d", profile: str = "v1", lookback: int = 500) -> int:
    """把当前扫描状态存为「已处理基准」。返回落盘的标的数。

    推飞书成功后自动调用; 页面上的「忽略」按钮也走这里。
    """
    path, _ = scan_store.resolve_file(level=level, profile=profile, lookback=lookback)
    if path is None:
        return 0
    df, _ = _current(path)
    cols = [c for c in _STATE_COLS if c in df.columns]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    df.select(cols).write_parquet(_state_path(level, profile, lookback))
    return df.height


def records(alerts: list[dict], as_of: str) -> list[dict[str, Any]]:
    """告警 -> 飞书记录。字段是独立设计的 (表还没建, 命名一次到位)。"""
    out: list[dict[str, Any]] = []
    for a in alerts:
        symbol = str(a.get("symbol") or "")
        code, _, market = symbol.partition(".")
        buy = str(a.get("buy_type") or "")
        detail_parts = []
        if buy:
            detail_parts.append(_BSP_LABEL.get(buy, buy))
        if a.get("prev_zs_pos"):
            detail_parts.append(f"上次:{_ZS_PREV_LABEL.get(str(a['prev_zs_pos']), a['prev_zs_pos'])}")
        out.append({
            "代码": code,
            "名称": str(a.get("name") or ""),
            "市场": market,
            "信号日期": as_of,
            "告警类型": KIND_LABELS.get(str(a.get("kind")), str(a.get("kind"))),
            "详情": "; ".join(detail_parts),
            "收盘价": _r2(a.get("close")),
            # 扫描行的 chg_pct / zs_dist 已是百分数, 只取数不放大 (同 lark.py 修复)
            "涨跌幅%": _r2(a.get("chg_pct")),
            "中枢位置": _ZS_POS_LABEL.get(str(a.get("zs_pos") or ""), ""),
            "距中枢%": _r2(a.get("zs_dist")),
        })
    return out


_ZS_PREV_LABEL = {"above": "中枢上方", "inside": "中枢内", "below": "中枢下方"}
_ZS_POS_LABEL = _ZS_PREV_LABEL


def push(
    base_token: str,
    table_id: str,
    *,
    level: str = "1d",
    profile: str = "v1",
    lookback: int = 500,
    force: bool = False,
) -> dict:
    """检测 -> 推飞书 -> 成功后落基准。任何一步失败都带原因返回, 不抛异常。"""
    result = detect(level=level, profile=profile, lookback=lookback)
    alerts = result["alerts"]
    as_of = result["as_of"]
    out: dict[str, Any] = {
        "ok": False,
        "detected": len(alerts),
        "as_of": as_of,
        "pushed": 0,
        "skipped": 0,
        "details": [],
        "error": None,
        "force": force,
    }
    if not alerts:
        out["ok"] = True
        out["error"] = None
        out["details"].append("没有新告警")
        return out
    if not as_of:
        out["error"] = "扫描产物缺 last_date, 无法定信号日期"
        return out

    recs = records(alerts, as_of)
    res: PushResult = lark_push(
        base_token, table_id, recs, force=force, key_fields=ALERT_KEY_FIELDS,
    )
    out["ok"] = res.ok
    out["pushed"] = res.pushed
    out["skipped"] = res.skipped
    out["details"].extend(res.details)
    if res.error:
        out["error"] = res.error
    if res.ok and res.pushed > 0:
        n = ack(level=level, profile=profile, lookback=lookback)
        out["details"].append(f"已确认基准 ({n} 只)")
    return out
