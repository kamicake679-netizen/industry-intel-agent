"""FastAPI 后端服务入口：看板聚合、头条、LangGraph SSE 流式问答、JD 抽取与简报推送。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agent.graph import astream_agent
from app.config import DB_PATH, WEB_DIR, settings
from app.db.database import db_session
from app.db.seed_loader import init_and_seed_db
from app.pipeline.extractor import extract_and_save_job
from app.services.analytics import get_dashboard
from app.services.headline import clear_headline_cache, get_headline
from app.services.notifier import generate_brief, push_webhook
from app.services.ratelimit import RateLimitError, get_client_ip, rate_limiter


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：启动时幂等初始化并加载岗位数据库。"""
    init_and_seed_db(DB_PATH)
    yield


app = FastAPI(
    title="INDUSTRY INTEL API",
    description="垂直行业情报采集与对话式分析 Agent 后端服务",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ---------- 全局统一异常处理 ----------


@app.exception_handler(RateLimitError)
async def _handle_rate_limit(_request: Request, exc: RateLimitError) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": exc.error_code, "message": exc.message},
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(StarletteHTTPException)
async def _handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": f"http_{exc.status_code}", "message": str(exc.detail)},
    )


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first_err = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first_err.get("loc", []))
    msg = first_err.get("msg", "请求参数校验失败")
    detail = f"参数校验失败（{loc}）：{msg}" if loc else f"参数校验失败：{msg}"
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "message": detail},
    )


@app.exception_handler(ValueError)
async def _handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "message": str(exc)},
    )


@app.exception_handler(Exception)
async def _handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "message": f"服务内部异常：{exc}"},
    )


# ---------- 请求体模型 ----------


class ChatRequest(BaseModel):
    """问询台对话请求体。"""

    question: str = Field(..., min_length=1, max_length=500, description="用户自然语言提问")
    history: list[dict[str, str]] = Field(default_factory=list, description="最近对话历史（最多保留最近 3 轮）")


class ExtractRequest(BaseModel):
    """JD 结构化抽取投稿请求体。"""

    raw_text: str = Field(..., min_length=5, max_length=10000, description="原始岗位 JD 文本")


class BriefRequest(BaseModel):
    """情报简报生成与推送请求体。"""

    city: str | None = Field(default=None, description="可选城市筛选，如 深圳/广州/东莞")
    webhook_url: str | None = Field(default=None, description="可选覆盖配置中的 Webhook 地址")
    push: bool = Field(default=True, description="是否尝试推送到 Webhook（false 时仅返回预览）")


# ---------- 页面与健康检查 ----------


@app.get("/", include_in_schema=False)
async def index():
    """返回前端首页；若静态页面尚未构建则返回提示页。"""
    index_path = WEB_DIR / "index.html"
    if index_path.exists() and index_path.stat().st_size > 0:
        return FileResponse(str(index_path))
    return HTMLResponse(
        content=(
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>INDUSTRY INTEL API</title></head>"
            "<body style='font-family:sans-serif;background:#F3EFE6;color:#1B1A17;padding:40px;'>"
            "<h1>INDUSTRY INTEL · 后端服务运行中</h1>"
            "<p>前端编辑部页面将在 Step 4 构建。当前可访问 <a href='/docs'>/docs</a> 查看全部接口文档。</p>"
            "</body></html>"
        )
    )


@app.get("/api/health", summary="服务健康检查与每日预算状态")
async def api_health() -> dict[str, Any]:
    """返回服务健康状态、数据库岗位总量、模型配置与剩余每日 LLM 预算。"""
    with db_session(DB_PATH) as conn:
        total_jobs = int(conn.execute("SELECT COUNT(*) FROM job_intel").fetchone()[0])

    api_key = settings.siliconflow_api_key.strip()
    llm_configured = bool(api_key and api_key != "your_api_key_here")
    return {
        "status": "ok",
        "total_jobs": total_jobs,
        "llm_model": settings.llm_model,
        "llm_configured": llm_configured,
        "budget": rate_limiter.get_budget_status(),
    }


# ---------- 看板与头条 ----------


@app.get("/api/dashboard", summary="获取行业情报看板聚合数据")
async def api_dashboard(
    city: str | None = Query(default=None, description="城市筛选：全部/深圳/广州/东莞"),
) -> dict[str, Any]:
    """返回 KPI、技能 TOP 10、三城对照、经验薪资、岗位类别构成、最新入库及数据元信息。"""
    return await run_in_threadpool(get_dashboard, city=city, db_path=DB_PATH)


@app.get("/api/headline", summary="获取当日头版头条（按日期与城市缓存）")
async def api_headline(
    city: str | None = Query(default=None, description="城市筛选：全部/深圳/广州/东莞"),
    refresh: bool = Query(default=False, description="是否忽略缓存强制刷新"),
) -> dict[str, Any]:
    """根据看板实时数据生成当日头条，按日期+城市缓存，异常时自动回退模板句。"""
    return await run_in_threadpool(get_headline, city=city, db_path=DB_PATH, force_refresh=refresh)


# ---------- 问询台 Text-to-SQL 对话 ----------


@app.post("/api/chat", summary="问询台同步问答")
async def api_chat(req: ChatRequest, request: Request) -> dict[str, Any]:
    """执行 LangGraph Text-to-SQL Agent，完成后一次性返回完整结果与轨迹。"""
    rate_limiter.check_and_consume(get_client_ip(request), cost=1)

    final_payload: dict[str, Any] = {}
    async for event in astream_agent(req.question.strip(), history=req.history):
        if event.get("type") == "final":
            final_payload = {k: v for k, v in event.items() if k != "type"}
    return final_payload


@app.post("/api/chat/stream", summary="问询台 SSE 流式问答")
async def api_chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
    """基于 LangGraph astream 逐节点推送 SSE 事件：node / final / error。"""
    rate_limiter.check_and_consume(get_client_ip(request), cost=1)
    question = req.question.strip()
    history = req.history

    async def event_generator():
        try:
            async for event in astream_agent(question, history=history):
                if await request.is_disconnected():
                    break
                event_type = str(event.get("type") or "node")
                payload = {k: v for k, v in event.items() if k != "type"}
                yield {
                    "event": event_type,
                    "data": json.dumps(payload, ensure_ascii=False),
                }
        except Exception as exc:
            yield {
                "event": "error",
                "data": json.dumps(
                    {"error": "stream_error", "message": f"流式分析异常：{exc}"},
                    ensure_ascii=False,
                ),
            }

    return EventSourceResponse(event_generator())


# ---------- JD 结构化抽取投稿 ----------


@app.post("/api/extract", summary="投稿一份 JD 并结构化入库")
async def api_extract(req: ExtractRequest, request: Request) -> dict[str, Any]:
    """调用 LLM 从原始 JD 文本抽取结构化字段并幂等写入数据库。"""
    rate_limiter.check_and_consume(get_client_ip(request), cost=1)
    result = await run_in_threadpool(extract_and_save_job, req.raw_text, source="user_submit", db_path=DB_PATH)
    if result.get("is_new"):
        clear_headline_cache()
    return result


# ---------- 简报生成与 Webhook 推送 ----------


@app.post("/api/brief", summary="生成今日简报 Markdown 并可选推送 Webhook")
async def api_brief(req: BriefRequest) -> dict[str, Any]:
    """生成今日行业情报 Markdown 简报；未配置 Webhook 或 push=false 时返回预览模式。"""
    md = await run_in_threadpool(generate_brief, city=req.city, db_path=DB_PATH)
    if not req.push:
        return {
            "mode": "preview",
            "sent": False,
            "platform": "none",
            "status_code": None,
            "markdown": md,
            "message": "已生成简报预览。",
        }
    return await run_in_threadpool(push_webhook, md, url=req.webhook_url)
