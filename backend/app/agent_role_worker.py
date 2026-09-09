"""Independent Redis Stream workers for remotely executable Agent roles."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx

from backend.app.clients.amap_client import build_map_provider
from backend.app.core.config import Settings, get_settings
from backend.app.core.metrics_server import start_metrics_server
from backend.app.core.observability import metrics
from backend.app.core.telemetry import configure_telemetry
from backend.app.infrastructure.runtime_store import build_runtime_store
from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessage,
    AgentMessageType,
)
from backend.app.schemas.ai_intent import Coordinate
from backend.app.schemas.dynamic_replanning import ReplanDirective, TripEventArtifact
from backend.app.services.agent_context import CriticContext, PlanningContext
from backend.app.services.agent_tool_adapters import (
    AgentToolRuntime,
    MCPToolAdapter,
    parse_mcp_server_configs,
)
from backend.app.services.agent_transport import (
    AgentTaskWorker,
    RecoverableAgentMessageBus,
    RedisStreamAgentMessageTransport,
    build_agent_message_bus,
)
from backend.app.services.agents.base import AgentExecution
from backend.app.services.agents.critic_agent import build_critic_agent
from backend.app.services.agents.planner_agent import PlannerAgent
from backend.app.services.agents.replanner_agent import ReplannerAgent

logger = logging.getLogger("mapgo.agent-role-worker")
ROLE_ENDPOINTS = {
    "planner": AgentEndpoint.planner,
    "critic": AgentEndpoint.critic,
    "replanner": AgentEndpoint.replanner,
}
RoleHandler = Callable[[AgentMessage], Awaitable[AgentMessage]]


async def monitor_agent_queue(
    transport: RedisStreamAgentMessageTransport,
    endpoint: AgentEndpoint,
    *,
    interval_seconds: int = 15,
) -> None:
    labels = {"role": endpoint.value}
    while True:
        try:
            metrics.set_gauge(
                "mapgo_agent_pending_messages",
                await transport.pending_count(endpoint),
                labels,
            )
            metrics.set_gauge(
                "mapgo_agent_dlq_messages",
                await transport.dead_letter_count(endpoint),
                labels,
            )
            metrics.set_gauge(
                "mapgo_agent_oldest_pending_age_ms",
                await transport.oldest_pending_age_ms(endpoint),
                labels,
            )
        except Exception:  # noqa: BLE001 - monitoring must not stop task processing
            metrics.increment("mapgo_agent_queue_monitor_errors_total", labels)
            logger.exception("Agent queue monitoring failed role=%s", endpoint.value)
        await asyncio.sleep(interval_seconds)


def _execution_content(execution: AgentExecution[Any]) -> dict[str, Any]:
    return {
        "output": execution.output.model_dump(mode="json"),
        "artifact": execution.artifact.model_dump(mode="json"),
        "metrics": {
            "latency_ms": execution.latency_ms,
            "input_tokens": execution.input_tokens,
            "output_tokens": execution.output_tokens,
            "estimated_cost_usd": execution.estimated_cost_usd,
            "fallback_used": execution.fallback_used,
            "reason": execution.reason,
        },
        "tool_calls": [item.model_dump(mode="json") for item in execution.tool_calls],
        "tool_audit_complete": execution.tool_audit_complete,
    }


def _response(
    bus: RecoverableAgentMessageBus,
    request: AgentMessage,
    *,
    sender: AgentEndpoint,
    artifact_type: str,
    content: dict[str, Any],
    receiver: AgentEndpoint = AgentEndpoint.supervisor,
) -> AgentMessage:
    if request.reply_to is None:
        raise ValueError("distributed Agent request is missing its reply inbox")
    return bus.router.build(
        task_id=request.task_id,
        sender=sender,
        receiver=receiver,
        message_type=AgentMessageType.result,
        artifact_type=artifact_type,
        content=content,
        correlation_id=request.correlation_id,
        causation_id=request.message_id,
        inbox=request.reply_to,
    )


async def handle_replanner_message(
    message: AgentMessage, bus: RecoverableAgentMessageBus
) -> AgentMessage:
    event = TripEventArtifact.model_validate(message.content)
    runtime = (event.payload_summary or {}).get("_runtime") or {}
    execution: AgentExecution[ReplanDirective] = await ReplannerAgent().run(
        event,
        current_location=Coordinate.model_validate(runtime.get("current_location")),
        completed_stop_ids=[str(item) for item in runtime.get("completed_stop_ids") or []],
        event_payload=runtime.get("event_payload") or {},
        weather=runtime.get("weather"),
    )
    return _response(
        bus,
        message,
        sender=AgentEndpoint.replanner,
        receiver=AgentEndpoint.planner,
        artifact_type="replan_directive",
        content={
            "directive": execution.output.model_dump(mode="json"),
            "execution": _execution_content(execution)["metrics"],
        },
    )


def build_role_handler(
    role: str,
    *,
    bus: RecoverableAgentMessageBus,
    settings: Settings,
    client: httpx.AsyncClient,
    external_tool_runtime: AgentToolRuntime | None = None,
) -> RoleHandler:
    if role == "planner":
        planner = PlannerAgent(
            build_map_provider(settings, client),
            settings,
            external_tool_runtime=external_tool_runtime,
        )

        async def handle_planner(message: AgentMessage) -> AgentMessage:
            context = PlanningContext.model_validate(message.content["context"])
            execution = await planner.run(context)
            return _response(
                bus,
                message,
                sender=AgentEndpoint.planner,
                artifact_type="planner_execution",
                content=_execution_content(execution),
            )

        return handle_planner
    if role == "critic":
        critic = build_critic_agent(settings, client)

        async def handle_critic(message: AgentMessage) -> AgentMessage:
            context = CriticContext.model_validate(message.content["context"])
            execution = await critic.run(context)
            return _response(
                bus,
                message,
                sender=AgentEndpoint.critic,
                artifact_type="critic_execution",
                content=_execution_content(execution),
            )

        return handle_critic
    if role == "replanner":
        return lambda message: handle_replanner_message(message, bus)
    raise ValueError(f"unsupported Agent worker role: {role}")


async def run_agent_role_worker(role: str) -> None:
    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError("Agent role workers require REDIS_URL")
    service_name = f"mapgo-agent-{role}"
    configure_telemetry(
        service_name,
        endpoint=settings.otel_exporter_otlp_endpoint,
        environment=settings.environment,
    )
    metrics_server = await start_metrics_server(
        service_name=service_name,
        host=settings.metrics_host,
        port=settings.metrics_port,
    )
    store = await build_runtime_store(settings.redis_url)
    bus = build_agent_message_bus(
        mode=settings.agent_message_transport,
        runtime_store=store,
        stream_prefix=settings.agent_stream_prefix,
        group_prefix=settings.agent_consumer_group_prefix,
        idempotency_ttl_seconds=settings.idempotency_ttl_seconds,
        max_stream_length=settings.agent_stream_max_length,
    )
    if not isinstance(bus.transport, RedisStreamAgentMessageTransport):
        await store.close()
        raise RuntimeError("independent Agent role workers require Redis Stream transport")
    timeout = httpx.Timeout(
        settings.external_timeout_seconds,
        connect=settings.external_connect_timeout_seconds,
    )
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    mcp_adapters = [
        MCPToolAdapter(
            client,
            server,
            timeout_seconds=settings.mcp_timeout_seconds,
            max_response_bytes=settings.mcp_max_response_bytes,
        )
        for server in parse_mcp_server_configs(settings.mcp_servers_json)
    ]
    external_tool_runtime = AgentToolRuntime(mcp_adapters) if mcp_adapters else None
    endpoint = ROLE_ENDPOINTS[role]
    identity = os.getenv("HOSTNAME") or uuid4().hex[:12]
    handler = build_role_handler(
        role,
        bus=bus,
        settings=settings,
        client=client,
        external_tool_runtime=external_tool_runtime,
    )
    worker = AgentTaskWorker(
        bus=bus,
        endpoint=endpoint,
        consumer=f"{role}-{identity}",
        handler=handler,
        max_attempts=settings.agent_message_max_attempts,
        reclaim_idle_ms=settings.agent_message_reclaim_idle_ms,
    )
    monitor = asyncio.create_task(
        monitor_agent_queue(bus.transport, endpoint),
        name=f"agent-queue-monitor-{role}",
    )
    logger.info("Agent role worker started role=%s consumer=%s", role, worker.consumer)
    try:
        while True:
            await worker.run_once(block_ms=1_000)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        metrics_server.close()
        await metrics_server.wait_closed()
        await client.aclose()
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one independently scalable Agent role")
    parser.add_argument("--role", required=True, choices=sorted(ROLE_ENDPOINTS))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_agent_role_worker(args.role))


if __name__ == "__main__":
    main()
