import asyncio

from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore
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
