"""AI 大盘复盘 API — 流式复盘 + 报告持久化。

路由前缀: /api/market-recap

端点:
  POST /analyze                AI 流式大盘复盘(NDJSON)
  GET  /reports                历史复盘列表
  POST /reports                保存一条复盘报告
  DELETE /reports/{report_id}  删除一条复盘报告
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import market_recap_reports
from app.services.market_recap import recap_market_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-recap", tags=["market-recap"])


class AnalyzeRequest(BaseModel):
    """AI 大盘复盘请求。"""
    as_of: str | None = None  # 可选:复盘日期(YYYY-MM-DD),缺省取最新有数据日
    focus: str = ""           # 可选:用户追加的复盘关注点
    market: str = "cn"        # cn | hk | us（多市场扩展）


@router.post("/analyze")
async def analyze_market(request: Request, req: AnalyzeRequest):
    """AI 大盘复盘 — NDJSON 流式返回。

    装配市场总览(指数/涨跌/连板/封板/板块/情绪雷达)→ 复盘提示词 →
    流式调用 LLM → 逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。

    market=hk|us（多市场扩展）: 走港美股复盘(新高/动量替代涨停/连板)。

    协议:
      {"type":"meta","as_of","emotion_score","emotion_label","summary"}
      {"type":"delta","content":"..."}
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    from datetime import date as date_cls

    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    depth_service = getattr(request.app.state, "depth_service", None)

    as_of = None
    if req.as_of:
        try:
            as_of = date_cls.fromisoformat(req.as_of)
        except ValueError:
            raise HTTPException(400, f"as_of 格式应为 YYYY-MM-DD,收到: {req.as_of}")

    if req.market in ("hk", "us"):
        from app.services.market_recap import recap_market_stream_market

        async def stream_gen_market():
            async for chunk in recap_market_stream_market(repo, req.market, as_of, req.focus):
                yield chunk + "\n"

        return StreamingResponse(
            stream_gen_market(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def stream_gen():
        async for chunk in recap_market_stream(repo, quote_service, depth_service, as_of, req.focus):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/market-data")
def market_data_recap(request: Request, market: str = Query("hk", description="hk|us")):
    """港美股数据版复盘（多市场扩展）— 不依赖 AI 接口。

    基于 build_market_overview_market 的结构化数据，直接生成 Markdown 复盘文本。
    AI 复盘（POST /analyze）需配置 AI 接口且暂仅支持 A 股。
    """
    if market not in ("hk", "us"):
        raise HTTPException(status_code=400, detail="market 必须为 hk|us")
    from app.services.market_overview_builder import build_market_overview_market

    repo = request.app.state.repo
    ov = build_market_overview_market(repo, market)
    if not ov.get("as_of"):
        return {"as_of": None, "content": f"## {market.upper()} 市场复盘\n\n暂无数据，请先在数据页同步{market}数据。", "market": market}

    b = ov.get("breadth") or {}
    t = ov.get("trend") or {}
    l = ov.get("limit") or {}
    e = ov.get("emotion") or {}
    lines = [
        f"## {market.upper()}市场数据复盘（{ov['as_of']}）",
        "",
        f"**市场情绪**: {e.get('label')}（{e.get('score')} 分）",
        "",
        "### 涨跌概况",
        f"- 全市场 {b.get('total')} 只：上涨 {b.get('up')} 只（{b.get('up_pct', 0):.1f}%）、下跌 {b.get('down')} 只、平均涨跌 {b.get('avg_pct', 0) * 100:+.2f}%",
        f"- 涨超 3%：{b.get('strong_up', 0)} 只；跌超 3%：{b.get('strong_down', 0)} 只",
        "",
        "### 强度",
        f"- 60日新高 {l.get('limit_up', 0)} 只 / 60日新低 {l.get('limit_down', 0)} 只（港美股无涨停概念，以新高新低替代）",
        f"- 站上 MA20：{t.get('above_ma20_pct', 0):.1f}%；站上 MA60：{t.get('above_ma60_pct', 0):.1f}%",
        "",
        "### 领涨榜",
    ]
    for r in (ov.get("top_gainers") or [])[:8]:
        lines.append(f"- {r.get('symbol')} {r.get('name') or ''} {r.get('change_pct', 0) * 100:+.2f}%")
    lines += ["", "### 领跌榜"]
    for r in (ov.get("top_losers") or [])[:5]:
        lines.append(f"- {r.get('symbol')} {r.get('name') or ''} {r.get('change_pct', 0) * 100:+.2f}%")
    lines += ["", "> 数据来源：本地 enriched 表（TickFlow 免费日K）。此复盘为数据模板，AI 深度解读需配置 AI 接口。"]
    return {"as_of": ov.get("as_of"), "content": "\n".join(lines), "market": market}


# ================================================================
# 报告 CRUD(历史复盘持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 大盘复盘报告。"""
    as_of: str
    focus: str = ""
    content: str
    summary: str = ""
    emotion_score: int | None = None
    emotion_label: str = ""
    market: str = "cn"    # cn | hk | us（多市场扩展）


@router.get("/reports")
def list_reports(request: Request, market: str = Query("cn", description="cn|hk|us")):
    """获取全部历史复盘(按时间降序,后端已裁剪到上限)。market 过滤。"""
    return {"reports": market_recap_reports.list_reports(market)}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条复盘报告。"""
    report = market_recap_reports.save_report({
        "as_of": req.as_of,
        "focus": req.focus,
        "content": req.content,
        "summary": req.summary,
        "emotion_score": req.emotion_score,
        "emotion_label": req.emotion_label,
        "market": req.market,
    })
    # 推送到飞书(可选): 与定时复盘共用同一开关 review_push_enabled 与 _maybe_push_review。
    # 内部 try/except 静默降级, 不影响归档返回值。
    from app.jobs.daily_pipeline import _maybe_push_review
    _maybe_push_review(req.content, {
        "as_of": req.as_of,
        "emotion_label": req.emotion_label,
    })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条复盘报告。"""
    ok = market_recap_reports.delete_report(report_id)
    return {"ok": ok}
