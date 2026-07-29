"""Fault-injection style checks for runtime store retry/DLQ and lock contention."""

import asyncio

from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore
from backend.app.services.notifications import NotificationService


def test_notification_dedupe_and_retry_to_dlq():
    async def scenario():
        store = InMemoryRuntimeStore()
        service = NotificationService(store)
        first = await service.enqueue(
            trip_id=1,
            user_id=1,
            channel="in_app",
            event_type="DeadlineRisk",
            title="t",
            body="b",
            template_key="DeadlineRisk:critical",
        )
        second = await service.enqueue(
            trip_id=1,
            user_id=1,
            channel="in_app",
            event_type="DeadlineRisk",
            title="t",
            body="b",
            template_key="DeadlineRisk:critical",
        )
        assert first["deduplicated"] is False
        assert second["deduplicated"] is True

        payload = first["notification"]
        for attempt in range(1, 6):
            disposition = await service.mark_failed(payload, "boom")
            payload = {**payload, "attempts": attempt}
            if attempt < 5:
                assert disposition == "retry"
            else:
                assert disposition == "dlq"

        # Lock contention: second acquire fails until release.
        token = await store.acquire_lock("trip-mutate:1", 5)
        assert token
        assert await store.acquire_lock("trip-mutate:1", 5) is None
        await store.release_lock("trip-mutate:1", token)

    asyncio.run(scenario())
