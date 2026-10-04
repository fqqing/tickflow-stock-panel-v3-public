"""飞书多维表格推送 API —— 面板上选「策略 + 日期」即时推送。

取代原先「Windows 计划任务定时跑 scripts/push_screener_to_lark.py」的方式:
策略与日期写死在脚本里, 想换就得改代码, 且跑没跑、推了几条全靠翻日志。
现在由前端显式触发, 结果直接回给面板。
"""
from __future__ import annotations

import logging
import os
import shutil

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.services import lark_bitable as lb
from app.services import lark_screener as lark_screener

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lark", tags=["lark"])


class PushRequest(BaseModel):
    strategy_id: str = Field(..., description="策略 id 或 abnormal")
    as_of: str | None = Field(None, description="交易日 YYYY-MM-DD, 空则取最新")
    force: bool = Field(False, description="跳过去重强制全量推送")
    dry_run: bool = Field(False, description="只算记录不写入飞书")
    min_closeness: float = Field(0.7, ge=0.0, le=1.0, description="仅 abnormal: 接近度下限")
    with_momentum: bool = Field(True, description="仅 trend_dragon: 是否补资金动能")
    market: str = Field("cn", description="cn|hk|us")


@router.get("/status")
def status():
    """lark-cli 是否可用 —— 不可用时前端直接禁用推送按钮并给出提示。"""
    cli = lb.find_lark_cli()
    available = bool(shutil.which(cli) or (os.path.isabs(cli) and os.path.exists(cli)))
    return {"cli": cli, "available": available}


@router.get("/tables")
def tables():
    """可推送的策略清单 + 各表是否已配置。"""
    return {"tables": lark_screener.table_options()}


@router.post("/push")
def push(req: PushRequest, request: Request):
    """推送指定策略在指定日期的选股结果到飞书。

    同步执行(端点用 def, FastAPI 放线程池, 不阻塞事件循环)。策略计算 + 去重
    读表 + 写表通常几十秒, 前端走 loading 态等待。
    """
    repo = request.app.state.repo
    engine = getattr(request.app.state, "strategy_engine", None)
    quote_service = getattr(request.app.state, "quote_service", None)
    data_dir = repo.store.data_dir
    result = lark_screener.run_push(
        req.strategy_id,
        repo=repo,
        engine=engine,
        quote_service=quote_service,
        data_dir=data_dir,
        as_of=req.as_of,
        force=req.force,
        dry_run=req.dry_run,
        min_closeness=req.min_closeness,
        with_momentum=req.with_momentum,
        market=req.market,
    )
    logger.info(
        "[lark/push] %s as_of=%s 选中=%s 推送=%s 跳过=%s ok=%s",
        req.strategy_id, result.get("as_of"), result.get("selected"),
        result.get("pushed"), result.get("skipped"), result.get("ok"),
    )
    return result
