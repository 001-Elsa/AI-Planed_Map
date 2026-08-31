"""Transport adapters for permission-checked Agent tools.

Adapters only decide *how* an already-authorized capability is executed. They
cannot add tools, agents, scopes, or invocation modes to the capability
registry. This keeps a remote MCP/HTTP server outside the trust boundary.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from backend.app.schemas.agent_artifacts import AgentType
from backend.app.services.agent_tool_contracts import (
    ToolResultEnvelope,
    stable_tool_error,
    tool_result_error,
    validate_tool_arguments,
)
from backend.app.services.agent_tool_registry import (
    TOOL_REGISTRY,
    AgentToolRegistry,
    DataScope,
    InvocationMode,
)

ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResultEnvelope | dict[str, Any]]]


@dataclass(frozen=True)
class ToolInvocation:
    agent_type: AgentType
    capability: str
    invocation_mode: InvocationMode
    requested_scopes: frozenset[DataScope]
    arguments: dict[str, Any]
    idempotency_key: str | None = None


class ToolAdapter(Protocol):
    name: str

    def supports(self, capability: str) -> bool: ...

    async def invoke(self, capability: str, arguments: dict[str, Any]) -> ToolResultEnvelope: ...


class LocalToolAdapter:
    name = "local"

    def __init__(self, handlers: Mapping[str, ToolHandler]) -> None:
        self._handlers = dict(handlers)

    def supports(self, capability: str) -> bool:
        return capability in self._handlers

    async def invoke(self, capability: str, arguments: dict[str, Any]) -> ToolResultEnvelope:
        handler = self._handlers.get(capability)
        if handler is None:
            return tool_result_error("TOOL_ADAPTER_UNAVAILABLE", retryable=False, source=self.name)
        try:
            result = await handler(arguments)
            return (
                result
                if isinstance(result, ToolResultEnvelope)
                else ToolResultEnvelope.model_validate(result)
            )
        except Exception as exc:  # noqa: BLE001 - transport boundary sanitizes failures
            code = stable_tool_error(exc)
            return tool_result_error(
                code,
                retryable=code in {"UPSTREAM_TIMEOUT", "UPSTREAM_ERROR"},
                source=self.name,
            )


@dataclass(frozen=True)
class HTTPToolEndpoint:
    capability: str
    url: str
    bearer_token: str = ""


class HTTPToolAdapter:
    """Allowlisted JSON HTTP adapter; arbitrary model-provided URLs are impossible."""

    name = "http"

    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoints: Mapping[str, HTTPToolEndpoint],
        *,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = 512_000,
    ) -> None:
        self.client = client
        self._endpoints = dict(endpoints)
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def supports(self, capability: str) -> bool:
        return capability in self._endpoints

    async def invoke(self, capability: str, arguments: dict[str, Any]) -> ToolResultEnvelope:
        endpoint = self._endpoints.get(capability)
        if endpoint is None:
            return tool_result_error("TOOL_ADAPTER_UNAVAILABLE", retryable=False, source=self.name)
        headers = {"Accept": "application/json"}
        if endpoint.bearer_token:
            headers["Authorization"] = f"Bearer {endpoint.bearer_token}"
        try:
            response = await self.client.post(
                endpoint.url,
                json={"arguments": arguments},
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            if len(response.content) > self.max_response_bytes:
                return tool_result_error(
                    "UPSTREAM_RESPONSE_TOO_LARGE", retryable=False, source=self.name
                )
            return ToolResultEnvelope.model_validate(response.json())
        except Exception as exc:  # noqa: BLE001 - adapter returns stable errors only
            code = stable_tool_error(exc)
            return tool_result_error(code, retryable=code == "UPSTREAM_TIMEOUT", source=self.name)


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    endpoint: str
    allowed_tools: frozenset[str]
    bearer_token: str = ""
    protocol_version: str = "2025-11-25"


class MCPToolAdapter:
    """Minimal stateless Streamable HTTP MCP client with pinned local schemas."""

    name = "mcp"

    def __init__(
        self,
        client: httpx.AsyncClient,
        server: MCPServerConfig,
        *,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = 512_000,
    ) -> None:
        self.client = client
        self.server = server
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._request_id = 0
        self._discovered: dict[str, dict[str, Any]] | None = None
        self._discovery_lock = asyncio.Lock()

    def supports(self, capability: str) -> bool:
        return capability in self.server.allowed_tools

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.server.protocol_version,
        }
        if self.server.bearer_token:
            headers["Authorization"] = f"Bearer {self.server.bearer_token}"
        response = await self.client.post(
            self.server.endpoint,
            json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        if len(response.content) > self.max_response_bytes:
            raise ValueError("UPSTREAM_RESPONSE_TOO_LARGE")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise ValueError("UPSTREAM_ERROR")
        if payload.get("error") is not None:
            raise ValueError("UPSTREAM_ERROR")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("UPSTREAM_ERROR")
        return result

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.server.protocol_version,
        }
        if self.server.bearer_token:
            headers["Authorization"] = f"Bearer {self.server.bearer_token}"
        response = await self.client.post(
            self.server.endpoint,
            json={"jsonrpc": "2.0", "method": method, "params": params},
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

    async def _discover(self) -> dict[str, dict[str, Any]]:
        if self._discovered is not None:
            return self._discovered
        async with self._discovery_lock:
            if self._discovered is not None:
                return self._discovered
            await self._rpc(
                "initialize",
                {
                    "protocolVersion": self.server.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "mapgo-agent-runtime", "version": "1.0.0"},
                },
            )
            await self._notify("notifications/initialized", {})
            listed = await self._rpc("tools/list", {})
            tools = listed.get("tools")
            if not isinstance(tools, list):
                raise ValueError("UPSTREAM_ERROR")
            discovered: dict[str, dict[str, Any]] = {}
            for item in tools:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    discovered[item["name"]] = item
            self._discovered = discovered
            return discovered

    async def invoke(self, capability: str, arguments: dict[str, Any]) -> ToolResultEnvelope:
        if not self.supports(capability):
            return tool_result_error("TOOL_ADAPTER_UNAVAILABLE", retryable=False, source=self.name)
        try:
            tools = await self._discover()
            remote = tools.get(capability)
            local_schema = TOOL_REGISTRY.argument_schema(capability)
            if remote is None or remote.get("inputSchema") != local_schema:
                return tool_result_error("MCP_SCHEMA_MISMATCH", retryable=False, source=self.name)
            result = await self._rpc("tools/call", {"name": capability, "arguments": arguments})
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                raise ValueError("UPSTREAM_ERROR")
            return ToolResultEnvelope.model_validate(structured)
        except Exception as exc:  # noqa: BLE001 - never leak remote errors to a model
            code = stable_tool_error(exc)
            if str(exc) in {"UPSTREAM_RESPONSE_TOO_LARGE", "MCP_SCHEMA_MISMATCH"}:
                code = str(exc)
            return tool_result_error(code, retryable=code == "UPSTREAM_TIMEOUT", source=self.name)


class AgentToolRuntime:
    """Authorize, validate, then dispatch through a configured transport adapter."""

    def __init__(
        self,
        adapters: Sequence[ToolAdapter],
        *,
        registry: AgentToolRegistry = TOOL_REGISTRY,
    ) -> None:
        self.adapters = list(adapters)
        self.registry = registry

    async def execute(self, invocation: ToolInvocation) -> ToolResultEnvelope:
        self.registry.authorize(
            agent_type=invocation.agent_type,
            capability=invocation.capability,
            invocation_mode=invocation.invocation_mode,
            requested_scopes=invocation.requested_scopes,
        )
        try:
            arguments = validate_tool_arguments(invocation.capability, invocation.arguments)
        except Exception:
            return tool_result_error("INVALID_TOOL_ARGUMENTS", retryable=False)
        adapter = next(
            (item for item in self.adapters if item.supports(invocation.capability)), None
        )
        if adapter is None:
            return tool_result_error("TOOL_ADAPTER_UNAVAILABLE", retryable=False)
        return await adapter.invoke(invocation.capability, arguments)

    def executor(
        self,
        *,
        agent_type: AgentType,
        invocation_mode: InvocationMode,
        scopes_by_tool: Mapping[str, frozenset[DataScope]],
    ) -> Callable[[str, dict[str, Any]], Awaitable[ToolResultEnvelope]]:
        async def execute(tool: str, arguments: dict[str, Any]) -> ToolResultEnvelope:
            scopes = scopes_by_tool.get(tool, frozenset())
            return await self.execute(
                ToolInvocation(
                    agent_type=agent_type,
                    capability=tool,
                    invocation_mode=invocation_mode,
                    requested_scopes=scopes,
                    arguments=arguments,
                )
            )

        return execute


def parse_mcp_server_configs(raw: str) -> list[MCPServerConfig]:
    """Parse operator-owned config; endpoint URLs never come from model output."""

    if not raw.strip():
        return []
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("MCP_SERVERS_JSON must be a JSON array")
    configs: list[MCPServerConfig] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("every MCP server config must be an object")
        endpoint = str(item["endpoint"])
        parsed = urlparse(endpoint)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("MCP endpoints must use HTTPS except for loopback development")
        allowed_tools = frozenset(str(tool) for tool in item.get("allowed_tools", []))
        for tool in allowed_tools:
            capability = TOOL_REGISTRY.get(tool)
            if capability is None or capability.invocation_mode == InvocationMode.workflow_only:
                raise ValueError(f"MCP tool is not an Agent capability: {tool}")
        configs.append(
            MCPServerConfig(
                name=str(item["name"]),
                endpoint=endpoint,
                allowed_tools=allowed_tools,
                bearer_token=str(item.get("bearer_token", "")),
                protocol_version=str(item.get("protocol_version", "2025-11-25")),
            )
        )
    return configs
