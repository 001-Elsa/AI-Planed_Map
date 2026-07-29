import asyncio

from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore


def test_runtime_store_counter_json_queue_lock_and_retry():
    async def scenario():
        store = InMemoryRuntimeStore()
        assert await store.increment("rate:user", 60) == 1
        assert await store.increment("rate:user", 60) == 2
        assert await store.increment("tokens:user", 60, 25) == 25
        await store.set_json("trip:1", {"state": "ACTIVE_TRIP"}, 60)
        assert await store.get_json("trip:1") == {"state": "ACTIVE_TRIP"}
        await store.enqueue("events", {"event_id": 1})
        assert await store.dequeue("events", timeout_seconds=1) == {"event_id": 1}
        token = await store.acquire_lock("agent-run:1", 30)
        assert token
        assert await store.acquire_lock("agent-run:1", 30) is None
        assert await store.release_lock("agent-run:1", token) is True
        disposition = await store.enqueue_retry(
            "events", {"event_id": 2}, attempt=1, max_attempts=2, delay_seconds=0
        )
        assert disposition == "retry"
        dlq = await store.enqueue_retry(
            "events", {"event_id": 3}, attempt=2, max_attempts=2, delay_seconds=0
        )
        assert dlq == "dlq"
        await store.publish("trip:1", {"hello": "world"})
        await store.close()

    asyncio.run(scenario())
