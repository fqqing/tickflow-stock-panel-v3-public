"""飞书多维表格推送服务 —— 面板内选「策略 + 日期」驱动。

历史沿革
========
最早只有 ``backend/scripts/push_screener_to_lark.py``, 靠 Windows 计划任务每天
定时跑。问题在于表 / 策略 / 日期全写死在脚本里, 换一个就要改代码, 用户反馈
"定时注册不合适, 我想在前端选推哪个策略哪个日期"。于是把实现提到这里:

- 本模块是**唯一实现**, ``/api/lark`` (面板) 与 ``scripts/`` (命令行) 都复用,
  避免两份口径漂移;
- 取数走后端内部对象 (repo / strategy_engine), 不再 HTTP 自调用本机端口;
- 推送动作由前端显式触发, 去重仍按 (代码, 日期) 键, 同一天重复推安全。

字段口径备忘(与 qushiqinlong 源脚本推送的表结构对齐):
  - 涨跌幅%: screener 返回小数, 推送时 x100 (表内是百分数数值)。
  - 趋势擒龙表 MA13: enriched 无 ma13 (只有 ma5/10/20/30/60), 置空。
  - 趋势擒龙表 资金动能: 与 /api/kline/daily?indicators=capital_momentum 同口径
    (RS/RS_MA52-1, 已乘 10), 这里直接用 formula_signals.capital_momentum 算。
  - 底部结构表 信号状态/钝化类型: 策略矩阵内部值, 输出行不携带, 置空。
  - 启动策略表 (2026-10-01 新建, 表结构由本模块定义): 代码/名称/市场/信号日期/
    收盘价/涨跌幅%/量比5日/换手率%/20日动量%/MA5/MA20/评分。换手率 enriched 已是
    百分数, 涨跌幅与 20日动量是小数需 x100。
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from app.services import lark_bitable as lb

logger = logging.getLogger("lark_screener")

# ---------------------------------------------------------------------------
# 数据源 -> 目标表 映射
#   前三个沿用 qushiqinlong 三表 (走策略引擎 run_preset 同源口径);
#   abnormal 走异动监控 (交易所异动规则口径), 表字段各不相同,
#   故 key_fields / date_fields 按表配置而非常量。
# ---------------------------------------------------------------------------
STRATEGY_TABLES: dict[str, dict[str, Any]] = {
    "bottom_structure": {
        "label": "底部结构",
        "base_token": "EEYSbsdLpa9QkZsmGyVc7vCCnbb",
        "table_id": "tbl8TfrGiJYfzDd7",
    },
    "upward_trend_breakout": {
        "label": "向上趋势并突破",
        "base_token": "E1yLbkvgQaO28rssEbAca1twnxc",
        "table_id": "tbl9qX3qJx0Wr5vf",
    },
    "trend_dragon": {
        "label": "趋势擒龙",
        "base_token": "S4lKbOf6TaQ7A2sFw4hcDbyanFE",
        "table_id": "tbl8ZWQKMgGqyawK",
    },
    "abnormal": {
        "label": "异动预警",
        "base_token": "G5pgbvQSIaJHu6sJibNcK43Inkb",
        "table_id": "tbllEDcfkJ6JR06M",
        "key_fields": ("代码", "日期"),
        "date_fields": ("日期",),
    },
    # 启动策略: 2026-10-01 新建的独立表(字段由本模块定义, 见 _startup_records)。
    # 此前该策略一直留空 —— 用户给的 G5pg.../tbllED... 实测是异动预警口径
    # (触发信号次数/所需最小涨幅), 与选股字段完全不同, 不能复用。
    "startup_surge": {
        "label": "启动策略",
        "base_token": "RSYubVkH8aJajys5sh2c6NGAn62",
        "table_id": "tbl1VE8bY2Jos8RY",
    },
}

_KEY_FIELDS = ("代码", "信号日期")
_DATE_FIELDS = ("信号日期",)

# 板块 -> 单日涨跌幅上限 (判断「下一日是否可能触发」时用它封顶)
_LIMIT_UP_BY_BOARD = {"主板": 0.10, "创业板/科创板": 0.20, "北交所": 0.30}

# 资金动能基准指数 (与 app/api/kline.py 同源, 这里复制一份避免 api -> service 反向依赖)
_BENCHMARK_INDEX_BY_EXCHANGE = {"SH": "000001.SH", "SZ": "399001.SZ", "BJ": "899050.BJ"}
_DEFAULT_BENCHMARK_INDEX = "000001.SH"
#: 取多少**日历日**的历史算资金动能 (52 交易日窗口需要约 3 个月日历日才够)。
_MOMENTUM_LOOKBACK_DAYS = 200


# ---------------------------------------------------------------------------
# 记录映射
# ---------------------------------------------------------------------------

def _abnormal_records(
    rows: list[dict], as_of: str, only_actionable: bool = True
) -> list[dict]:
    """异动边缘行 -> 异动预警表记录。

    口径(与表内历史数据对齐, 由表内既有记录反推):
      - 所需最小涨幅 = 目标窗口阈值 - 当前偏离值 (线性近似, 非复利)。
      - 目标等级 = 未触发窗口里「所需涨幅最小」的那个; 全触发则取偏离最大的窗口。
      - 下一日可能触发 = 所需涨幅 <= 该板块单日涨跌幅上限。
      - 是否异动类型 = 已触发窗口的列举, 形如 "10日涨跌幅异常(53.49%)"。

    only_actionable=True (默认) 时只留「明日可能触发」或「已触发」的行 ——
    否则会把「还需涨 133% 才够」这类无行动价值的噪音一起推 (实测默认口径下有
    近 200 条, 有效 actionable 只有几十条)。
    """
    records: list[dict] = []
    for r in rows:
        symbol = str(r.get("symbol") or "")
        code, _, market = symbol.partition(".")
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market, market.lower())
        windows = r.get("windows") or {}

        triggered: list[tuple[int, float]] = []
        pending: dict[int, float] = {}
        for key, w in windows.items():
            try:
                n = int(str(key).rstrip("d"))
            except ValueError:
                continue
            val = lb.to_num(w.get("value")) or 0.0
            thr = lb.to_num(w.get("threshold")) or 0.0
            if (lb.to_num(w.get("closeness")) or 0.0) >= 1.0:
                triggered.append((n, val))
            else:
                pending[n] = thr - val

        if pending:
            target_n = min(pending, key=lambda k: pending[k])
            need = pending[target_n]
        elif triggered:
            target_n = max(triggered, key=lambda t: t[1])[0]
            need = 0.0
        else:
            continue

        limit_up = _LIMIT_UP_BY_BOARD.get(str(r.get("board") or ""), 0.10)
        if only_actionable and need > limit_up and not triggered:
            continue
        desc = ", ".join(f"{n}日涨跌幅异常({v * 100:.2f}%)" for n, v in sorted(triggered))
        records.append({
            "代码": f"{prefix}{code}",
            "名称": str(r.get("name") or ""),
            "日期": as_of,
            "收盘价": lb.to_num(r.get("close")),
            "触发信号次数": len(triggered),
            "所需最小涨幅": round(need, 6),
            "是否异动类型": desc or None,
            "预警信息": f"明日若涨 {need * 100:.2f}% 将触发{target_n}日异动" if need > 0 else None,
            "目标等级": f"{target_n}日异动",
            "下一日可能触发": "True" if need <= limit_up else "False",
        })
    return records


def _pct(x: float | None) -> float | None:
    """小数涨跌幅 -> 百分数数值。"""
    v = lb.to_num(x)
    return round(v * 100, 4) if v is not None else None


def _r2(x: Any) -> float | None:
    """数值 -> 保留 2 位。表内数值列精度设为 2, 存原值会带一堆浮点尾巴, 先抹平。"""
    v = lb.to_num(x)
    return round(v, 2) if v is not None else None


def _bias_ma20(close: Any, ma20: Any) -> float | None:
    """乖离MA20% = (close / ma20 - 1) * 100。"""
    c, m = lb.to_num(close), lb.to_num(ma20)
    if c is None or m is None or m == 0:
        return None
    return round((c / m - 1) * 100, 4)


def build_records(
    strategy_id: str,
    rows: list[dict],
    as_of: str,
    momentum_map: dict[str, float | None] | None = None,
) -> list[dict]:
    """把 screener 行映射成目标表记录 (各表字段对齐 qushiqinlong 源脚本)。"""
    if strategy_id == "abnormal":
        return _abnormal_records(rows, as_of)
    records: list[dict] = []
    for r in rows:
        symbol = str(r.get("symbol") or "")
        code, _, market = symbol.partition(".")
        base = {
            "代码": code,
            "名称": str(r.get("name") or ""),
            "市场": market,
            "信号日期": as_of,
            "收盘价": lb.to_num(r.get("close")),
            "涨跌幅%": _pct(r.get("change_pct")),
        }
        if strategy_id == "trend_dragon":
            base.update({
                "MA5": lb.to_num(r.get("ma5")),
                "MA13": None,  # enriched 无 ma13
                "乖离MA20%": _bias_ma20(r.get("close"), r.get("ma20")),
                "资金动能": (momentum_map or {}).get(symbol),
            })
        elif strategy_id == "startup_surge":
            base.update({
                "量比5日": _r2(r.get("vol_ratio_5d")),
                "换手率%": _r2(r.get("turnover_rate")),
                "20日动量%": _pct(r.get("momentum_20d")),
                "MA5": _r2(r.get("ma5")),
                "MA20": _r2(r.get("ma20")),
                "评分": _r2(r.get("score")),
            })
        elif strategy_id == "bottom_structure":
            base.update({
                "信号状态": "",   # 策略矩阵内部值, 输出行不携带
                "DIF": lb.to_num(r.get("macd_dif")),
                "DEA": lb.to_num(r.get("macd_dea")),
                "钝化类型": "",
                "MA5": lb.to_num(r.get("ma5")),
                "MA20": lb.to_num(r.get("ma20")),
            })
        records.append(base)
    return records


def table_options() -> list[dict[str, Any]]:
    """给前端下拉用: 每个可选策略的中文名 + 飞书表是否已配置。"""
    return [
        {
            "id": sid,
            "label": str(cfg["label"]),
            "configured": bool(cfg.get("base_token") and cfg.get("table_id")),
        }
        for sid, cfg in STRATEGY_TABLES.items()
    ]


# ---------------------------------------------------------------------------
# 取数 (后端内部对象, 不走 HTTP)
# ---------------------------------------------------------------------------

def _as_date(value: Any) -> date | None:
    """str / date / datetime -> date, 其它 -> None。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _safe(result_dict: dict) -> dict:
    """sanitize for JSON (NaN / Inf -> None)。"""
    for r in result_dict.get("rows") or []:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return result_dict


def fetch_strategy_rows(
    repo: Any,
    engine: Any,
    data_dir: Any,
    strategy_id: str,
    as_of: Any = None,
    market: str = "cn",
) -> tuple[str, list[dict]]:
    """跑一次策略引擎, 返回 (as_of, rows)。口径与 /api/screener/run_preset 一致。"""
    from app.services.screener import ScreenerService
    from app.strategy import config as strategy_config

    # 先校验引擎: 比查库便宜, 且「引擎没起来」比「没数据」更该先报出来
    if engine is None:
        raise RuntimeError("策略引擎未初始化")
    if not engine.has(strategy_id):
        raise RuntimeError(f"unknown strategy: {strategy_id}")
    svc = ScreenerService(repo, asset_type="stock", market=market)
    day = _as_date(as_of) or svc.latest_date()
    if not day:
        raise RuntimeError("无可用数据日期 — enriched 表为空, 请先运行盘后管道")

    overrides = strategy_config.load_override(data_dir, strategy_id)
    params = dict(overrides.get("params") or {})
    context = svc.build_strategy_context(
        engine,
        day,
        [strategy_id],
        timeframe="1d",
        params_map={strategy_id: params},
        overrides_map={strategy_id: overrides or {}},
    )
    result = engine.run(strategy_id, context, params=params, overrides=overrides or None)
    safe_data = _safe(asdict(result))
    return str(day), list(safe_data.get("rows") or [])


def fetch_abnormal_rows(
    repo: Any, quote_service: Any = None, min_closeness: float = 0.7
) -> tuple[str, list[dict]]:
    """异动边缘行, 返回 (as_of, rows)。

    min_closeness 是「接近度」下限 (|偏离|/阈值): 0.5 观察 / 0.7 边缘 / 1.0 已触发。
    默认 0.7 —— 0.5 会把「才刚过半程」的一起拉进来, 实测全市场 187 行(过滤后仍有
    106 条), 与该表历史每天几条的量级不符; 0.7 时约 59 条。
    """
    from app.services.abnormal_moves import build_overview

    resp = build_overview(repo, quote_service, min_closeness=min_closeness, limit=500)
    return str(resp.get("cache_date") or "")[:10], list(resp.get("rows") or [])


def _capital_momentum(repo: Any, symbol: str, end: date) -> float | None:
    """取某只股票截至 end 的资金动能 cm_value (末根)。失败返回 None, 不阻断主流程。"""
    from app.indicators.formula_signals import (
        CAPITAL_MOMENTUM_WINDOW,
        capital_momentum,
    )

    start = end - timedelta(days=_MOMENTUM_LOOKBACK_DAYS)
    index_symbol = _BENCHMARK_INDEX_BY_EXCHANGE.get(
        symbol.partition(".")[2].upper(), _DEFAULT_BENCHMARK_INDEX
    )
    try:
        stock_df = repo.get_daily_asset("stock", symbol, start, end, columns=["date", "close"])
        index_df = repo.get_daily_asset("index", index_symbol, start, end, columns=["date", "close"])
    except Exception as e:  # pragma: no cover - 依赖仓储实现, 失败只降级
        logger.debug("资金动能取数失败 %s: %s", symbol, e)
        return None
    if stock_df.is_empty() or index_df.is_empty():
        return None

    def _map(df: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        for d, c in df.select(["date", "close"]).iter_rows():
            if c is None or not math.isfinite(float(c)) or float(c) <= 0:
                continue
            out[str(d)[:10]] = float(c)
        return out

    stock_map, index_map = _map(stock_df), _map(index_df)
    common = sorted(d for d in stock_map if d in index_map)
    if len(common) < CAPITAL_MOMENTUM_WINDOW:
        return None
    values = capital_momentum(
        np.array([stock_map[d] for d in common], dtype=np.float64),
        np.array([index_map[d] for d in common], dtype=np.float64),
    )
    last = float(values[-1])
    return round(last, 4) if math.isfinite(last) else None


# ---------------------------------------------------------------------------
# 执行推送
# ---------------------------------------------------------------------------

def push_records_for(strategy_id: str, records: list[dict], force: bool = False) -> lb.PushResult:
    """按策略找到目标表并执行推送。"""
    cfg = STRATEGY_TABLES[strategy_id]
    return lb.push_records(
        cfg["base_token"],
        cfg["table_id"],
        records,
        key_fields=cfg.get("key_fields", _KEY_FIELDS),
        force=force,
        date_fields=cfg.get("date_fields", _DATE_FIELDS),
    )


def run_push(
    strategy_id: str,
    *,
    repo: Any,
    engine: Any = None,
    quote_service: Any = None,
    data_dir: Any = None,
    as_of: Any = None,
    force: bool = False,
    dry_run: bool = False,
    min_closeness: float = 0.7,
    with_momentum: bool = True,
    market: str = "cn",
) -> dict[str, Any]:
    """推送单个策略到飞书, 返回可 JSON 序列化的结果摘要。

    永不抛异常 —— 面板需要把失败原因展示给用户, 而不是弹一个 500。
    """
    cfg = STRATEGY_TABLES.get(strategy_id)
    if cfg is None:
        return {
            "ok": False, "strategy_id": strategy_id, "label": strategy_id,
            "error": f"未知策略: {strategy_id} (可选: {list(STRATEGY_TABLES)})",
            "pushed": 0, "skipped": 0, "selected": 0, "details": [],
        }
    label = str(cfg["label"])
    out: dict[str, Any] = {
        "ok": False, "strategy_id": strategy_id, "label": label,
        "as_of": None, "selected": 0, "pushed": 0, "skipped": 0,
        "details": [], "error": None, "dry_run": dry_run,
    }
    if not cfg.get("base_token") or not cfg.get("table_id"):
        out["error"] = "飞书表未配置(base_token/table_id 为空), 已跳过"
        out["details"].append("请在 backend/app/services/lark_screener.py 的 STRATEGY_TABLES 补上该表")
        return out

    try:
        if strategy_id == "abnormal":
            as_of_day, rows = fetch_abnormal_rows(repo, quote_service, min_closeness)
        else:
            as_of_day, rows = fetch_strategy_rows(repo, engine, data_dir, strategy_id, as_of, market)
    except Exception as e:  # pragma: no cover - 兜底, 面板要看到原因而不是 500
        logger.warning("[%s] 取数失败: %s", label, e)
        out["error"] = f"取数失败: {e}"
        return out

    out["as_of"] = as_of_day
    out["selected"] = len(rows)
    if not rows:
        out["error"] = f"{as_of_day or '该日期'} 无选中个股"
        return out

    momentum_map: dict[str, float | None] | None = None
    if strategy_id == "trend_dragon" and with_momentum:
        momentum_map = {}
        end = _as_date(as_of_day) or date.today()
        for r in rows:
            symbol = str(r.get("symbol") or "")
            momentum_map[symbol] = _capital_momentum(repo, symbol, end)

    records = build_records(strategy_id, rows, as_of_day, momentum_map)
    if dry_run:
        out["ok"] = True
        out["sample"] = records[:5]
        out["details"].append(f"试运行: 共 {len(records)} 条, 未写入飞书")
        return out

    result = push_records_for(strategy_id, records, force=force)
    out["pushed"] = result.pushed
    out["skipped"] = result.skipped
    out["details"].extend(result.details)
    if not result.ok:
        out["error"] = result.error or "推送失败"
        return out
    out["ok"] = True
    return out
