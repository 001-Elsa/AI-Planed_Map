import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.api import ai_planner, auth, companion, data, social, system
from backend.app.clients.amap_client import build_map_provider
from backend.app.clients.knowledge_client import CuratedKnowledgeProvider
from backend.app.clients.weather_client import build_weather_provider
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.core.observability import metrics
from backend.app.db.session import check_database, engine
from backend.app.infrastructure.runtime_store import build_runtime_store


@asynccontextmanager
async def lifespan(application: FastAPI):
    if settings.environment == "production" and not settings.location_encryption_key:
        raise RuntimeError("LOCATION_ENCRYPTION_KEY is required in production")
    await check_database()
    timeout = httpx.Timeout(
        settings.external_timeout_seconds,
        connect=settings.external_connect_timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=settings.http_max_connections,
        max_keepalive_connections=settings.http_max_keepalive_connections,
    )
    client = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)
    application.state.http_client = client
    application.state.map_provider = build_map_provider(settings, client)
    application.state.weather_provider = build_weather_provider(
        client, settings.mock_weather_provider
    )
    application.state.knowledge_provider = CuratedKnowledgeProvider()
    application.state.runtime_store = await build_runtime_store(settings.redis_url)
    try:
        yield
    finally:
        await client.aclose()
        await application.state.runtime_store.close()
        await engine.dispose()


settings = get_settings()
logger = logging.getLogger("mapgo")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="大模型意图解析 + 真实 POI + 确定性约束路线优化",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
    trace_id = request.headers.get("X-Trace-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    started = time.perf_counter()
    if request.url.path.startswith("/api/") and request.url.path not in {
        "/api/health",
        "/api/config",
    }:
        identity = request.client.host if request.client else "unknown"
        count = await request.app.state.runtime_store.increment(f"rate:api:{identity}", 60)
        if count > settings.api_requests_per_minute:
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "code": "RATE_LIMITED",
                    "msg": "请求过于频繁，请稍后重试",
                    "request_id": request_id,
                    "details": {"retry_after_seconds": 60},
                },
                headers={"Retry-After": "60"},
            )
    content_length = int(request.headers.get("content-length", "0") or 0)
    if content_length > settings.max_request_bytes:
        return JSONResponse(
            status_code=413,
            content={
                "ok": False,
                "code": "PAYLOAD_TOO_LARGE",
                "msg": "请求体过大",
                "request_id": request_id,
                "details": {},
            },
        )
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Trace-ID"] = trace_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    logger.info(
        json.dumps(
            {
                "event": "http_request",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": elapsed_ms,
            },
            ensure_ascii=False,
        )
    )
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    metrics.increment(
        "mapgo_api_requests_total",
        {
            "method": request.method,
            "path": route_path,
            "status": str(response.status_code),
        },
    )
    metrics.observe(
        "mapgo_api_request_duration_seconds",
        elapsed_ms / 1000,
        {"method": request.method, "path": route_path},
    )
    return response


@app.exception_handler(AppError)
async def app_error(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "code": exc.code,
            "msg": exc.message,
            "request_id": request.state.request_id,
            "details": exc.details,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    status_code = 400 if any(item.get("type") == "json_invalid" for item in exc.errors()) else 422
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "code": "INVALID_JSON" if status_code == 400 else "VALIDATION_ERROR",
            "msg": "JSON 请求体格式错误" if status_code == 400 else "请求参数未通过校验",
            "request_id": request.state.request_id,
            "details": {"errors": exc.errors()},
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    messages = {404: "接口或资源不存在", 405: "请求方法不允许"}
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "code": f"HTTP_{exc.status_code}",
            "msg": messages.get(exc.status_code, str(exc.detail)),
            "request_id": request.state.request_id,
            "details": {},
        },
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logger.exception("Unhandled error request_id=%s", request.state.request_id)
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "code": "INTERNAL_ERROR",
            "msg": "服务器内部错误",
            "request_id": request.state.request_id,
            "details": {},
        },
    )


for api_router in (
    auth.router,
    data.router,
    social.router,
    system.router,
    ai_planner.router,
    companion.router,
):
    app.include_router(api_router, prefix="/api")


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@app.api_route("/_AMapService/{rest_path:path}", methods=["GET", "POST", "PUT"])
async def amap_security_proxy(rest_path: str, request: Request):
    if not rest_path.startswith("v"):
        raise AppError(403, "AMAP_PATH_DENIED", "高德代理路径不允许")
    # The proxy credential is never returned to the browser.
    from backend.app.db.session import SessionLocal

    async with SessionLocal() as db:
        jscode = await system.setting(db, "amap_jscode") or settings.amap_jscode
    if not jscode:
        raise AppError(503, "AMAP_NOT_CONFIGURED", "高德安全密钥尚未配置")
    params = dict(request.query_params)
    params["jscode"] = jscode
    upstream = await request.app.state.http_client.request(
        request.method,
        f"https://restapi.amap.com/{rest_path}",
        params=params,
        content=await request.body(),
        headers={"User-Agent": "MapGo-AI-Proxy"},
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


if settings.public_dir.exists():
    app.mount("/", StaticFiles(directory=settings.public_dir, html=True), name="public")
