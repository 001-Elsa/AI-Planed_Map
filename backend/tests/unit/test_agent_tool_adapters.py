import json

import httpx
import pytest

from backend.app.schemas.agent_artifacts import AgentType
from backend.app.schemas.ai_intent import Coordinate
from backend.app.services.agent_tool_adapters import (
    AgentToolRuntime,
    LocalToolAdapter,
    MCPServerConfig,
    MCPToolAdapter,
    ToolInvocation,
    parse_mcp_server_configs,
)
from backend.app.services.agent_tool_contracts import tool_result_success
from backend.app.services.agent_tool_registry import (
    TOOL_REGISTRY,
    CapabilityAuthorizationError,
    DataScope,
    InvocationMode,
)


def _search_arguments() -> dict[str, object]:
    return {
        "keyword": "museum",
        "origin": Coordinate(lng=120.1, lat=30.2).model_dump(mode="json"),
        "city": "Hangzhou",
    }


def test_mcp_config_rejects_insecure_and_workflow_only_capabilities():
    with pytest.raises(ValueError, match="HTTPS"):
        parse_mcp_server_configs(
            '[{"name":"bad","endpoint":"http://example.com/mcp","allowed_tools":["search_poi"]}]'
        )
    with pytest.raises(ValueError, match="not an Agent capability"):
        parse_mcp_server_configs(
            '[{"name":"bad","endpoint":"https://example.com/mcp",'
            '"allowed_tools":["save_explicit_preference"]}]'
        )
    parsed = parse_mcp_server_configs(
        '[{"name":"local","endpoint":"http://127.0.0.1:9000/mcp","allowed_tools":["search_poi"]}]'
    )
    assert parsed[0].allowed_tools == frozenset({"search_poi"})


@pytest.mark.asyncio
async def test_local_adapter_cannot_widen_registry_permissions():
    async def handler(arguments):
        return tool_result_success("search_poi", {"arguments": arguments})

    runtime = AgentToolRuntime([LocalToolAdapter({"search_poi": handler})])
    with pytest.raises(CapabilityAuthorizationError):
        await runtime.execute(
            ToolInvocation(
                agent_type=AgentType.planner,
                capability="search_poi",
                invocation_mode=InvocationMode.internal_stage,
                requested_scopes=frozenset({DataScope.map_search}),
                arguments=_search_arguments(),
            )
        )


@pytest.mark.asyncio
async def test_mcp_adapter_initializes_discovers_pinned_schema_and_calls_tool():
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
        elif payload["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "search_poi",
                        "inputSchema": TOOL_REGISTRY.argument_schema("search_poi"),
                    }
                ]
            }
        else:
            result = {
                "content": [],
                "structuredContent": tool_result_success(
                    "search_poi", {"candidate_count": 2}, source="remote-map"
                ).model_dump(mode="json"),
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = MCPToolAdapter(
            client,
            MCPServerConfig(
                name="test",
                endpoint="https://mcp.example/mcp",
                allowed_tools=frozenset({"search_poi"}),
                bearer_token="secret",
            ),
        )
        result = await adapter.invoke("search_poi", _search_arguments())

    assert result.success is True
    assert result.source == "remote-map"
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]


@pytest.mark.asyncio
async def test_mcp_adapter_rejects_remote_schema_drift_before_tool_call():
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = (
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
            if payload["method"] == "initialize"
            else {
                "tools": [
                    {
                        "name": "search_poi",
                        "inputSchema": {"type": "object", "additionalProperties": True},
                    }
                ]
            }
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await MCPToolAdapter(
            client,
            MCPServerConfig(
                name="test",
                endpoint="https://mcp.example/mcp",
                allowed_tools=frozenset({"search_poi"}),
            ),
        ).invoke("search_poi", _search_arguments())

    assert result.success is False
    assert result.error_code == "MCP_SCHEMA_MISMATCH"
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
