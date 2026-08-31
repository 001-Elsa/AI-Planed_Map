import asyncio

from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore, lock_fencing_token
from backend.app.services.notifications import NotificationService
from backend.app.services.trip_stream import publish_trip_stream


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
        await store.enqueue("reliable", {"event_id": "reserved"})
        reserved = await store.reserve("reliable", timeout_seconds=1)
        assert reserved and reserved.payload == {"event_id": "reserved"}
        assert await store.recover_processing("reliable") == 1
        reserved_again = await store.reserve("reliable", timeout_seconds=1)
        assert reserved_again and reserved_again.payload == {"event_id": "reserved"}
        assert await store.acknowledge("reliable", reserved_again.receipt) is True
        assert await store.recover_processing("reliable") == 0
        token = await store.acquire_lock("agent-run:1", 30)
        assert token
        assert lock_fencing_token(token) == 1
        assert await store.acquire_lock("agent-run:1", 30) is None
        assert await store.is_lock_owner("agent-run:1", token) is True
        assert await store.renew_lock("agent-run:1", token, 30) is True
        assert await store.renew_lock("agent-run:1", "wrong-token", 30) is False
        assert await store.release_lock("agent-run:1", token) is True
        next_token = await store.acquire_lock("agent-run:1", 30)
        assert next_token and lock_fencing_token(next_token) == 2
        assert await store.is_lock_owner("agent-run:1", token) is False
        assert await store.release_lock("agent-run:1", next_token) is True
        disposition = await store.enqueue_retry(
            "events", {"event_id": 2}, attempt=1, max_attempts=2, delay_seconds=0
        )
        assert disposition == "retry"
        dlq = await store.enqueue_retry(
            "events", {"event_id": 3}, attempt=2, max_attempts=2, delay_seconds=0
        )
        assert dlq == "dlq"
        await store.publish("trip:1", {"hello": "world"})
        notifications = NotificationService(store)
        await notifications.enqueue(
            trip_id=9,
            user_id=1,
            channel="in_app",
            event_type="DeadlineRiskDetected",
            title="风险",
            body="测试",
        )
        first_stream = await store.get_json("trip-stream:9")
        second_stream = await publish_trip_stream(store, 9, {"type": "TripStateChanged"})
        assert second_stream["sequence"] == first_stream["sequence"] + 1
        await store.close()

    asyncio.run(scenario())


def test_in_memory_runtime_store_bounds_untrusted_cache_and_counter_keys():
    async def scenario():
        store = InMemoryRuntimeStore(max_values=2, max_value_bytes=50, max_counters=2)

        await store.set_json("cache:1", {"value": "a" * 10}, 60)
        await store.set_json("cache:2", {"value": "b" * 10}, 60)
        await store.set_json("cache:3", {"value": "c" * 10}, 60)
        assert await store.get_json("cache:1") is None
        assert await store.get_json("cache:2") == {"value": "b" * 10}
        assert await store.get_json("cache:3") == {"value": "c" * 10}

        assert await store.increment("counter:1", 60) == 1
        assert await store.increment("counter:2", 60) == 1
        assert await store.increment("counter:3", 60) > 1
        assert len(store._counters) == 2

        await store.close()

    asyncio.run(scenario())
