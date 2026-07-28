import uuid
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.app.api import ai_planner, auth, data, social, system
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.db.session import create_schema


@asynccontextmanager
async def lifespan(_: FastAPI):
    await create_schema()
    yield


settings = get_settings()
logger = logging.getLogger("mapgo")
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="大模型意图解析 + 真实 POI + 确定性约束路线优化",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
    request.state.request_id = request_id
    content_length = int(request.headers.get("content-length", "0") or 0)
    if content_length > settings.max_request_bytes:
        return JSONResponse(
            status_code=413,
            content={"ok": False, "code": "PAYLOAD_TOO_LARGE", "msg": "请求体过大", "request_id": request_id, "details": {}},
        )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
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
            "ok": False, "code": f"HTTP_{exc.status_code}",
            "msg": messages.get(exc.status_code, str(exc.detail)),
            "request_id": request.state.request_id, "details": {},
        },
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logger.exception("Unhandled error request_id=%s", request.state.request_id)
    return JSONResponse(
        status_code=500,
        content={
            "ok": False, "code": "INTERNAL_ERROR", "msg": "服务器内部错误",
            "request_id": request.state.request_id, "details": {},
        },
    )


for api_router in (auth.router, data.router, social.router, system.router, ai_planner.router):
    app.include_router(api_router, prefix="/api")


@app.api_route("/_AMapService/{rest_path:path}", methods=["GET", "POST", "PUT"])
async def amap_security_proxy(rest_path: str, request: Request):
    if not rest_path.startswith("v"):
        raise AppError(403, "AMAP_PATH_DENIED", "高德代理路径不允许")
    async with httpx.AsyncClient(timeout=settings.external_timeout_seconds) as client:
        # The proxy credential is never returned to the browser.
        from backend.app.db.session import SessionLocal
        async with SessionLocal() as db:
            jscode = await system.setting(db, "amap_jscode") or settings.amap_jscode
        if not jscode:
            raise AppError(503, "AMAP_NOT_CONFIGURED", "高德安全密钥尚未配置")
        params = dict(request.query_params)
        params["jscode"] = jscode
        upstream = await client.request(
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
