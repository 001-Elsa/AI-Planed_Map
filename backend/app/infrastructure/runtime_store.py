import asyncio
import json
import time
import uuid
from collections import defaultdict
from typing import Any, Protocol


class RuntimeStore(Protocol):
    async def increment(self, key: str, ttl_seconds: int, amount: int = 1) -> int: ...
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None: ...
    async def enqueue(self, queue: str, value: dict[str, Any]) -> None: ...
    async def dequeue(self, queue: str, timeout_seconds: int = 30) -> dict[str, Any] | None: ...
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
    async def release_lock(self, name: str, token: str) -> bool: ...
    async def publish(self, channel: str, value: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...


def _retry_delay(attempt: int) -> int:
    return min(300, 2**attempt)


class InMemoryRuntimeStore:
    def __init__(self) -> None:
        self._values: dict[str, tuple[Any, float]] = {}
        self._counters: dict[str, tuple[int, float]] = {}
        self._queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._locks: dict[str, tuple[str, float]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def increment(self, key: str, ttl_seconds: int, amount: int = 1) -> int:
        async with self._lock:
            now = time.monotonic()
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
                self._values.pop(key, None)
                return None
            return item[0]

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        async with self._lock:
            self._values[key] = (value, time.monotonic() + ttl_seconds)

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

    async def enqueue(self, queue: str, value: dict[str, Any]) -> None:
        await self.client.lpush(queue, json.dumps(value, ensure_ascii=False))

    async def dequeue(self, queue: str, timeout_seconds: int = 30) -> dict[str, Any] | None:
        item = await self.client.brpop(queue, timeout=timeout_seconds)
        if not item:
            return None
        _, payload = item
        return json.loads(payload)

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
