"""Optional stateless Streamable HTTP MCP facade for read-only MAPGO tools."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend.app.core.config import get_settings
from backend.app.schemas.agent_artifacts import AgentType
from backend.app.schemas.ai_intent import Coordinate
from backend.app.services.agent_tool_adapters import (
    AgentToolRuntime,
    LocalToolAdapter,
    ToolInvocation,
)
from backend.app.services.agent_tool_contracts import (
    RouteMatrixArgs,
    SearchPoiArgs,
    ToolResultEnvelope,
    WeatherQueryArgs,
    default_tool_expiry,
    stable_tool_error,
    tool_result_error,
    tool_result_success,
)
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, DataScope, InvocationMode
from backend.app.services.agents.base import canonical_hash

router = APIRouter()
PROTOCOL_VERSION = "2025-11-25"
EXPORTED_TOOLS = (
    "search_poi",
    "get_route_matrix",
    "verify_transit_edges",
    "get_weather",
)


def _rpc_result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _authorized(request: Request) -> bool:
    settings = get_settings()
    if not settings.mcp_internal_token:
        return False
    provided = request.headers.get("authorization", "")
    expected = f"Bearer {settings.mcp_internal_token}"
    return hmac.compare_digest(provided, expected)


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = {
        item.strip() for item in get_settings().mcp_allowed_origins.split(",") if item.strip()
    }
    return origin in allowed


def _tool_definition(name: str) -> dict[str, Any]:
    capability = TOOL_REGISTRY.get(name)
    schema = TOOL_REGISTRY.argument_schema(name)
    if capability is None or schema is None:
        raise RuntimeError(f"MCP tool {name!r} is missing its registered schema")
    read_only = capability.side_effect == "read_only"
    return {
        "name": name,
        "description": f"MAPGO permission-scoped {name} capability",
        "inputSchema": schema,
        "outputSchema": ToolResultEnvelope.model_json_schema(),
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "idempotentHint": read_only,
            "openWorldHint": name in {"search_poi", "get_weather"},
        },
    }


def _local_runtime(request: Request) -> AgentToolRuntime:
    map_provider = request.app.state.map_provider
    weather_provider = request.app.state.weather_provider

    async def search(arguments: dict[str, Any]) -> ToolResultEnvelope:
        args = SearchPoiArgs.model_validate(arguments)
        candidates = await map_provider.search_poi(args.keyword, args.origin, args.city)
        return tool_result_success(
            "search_poi",
            {"candidates": [item.model_dump(mode="json") for item in candidates]},
            source=map_provider.name,
            expires_at=default_tool_expiry(),
            confidence=min((item.confidence for item in candidates), default=0.5),
            artifact_ref=f"poi:{canonical_hash(arguments)[:24]}",
        )

    async def matrix(arguments: dict[str, Any]) -> ToolResultEnvelope:
        args = RouteMatrixArgs.model_validate(arguments)
        result = await map_provider.route_matrix(args.points, args.transport_mode)
        return tool_result_success(
            "get_route_matrix",
            {"matrix": result.model_dump(mode="json")},
            source=map_provider.name,
            expires_at=default_tool_expiry(60),
            confidence=min((edge.confidence for row in result.edges for edge in row), default=0.5),
            artifact_ref=f"matrix:{canonical_hash(arguments)[:24]}",
        )

    async def transit(arguments: dict[str, Any]) -> ToolResultEnvelope:
        args = RouteMatrixArgs.model_validate(arguments)
        result = await map_provider.transit_route_edges(args.points, None)
        return tool_result_success(
            "verify_transit_edges",
            {"edges": [item.model_dump(mode="json") for item in result]},
            source=map_provider.name,
            expires_at=default_tool_expiry(60),
            confidence=min((item.confidence for item in result), default=0.5),
            artifact_ref=f"transit:{canonical_hash(arguments)[:24]}",
        )

    async def weather(arguments: dict[str, Any]) -> ToolResultEnvelope:
        args = WeatherQueryArgs.model_validate(arguments)
        if args.location is None:
            return tool_result_error("INVALID_TOOL_ARGUMENTS", retryable=False)
        result = await weather_provider.current(Coordinate.model_validate(args.location))
        return tool_result_success(
            "get_weather",
            {"weather": result.model_dump(mode="json")},
            source=weather_provider.name,
            expires_at=default_tool_expiry(600),
            confidence=result.confidence,
            artifact_ref=f"weather:{canonical_hash(arguments)[:24]}",
        )

    return AgentToolRuntime(
        [
            LocalToolAdapter(
                {
                    "search_poi": search,
                    "get_route_matrix": matrix,
                    "verify_transit_edges": transit,
                    "get_weather": weather,
                }
            )
        ]
    )


def _invocation(name: str, arguments: dict[str, Any]) -> ToolInvocation | None:
    mapping = {
        "search_poi": (AgentType.search, frozenset({DataScope.map_search})),
        "get_route_matrix": (AgentType.planner, frozenset({DataScope.route_matrix})),
        "verify_transit_edges": (AgentType.planner, frozenset({DataScope.transit_routes})),
        "get_weather": (AgentType.companion, frozenset({DataScope.weather})),
    }
    item = mapping.get(name)
    if item is None:
        return None
    agent_type, scopes = item
    mode = (
        InvocationMode.agent_callable
        if agent_type == AgentType.companion
        else InvocationMode.internal_stage
    )
    return ToolInvocation(
        agent_type=agent_type,
        capability=name,
        invocation_mode=mode,
        requested_scopes=scopes,
        arguments=arguments,
    )


@router.get("/mcp", include_in_schema=False)
async def mcp_get() -> Response:
    # Stateless JSON response mode has no server-initiated SSE stream.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request) -> Response:
    settings = get_settings()
    if not settings.mcp_server_enabled:
        return Response(status_code=404)
    if not _origin_allowed(request):
        return Response(status_code=403)
    if not _authorized(request):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    identity = request.client.host if request.client else "unknown"
    identity_hash = hashlib.sha256(identity.encode()).hexdigest()[:24]
    count = await request.app.state.runtime_store.increment(f"rate:mcp:{identity_hash}", 60)
    if count > settings.mcp_requests_per_minute:
        return Response(status_code=429, headers={"Retry-After": "60"})
    try:
        payload = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return _rpc_error(
            payload.get("id") if isinstance(payload, dict) else None, -32600, "Invalid Request"
        )
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    protocol_header = request.headers.get("mcp-protocol-version")
    if method != "initialize" and protocol_header != PROTOCOL_VERSION:
        return _rpc_error(request_id, -32600, "Unsupported or missing MCP protocol version")
    if method == "initialize":
        return _rpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mapgo", "version": settings.app_version},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "notifications/initialized" and request_id is None:
        return Response(status_code=202)
    if method == "tools/list":
        return _rpc_result(
            request_id, {"tools": [_tool_definition(name) for name in EXPORTED_TOOLS]}
        )
    if method != "tools/call":
        return _rpc_error(request_id, -32601, "Method not found")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _rpc_error(request_id, -32602, "Invalid params")
    name = params["name"]
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return _rpc_error(request_id, -32602, "Invalid params")
    invocation = _invocation(name, arguments)
    if invocation is None or name not in EXPORTED_TOOLS:
        return _rpc_error(request_id, -32602, "Tool is not exported")
    try:
        result = await _local_runtime(request).execute(invocation)
    except Exception as exc:  # noqa: BLE001 - MCP boundary emits a stable tool error
        code = stable_tool_error(exc)
        result = tool_result_error(code, retryable=code == "UPSTREAM_TIMEOUT")
    structured = result.model_dump(mode="json")
    return _rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
            "structuredContent": structured,
            "isError": not result.success,
        },
    )
