"""Recoverable transports for validated Agent messages.

The protocol router owns authorization and schema validation. Transports own
durability, consumer claims, acknowledgement, retries and dead-lettering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from backend.app.core.observability import metrics
from backend.app.core.telemetry import inject_trace_context, traced
from backend.app.schemas.agent_artifacts import AgentEndpoint, AgentMessage, TraceContext
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_tool_contracts import stable_tool_error

logger = logging.getLogger("mapgo.agent-transport")


@dataclass(frozen=True)
class AgentMessageDelivery:
    message: AgentMessage
    receipt: str
    consumer: str
    delivery_count: int = 1
    reclaimed: bool = False
    pending_idle_ms: int = 0


@dataclass(frozen=True)
class AgentPublishResult:
    status: Literal["published", "duplicate"]
    stream_id: str | None = None


def new_agent_reply_inbox() -> str:
    """Return an opaque inbox token suitable for one request/response exchange."""

    return f"reply-{uuid4().hex}"


class AgentMessageTransport(Protocol):
    async def publish(self, message: AgentMessage) -> AgentPublishResult: ...

    async def receive(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        block_ms: int = 1_000,
        inbox: str | None = None,
    ) -> AgentMessageDelivery | None: ...

    async def acknowledge(self, delivery: AgentMessageDelivery) -> bool: ...

    async def retry(
        self,
        delivery: AgentMessageDelivery,
        *,
        error_code: str,
        max_attempts: int,
    ) -> Literal["retry", "dlq"]: ...

    async def reclaim(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        min_idle_ms: int,
        count: int = 10,
        inbox: str | None = None,
    ) -> list[AgentMessageDelivery]: ...

    async def pending_count(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int: ...

    async def dead_letters(
        self, receiver: AgentEndpoint, *, count: int = 20, inbox: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def dead_letter_count(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int: ...

    async def oldest_pending_age_ms(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int: ...


@dataclass
class _MemoryClaim:
    delivery: AgentMessageDelivery
    claimed_at: float


class InMemoryAgentMessageTransport:
    """Development transport with the same claim/ACK/retry contract as Redis."""

    def __init__(
        self,
        router: AgentMessageRouter | None = None,
        *,
        idempotency_ttl_seconds: int = 86_400,
        max_messages_per_role: int = 10_000,
    ) -> None:
        self.router = router or AgentMessageRouter()
        self.idempotency_ttl_seconds = max(60, idempotency_ttl_seconds)
        self.max_messages_per_role = max(100, max_messages_per_role)
        self._queues: dict[tuple[AgentEndpoint, str | None], deque[AgentMessage]] = defaultdict(
            deque
        )
        self._pending: dict[
            tuple[AgentEndpoint, str | None], dict[str, _MemoryClaim]
        ] = defaultdict(dict)
        self._dead_letters: dict[
            tuple[AgentEndpoint, str | None], list[dict[str, Any]]
        ] = defaultdict(list)
        self._published: dict[str, float] = {}
        self._condition = asyncio.Condition()

    async def publish(self, message: AgentMessage) -> AgentPublishResult:
        self.router.validate(message)
        async with self._condition:
            now = time.monotonic()
            for key, expires_at in list(self._published.items()):
                if expires_at <= now:
                    self._published.pop(key, None)
            if message.idempotency_key in self._published:
                return AgentPublishResult(status="duplicate")
            channel = (message.receiver, message.inbox)
            if len(self._queues[channel]) >= self.max_messages_per_role:
                raise RuntimeError("agent_message_queue_capacity_exceeded")
            while len(self._published) >= self.max_messages_per_role:
                self._published.pop(next(iter(self._published)))
            self._published[message.idempotency_key] = now + self.idempotency_ttl_seconds
            self._queues[channel].append(message)
            self._condition.notify_all()
        metrics.increment(
            "mapgo_agent_messages_published_total",
            {"transport": "memory", "receiver": message.receiver.value},
        )
        return AgentPublishResult(status="published", stream_id=str(message.message_id))

    async def receive(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        block_ms: int = 1_000,
        inbox: str | None = None,
    ) -> AgentMessageDelivery | None:
        deadline = time.monotonic() + max(0, block_ms) / 1_000
        channel = (receiver, inbox)
        async with self._condition:
            while not self._queues[channel]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except asyncio.TimeoutError:  # noqa: UP041 - distinct from built-in on Python 3.10
                    return None
            message = self._queues[channel].popleft()
            self.router.validate(message)
            receipt = uuid4().hex
            delivery = AgentMessageDelivery(
                message=message,
                receipt=receipt,
                consumer=consumer,
                delivery_count=message.attempt,
            )
            self._pending[channel][receipt] = _MemoryClaim(
                delivery=delivery, claimed_at=time.monotonic()
            )
            return delivery

    async def acknowledge(self, delivery: AgentMessageDelivery) -> bool:
        channel = (delivery.message.receiver, delivery.message.inbox)
        async with self._condition:
            removed = self._pending[channel].pop(delivery.receipt, None)
        if removed:
            metrics.increment(
                "mapgo_agent_messages_acked_total",
                {"transport": "memory", "receiver": delivery.message.receiver.value},
            )
        return removed is not None

    async def retry(
        self,
        delivery: AgentMessageDelivery,
        *,
        error_code: str,
        max_attempts: int,
    ) -> Literal["retry", "dlq"]:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between one and five")
        channel = (delivery.message.receiver, delivery.message.inbox)
        async with self._condition:
            claim = self._pending[channel].pop(delivery.receipt, None)
            if claim is None:
                raise ValueError("delivery is no longer pending")
            if max(delivery.message.attempt, delivery.delivery_count) >= max_attempts:
                self._dead_letters[channel].append(_dead_letter_payload(delivery, error_code))
                self._dead_letters[channel] = self._dead_letters[channel][
                    -self.max_messages_per_role :
                ]
                disposition: Literal["retry", "dlq"] = "dlq"
            else:
                retry_message = delivery.message.model_copy(
                    update={"attempt": delivery.message.attempt + 1}
                )
                self._queues[channel].append(retry_message)
                self._condition.notify_all()
                disposition = "retry"
        metrics.increment(
            "mapgo_agent_message_retries_total",
            {"transport": "memory", "disposition": disposition},
        )
        return disposition

    async def reclaim(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        min_idle_ms: int,
        count: int = 10,
        inbox: str | None = None,
    ) -> list[AgentMessageDelivery]:
        threshold = max(0, min_idle_ms) / 1_000
        now = time.monotonic()
        channel = (receiver, inbox)
        reclaimed: list[AgentMessageDelivery] = []
        async with self._condition:
            for receipt, claim in list(self._pending[channel].items()):
                if len(reclaimed) >= max(0, count) or now - claim.claimed_at < threshold:
                    continue
                delivery = AgentMessageDelivery(
                    message=claim.delivery.message,
                    receipt=receipt,
                    consumer=consumer,
                    delivery_count=claim.delivery.delivery_count + 1,
                    reclaimed=True,
                    pending_idle_ms=int((now - claim.claimed_at) * 1_000),
                )
                self._pending[channel][receipt] = _MemoryClaim(
                    delivery=delivery, claimed_at=now
                )
                reclaimed.append(delivery)
        if reclaimed:
            metrics.increment(
                "mapgo_agent_messages_reclaimed_total",
                {"transport": "memory", "receiver": receiver.value},
                value=len(reclaimed),
            )
        return reclaimed

    async def pending_count(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int:
        async with self._condition:
            return len(self._pending[(receiver, inbox)])

    async def dead_letters(
        self, receiver: AgentEndpoint, *, count: int = 20, inbox: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._condition:
            return list(self._dead_letters[(receiver, inbox)][-max(0, count) :])

    async def dead_letter_count(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int:
        async with self._condition:
            return len(self._dead_letters[(receiver, inbox)])

    async def oldest_pending_age_ms(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int:
        async with self._condition:
            claims = self._pending[(receiver, inbox)].values()
            oldest = min((claim.claimed_at for claim in claims), default=None)
        return int((time.monotonic() - oldest) * 1_000) if oldest is not None else 0


class RedisStreamAgentMessageTransport:
    """Redis Streams consumer-group transport with durable idempotency and PEL recovery."""

    _PUBLISH_SCRIPT = """
    if redis.call('set', KEYS[1], '1', 'NX', 'EX', ARGV[1]) then
        local entry_id = redis.call('xadd', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', 'message', ARGV[3])
        if ARGV[4] == '1' then
            redis.call('expire', KEYS[2], ARGV[1])
        end
        return entry_id
    end
    return false
    """

    def __init__(
        self,
        client: Any,
        *,
        router: AgentMessageRouter | None = None,
        stream_prefix: str = "mapgo:agent-messages",
        group_prefix: str = "mapgo:agent-workers",
        idempotency_ttl_seconds: int = 86_400,
        max_stream_length: int = 20_000,
    ) -> None:
        self.client = client
        self.router = router or AgentMessageRouter()
        self.stream_prefix = stream_prefix.rstrip(":")
        self.group_prefix = group_prefix.rstrip(":")
        self.idempotency_ttl_seconds = max(60, idempotency_ttl_seconds)
        self.max_stream_length = max(100, max_stream_length)
        self._initialized: set[tuple[AgentEndpoint, str | None]] = set()
        self._group_lock = asyncio.Lock()

    def _stream(self, receiver: AgentEndpoint, inbox: str | None = None) -> str:
        suffix = f":inbox:{inbox}" if inbox else ""
        return f"{self.stream_prefix}:{receiver.value}{suffix}"

    def _group(self, receiver: AgentEndpoint, inbox: str | None = None) -> str:
        suffix = f":inbox:{inbox}" if inbox else ""
        return f"{self.group_prefix}:{receiver.value}{suffix}"

    def _dlq(self, receiver: AgentEndpoint, inbox: str | None = None) -> str:
        return f"{self._stream(receiver, inbox)}:dlq"

    async def _ensure_group(self, receiver: AgentEndpoint, inbox: str | None = None) -> None:
        channel = (receiver, inbox)
        if channel in self._initialized:
            return
        async with self._group_lock:
            if channel in self._initialized:
                return
            try:
                await self.client.xgroup_create(
                    self._stream(receiver, inbox),
                    self._group(receiver, inbox),
                    id="0-0",
                    mkstream=True,
                )
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            if inbox:
                await self.client.expire(
                    self._stream(receiver, inbox), self.idempotency_ttl_seconds
                )
            self._initialized.add(channel)

    async def publish(self, message: AgentMessage) -> AgentPublishResult:
        self.router.validate(message)
        idempotency_key = f"{self.stream_prefix}:dedupe:{message.idempotency_key}"
        result = await self.client.eval(
            self._PUBLISH_SCRIPT,
            2,
            idempotency_key,
            self._stream(message.receiver, message.inbox),
            self.idempotency_ttl_seconds,
            self.max_stream_length,
            message.model_dump_json(),
            1 if message.inbox else 0,
        )
        if not result:
            return AgentPublishResult(status="duplicate")
        metrics.increment(
            "mapgo_agent_messages_published_total",
            {"transport": "redis_stream", "receiver": message.receiver.value},
        )
        return AgentPublishResult(status="published", stream_id=str(result))

    async def receive(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        block_ms: int = 1_000,
        inbox: str | None = None,
    ) -> AgentMessageDelivery | None:
        await self._ensure_group(receiver, inbox)
        rows = await self.client.xreadgroup(
            self._group(receiver, inbox),
            consumer,
            {self._stream(receiver, inbox): ">"},
            count=1,
            block=max(0, block_ms),
        )
        if not rows:
            return None
        _stream, entries = rows[0]
        receipt, fields = entries[0]
        message = _decode_stream_message(fields)
        self.router.validate(message)
        return AgentMessageDelivery(
            message=message,
            receipt=str(receipt),
            consumer=consumer,
            delivery_count=message.attempt,
        )

    async def acknowledge(self, delivery: AgentMessageDelivery) -> bool:
        receiver = delivery.message.receiver
        inbox = delivery.message.inbox
        acknowledged = await self.client.xack(
            self._stream(receiver, inbox), self._group(receiver, inbox), delivery.receipt
        )
        if acknowledged:
            metrics.increment(
                "mapgo_agent_messages_acked_total",
                {"transport": "redis_stream", "receiver": receiver.value},
            )
        return bool(acknowledged)

    async def retry(
        self,
        delivery: AgentMessageDelivery,
        *,
        error_code: str,
        max_attempts: int,
    ) -> Literal["retry", "dlq"]:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between one and five")
        receiver = delivery.message.receiver
        inbox = delivery.message.inbox
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.xack(
                self._stream(receiver, inbox),
                self._group(receiver, inbox),
                delivery.receipt,
            )
            if max(delivery.message.attempt, delivery.delivery_count) >= max_attempts:
                pipe.xadd(
                    self._dlq(receiver, inbox),
                    {"dead_letter": json.dumps(_dead_letter_payload(delivery, error_code))},
                    maxlen=self.max_stream_length,
                    approximate=True,
                )
                if inbox:
                    pipe.expire(self._dlq(receiver, inbox), self.idempotency_ttl_seconds)
                disposition: Literal["retry", "dlq"] = "dlq"
            else:
                retry_message = delivery.message.model_copy(
                    update={"attempt": delivery.message.attempt + 1}
                )
                pipe.xadd(
                    self._stream(receiver, inbox),
                    {"message": retry_message.model_dump_json()},
                    maxlen=self.max_stream_length,
                    approximate=True,
                )
                disposition = "retry"
            await pipe.execute()
        metrics.increment(
            "mapgo_agent_message_retries_total",
            {"transport": "redis_stream", "disposition": disposition},
        )
        return disposition

    async def reclaim(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        min_idle_ms: int,
        count: int = 10,
        inbox: str | None = None,
    ) -> list[AgentMessageDelivery]:
        await self._ensure_group(receiver, inbox)
        result = await self.client.xautoclaim(
            self._stream(receiver, inbox),
            self._group(receiver, inbox),
            consumer,
            min_idle_time=max(0, min_idle_ms),
            start_id="0-0",
            count=max(1, count),
        )
        entries = result[1] if result and len(result) > 1 else []
        deliveries: list[AgentMessageDelivery] = []
        for receipt, fields in entries:
            message = _decode_stream_message(fields)
            self.router.validate(message)
            delivery_count = message.attempt
            pending_rows = await self.client.xpending_range(
                self._stream(receiver, inbox),
                self._group(receiver, inbox),
                min=receipt,
                max=receipt,
                count=1,
            )
            if pending_rows:
                row = pending_rows[0]
                delivery_count = int(
                    row.get("times_delivered") or row.get(b"times_delivered") or delivery_count
                )
                pending_idle_ms = int(
                    row.get("time_since_delivered")
                    or row.get(b"time_since_delivered")
                    or 0
                )
            else:
                pending_idle_ms = 0
            deliveries.append(
                AgentMessageDelivery(
                    message=message,
                    receipt=str(receipt),
                    consumer=consumer,
                    delivery_count=delivery_count,
                    reclaimed=True,
                    pending_idle_ms=pending_idle_ms,
                )
            )
        if deliveries:
            metrics.increment(
                "mapgo_agent_messages_reclaimed_total",
                {"transport": "redis_stream", "receiver": receiver.value},
                value=len(deliveries),
            )
        return deliveries

    async def pending_count(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int:
        await self._ensure_group(receiver, inbox)
        summary = await self.client.xpending(
            self._stream(receiver, inbox), self._group(receiver, inbox)
        )
        if isinstance(summary, dict):
            return int(summary.get("pending") or 0)
        return int(summary[0]) if summary else 0

    async def dead_letters(
        self, receiver: AgentEndpoint, *, count: int = 20, inbox: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self.client.xrevrange(
            self._dlq(receiver, inbox), count=max(0, count)
        )
        result: list[dict[str, Any]] = []
        for _entry_id, fields in rows:
            raw = fields.get("dead_letter") or fields.get(b"dead_letter")
            if raw:
                result.append(json.loads(raw))
        return result

    async def dead_letter_count(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int:
        return int(await self.client.xlen(self._dlq(receiver, inbox)))

    async def oldest_pending_age_ms(
        self, receiver: AgentEndpoint, *, inbox: str | None = None
    ) -> int:
        await self._ensure_group(receiver, inbox)
        rows = await self.client.xpending_range(
            self._stream(receiver, inbox),
            self._group(receiver, inbox),
            min="-",
            max="+",
            count=1,
        )
        if not rows:
            return 0
        row = rows[0]
        return int(row.get("time_since_delivered") or row.get(b"time_since_delivered") or 0)


class RecoverableAgentMessageBus:
    """Validated message bus used by independently deployable Agent workers."""

    def __init__(self, transport: AgentMessageTransport, router: AgentMessageRouter) -> None:
        self.transport = transport
        self.router = router

    async def publish(self, message: AgentMessage) -> AgentPublishResult:
        with traced(
            f"agent.message.publish.{message.receiver.value}",
            kind="producer",
            attributes={
                "messaging.destination.name": message.receiver.value,
                "messaging.message.id": str(message.message_id),
                "messaging.operation": "publish",
                "messaging.system": "redis",
            },
        ):
            carrier = inject_trace_context()
            outbound = (
                message.model_copy(
                    update={"trace_context": TraceContext.model_validate(carrier)}
                )
                if carrier
                else message
            )
            self.router.validate(outbound)
            return await self.transport.publish(outbound)

    async def receive(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        block_ms: int = 1_000,
        inbox: str | None = None,
    ) -> AgentMessageDelivery | None:
        delivery = await self.transport.receive(
            receiver, consumer, block_ms=block_ms, inbox=inbox
        )
        if delivery is not None:
            self.router.validate(delivery.message)
        return delivery


AgentTaskHandler = Callable[[AgentMessage], Awaitable[AgentMessage | list[AgentMessage] | None]]


class AgentTaskWorker:
    """One-message worker with crash reclaim, ACK and stable-error retry handling."""

    def __init__(
        self,
        *,
        bus: RecoverableAgentMessageBus,
        endpoint: AgentEndpoint,
        consumer: str,
        handler: AgentTaskHandler,
        max_attempts: int = 3,
        reclaim_idle_ms: int = 30_000,
    ) -> None:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between one and five")
        self.bus = bus
        self.endpoint = endpoint
        self.consumer = consumer
        self.handler = handler
        self.max_attempts = max_attempts
        self.reclaim_idle_ms = max(1_000, reclaim_idle_ms)

    async def run_once(self, *, block_ms: int = 1_000) -> Literal["idle", "acked", "retry", "dlq"]:
        reclaimed = await self.bus.transport.reclaim(
            self.endpoint,
            self.consumer,
            min_idle_ms=self.reclaim_idle_ms,
            count=1,
        )
        delivery = (
            reclaimed[0]
            if reclaimed
            else await self.bus.receive(self.endpoint, self.consumer, block_ms=block_ms)
        )
        if delivery is None:
            return "idle"
        created_at = delivery.message.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        labels = {"role": self.endpoint.value}
        metrics.observe(
            "mapgo_agent_task_queue_delay_ms",
            max(0, (datetime.now(timezone.utc) - created_at).total_seconds() * 1_000),
            labels,
        )
        metrics.observe("mapgo_agent_task_attempt_count", delivery.delivery_count, labels)
        if delivery.reclaimed:
            metrics.observe("mapgo_agent_reclaim_age_ms", delivery.pending_idle_ms, labels)
        if delivery.delivery_count > self.max_attempts:
            disposition = await self.bus.transport.retry(
                delivery,
                error_code="DELIVERY_LIMIT_EXCEEDED",
                max_attempts=self.max_attempts,
            )
            metrics.increment("mapgo_agent_task_results_total", {**labels, "result": disposition})
            return disposition
        started = time.perf_counter()
        try:
            with traced(
                f"agent.task.{self.endpoint.value}",
                carrier=delivery.message.trace_context.carrier(),
                kind="consumer",
                attributes={
                    "agent.role": self.endpoint.value,
                    "messaging.message.id": str(delivery.message.message_id),
                    "messaging.operation": "process",
                    "messaging.system": "redis",
                },
            ):
                outputs = await self.handler(delivery.message)
            if outputs is not None:
                messages = outputs if isinstance(outputs, list) else [outputs]
                for message in messages:
                    if delivery.message.reply_to and message.inbox != delivery.message.reply_to:
                        raise ValueError("agent response did not target the request reply inbox")
                    await self.bus.publish(message)
            if not await self.bus.transport.acknowledge(delivery):
                metrics.increment(
                    "mapgo_agent_message_ack_failures_total",
                    {"receiver": self.endpoint.value},
                )
                # Leave an unacknowledged Redis entry in the PEL for reclaim.
                # A missing in-memory claim has already been handled elsewhere.
                metrics.increment(
                    "mapgo_agent_task_results_total", {**labels, "result": "retry"}
                )
                return "retry"
            metrics.increment("mapgo_agent_task_results_total", {**labels, "result": "acked"})
            return "acked"
        except Exception as exc:
            logger.exception(
                "Agent role handler failed endpoint=%s consumer=%s",
                self.endpoint.value,
                self.consumer,
            )
            disposition = await self.bus.transport.retry(
                delivery,
                error_code=stable_tool_error(exc),
                max_attempts=self.max_attempts,
            )
            metrics.increment("mapgo_agent_task_results_total", {**labels, "result": disposition})
            return disposition
        finally:
            metrics.observe(
                "mapgo_agent_task_duration_ms",
                (time.perf_counter() - started) * 1_000,
                labels,
            )


def build_agent_message_bus(
    *,
    mode: Literal["memory", "redis_stream", "auto"],
    runtime_store: Any,
    stream_prefix: str = "mapgo:agent-messages",
    group_prefix: str = "mapgo:agent-workers",
    idempotency_ttl_seconds: int = 86_400,
    max_stream_length: int = 20_000,
) -> RecoverableAgentMessageBus:
    router = AgentMessageRouter()
    redis_client = getattr(runtime_store, "client", None)
    selected = "redis_stream" if mode == "auto" and redis_client is not None else mode
    if selected == "auto":
        selected = "memory"
    if selected == "redis_stream":
        if redis_client is None:
            raise ValueError("redis_stream Agent transport requires a Redis runtime store")
        transport: AgentMessageTransport = RedisStreamAgentMessageTransport(
            redis_client,
            router=router,
            stream_prefix=stream_prefix,
            group_prefix=group_prefix,
            idempotency_ttl_seconds=idempotency_ttl_seconds,
            max_stream_length=max_stream_length,
        )
    else:
        transport = InMemoryAgentMessageTransport(
            router,
            idempotency_ttl_seconds=idempotency_ttl_seconds,
            max_messages_per_role=max_stream_length,
        )
    return RecoverableAgentMessageBus(transport, router)


def _decode_stream_message(fields: dict[Any, Any]) -> AgentMessage:
    raw = fields.get("message") or fields.get(b"message")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise ValueError("Redis Stream entry is missing the Agent message")
    return AgentMessage.model_validate_json(raw)


def _dead_letter_payload(delivery: AgentMessageDelivery, error_code: str) -> dict[str, Any]:
    return {
        "message": delivery.message.model_dump(mode="json"),
        "error_code": error_code[:120],
        "consumer": delivery.consumer[:120],
        "delivery_count": delivery.delivery_count,
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
