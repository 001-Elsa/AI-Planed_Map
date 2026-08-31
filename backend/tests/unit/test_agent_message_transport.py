import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessageType,
)
from backend.app.schemas.ai_intent import AIPlanRequest, Coordinate
from backend.app.schemas.dynamic_replanning import TripEventArtifact
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_transport import (
    AgentTaskWorker,
    InMemoryAgentMessageTransport,
    RecoverableAgentMessageBus,
    RedisStreamAgentMessageTransport,
    build_agent_message_bus,
)


def _planning_message(router: AgentMessageRouter):
    return router.build(
        task_id="plan-transport-test",
        sender=AgentEndpoint.user,
        receiver=AgentEndpoint.supervisor,
        message_type=AgentMessageType.command,
        artifact_type="planning_request",
        content=AIPlanRequest(text="visit a museum tomorrow").model_dump(mode="json"),
    )


def test_memory_transport_deduplicates_reclaims_acks_and_dead_letters():
    async def scenario() -> None:
        router = AgentMessageRouter()
        transport = InMemoryAgentMessageTransport(router)
        bus = RecoverableAgentMessageBus(transport, router)
        message = _planning_message(router)

        assert (await bus.publish(message)).status == "published"
        assert (await bus.publish(message)).status == "duplicate"
        first = await bus.receive(AgentEndpoint.supervisor, "worker-a", block_ms=0)
        assert first is not None
        assert await transport.pending_count(AgentEndpoint.supervisor) == 1

        reclaimed = await transport.reclaim(
            AgentEndpoint.supervisor, "worker-b", min_idle_ms=0, count=1
        )
        assert len(reclaimed) == 1
        assert reclaimed[0].reclaimed is True
        assert reclaimed[0].consumer == "worker-b"
        assert await transport.acknowledge(reclaimed[0]) is True
        assert await transport.pending_count(AgentEndpoint.supervisor) == 0

        second_message = router.build(
            task_id="plan-transport-retry",
            sender=AgentEndpoint.user,
            receiver=AgentEndpoint.supervisor,
            message_type=AgentMessageType.command,
            artifact_type="planning_request",
            content=AIPlanRequest(text="visit a park tomorrow").model_dump(mode="json"),
        )
        await bus.publish(second_message)

        async def fail(_message):
            raise RuntimeError("redis://internal-host?password=must-not-leak")

        worker = AgentTaskWorker(
            bus=bus,
            endpoint=AgentEndpoint.supervisor,
            consumer="worker-c",
            handler=fail,
            max_attempts=2,
        )
        assert await worker.run_once(block_ms=0) == "retry"
        assert await worker.run_once(block_ms=0) == "dlq"
        letters = await transport.dead_letters(AgentEndpoint.supervisor)
        assert len(letters) == 1
        assert letters[0]["error_code"] == "UPSTREAM_ERROR"
        assert "internal-host" not in str(letters)
        assert "must-not-leak" not in str(letters)

    asyncio.run(scenario())


class _FakePipeline:
    def __init__(self, client: "_FakeRedis") -> None:
        self.client = client
        self.operations: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def xack(self, *args, **kwargs):
        self.operations.append(("xack", args, kwargs))

    def xadd(self, *args, **kwargs):
        self.operations.append(("xadd", args, kwargs))

    async def execute(self):
        results = []
        for name, args, kwargs in self.operations:
            results.append(await getattr(self.client, name)(*args, **kwargs))
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
        self.pending: dict[tuple[str, str], dict[str, dict[str, str]]] = defaultdict(dict)
        self.dedupe: set[str] = set()
        self.sequence = 0

    async def xgroup_create(self, _stream, _group, **_kwargs):
        return True

    async def eval(self, _script, _keys, dedupe_key, stream, _ttl, _maxlen, message):
        if dedupe_key in self.dedupe:
            return None
        self.dedupe.add(dedupe_key)
        return await self.xadd(stream, {"message": message})

    async def xadd(self, stream, fields, **_kwargs):
        self.sequence += 1
        entry_id = f"{self.sequence}-0"
        self.streams[stream].append((entry_id, fields))
        return entry_id

    async def xreadgroup(self, group, _consumer, streams, **_kwargs):
        stream = next(iter(streams))
        if not self.streams[stream]:
            return []
        entry = self.streams[stream].pop(0)
        self.pending[(stream, group)][entry[0]] = entry[1]
        return [(stream, [entry])]

    async def xack(self, stream, group, receipt):
        return int(self.pending[(stream, group)].pop(receipt, None) is not None)

    async def xautoclaim(self, stream, group, _consumer, **_kwargs):
        entries = list(self.pending[(stream, group)].items())
        return ["0-0", entries, []]

    async def xpending(self, stream, group):
        return {"pending": len(self.pending[(stream, group)])}

    async def xpending_range(self, stream, group, **_kwargs):
        return [
            {"message_id": receipt, "times_delivered": 2}
            for receipt in self.pending[(stream, group)]
        ][:1]

    async def xrevrange(self, stream, **_kwargs):
        return list(reversed(self.streams[stream]))

    def pipeline(self, **_kwargs):
        return _FakePipeline(self)


def test_redis_stream_transport_uses_consumer_group_ack_retry_and_dlq():
    async def scenario() -> None:
        router = AgentMessageRouter()
        client = _FakeRedis()
        transport = RedisStreamAgentMessageTransport(client, router=router)
        message = _planning_message(router)

        published = await transport.publish(message)
        assert published.status == "published"
        assert (await transport.publish(message)).status == "duplicate"
        delivery = await transport.receive(
            AgentEndpoint.supervisor, "supervisor-worker-1", block_ms=0
        )
        assert delivery is not None
        assert await transport.pending_count(AgentEndpoint.supervisor) == 1
        reclaimed = await transport.reclaim(
            AgentEndpoint.supervisor,
            "supervisor-worker-recovery",
            min_idle_ms=0,
            count=1,
        )
        assert reclaimed[0].reclaimed is True
        assert reclaimed[0].delivery_count == 2
        assert await transport.acknowledge(reclaimed[0]) is True

        retry_message = router.build(
            task_id="plan-redis-retry",
            sender=AgentEndpoint.user,
            receiver=AgentEndpoint.supervisor,
            message_type=AgentMessageType.command,
            artifact_type="planning_request",
            content=AIPlanRequest(text="visit a gallery tomorrow").model_dump(mode="json"),
        )
        assert (await transport.publish(retry_message)).status == "published"
        delivery = await transport.receive(
            AgentEndpoint.supervisor, "supervisor-worker-1", block_ms=0
        )
        assert delivery is not None
        assert (
            await transport.retry(delivery, error_code="UPSTREAM_TIMEOUT", max_attempts=2)
            == "retry"
        )

        retry_delivery = await transport.receive(
            AgentEndpoint.supervisor, "supervisor-worker-2", block_ms=0
        )
        assert retry_delivery is not None
        assert retry_delivery.message.attempt == 2
        assert (
            await transport.retry(retry_delivery, error_code="UPSTREAM_TIMEOUT", max_attempts=2)
            == "dlq"
        )
        letters = await transport.dead_letters(AgentEndpoint.supervisor)
        assert letters[0]["error_code"] == "UPSTREAM_TIMEOUT"

    asyncio.run(scenario())


def test_transport_factory_auto_selects_runtime_capability_without_silent_redis_fallback():
    memory_bus = build_agent_message_bus(mode="auto", runtime_store=object())
    assert isinstance(memory_bus.transport, InMemoryAgentMessageTransport)

    redis_store = type("RedisStore", (), {"client": _FakeRedis()})()
    redis_bus = build_agent_message_bus(mode="auto", runtime_store=redis_store)
    assert isinstance(redis_bus.transport, RedisStreamAgentMessageTransport)

    try:
        build_agent_message_bus(mode="redis_stream", runtime_store=object())
    except ValueError as exc:
        assert "requires a Redis runtime store" in str(exc)
    else:
        raise AssertionError("explicit Redis transport must fail closed without Redis")


def test_replanner_worker_owns_role_and_returns_typed_directive():
    async def scenario() -> None:
        router = AgentMessageRouter()
        transport = InMemoryAgentMessageTransport(router)
        bus = RecoverableAgentMessageBus(transport, router)
        event = TripEventArtifact(
            trip_id=42,
            event_id=7,
            event_type="TrafficChanged",
            occurred_at=datetime.now(timezone.utc),
            impact_level="high",
            reason="traffic incident",
            payload_summary={
                "delay_minutes": 25,
                "_runtime": {
                    "current_location": Coordinate(lng=120.62, lat=31.32).model_dump(),
                    "completed_stop_ids": ["stop-1"],
                    "event_payload": {"delay_minutes": 25},
                    "weather": None,
                },
            },
            base_plan_version=3,
        )
        request = router.build(
            task_id="distributed-replanner-test",
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.replanner,
            message_type=AgentMessageType.command,
            artifact_type="trip_event_artifact",
            content=event.model_dump(mode="json"),
        )
        await bus.publish(request)

        from backend.app.worker import _handle_replanner_message

        worker = AgentTaskWorker(
            bus=bus,
            endpoint=AgentEndpoint.replanner,
            consumer="replanner-test-worker",
            handler=lambda message: _handle_replanner_message(message, router),
        )
        assert await worker.run_once(block_ms=0) == "acked"

        response = await bus.receive(AgentEndpoint.planner, "workflow-test", block_ms=0)
        assert response is not None
        assert response.message.sender == AgentEndpoint.replanner
        assert response.message.causation_id == request.message_id
        assert response.message.artifact_type == "replan_directive"
        assert response.message.content["directive"]["strategy"] == "fastest_feasible_route"

    asyncio.run(scenario())
