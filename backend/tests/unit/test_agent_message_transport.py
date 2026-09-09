import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from backend.app.core.observability import MetricsRegistry
from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessageType,
)
from backend.app.schemas.ai_intent import AIPlanRequest, Coordinate
from backend.app.schemas.dynamic_replanning import TripEventArtifact
from backend.app.services import agent_protocol, agent_transport
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_transport import (
    AgentTaskWorker,
    InMemoryAgentMessageTransport,
    RecoverableAgentMessageBus,
    RedisStreamAgentMessageTransport,
    build_agent_message_bus,
    new_agent_reply_inbox,
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


def test_worker_records_retry_when_acknowledgement_fails(monkeypatch):
    async def scenario() -> None:
        router = AgentMessageRouter()
        transport = InMemoryAgentMessageTransport(router)
        bus = RecoverableAgentMessageBus(transport, router)
        await bus.publish(_planning_message(router))

        async def fail_ack(_delivery):
            return False

        monkeypatch.setattr(transport, "acknowledge", fail_ack)
        test_metrics = MetricsRegistry()
        monkeypatch.setattr(agent_transport, "metrics", test_metrics)
        worker = AgentTaskWorker(
            bus=bus,
            endpoint=AgentEndpoint.supervisor,
            consumer="ack-failure-worker",
            handler=lambda _message: asyncio.sleep(0, result=None),
        )

        assert await worker.run_once(block_ms=0) == "retry"
        assert (
            'mapgo_agent_task_results_total{result="retry",role="supervisor"} 1'
            in test_metrics.render()
        )

    asyncio.run(scenario())


def test_agent_message_carries_w3c_trace_context(monkeypatch):
    traceparent = f"00-{'1' * 32}-{'2' * 16}-01"
    monkeypatch.setattr(
        agent_protocol,
        "inject_trace_context",
        lambda: {"traceparent": traceparent, "tracestate": "mapgo=test"},
    )

    message = _planning_message(AgentMessageRouter())

    assert message.trace_context.traceparent == traceparent
    assert message.trace_context.tracestate == "mapgo=test"


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

    def expire(self, *args, **kwargs):
        self.operations.append(("expire", args, kwargs))

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

    async def eval(
        self, _script, _keys, dedupe_key, stream, _ttl, _maxlen, message, _targeted
    ):
        if dedupe_key in self.dedupe:
            return None
        self.dedupe.add(dedupe_key)
        return await self.xadd(stream, {"message": message})

    async def xadd(self, stream, fields, **_kwargs):
        self.sequence += 1
        entry_id = f"{self.sequence}-0"
        self.streams[stream].append((entry_id, fields))
        return entry_id

    async def expire(self, _stream, _ttl):
        return True

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
            {
                "message_id": receipt,
                "times_delivered": 2,
                "time_since_delivered": 31_000,
            }
            for receipt in self.pending[(stream, group)]
        ][:1]

    async def xrevrange(self, stream, **_kwargs):
        return list(reversed(self.streams[stream]))

    async def xlen(self, stream):
        return len(self.streams[stream])

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
        assert reclaimed[0].pending_idle_ms == 31_000
        assert await transport.oldest_pending_age_ms(AgentEndpoint.supervisor) == 31_000
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
        assert await transport.dead_letter_count(AgentEndpoint.supervisor) == 1

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
            reply_to=new_agent_reply_inbox(),
        )
        await bus.publish(request)

        from backend.app.agent_role_worker import handle_replanner_message

        worker = AgentTaskWorker(
            bus=bus,
            endpoint=AgentEndpoint.replanner,
            consumer="replanner-test-worker",
            handler=lambda message: handle_replanner_message(message, bus),
        )
        assert await worker.run_once(block_ms=0) == "acked"

        response = await bus.receive(
            AgentEndpoint.planner,
            "workflow-test",
            block_ms=0,
            inbox=request.reply_to,
        )
        assert response is not None
        assert response.message.sender == AgentEndpoint.replanner
        assert response.message.causation_id == request.message_id
        assert response.message.artifact_type == "replan_directive"
        assert response.message.content["directive"]["strategy"] == "fastest_feasible_route"

    asyncio.run(scenario())


def test_redis_reply_inboxes_isolate_concurrent_workflows():
    async def scenario() -> None:
        router = AgentMessageRouter()
        client = _FakeRedis()
        transport = RedisStreamAgentMessageTransport(client, router=router)
        bus = RecoverableAgentMessageBus(transport, router)
        event = TripEventArtifact(
            trip_id=42,
            event_id=8,
            event_type="TrafficChanged",
            occurred_at=datetime.now(timezone.utc),
            impact_level="high",
            reason="traffic incident",
            payload_summary={
                "delay_minutes": 25,
                "_runtime": {
                    "current_location": Coordinate(lng=120.62, lat=31.32).model_dump(),
                    "completed_stop_ids": [],
                    "event_payload": {"delay_minutes": 25},
                    "weather": None,
                },
            },
            base_plan_version=3,
        )
        requests = [
            router.build(
                task_id=f"distributed-replanner-{suffix}",
                sender=AgentEndpoint.supervisor,
                receiver=AgentEndpoint.replanner,
                message_type=AgentMessageType.command,
                artifact_type="trip_event_artifact",
                content=event.model_dump(mode="json"),
                reply_to=new_agent_reply_inbox(),
            )
            for suffix in ("workflow-a", "workflow-b")
        ]
        for request in requests:
            await bus.publish(request)

        from backend.app.agent_role_worker import handle_replanner_message

        worker = AgentTaskWorker(
            bus=bus,
            endpoint=AgentEndpoint.replanner,
            consumer="replanner-concurrent-worker",
            handler=lambda message: handle_replanner_message(message, bus),
        )
        assert await worker.run_once(block_ms=0) == "acked"
        assert await worker.run_once(block_ms=0) == "acked"

        response_b = await bus.receive(
            AgentEndpoint.planner,
            "workflow-b",
            block_ms=0,
            inbox=requests[1].reply_to,
        )
        response_a = await bus.receive(
            AgentEndpoint.planner,
            "workflow-a",
            block_ms=0,
            inbox=requests[0].reply_to,
        )
        shared = await bus.receive(AgentEndpoint.planner, "wrong-shared-consumer", block_ms=0)

        assert response_a is not None and response_b is not None
        assert response_a.message.task_id == requests[0].task_id
        assert response_b.message.task_id == requests[1].task_id
        assert response_a.message.causation_id == requests[0].message_id
        assert response_b.message.causation_id == requests[1].message_id
        assert shared is None
        assert await transport.acknowledge(response_a) is True
        assert await transport.acknowledge(response_b) is True

    asyncio.run(scenario())


def test_worker_rejects_response_that_omits_requested_reply_inbox():
    async def scenario() -> None:
        router = AgentMessageRouter()
        transport = InMemoryAgentMessageTransport(router)
        bus = RecoverableAgentMessageBus(transport, router)
        request = _planning_message(router).model_copy(
            update={"reply_to": new_agent_reply_inbox()}
        )
        # Rebuild through the router so reply_to is covered by the idempotency key.
        request = router.build(
            task_id=request.task_id,
            sender=request.sender,
            receiver=request.receiver,
            message_type=request.message_type,
            artifact_type=request.artifact_type,
            content=request.content,
            reply_to=request.reply_to,
        )
        await bus.publish(request)

        async def bad_handler(message):
            return router.build(
                task_id=message.task_id,
                sender=AgentEndpoint.supervisor,
                receiver=AgentEndpoint.final_answer,
                message_type=AgentMessageType.result,
                artifact_type="final_answer",
                content={"status": "success"},
                correlation_id=message.correlation_id,
                causation_id=message.message_id,
            )

        worker = AgentTaskWorker(
            bus=bus,
            endpoint=AgentEndpoint.supervisor,
            consumer="bad-reply-worker",
            handler=bad_handler,
            max_attempts=1,
        )
        assert await worker.run_once(block_ms=0) == "dlq"
        assert await bus.receive(AgentEndpoint.final_answer, "shared-reader", block_ms=0) is None
        letters = await transport.dead_letters(AgentEndpoint.supervisor)
        assert letters[0]["error_code"] == "UPSTREAM_ERROR"

    asyncio.run(scenario())
