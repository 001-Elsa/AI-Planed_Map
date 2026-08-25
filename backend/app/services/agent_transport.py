"""Recoverable transports for validated Agent messages.

The protocol router owns authorization and schema validation. Transports own
durability, consumer claims, acknowledgement, retries and dead-lettering.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from backend.app.core.observability import metrics
from backend.app.schemas.agent_artifacts import AgentEndpoint, AgentMessage
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_tool_contracts import stable_tool_error


@dataclass(frozen=True)
class AgentMessageDelivery:
    message: AgentMessage
    receipt: str
    consumer: str
    delivery_count: int = 1
    reclaimed: bool = False


@dataclass(frozen=True)
class AgentPublishResult:
    status: Literal["published", "duplicate"]
    stream_id: str | None = None


class AgentMessageTransport(Protocol):
    async def publish(self, message: AgentMessage) -> AgentPublishResult: ...

    async def receive(
        self,
        receiver: AgentEndpoint,
        consumer: str,
        *,
        block_ms: int = 1_000,
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
    ) -> list[AgentMessageDelivery]: ...

    async def pending_count(self, receiver: AgentEndpoint) -> int: ...

    async def dead_letters(
        self, receiver: AgentEndpoint, *, count: int = 20
    ) -> list[dict[str, Any]]: ...


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
        self._queues: dict[AgentEndpoint, deque[AgentMessage]] = defaultdict(deque)
        self._pending: dict[AgentEndpoint, dict[str, _MemoryClaim]] = defaultdict(dict)
        self._dead_letters: dict[AgentEndpoint, list[dict[str, Any]]] = defaultdict(list)
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
            if len(self._queues[message.receiver]) >= self.max_messages_per_role:
                raise RuntimeError("agent_message_queue_capacity_exceeded")
            while len(self._published) >= self.max_messages_per_role:
                self._published.pop(next(iter(self._published)))
            self._published[message.idempotency_key] = now + self.idempotency_ttl_seconds
            self._queues[message.receiver].append(message)
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
    ) -> AgentMessageDelivery | None:
        deadline = time.monotonic() + max(0, block_ms) / 1_000
        async with self._condition:
            while not self._queues[receiver]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except TimeoutError:
                    return None
            message = self._queues[receiver].popleft()
            self.router.validate(message)
            receipt = uuid4().hex
            delivery = AgentMessageDelivery(
                message=message,
                receipt=receipt,
                consumer=consumer,
                delivery_count=message.attempt,
            )
            self._pending[receiver][receipt] = _MemoryClaim(
                delivery=delivery, claimed_at=time.monotonic()
            )
            return delivery

    async def acknowledge(self, delivery: AgentMessageDelivery) -> bool:
        async with self._condition:
            removed = self._pending[delivery.message.receiver].pop(delivery.receipt, None)
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
        async with self._condition:
            claim = self._pending[delivery.message.receiver].pop(delivery.receipt, None)
            if claim is None:
                raise ValueError("delivery is no longer pending")
            if max(delivery.message.attempt, delivery.delivery_count) >= max_attempts:
                self._dead_letters[delivery.message.receiver].append(
                    _dead_letter_payload(delivery, error_code)
                )
                self._dead_letters[delivery.message.receiver] = self._dead_letters[
                    delivery.message.receiver
                ][-self.max_messages_per_role :]
                disposition: Literal["retry", "dlq"] = "dlq"
            else:
                retry_message = delivery.message.model_copy(
                    update={"attempt": delivery.message.attempt + 1}
                )
                self._queues[retry_message.receiver].append(retry_message)
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
    ) -> list[AgentMessageDelivery]:
        threshold = max(0, min_idle_ms) / 1_000
        now = time.monotonic()
        reclaimed: list[AgentMessageDelivery] = []
        async with self._condition:
            for receipt, claim in list(self._pending[receiver].items()):
                if len(reclaimed) >= max(0, count) or now - claim.claimed_at < threshold:
                    continue
                delivery = AgentMessageDelivery(
                    message=claim.delivery.message,
                    receipt=receipt,
                    consumer=consumer,
                    delivery_count=claim.delivery.delivery_count + 1,
                    reclaimed=True,
                )
                self._pending[receiver][receipt] = _MemoryClaim(delivery=delivery, claimed_at=now)
                reclaimed.append(delivery)
        if reclaimed:
            metrics.increment(
                "mapgo_agent_messages_reclaimed_total",
                {"transport": "memory", "receiver": receiver.value},
                value=len(reclaimed),
            )
        return reclaimed

    async def pending_count(self, receiver: AgentEndpoint) -> int:
        async with self._condition:
            return len(self._pending[receiver])

    async def dead_letters(
        self, receiver: AgentEndpoint, *, count: int = 20
    ) -> list[dict[str, Any]]:
        async with self._condition:
            return list(self._dead_letters[receiver][-max(0, count) :])


class RedisStreamAgentMessageTransport:
    """Redis Streams consumer-group transport with durable idempotency and PEL recovery."""

    _PUBLISH_SCRIPT = """
    if redis.call('set', KEYS[1], '1', 'NX', 'EX', ARGV[1]) then
        return redis.call('xadd', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', 'message', ARGV[3])
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
        self._initialized: set[AgentEndpoint] = set()
        self._group_lock = asyncio.Lock()

    def _stream(self, receiver: AgentEndpoint) -> str:
        return f"{self.stream_prefix}:{receiver.value}"

    def _group(self, receiver: AgentEndpoint) -> str:
        return f"{self.group_prefix}:{receiver.value}"

    def _dlq(self, receiver: AgentEndpoint) -> str:
        return f"{self._stream(receiver)}:dlq"

    async def _ensure_group(self, receiver: AgentEndpoint) -> None:
        if receiver in self._initialized:
            return
        async with self._group_lock:
            if receiver in self._initialized:
                return
            try:
                await self.client.xgroup_create(
                    self._stream(receiver), self._group(receiver), id="0-0", mkstream=True
                )
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._initialized.add(receiver)

    async def publish(self, message: AgentMessage) -> AgentPublishResult:
        self.router.validate(message)
        idempotency_key = f"{self.stream_prefix}:dedupe:{message.idempotency_key}"
        result = await self.client.eval(
            self._PUBLISH_SCRIPT,
            2,
            idempotency_key,
            self._stream(message.receiver),
            self.idempotency_ttl_seconds,
            self.max_stream_length,
            message.model_dump_json(),
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
    ) -> AgentMessageDelivery | None:
        await self._ensure_group(receiver)
        rows = await self.client.xreadgroup(
            self._group(receiver),
            consumer,
            {self._stream(receiver): ">"},
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
        acknowledged = await self.client.xack(
            self._stream(receiver), self._group(receiver), delivery.receipt
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
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.xack(self._stream(receiver), self._group(receiver), delivery.receipt)
            if max(delivery.message.attempt, delivery.delivery_count) >= max_attempts:
                pipe.xadd(
                    self._dlq(receiver),
                    {"dead_letter": json.dumps(_dead_letter_payload(delivery, error_code))},
                    maxlen=self.max_stream_length,
                    approximate=True,
                )
                disposition: Literal["retry", "dlq"] = "dlq"
            else:
                retry_message = delivery.message.model_copy(
                    update={"attempt": delivery.message.attempt + 1}
                )
                pipe.xadd(
                    self._stream(receiver),
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
    ) -> list[AgentMessageDelivery]:
        await self._ensure_group(receiver)
        result = await self.client.xautoclaim(
            self._stream(receiver),
            self._group(receiver),
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
                self._stream(receiver),
                self._group(receiver),
                min=receipt,
                max=receipt,
                count=1,
            )
            if pending_rows:
                row = pending_rows[0]
                delivery_count = int(
                    row.get("times_delivered") or row.get(b"times_delivered") or delivery_count
                )
            deliveries.append(
                AgentMessageDelivery(
                    message=message,
                    receipt=str(receipt),
                    consumer=consumer,
                    delivery_count=delivery_count,
                    reclaimed=True,
                )
            )
        if deliveries:
            metrics.increment(
                "mapgo_agent_messages_reclaimed_total",
                {"transport": "redis_stream", "receiver": receiver.value},
                value=len(deliveries),
            )
        return deliveries

    async def pending_count(self, receiver: AgentEndpoint) -> int:
        await self._ensure_group(receiver)
        summary = await self.client.xpending(self._stream(receiver), self._group(receiver))
        if isinstance(summary, dict):
            return int(summary.get("pending") or 0)
        return int(summary[0]) if summary else 0

    async def dead_letters(
        self, receiver: AgentEndpoint, *, count: int = 20
    ) -> list[dict[str, Any]]:
        rows = await self.client.xrevrange(self._dlq(receiver), count=max(0, count))
        result: list[dict[str, Any]] = []
        for _entry_id, fields in rows:
            raw = fields.get("dead_letter") or fields.get(b"dead_letter")
            if raw:
                result.append(json.loads(raw))
        return result


class RecoverableAgentMessageBus:
    """Validated message bus used by independently deployable Agent workers."""

    def __init__(self, transport: AgentMessageTransport, router: AgentMessageRouter) -> None:
        self.transport = transport
        self.router = router

    async def publish(self, message: AgentMessage) -> AgentPublishResult:
        self.router.validate(message)
        return await self.transport.publish(message)

    async def receive(
        self, receiver: AgentEndpoint, consumer: str, *, block_ms: int = 1_000
    ) -> AgentMessageDelivery | None:
        delivery = await self.transport.receive(receiver, consumer, block_ms=block_ms)
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
        if delivery.delivery_count > self.max_attempts:
            return await self.bus.transport.retry(
                delivery,
                error_code="DELIVERY_LIMIT_EXCEEDED",
                max_attempts=self.max_attempts,
            )
        try:
            outputs = await self.handler(delivery.message)
            if outputs is not None:
                messages = outputs if isinstance(outputs, list) else [outputs]
                for message in messages:
                    await self.bus.publish(message)
            if not await self.bus.transport.acknowledge(delivery):
                metrics.increment(
                    "mapgo_agent_message_ack_failures_total",
                    {"receiver": self.endpoint.value},
                )
                # Leave an unacknowledged Redis entry in the PEL for reclaim.
                # A missing in-memory claim has already been handled elsewhere.
                return "retry"
            return "acked"
        except Exception as exc:
            return await self.bus.transport.retry(
                delivery,
                error_code=stable_tool_error(exc),
                max_attempts=self.max_attempts,
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
