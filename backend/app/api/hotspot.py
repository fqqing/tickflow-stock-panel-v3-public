"""市场主线全景 / 主线转换嗅探 / 快报热点聚合 — API。"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.services import market_hotspot

router = APIRouter(prefix="/api/hotspot", tags=["hotspot"])


@router.get("/mainline")
def mainline_panorama(request: Request, top_n: int = Query(8, ge=1, le=20),
                      kind: str = Query("concept")):
    """主线梯队全景: 主线 → 龙头 → 前排 → 中排 → 后排。"""
    repo = request.app.state.repo
    return market_hotspot.build_mainline_panorama(repo, top_n=top_n, kind=kind)


@router.get("/mainline-calendar")
def mainline_calendar(request: Request, days: int = Query(30, ge=5, le=90),
                      top_n: int = Query(8, ge=3, le=15),
                      kind: str = Query("concept")):
    """板块周期表: 往日每日主线排行 → 日期 × 名次矩阵(含连续上榜天数/新晋标记)。"""
    repo = request.app.state.repo
    return market_hotspot.build_mainline_calendar(repo, days=days, top_n=top_n, kind=kind)


@router.get("/rotation")
def rotation_sniff(request: Request, days: int = Query(12, ge=7, le=30),
                   kind: str = Query("concept")):
    """主线转换嗅探: 可能转主线 / 可能退潮 / 连续霸榜。"""
    repo = request.app.state.repo
    return market_hotspot.sniff_rotation(repo, days=days, kind=kind)


@router.get("/mainline-reason")
async def mainline_reason(request: Request, top_n: int = Query(8, ge=1, le=20),
                          kind: str = Query("concept")):
    """主线原因: 用快讯(标题 + boards 板块)反查每条主线的驱动消息, 叠 LLM 归纳。"""
    repo = request.app.state.repo
    return await market_hotspot.build_mainline_reason(repo, top_n=top_n, kind=kind)


@router.get("/mainline-compare")
def mainline_compare(request: Request, top_n: int = Query(8, ge=1, le=20),
                     kind: str = Query("concept"),
                     plate_scope: int = Query(30, ge=5, le=60)):
    """自算主线 vs 腾讯板块涨幅榜交叉对照(两边都强=真主线 / 自算强但腾讯不认=警惕)。"""
    repo = request.app.state.repo
    return market_hotspot.build_mainline_compare(repo, top_n=top_n, kind=kind,
                                                 plate_scope=plate_scope)


@router.get("/flash-summary")
async def flash_hotspot(request: Request, size: int = Query(60, ge=10, le=200),
                        top_n: int = Query(12, ge=1, le=30)):
    """快报 → 今日热点聚合(东财 7x24 快讯按题材聚类 + 关联个股 + LLM 归纳)。"""
    repo = request.app.state.repo
    return await market_hotspot.build_flash_hotspot(repo, size=size, top_n=top_n)
