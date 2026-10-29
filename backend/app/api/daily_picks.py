"""每日精选 API — 选股触发 + 今日/历史/命中率查询。"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services.daily_picks import DailyPicksService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daily-picks", tags=["daily-picks"])


class RunRequest(BaseModel):
    top_n: int = 6
    min_family_count: int = 3
    hold_days: int = 1
    as_of: Optional[date] = None
    min_listing_days: Optional[int] = None  # 次新股过滤阈值(自然日), None=用服务默认 60


def _svc(request: Request) -> DailyPicksService:
    svc = getattr(request.app.state, "daily_picks_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="每日精选服务未初始化")
    return svc


@router.post("/run")
def run_picks(req: RunRequest, request: Request):
    """手动触发一次选股(盘后管道已定时调用, 这里供调试/补跑)。"""
    svc = _svc(request)
    ml = req.min_listing_days if req.min_listing_days is not None else 60
    rows = svc.run(
        as_of=req.as_of, top_n=req.top_n, min_family_count=req.min_family_count,
        hold_days=req.hold_days, min_listing_days=ml,
    )
    return {
        "pick_date": rows[0]["pick_date"] if rows else None,
        "picks": rows,
        "total": len(rows),
    }


@router.get("/today")
def today(request: Request):
    """最新一期精选(先结算到期条目)。"""
    svc = _svc(request)
    svc.settle_pending()
    return svc.today()


@router.get("/history")
def history(request: Request, limit: int = Query(200, ge=1, le=1000)):
    """历史精选(含命中标记), 按日期降序。"""
    svc = _svc(request)
    svc.settle_pending()
    return {"picks": svc.history(limit=limit)}


@router.get("/stats")
def stats(request: Request):
    """命中率统计。"""
    svc = _svc(request)
    svc.settle_pending()
    return svc.stats()


@router.post("/push")
def push_to_lark(request: Request):
    """手动把最新一期精选推送到飞书, 并回写已结算条目的命中信息。"""
    svc = _svc(request)
    svc.settle_pending()
    latest = svc.today()
    push_res = svc.push_rows_to_lark(latest["picks"])
    sync_res = svc.sync_settlement_to_lark()
    return {"push": push_res, "sync": sync_res}
