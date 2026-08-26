import asyncio
import copy
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ReservedMessage:
    receipt: str
    payload: dict[str, Any]


class RuntimeStore(Protocol):
    async def increment(self, key: str, ttl_seconds: int, amount: int = 1) -> int: ...
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None: ...
    async def delete_json(self, key: str) -> bool: ...
    async def compare_set_json(
        self, key: str, expected_revision: int, value: Any, ttl_seconds: int
    ) -> bool: ...
    async def enqueue(self, queue: str, value: dict[str, Any]) -> None: ...
    async def dequeue(self, queue: str, timeout_seconds: int = 30) -> dict[str, Any] | None: ...
    async def reserve(self, queue: str, timeout_seconds: int = 30) -> ReservedMessage | None: ...
    async def acknowledge(self, queue: str, receipt: str) -> bool: ...
    async def recover_processing(self, queue: str, limit: int = 100) -> int: ...
    async def enqueue_retry(
        self,
        queue: str,
        value: dict[str, Any],
        *,
        attempt: int,
        max_attempts: int = 5,
        delay_seconds: int | None = None,
    ) -> str: ...
    async def acquire_lock(
        self, name: str, ttl_seconds: int, token: str | None = None
    ) -> str | None: ...
    async def renew_lock(self, name: str, token: str, ttl_seconds: int) -> bool: ...
    async def release_lock(self, name: str, token: str) -> bool: ...
    async def publish(self, channel: str, value: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...


def _retry_delay(attempt: int) -> int:
    return min(300, 2**attempt)


class InMemoryRuntimeStore:
    def __init__(
        self,
        *,
        max_values: int = 10_000,
        max_value_bytes: int = 64 * 1024 * 1024,
        max_counters: int = 50_000,
    ) -> None:
        if min(max_values, max_value_bytes, max_counters) <= 0:
            raise ValueError("in-memory runtime store limits must be positive")
        self._values: dict[str, tuple[Any, float]] = {}
        self._value_sizes: dict[str, int] = {}
        self._value_bytes = 0
        self._counters: dict[str, tuple[int, float]] = {}
        self._counter_operations = 0
        self._max_values = max_values
        self._max_value_bytes = max_value_bytes
        self._max_counters = max_counters
        self._queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._processing: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._locks: dict[str, tuple[str, float]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def increment(self, key: str, ttl_seconds: int, amount: int = 1) -> int:
        async with self._lock:
            now = time.monotonic()
            self._counter_operations += 1
            if self._counter_operations % 256 == 0 or len(self._counters) >= self._max_counters:
                self._purge_expired_counters(now)
            if key not in self._counters and len(self._counters) >= self._max_counters:
                # Fail closed under a key-flood instead of evicting active rate
                # limits, which would let the attacker reset their own budget.
                return 2**63 - 1
            value, expires = self._counters.get(key, (0, 0.0))
            if expires <= now:
                value, expires = 0, now + ttl_seconds
            value += amount
            self._counters[key] = value, expires
            return value

    async def get_json(self, key: str) -> Any | None:
        async with self._lock:
            item = self._values.get(key)
            if item is None or item[1] <= time.monotonic():
                self._remove_value(key)
                return None
            return copy.deepcopy(item[0])

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        async with self._lock:
            now = time.monotonic()
            self._purge_expired_values(now)
            encoded_size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            self._remove_value(key)
            if encoded_size > self._max_value_bytes:
                return
            while self._values and (
                len(self._values) >= self._max_values
                or self._value_bytes + encoded_size > self._max_value_bytes
            ):
                self._remove_value(next(iter(self._values)))
            self._values[key] = (copy.deepcopy(value), time.monotonic() + ttl_seconds)
            self._value_sizes[key] = encoded_size
            self._value_bytes += encoded_size

    async def delete_json(self, key: str) -> bool:
        async with self._lock:
            existed = key in self._values
            self._remove_value(key)
            return existed

    async def compare_set_json(
        self, key: str, expected_revision: int, value: Any, ttl_seconds: int
    ) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._purge_expired_values(now)
            current = self._values.get(key)
            if current is None:
                if expected_revision != -1:
                    return False
            else:
                current_value = current[0]
                if (
                    not isinstance(current_value, dict)
                    or int(current_value.get("revision", -1)) != expected_revision
                ):
                    return False
            encoded_size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            if encoded_size > self._max_value_bytes:
                return False
            self._remove_value(key)
            while self._values and (
                len(self._values) >= self._max_values
                or self._value_bytes + encoded_size > self._max_value_bytes
            ):
                self._remove_value(next(iter(self._values)))
            self._values[key] = (copy.deepcopy(value), now + ttl_seconds)
            self._value_sizes[key] = encoded_size
            self._value_bytes += encoded_size
            return True

    def _remove_value(self, key: str) -> None:
        self._values.pop(key, None)
        self._value_bytes -= self._value_sizes.pop(key, 0)

    def _purge_expired_values(self, now: float) -> None:
        for key, (_, expires) in list(self._values.items()):
            if expires <= now:
                self._remove_value(key)

    def _purge_expired_counters(self, now: float) -> None:
        for key, (_, expires) in list(self._counters.items()):
            if expires <= now:
                self._counters.pop(key, None)

    async def enqueue(self, queue: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._queues[queue].append(value)

    async def dequeue(self, queue: str, timeout_seconds: int = 30) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0, timeout_seconds)
        while True:
            async with self._lock:
                if self._queues[queue]:
                    return self._queues[queue].pop(0)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def reserve(self, queue: str, timeout_seconds: int = 30) -> ReservedMessage | None:
        deadline = time.monotonic() + max(0, timeout_seconds)
        while True:
            async with self._lock:
                if self._queues[queue]:
                    payload = self._queues[queue].pop(0)
                    receipt = uuid.uuid4().hex
                    self._processing[queue][receipt] = payload
                    return ReservedMessage(receipt=receipt, payload=payload)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def acknowledge(self, queue: str, receipt: str) -> bool:
        async with self._lock:
            return self._processing[queue].pop(receipt, None) is not None

    async def recover_processing(self, queue: str, limit: int = 100) -> int:
        async with self._lock:
            pending = list(self._processing[queue].items())[: max(0, limit)]
            for receipt, payload in pending:
                self._queues[queue].insert(0, payload)
                self._processing[queue].pop(receipt, None)
            return len(pending)

    async def enqueue_retry(
        self,
        queue: str,
        value: dict[str, Any],
        *,
        attempt: int,
        max_attempts: int = 5,
        delay_seconds: int | None = None,
    ) -> str:
        payload = dict(value)
        payload["_attempt"] = attempt
        payload["_max_attempts"] = max_attempts
        if attempt >= max_attempts:
            await self.enqueue(f"{queue}:dlq", payload)
            return "dlq"
        delay = delay_seconds if delay_seconds is not None else _retry_delay(attempt)
        payload["_available_at"] = time.time() + delay
        await self.enqueue(f"{queue}:retry", payload)
        return "retry"

    async def acquire_lock(
        self, name: str, ttl_seconds: int, token: str | None = None
    ) -> str | None:
        lock_token = token or uuid.uuid4().hex
        async with self._lock:
            now = time.monotonic()
            current = self._locks.get(name)
            if current and current[1] > now and current[0] != lock_token:
                return None
            self._locks[name] = (lock_token, now + ttl_seconds)
            return lock_token

    async def release_lock(self, name: str, token: str) -> bool:
        async with self._lock:
            current = self._locks.get(name)
            if current is None or current[0] != token:
                return False
            self._locks.pop(name, None)
            return True

    async def renew_lock(self, name: str, token: str, ttl_seconds: int) -> bool:
        async with self._lock:
            current = self._locks.get(name)
            now = time.monotonic()
            if current is None or current[0] != token or current[1] <= now:
                return False
            self._locks[name] = (token, now + ttl_seconds)
            return True

    async def publish(self, channel: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._values[f"pub:{channel}:latest"] = (value, time.monotonic() + 86_400)
            subscribers = list(self._subscribers.get(channel, []))
        for queue in subscribers:
            await queue.put(value)

    async def subscribe(self, channel: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscribers[channel].append(queue)
        return queue

    async def close(self) -> None:
        return None


class RedisRuntimeStore:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._pubsub = None

    async def increment(self, key: str, ttl_seconds: int, amount: int = 1) -> int:
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.incrby(key, amount)
            pipe.expire(key, ttl_seconds, nx=True)
            value, _ = await pipe.execute()
        return int(value)

    async def get_json(self, key: str) -> Any | None:
        value = await self.client.get(key)
        return json.loads(value) if value else None

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        await self.client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)

    async def delete_json(self, key: str) -> bool:
        return bool(await self.client.delete(key))

    async def compare_set_json(
        self, key: str, expected_revision: int, value: Any, ttl_seconds: int
    ) -> bool:
        script = """
        local current = redis.call('get', KEYS[1])
        if current then
            local decoded = cjson.decode(current)
            if tonumber(decoded['revision']) ~= tonumber(ARGV[1]) then
                return 0
            end
        elseif tonumber(ARGV[1]) ~= -1 then
            return 0
        end
        redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
        return 1
        """
        result = await self.client.eval(
            script,
            1,
            key,
            expected_revision,
            json.dumps(value, ensure_ascii=False),
            ttl_seconds,
        )
        return bool(result)

    async def enqueue(self, queue: str, value: dict[str, Any]) -> None:
        await self.client.lpush(queue, json.dumps(value, ensure_ascii=False))

    async def dequeue(self, queue: str, timeout_seconds: int = 30) -> dict[str, Any] | None:
        item = await self.client.brpop(queue, timeout=timeout_seconds)
        if not item:
            return None
        _, payload = item
        return json.loads(payload)

    async def reserve(self, queue: str, timeout_seconds: int = 30) -> ReservedMessage | None:
        processing = f"{queue}:processing"
        payload = await self.client.brpoplpush(queue, processing, timeout=timeout_seconds)
        if not payload:
            return None
        return ReservedMessage(receipt=str(payload), payload=json.loads(payload))

    async def acknowledge(self, queue: str, receipt: str) -> bool:
        return bool(await self.client.lrem(f"{queue}:processing", 1, receipt))

    async def recover_processing(self, queue: str, limit: int = 100) -> int:
        moved = 0
        for _ in range(max(0, limit)):
            payload = await self.client.rpoplpush(f"{queue}:processing", queue)
            if payload is None:
                break
            moved += 1
        return moved

    async def enqueue_retry(
        self,
        queue: str,
        value: dict[str, Any],
        *,
        attempt: int,
        max_attempts: int = 5,
        delay_seconds: int | None = None,
    ) -> str:
        payload = dict(value)
        payload["_attempt"] = attempt
        payload["_max_attempts"] = max_attempts
        encoded = json.dumps(payload, ensure_ascii=False)
        if attempt >= max_attempts:
            await self.client.lpush(f"{queue}:dlq", encoded)
            return "dlq"
        delay = delay_seconds if delay_seconds is not None else _retry_delay(attempt)
        score = time.time() + delay
        await self.client.zadd(f"{queue}:retry", {encoded: score})
        return "retry"

    async def promote_retries(self, queue: str, limit: int = 50) -> int:
        """Move due retry jobs back onto the primary queue."""
        now = time.time()
        key = f"{queue}:retry"
        items = await self.client.zrangebyscore(key, min=0, max=now, start=0, num=limit)
        moved = 0
        for item in items:
            removed = await self.client.zrem(key, item)
            if removed:
                await self.client.lpush(queue, item)
                moved += 1
        return moved

    async def acquire_lock(
        self, name: str, ttl_seconds: int, token: str | None = None
    ) -> str | None:
        lock_token = token or uuid.uuid4().hex
        acquired = await self.client.set(f"lock:{name}", lock_token, nx=True, ex=ttl_seconds)
        return lock_token if acquired else None

    async def release_lock(self, name: str, token: str) -> bool:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        result = await self.client.eval(script, 1, f"lock:{name}", token)
        return bool(result)

    async def renew_lock(self, name: str, token: str, ttl_seconds: int) -> bool:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('expire', KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = await self.client.eval(script, 1, f"lock:{name}", token, ttl_seconds)
        return bool(result)

    async def publish(self, channel: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        await self.client.set(f"pub:{channel}:latest", encoded, ex=86_400)
        await self.client.publish(channel, encoded)

    async def close(self) -> None:
        if self._pubsub is not None:
            await self._pubsub.aclose()
        await self.client.aclose()


async def build_runtime_store(redis_url: str) -> RuntimeStore:
    if not redis_url:
        return InMemoryRuntimeStore()
    from redis.asyncio import from_url

    client = from_url(redis_url, decode_responses=True)
    await client.ping()
    return RedisRuntimeStore(client)
