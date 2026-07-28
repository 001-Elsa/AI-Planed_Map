import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Protocol


class RuntimeStore(Protocol):
    async def increment(self, key: str, ttl_seconds: int, amount: int = 1) -> int: ...
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None: ...
    async def enqueue(self, queue: str, value: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...


class InMemoryRuntimeStore:
    def __init__(self) -> None:
        self._values: dict[str, tuple[Any, float]] = {}
        self._counters: dict[str, tuple[int, float]] = {}
        self._queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def increment(self, key: str, ttl_seconds: int, amount: int = 1) -> int:
        async with self._lock:
            now = time.monotonic()
            value, expires = self._counters.get(key, (0, 0))
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

    async def close(self) -> None:
        return None


class RedisRuntimeStore:
    def __init__(self, client: Any) -> None:
        self.client = client

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

    async def close(self) -> None:
        await self.client.aclose()


async def build_runtime_store(redis_url: str) -> RuntimeStore:
    if not redis_url:
        return InMemoryRuntimeStore()
    from redis.asyncio import from_url

    client = from_url(redis_url, decode_responses=True)
    await client.ping()
    return RedisRuntimeStore(client)
