"""信号实验室 (Signal Lab) API — 信号台账 / 战绩汇总 / 形态归因。

回答的是「这些信号本身后来怎么走」, 与 ``/api/backtest``(这套资金规则能赚多少)互补:
回测只统计实际成交的那部分信号, 受持仓名额和评分排序截断; 本 API 统计**全部**信号,
因此样本量大一个量级, 可以反复用不同持有期/止损分组复盘, 而不必重算信号。

端点
----
- ``GET  /api/signallab/strategies``  可复盘的策略(仅 matrix_native)
- ``GET  /api/signallab/datasets``    已落盘的台账数据集
- ``POST /api/signallab/runs``        触发一次复盘(异步, 返回 run_id)
- ``GET  /api/signallab/runs``        最近任务
- ``GET  /api/signallab/runs/{id}``   复盘进度
- ``GET  /api/signallab/outcomes``    台账明细(分页)
- ``GET  /api/signallab/summary``     战绩汇总(可分组)
- ``GET  /api/signallab/attribution`` 形态归因(按特征分桶)
- ``GET  /api/signallab/score-today`` 当日候选打分(把归因学到的档位搬到今天)
- ``POST /api/signallab/attribution-insight`` AI 解读归因(流式 NDJSON; 正文 delta 之外
  会多一个 ``suggestions`` 事件, 给出可直接送进参数网格搜索的参数范围)

口径见 ``app/signallab/outcome.py`` 顶部: 收益小数制、T+1 开盘成交、停牌/涨停封死顺延、
前瞻不足记 null 且不进分母。
"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging
import math
import re
from datetime import date, timedelta
from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.plugins.eltdx.provider import market_meta
from app.signallab.insight import analyze_attribution_stream
from app.signallab.lab import (
    CONTEXT_FEATURES,
    DEFAULT_ATTRIBUTION_FEATURES,
    LabRunConfig,
    list_datasets,
    load_ledger,
    resolve_dataset,
    run_lab,
)
from app.signallab.outcome import DEFAULT_HORIZONS, ret_column
from app.signallab.runs import run_store
from app.signallab.scoring import (
    CONTEXT_FEATURES as SCORING_FEATURES,
)
from app.signallab.scoring import score_today
from app.signallab.summary import attribute_outcomes, summarize_outcomes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/signallab", tags=["signallab"])

# 复盘是分钟级的 CPU 任务, 独占一个单线程池: 与数据同步互不干扰, 也不会被重复提交压垮。
_lab_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="signallab")

_RET_COL_RE = re.compile(r"^ret_(\d+)d$")

# 后台任务句柄持有处: 裸 asyncio.create_task 的返回值若被丢弃, 任务可能在未完成前被 GC
# (RUF006)。这里持有引用, 完成后自动摘除。
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    handle = asyncio.create_task(coro)
    _background_tasks.add(handle)
    handle.add_done_callback(_background_tasks.discard)
    return handle


class LabRunRequest(BaseModel):
    """触发一次复盘。除 strategy_id 外都可省略(给前端默认值)。"""

    strategy_id: str
    start: date | None = None
    end: date | None = None
    lookback_days: int = Field(320, ge=20, le=2000)
    limit: int = Field(300, ge=1, le=6000, description="未给 symbols 时按字典序取前 N 只标的")
    symbols: list[str] | None = None
    horizons: list[int] = Field(default_factory=lambda: list(DEFAULT_HORIZONS))
    entry_delay: int = Field(1, ge=0, le=10)
    max_delay_days: int = Field(3, ge=0, le=20)
    stop_loss: float | None = Field(None, description="负小数, 如 -0.06")
    take_profit: float | None = Field(None, description="正小数, 如 0.15")
    drop_warmup: int | None = Field(
        None, ge=0, le=1000,
        description="额外丢弃开头若干行; 默认按矩阵前置预热的缺口自动判定",
    )
    matrix_cache_mb: int = Field(768, ge=64, le=8192)
    with_features: bool = True

    @field_validator("horizons")
    @classmethod
    def _positive_horizons(cls, value: list[int]) -> list[int]:
        """持有期必须是正整数且至少一个(否则台账里一列收益都没有, 后续全是无意义的空表)。"""
        cleaned = sorted({int(item) for item in value if int(item) > 0})
        if not cleaned:
            raise ValueError("horizons 至少需要 1 个正的持有期")
        return cleaned

    @field_validator("stop_loss")
    @classmethod
    def _negative_stop_loss(cls, value: float | None) -> float | None:
        if value is not None and value >= 0:
            raise ValueError("stop_loss 须为负小数, 如 -0.06")
        return value

    @field_validator("take_profit")
    @classmethod
    def _positive_take_profit(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("take_profit 须为正小数, 如 0.15")
        return value


# ===== 序列化 =====


def _json_ready(frame: pl.DataFrame, *, round_to: int | None = None) -> list[dict[str, Any]]:
    """polars -> JSON 安全的 dict 列表。

    NaN/inf 必须转成 null: JSON 标准里没有 NaN, starlette 会直接把它序列化成裸 ``NaN``
    字面量, 前端 JSON.parse 直接抛错 —— 而「某只票前瞻窗口不足」恰恰会产生 NaN。
    """
    if frame.is_empty():
        return []
    expressions = []
    for name, dtype in frame.schema.items():
        column = pl.col(name)
        if dtype in (pl.Float32, pl.Float64):
            safe = pl.when(column.is_finite()).then(column).otherwise(None)
            if round_to is not None:
                safe = safe.round(round_to)
            expressions.append(safe.alias(name))
        elif dtype in (pl.Date, pl.Datetime):
            expressions.append(column.cast(pl.Utf8).alias(name))
        elif dtype == pl.Boolean:
            expressions.append(column.alias(name))
    if expressions:
        frame = frame.with_columns(expressions)
    records = frame.to_dicts()
    if round_to is None:
        # float 兜底(理论上已被上面清干净, 防御自定义列)
        return [{k: _clean_scalar(v) for k, v in row.items()} for row in records]
    return records


def _clean_scalar(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _attach_names(frame: pl.DataFrame) -> pl.DataFrame:
    """给台账补 ``name`` 列(代码 -> 中文名称)。

    台账是策略跑出来的, 上游只回代码。前端要显示名称就必须在这里补 —— 本地标的
    维表带 mtime 缓存, 在请求路径上调用是安全的。已有 name 列(上游带出来了)则不动。
    """
    if frame.is_empty() or "symbol" not in frame.columns or "name" in frame.columns:
        return frame
    meta = market_meta()
    names = [(meta.get(str(s)) or {}).get("name") for s in frame["symbol"].to_list()]
    return frame.with_columns(pl.Series("name", names, dtype=pl.Utf8))


def _fill_names(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """dict 列表版的名称兜底: 只在缺 name / name 为空时补, 不覆盖上游已有值。"""
    need = [str(r["symbol"]) for r in rows if r.get("symbol") and not r.get("name")]
    if not need:
        return rows
    meta = market_meta()
    for row in rows:
        if row.get("symbol") and not row.get("name"):
            row["name"] = (meta.get(str(row["symbol"])) or {}).get("name")
    return rows


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


def _dataset_meta(data_dir, strategy_id: str, start: date | None = None,
                  end: date | None = None) -> dict[str, Any] | None:
    path = resolve_dataset(data_dir, strategy_id, start, end)
    if path is None:
        return None
    for item in list_datasets(data_dir, strategy_id=strategy_id):
        if item["path"] == str(path):
            return item
    return {"strategy_id": strategy_id, "path": str(path)}


def _require_ledger(data_dir, strategy_id: str, start: date | None, end: date | None
                    ) -> tuple[pl.DataFrame, dict[str, Any]]:
    frame = load_ledger(data_dir, strategy_id, start, end)
    if frame is None:
        raise HTTPException(
            status_code=404,
            detail=f"策略 {strategy_id} 没有可用的台账数据集, 请先 POST /api/signallab/runs 跑一次",
        )
    meta = _dataset_meta(data_dir, strategy_id, start, end) or {
        "strategy_id": strategy_id,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }
    return frame, meta


def _horizons_of(frame: pl.DataFrame) -> list[int]:
    found = []
    for column in frame.columns:
        match = _RET_COL_RE.match(column)
        if match:
            found.append(int(match.group(1)))
    return sorted(found)


# ===== 元信息 =====


@router.get("/strategies")
def list_lab_strategies(request: Request) -> dict[str, Any]:
    """可复盘的策略清单。

    只有 ``matrix_native`` 策略能进 Signal Lab: 表达式/Python 后端策略没有
    ``matrix_strategy``, 拿不到全区间的信号矩阵。
    """
    engine = request.app.state.strategy_engine
    items: list[dict[str, Any]] = []
    for strategy in engine.strategy_definitions():
        meta = getattr(strategy, "meta", {}) or {}
        if meta.get("research_only"):
            continue
        # StrategyDef 没有 .id/.name 属性, 标识一律在 meta 里(与 list_strategies 一致)。
        sid = meta.get("id") or ""
        warmup = None
        try:
            warmup = engine.required_history_bars([sid])
        except Exception:
            warmup = getattr(strategy, "warmup_bars", None)
        items.append({
            "id": sid,
            "name": meta.get("name", sid),
            "execution_backend": getattr(strategy, "execution_backend", None),
            "supported": getattr(strategy, "execution_backend", None) == "matrix_native",
            "warmup_bars": int(warmup) if warmup else None,
            "category": meta.get("category"),
        })
    items.sort(key=lambda item: (not item["supported"], item["id"]))
    return {"strategies": items, "context_features": list(CONTEXT_FEATURES)}


@router.get("/datasets")
def list_lab_datasets(
    request: Request,
    strategy_id: str | None = Query(None),
) -> dict[str, Any]:
    """已落盘的台账数据集(按修改时间从新到旧)。"""
    data_dir = _data_dir(request)
    datasets = list_datasets(data_dir, strategy_id=strategy_id)
    for item in datasets:
        path = item.get("path")
        if not path:
            continue
        try:
            schema = pl.read_parquet_schema(path)  # type: ignore[attr-defined]
        except Exception:
            schema = None
        if schema:
            item["columns"] = list(schema.keys())
            item["horizons"] = sorted(
                int(m.group(1)) for m in (_RET_COL_RE.match(c) for c in schema) if m
            )
    return {"datasets": datasets}


# ===== 复盘任务 =====


@router.post("/runs")
async def start_lab_run(request: Request, payload: LabRunRequest) -> dict[str, Any]:
    """触发一次复盘, 立即返回 run_id, 前端轮询 /runs/{id} 拿进度。

    复盘是 CPU 密集 + 大内存(matrix 全区间信号), 已在跑时**复用**当前任务而不是
    再开一个 —— 两个全市场矩阵同时算会直接把内存打满。
    """
    repo = request.app.state.repo
    engine = request.app.state.strategy_engine
    data_dir = _data_dir(request)

    if not engine.has(payload.strategy_id):
        raise HTTPException(status_code=404, detail=f"unknown strategy: {payload.strategy_id}")
    strategy = engine.get(payload.strategy_id)
    if getattr(strategy, "execution_backend", None) != "matrix_native":
        raise HTTPException(
            status_code=400,
            detail=f"策略 {payload.strategy_id} 不是 matrix_native, Signal Lab 不支持",
        )

    _, latest = repo.get_enriched_latest()
    if latest is None:
        raise HTTPException(status_code=503, detail="enriched 数据不可用, 请先完成数据同步")
    end = payload.end or latest
    start = payload.start or end - timedelta(days=int(payload.lookback_days * 1.6))

    horizons = tuple(sorted({int(h) for h in payload.horizons if h > 0}))
    if not horizons:
        raise HTTPException(status_code=400, detail="horizons 必须至少有一个正持有期")

    # ⚠️ 标的池必须在建 config 之前解析: 给了 limit 却不传 symbols 的话,
    # StrategyBacktestConfig 会按 symbols=None 走全市场(5400 只), 与用户意图的
    # 「先小样本试算」完全相反 —— 慢十倍且内存翻几倍。
    symbols = _resolve_symbols(repo, payload)

    config = LabRunConfig(
        strategy_id=payload.strategy_id,
        start=start,
        end=end,
        symbols=tuple(symbols) if symbols else None,
        limit=payload.limit,
        horizons=horizons,
        entry_delay=payload.entry_delay,
        max_delay_days=payload.max_delay_days,
        stop_loss=payload.stop_loss,
        take_profit=payload.take_profit,
        drop_warmup=payload.drop_warmup,
        matrix_cache_mb=payload.matrix_cache_mb,
        with_features=payload.with_features,
    )

    run_id, is_new = run_store.create({
        "strategy_id": payload.strategy_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "horizons": list(horizons),
        "limit": payload.limit,
        "n_symbols": len(symbols) if symbols else None,
    })
    if not is_new:
        return {"run_id": run_id, "reused": True}

    async def task() -> None:
        def progress(stage: str, pct: int, msg: str) -> None:
            run_store.progress(run_id, stage, pct, msg)

        try:
            run_store.start(run_id)
            loop = asyncio.get_event_loop()

            def _run() -> pl.DataFrame:
                return run_lab(
                    repo, engine, config, data_dir=data_dir, on_progress=progress
                )

            frame = await loop.run_in_executor(_lab_executor, _run)
            run_store.succeed(run_id, {
                "strategy_id": payload.strategy_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "rows": frame.height,
                "columns": frame.columns,
                "horizons": list(horizons),
                "n_filled": int(frame["filled"].sum()) if "filled" in frame.columns else None,
            })
        except Exception as exc:
            logger.exception("signal lab run failed")
            run_store.fail(run_id, str(exc))

    _spawn(task())
    return {
        "run_id": run_id,
        "reused": False,
        "strategy_id": payload.strategy_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": len(symbols) if symbols else None,
    }


def _resolve_symbols(repo, payload: LabRunRequest) -> list[str] | None:
    if payload.symbols:
        return list(payload.symbols)
    if payload.limit and payload.limit > 0:
        instruments = repo.get_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return None
        column = instruments["symbol"]
        if "asset_type" in instruments.columns:
            column = instruments.filter(pl.col("asset_type") == "stock")["symbol"]
        values = sorted({str(v) for v in column.to_list() if v})
        return values[: payload.limit]
    return None


@router.get("/runs")
def list_lab_runs(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    return {"active_id": run_store.active_id(), "runs": run_store.list_recent(limit=limit)}


@router.get("/runs/{run_id}")
def get_lab_run(run_id: str) -> dict[str, Any]:
    run = run_store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


# ===== 查询 =====


@router.get("/outcomes")
def get_outcomes(
    request: Request,
    strategy_id: str = Query(...),
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    symbol: str | None = Query(None),
    filled_only: bool = Query(True),
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    sort: str = Query("signal_date", pattern="^(signal_date|fill_date|symbol)$"),
    descending: bool = Query(True),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """台账明细。默认只返回已成交样本(未成交的收益全是 null, 摆在表里是噪音)。"""
    data_dir = _data_dir(request)
    frame, meta = _require_ledger(data_dir, strategy_id, start, end)
    if filled_only and "filled" in frame.columns:
        frame = frame.filter(pl.col("filled"))
    if symbol:
        frame = frame.filter(pl.col("symbol") == symbol)
    if date_from:
        frame = frame.filter(pl.col("signal_date") >= date_from)
    if date_to:
        frame = frame.filter(pl.col("signal_date") <= date_to)
    total = frame.height
    if total and sort in frame.columns:
        frame = frame.sort(sort, descending=descending)
    page = frame.slice(offset, limit)
    page = _attach_names(page)
    return {
        "dataset": meta,
        "total": total,
        "offset": offset,
        "limit": limit,
        "columns": page.columns,
        "rows": _json_ready(page, round_to=6),
    }


@router.get("/summary")
def get_summary(
    request: Request,
    strategy_id: str = Query(...),
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    horizons: str | None = Query(None, description="逗号分隔, 默认台账里的全部持有期"),
    group_by: str | None = Query(None, description="分组列, 如 entry_signal_name"),
    with_excess: bool = Query(True),
) -> dict[str, Any]:
    """战绩汇总: 样本数 / 胜率 / 期望收益 / 盈亏比 / MFE-MAE。

    null 收益(停牌、前瞻不足)不进分母, 也不按 0 补齐 —— 「没观测到」≠「收益 0%」。
    """
    data_dir = _data_dir(request)
    frame, meta = _require_ledger(data_dir, strategy_id, start, end)
    available = _horizons_of(frame)
    if horizons:
        wanted = [int(h) for h in horizons.split(",") if h.strip()]
        unknown = [h for h in wanted if h not in available]
        if unknown:
            raise HTTPException(status_code=400, detail=f"台账没有持有期 {unknown}, 可用 {available}")
        use = wanted
    else:
        use = available or list(DEFAULT_HORIZONS)

    groups: list[dict[str, Any]] = []
    if group_by:
        for column in [c.strip() for c in group_by.split(",") if c.strip()]:
            if column not in frame.columns:
                raise HTTPException(status_code=400, detail=f"台账没有列 {column}")
        grouped = summarize_outcomes(
            frame, horizons=use, group_by=[c.strip() for c in group_by.split(",")],
            with_excess=with_excess,
        )
        groups = _json_ready(grouped, round_to=6)
    overall = summarize_outcomes(frame, horizons=use, with_excess=with_excess)
    overall_rows = _json_ready(overall, round_to=6)
    return {
        "dataset": meta,
        "horizons": use,
        "n_signals": frame.height,
        "overall": overall_rows[0] if overall_rows else {},
        "groups": groups,
    }


@router.get("/attribution")
def get_attribution(
    request: Request,
    strategy_id: str = Query(...),
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    features: str | None = Query(None, description="逗号分隔; 默认信号分支 + 形态特征"),
    horizon: int | None = Query(None, description="统计哪个持有期, 默认台账里最长的"),
    min_samples: int = Query(20, ge=1, le=10000),
    buckets: int = Query(4, ge=2, le=10),
    top_n: int = Query(0, ge=0, le=20),
) -> dict[str, Any]:
    """形态归因: 按特征分桶看哪个档位的信号更好。

    ⚠️ 特征必须是**信号当时已知**的量。台账里的 ``mfe/mae/ret_*/exc_*`` 都含未来信息,
    传进来会得到必然赚钱的假结论(这里不做拦截, 但前端只暴露 ctx_* 与 entry_signal_name)。
    """
    data_dir = _data_dir(request)
    frame, meta = _require_ledger(data_dir, strategy_id, start, end)
    available = _horizons_of(frame)
    if not available:
        raise HTTPException(status_code=400, detail="台账里没有收益列, 无法归因")
    target_horizon = horizon if horizon in available else max(available)
    if horizon is not None and horizon not in available:
        # 请求了不存在的持有期: 退回最长的并明确告知, 避免前端拿着别的口径当原口径展示
        logger.info("attribution horizon %s 不可用, 回退到 %s", horizon, target_horizon)

    wanted = (
        [f.strip() for f in features.split(",") if f.strip()]
        if features
        else list(DEFAULT_ATTRIBUTION_FEATURES)
    )
    usable = [f for f in wanted if f in frame.columns]
    if not usable:
        raise HTTPException(
            status_code=400,
            detail=f"台账没有可用特征列, 请求 {wanted}, 台账列 {frame.columns}",
        )
    result = attribute_outcomes(
        frame,
        usable,
        target_horizon,
        column=ret_column(target_horizon),
        buckets=buckets,
        min_samples=min_samples,
        top_n=top_n,
    )
    return {
        "dataset": meta,
        "horizon": target_horizon,
        "requested_horizon": horizon,
        "features": usable,
        "skipped_features": [f for f in wanted if f not in frame.columns],
        "rows": _json_ready(result, round_to=6),
    }


@router.get("/score-today")
def get_score_today(
    request: Request,
    strategy_id: str = Query(...),
    as_of: Annotated[date | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    horizon: int | None = Query(None, description="按哪个持有期的历史表现打分, 默认台账最长"),
    features: str | None = Query(None, description="逗号分隔; 默认 CONTEXT_FEATURES"),
    buckets: int = Query(4, ge=2, le=10),
    min_samples: int = Query(20, ge=1, le=10000),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """当日候选打分: 用台账归因学到的「形态档位 -> 历史收益」给今天选出的票排序。

    与 :func:`get_attribution` 是同一个模型的两面 —— 归因给出「哪个档位好」,
    这里把今天选出的票投到档位上, 谁落在历史赚钱的档位谁排前面。

    ⚠️ 打分只改排序, **不改选股结果**: 候选池仍是策略自己选出的那批票。
    """
    repo = request.app.state.repo
    engine = request.app.state.strategy_engine
    data_dir = _data_dir(request)
    frame, meta = _require_ledger(data_dir, strategy_id, start, end)

    available = _horizons_of(frame)
    if not available:
        raise HTTPException(status_code=400, detail="台账里没有收益列, 无法打分")
    target_horizon = horizon if horizon in available else max(available)

    _, latest = repo.get_enriched_latest()
    day = as_of or latest
    if day is None:
        raise HTTPException(status_code=503, detail="enriched 数据不可用, 请先完成数据同步")

    wanted = (
        [f.strip() for f in features.split(",") if f.strip()]
        if features
        else list(SCORING_FEATURES)
    )
    try:
        result = score_today(
            repo,
            engine,
            data_dir,
            frame,
            strategy_id,
            day,
            horizon=target_horizon,
            features=wanted,
            buckets=buckets,
            min_samples=min_samples,
            limit=limit,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    result["dataset"] = meta
    result["requested_horizon"] = horizon
    result["rows"] = _fill_names(list(result.get("rows") or []))
    return result


class InsightRequest(BaseModel):
    """AI 形态归因解读请求。"""

    strategy_id: str
    start: date | None = None
    end: date | None = None
    horizon: int | None = None
    features: list[str] | None = None
    buckets: int = Field(4, ge=2, le=10)
    min_samples: int = Field(20, ge=1, le=10000)
    focus: str = ""


@router.post("/attribution-insight")
async def analyze_attribution(request: Request, payload: InsightRequest):
    """AI 形态归因解读 — NDJSON 流式返回。

    事实(分桶统计)由后端算好塞进提示词, LLM 只做解读与参数建议, 不负责算数。
    协议同 ``/api/rps/rotation-analyze``: meta / delta / error / done。
    """
    data_dir = _data_dir(request)
    frame, meta = _require_ledger(data_dir, payload.strategy_id, payload.start, payload.end)
    available = _horizons_of(frame)
    if not available:
        raise HTTPException(status_code=400, detail="台账里没有收益列, 无法归因")
    horizon = payload.horizon if payload.horizon in available else max(available)

    engine = request.app.state.strategy_engine
    params: list[dict] = []
    name = payload.strategy_id
    desc = ""
    if engine is not None and engine.has(payload.strategy_id):
        strategy_meta = getattr(engine.get(payload.strategy_id), "meta", {}) or {}
        params = list(strategy_meta.get("params") or [])
        name = strategy_meta.get("name") or payload.strategy_id
        desc = str(strategy_meta.get("description") or "")

    async def stream_gen():
        async for chunk in analyze_attribution_stream(
            frame,
            meta,
            strategy_id=payload.strategy_id,
            strategy_name=name,
            strategy_desc=desc,
            params=params,
            horizon=horizon,
            features=payload.features,
            buckets=payload.buckets,
            min_samples=payload.min_samples,
            focus=payload.focus,
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
