import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.api import ai_planner, auth, companion, data, social, system
from backend.app.clients.amap_client import build_map_provider
from backend.app.clients.knowledge_client import CuratedKnowledgeProvider
from backend.app.clients.weather_client import build_weather_provider
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.core.observability import metrics
from backend.app.db.session import SessionLocal, check_database, engine
from backend.app.infrastructure.runtime_store import build_runtime_store
from backend.app.models import LocationSnapshot

CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _correlation_id(value: str | None, fallback_prefix: str = "") -> str:
    if value and CORRELATION_ID_PATTERN.fullmatch(value):
        return value
    generated = uuid.uuid4().hex
    return f"{fallback_prefix}{generated[:16]}" if fallback_prefix else generated


class RequestBodyLimitMiddleware:
    def __init__(self, application: ASGIApp, max_bytes: int) -> None:
        self.application = application
        self.max_bytes = max_bytes  # 最大允许请求大小

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return
        body = bytearray()
        disconnected = False
        while True:  # 客户端来一块，我收一块
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:  # 有没有超过最大允许请求大小
                request_id = ""
                for key, value in scope.get("headers", []):
                    if key.lower() == b"x-request-id":
                        request_id = value.decode("latin-1")[:100]
                        break
                response = JSONResponse(
                    status_code=413,
                    content={
                        "ok": False,
                        "code": "PAYLOAD_TOO_LARGE",
                        "msg": "请求体过大",
                        "request_id": request_id,
                        "details": {},
                    },
                )
                # 213行左右还有一次检查content_Length，因为客户端告诉服务器的长度可能>max或者不发 Content-Length或者SSE传输
                # 所以不能只相信header，还要RequestBodyLimitMiddleware真正读取字节并计数（真正的保险）
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def limited_receive() -> Message:
            nonlocal delivered
            if disconnected or delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.application(scope, limited_receive, send)


@asynccontextmanager
async def lifespan(application: FastAPI):
    if settings.environment == "production":  # api key的检查
        if settings.location_encryption_key in {
            "",
            "replace-with-secret-manager-value-in-production",
        }:
            raise RuntimeError(
                "a non-placeholder LOCATION_ENCRYPTION_KEY is required in production"
            )
        if settings.admin_init_token in {"", "change_this_before_production"}:
            raise RuntimeError("a non-placeholder ADMIN_INIT_TOKEN is required in production")
    await check_database()
    async with SessionLocal() as cleanup_db:
        await cleanup_db.execute(
            delete(LocationSnapshot).where(
                LocationSnapshot.expires_at <= datetime.now(timezone.utc)
            )
        )
        await cleanup_db.commit()
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
        yield  # 把yield当成一条分界线：yield 上面是服务器启动时执行；yield 下面服务器关闭时执行
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


@app.middleware("http")  # Middleware理解成所有 API 的公共检查站。
# 项目给 FastAPI 注册了一个 request_context 中间件。每个 HTTP 请求都会经过它。
async def request_context(request: Request, call_next):
    # request_id 和 trace_id 是为“定位一次请求”服务的
    # request_id串起一个请求的完整流程
    # trace_id是为了以后如果项目拆成多个服务，同一个 trace_id 可以跨服务跟踪整条调用链
    # Correlation IDs are reflected in response headers and logs. Restrict them
    # to a small visible-safe alphabet instead of trusting arbitrary input.
    request_id = _correlation_id(request.headers.get("X-Request-ID"), "req_")
    trace_id = _correlation_id(request.headers.get("X-Trace-ID"))
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    started = time.perf_counter()
    if request.url.path.startswith("/api/") and request.url.path not in {
        "/api/health",
        "/api/config",
    }:
        client_ip = request.client.host if request.client else "unknown"
        ip_digest = hashlib.sha256(client_ip.encode()).hexdigest()[:24]
        path = request.url.path

        # 因为公司、校园等环境可能存在 NAT，很多用户会共享公网 IP。
        # 项目保留宽松的 IP 粗粒度防滥用，同时对已登录请求再按 session 做身份粒度限流。
        # 项目做了两层限流：
        # 一层是 IP 总额度，用来防止某个网络来源疯狂攻击。
        # 另一层更细。登录和注册不能信任可伪造的设备 Header，因此仍按来源 IP；
        # 已经登录的请求根据 Bearer Token 生成 session 身份，其他匿名请求按 IP。

        # Keep a generous per-IP ceiling as abuse protection, but do not make
        # all signed-in users behind the same office/school NAT share the much
        # smaller normal API budget.
        ip_limit = (
            settings.auth_ip_requests_per_minute
            if path in {"/api/login", "/api/register"}
            else settings.api_ip_requests_per_minute
        )
        ip_count = await request.app.state.runtime_store.increment(f"rate:api-ip:{ip_digest}", 60)

        authorization = request.headers.get("authorization", "")
        if path in {"/api/login", "/api/register"}:
            # Device headers are attacker-controlled and must not create fresh
            # login budgets. Otherwise changing X-Device-Id bypasses throttling.
            identity = f"auth-source:{ip_digest}"
            identity_limit = settings.auth_device_requests_per_minute
        elif authorization.startswith("Bearer "):
            session_digest = hashlib.sha256(authorization[7:].encode()).hexdigest()[:24]
            identity = f"session:{session_digest}"
            identity_limit = settings.api_requests_per_minute
        else:
            identity = f"anonymous:{ip_digest}"
            identity_limit = settings.api_requests_per_minute

        count = await request.app.state.runtime_store.increment(f"rate:api:{identity}", 60)
        if ip_count > ip_limit or count > identity_limit:
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
    try:
        # Content-Length 是客户端在 Header 里声称“我的 body 有多大”
        # 但是不能完全相信客户端的声称
        # 所以这个项目又从 ASGI 底层真正读取收到的字节，并计算实际大小。
        # Middleware 能够直接拦截 receive，统计到底收到了多少字节。这就是为什么它能比普通 Content-Length 检查更可靠。
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        content_length = settings.max_request_bytes + 1
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
    # 业务接口执行完后，这个 Middleware 会计算耗时，把 X-Request-ID 和 X-Trace-ID 返回给客户端，同时设置若干安全 Header。
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Trace-ID"] = trace_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    logger.info(  # Logging 是查具体事情。
        json.dumps(
            # 然后它会输出结构化 JSON 日志，记录 method、path、status code、duration 等信息。
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
    # Never use the raw URL as a metric label. Random 404 paths would otherwise
    # create unbounded label cardinality and permanently grow the registry.
    route_path = getattr(route, "path", None) or "__unmatched__"
    metrics.increment(  # Metrics 是看整体健康状态
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


# Register this after BaseHTTPMiddleware-based request_context so the bounded
# buffer remains the outermost application wrapper. Otherwise Starlette can turn
# a streaming overflow into a generic JSON parse error before it reaches us.
app.add_middleware(
    cast(Any, RequestBodyLimitMiddleware),
    max_bytes=settings.max_request_bytes,
)


# 异常处理这一大块，核心是“后端内部错误”和“API 对外错误格式”分离
# 定义了4类异常处理，无论内部哪里出错，对前端都尽量返回统一的错误结构。
# 1、AppError 用来表示项目主动定义的业务错误，比如“高德代理路径不允许”。
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


# 2、RequestValidationError 处理 FastAPI/Pydantic 参数校验错误。
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


# 3、StarletteHTTPException 处理 404、405 这种 HTTP 层错误。
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


# 4、Exception 负责接住真正没有预料到的异常。
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


# API Middleware 在请求完成后记录请求总数和请求耗时等指标，应用通过 /metrics 暴露 Prometheus 格式数据，再由 Prometheus 抓取，Grafana 做可视化。
@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


AMAP_PROXY_ALLOWED_PATHS = {
    "v3/assistant/inputtips",
    "v3/config/district",
    "v3/direction/bicycling",
    "v3/direction/driving",
    "v3/direction/transit/integrated",
    "v3/direction/walking",
    "v3/geocode/geo",
    "v3/geocode/regeo",
    "v3/place/around",
    "v3/place/detail",
    "v3/place/polygon",
    "v3/place/text",
    "v3/weather/weatherInfo",
    "v4/direction/bicycling",
    "v5/direction/bicycling",
    "v5/direction/driving",
    "v5/direction/transit/integrated",
    "v5/direction/walking",
}


# 高德 API 的服务端接入层
@app.api_route("/_AMapService/{rest_path:path}", methods=["GET", "POST"])
async def amap_security_proxy(rest_path: str, request: Request):
    normalized_path = rest_path.rstrip("/")
    if normalized_path not in AMAP_PROXY_ALLOWED_PATHS:
        raise AppError(403, "AMAP_PATH_DENIED", "高德代理路径不允许")
    identity = request.client.host if request.client else "unknown"
    count = await request.app.state.runtime_store.increment(
        f"rate:amap-proxy:{identity}",
        60,
    )
    if count > settings.amap_proxy_requests_per_minute:
        raise AppError(
            429,
            "AMAP_PROXY_RATE_LIMITED",
            "地图代理请求过于频繁",
            {"retry_after_seconds": 60},
        )
    cache_key = None
    cacheable_paths = {
        "v3/assistant/inputtips": 86_400,
        "v3/config/district": 86_400,
        "v3/place/text": 86_400,
        # 模式切换、地图 moveend 和多用户热点位置会产生大量重复周边请求。
        # 短缓存既降低高德压力，也不会长期保留可能变化的商户数据。
        "v3/place/around": 300,
        "v3/geocode/regeo": 3600,
        "v3/weather/weatherInfo": 600,
        "v3/direction/walking": 60,
        "v3/direction/driving": 60,
        "v3/direction/transit/integrated": 60,
        "v4/direction/bicycling": 60,
        "v5/direction/bicycling": 60,
        "v5/direction/driving": 60,
        "v5/direction/transit/integrated": 60,
        "v5/direction/walking": 60,
    }
    if (
        request.method == "GET"
        and normalized_path in cacheable_paths
        and "callback" not in request.query_params
    ):
        stable_params = sorted(
            (key, value)
            for key, value in request.query_params.multi_items()
            if key not in {"key", "jscode", "csid"}
        )
        digest = (
            hashlib.sha256(  # 不是为了加密，而是把一组可能很长的查询条件稳定映射成固定长度的 key
                json.dumps(
                    {"path": normalized_path, "params": stable_params},
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        cache_key = f"amap:search:{digest}"
        cached = await request.app.state.runtime_store.get_json(cache_key)
        if cached:
            return Response(
                content=cached["content"],
                status_code=int(cached["status_code"]),
                media_type=cached.get("content_type"),
            )
    # The proxy credential is never returned to the browser
    from backend.app.db.session import SessionLocal

    async with SessionLocal() as db:
        configured_jscode = (
            "" if settings.disable_configured_map_credentials else settings.amap_jscode
        )
        jscode = await system.setting(db, "amap_jscode") or configured_jscode
    if not jscode:
        raise AppError(503, "AMAP_NOT_CONFIGURED", "高德安全密钥尚未配置")
    params = dict(request.query_params)
    params["jscode"] = jscode
    request_body = await request.body()
    for attempt in range(settings.upstream_max_retries + 1):
        content = bytearray()
        try:
            async with request.app.state.http_client.stream(
                request.method,
                f"https://restapi.amap.com/{normalized_path}",
                params=params,
                content=request_body,
                headers={"User-Agent": "MapGo-AI-Proxy"},
                timeout=httpx.Timeout(
                    settings.external_timeout_seconds,
                    connect=max(settings.external_connect_timeout_seconds, 5.0),
                ),
            ) as upstream:
                async for chunk in upstream.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > settings.amap_proxy_max_response_bytes:
                        raise AppError(502, "AMAP_RESPONSE_TOO_LARGE", "地图服务响应超过安全上限")
                status_code = upstream.status_code
                content_type = upstream.headers.get("content-type")
            break
        except httpx.HTTPError as exc:
            if attempt >= settings.upstream_max_retries:
                raise AppError(
                    502,
                    "AMAP_UPSTREAM_UNAVAILABLE",
                    "地图服务暂时不可用，请稍后重试",
                ) from exc
            await asyncio.sleep(0.15 * (2**attempt))
    if cache_key and status_code == 200 and len(content) <= settings.amap_proxy_max_cache_bytes:
        try:
            payload = json.loads(bytes(content))
            if str(payload.get("status")) == "1":
                await request.app.state.runtime_store.set_json(
                    cache_key,
                    {
                        "content": bytes(content).decode("utf-8"),
                        "status_code": status_code,
                        "content_type": content_type,
                    },
                    cacheable_paths[normalized_path],
                )
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return Response(
        content=bytes(content),
        status_code=status_code,
        media_type=content_type,
    )


if settings.public_dir.exists():
    app.mount("/", StaticFiles(directory=settings.public_dir, html=True), name="public")
