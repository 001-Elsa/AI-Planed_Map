import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy import event as sqlalchemy_event

from backend.app.core.config import get_settings
from backend.app.db.session import SessionLocal, engine
from backend.app.infrastructure.runtime_store import (
    RedisRuntimeStore,
    build_runtime_store,
    lock_fencing_token,
)
from backend.app.models import PlanningRun, TripEvent, TripSession, User
from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessageType,
)
from backend.app.schemas.ai_intent import AIPlanRequest
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_transport import RedisStreamAgentMessageTransport
from backend.app.worker import (
    TRIP_EVENTS_QUEUE,
    WorkerFenceRejectedError,
    _claim_worker_fence,
    _install_worker_fence_guard,
    _maintain_lock_lease,
    process_trip_event,
)

settings = get_settings()
REAL_INFRASTRUCTURE = settings.database_url.startswith("postgresql") and bool(settings.redis_url)
pytestmark = pytest.mark.skipif(
    not REAL_INFRASTRUCTURE,
    reason="requires real PostgreSQL and Redis",
)


async def _redis_store() -> RedisRuntimeStore:
    store = await build_runtime_store(settings.redis_url)
    assert isinstance(store, RedisRuntimeStore)
    return store


async def _create_event(*, suffix: str) -> tuple[int, int, int]:
    async with SessionLocal() as db:
        user = User(
            username=f"fault-{suffix}"[:20],
            nickname="fault-test",
            pass_hash="not-used",
        )
        db.add(user)
        await db.flush()
        planning_run = PlanningRun(
            user_id=user.id,
            input_text="fault recovery",
            intent_json="{}",
            status="success",
        )
        db.add(planning_run)
        await db.flush()
        trip = TripSession(
            user_id=user.id,
            planning_run_id=planning_run.id,
            state="ACTIVE_TRIP",
        )
        db.add(trip)
        await db.flush()
        event = TripEvent(
            trip_session_id=trip.id,
            event_id=f"fault-event-{suffix}",
            event_type="FaultInjection",
            payload_json="{}",
            occurred_at=datetime.now(timezone.utc),
            status="received",
            impact_level="none",
            decision_json="{}",
        )
        db.add(event)
        await db.commit()
        return user.id, planning_run.id, event.id


async def _delete_event_fixture(user_id: int, planning_run_id: int) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(TripSession).where(TripSession.planning_run_id == planning_run_id))
        await db.execute(delete(PlanningRun).where(PlanningRun.id == planning_run_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
    # pytest-asyncio uses a fresh loop per test; discard asyncpg connections
    # before that loop closes so the next fault case cannot reuse them.
    await engine.dispose()


@pytest.mark.asyncio
async def test_database_commit_before_ack_is_recovered_exactly_once() -> None:
    suffix = uuid4().hex[:10]
    user_id, planning_run_id, event_id = await _create_event(suffix=suffix)
    store = await _redis_store()
    trip_id = 0
    try:
        await store.client.delete(
            TRIP_EVENTS_QUEUE,
            f"{TRIP_EVENTS_QUEUE}:processing",
            f"trip-stream:{planning_run_id}",
        )
        payload = {
            "trip_id": None,
            "event_id": event_id,
            "event_type": "FaultInjection",
            "trace_id": suffix,
        }
        async with SessionLocal() as db:
            event = await db.get(TripEvent, event_id)
            assert event is not None
            payload["trip_id"] = event.trip_session_id
            trip_id = event.trip_session_id

        await store.enqueue(TRIP_EVENTS_QUEUE, payload)
        first = await store.reserve(TRIP_EVENTS_QUEUE, timeout_seconds=1)
        assert first is not None
        await process_trip_event(store, first.payload)

        async with SessionLocal() as db:
            committed = await db.get(TripEvent, event_id)
            assert committed is not None
            assert committed.status == "worker_processed"
            first_processed_at = committed.processed_at
            first_fence = committed.worker_fencing_token

        # Fault injection: process exits after DB commit but before queue ACK.
        assert await store.recover_processing(TRIP_EVENTS_QUEUE) == 1
        replay = await store.reserve(TRIP_EVENTS_QUEUE, timeout_seconds=1)
        assert replay is not None
        await process_trip_event(store, replay.payload)
        assert await store.acknowledge(TRIP_EVENTS_QUEUE, replay.receipt)

        async with SessionLocal() as db:
            recovered = await db.get(TripEvent, event_id)
            assert recovered is not None
            assert recovered.status == "worker_processed"
            assert recovered.processed_at == first_processed_at
            assert recovered.worker_fencing_token == first_fence
        stream = await store.get_json(f"trip-stream:{trip_id}")
        assert stream is not None and stream["sequence"] == 1
    finally:
        await store.client.delete(
            TRIP_EVENTS_QUEUE,
            f"{TRIP_EVENTS_QUEUE}:processing",
            f"trip-stream:{trip_id}",
        )
        await store.close()
        await _delete_event_fixture(user_id, planning_run_id)


@pytest.mark.asyncio
async def test_real_redis_stream_pending_entry_is_reclaimed() -> None:
    suffix = uuid4().hex
    store = await _redis_store()
    stream_prefix = f"fault:agent:{suffix}"
    group_prefix = f"fault:group:{suffix}"
    router = AgentMessageRouter()
    transport = RedisStreamAgentMessageTransport(
        store.client,
        router=router,
        stream_prefix=stream_prefix,
        group_prefix=group_prefix,
    )
    message = router.build(
        task_id=f"fault-{suffix}",
        sender=AgentEndpoint.user,
        receiver=AgentEndpoint.supervisor,
        message_type=AgentMessageType.command,
        artifact_type="planning_request",
        content=AIPlanRequest(text="visit a museum").model_dump(mode="json"),
    )
    try:
        assert (await transport.publish(message)).status == "published"
        delivery = await transport.receive(AgentEndpoint.supervisor, "crashed-worker", block_ms=100)
        assert delivery is not None
        assert await transport.pending_count(AgentEndpoint.supervisor) == 1

        reclaimed = await transport.reclaim(
            AgentEndpoint.supervisor,
            "recovery-worker",
            min_idle_ms=0,
            count=1,
        )
        assert len(reclaimed) == 1
        assert reclaimed[0].reclaimed is True
        assert reclaimed[0].delivery_count >= 2
        assert await transport.acknowledge(reclaimed[0])
        assert await transport.pending_count(AgentEndpoint.supervisor) == 0
    finally:
        await store.client.delete(
            f"{stream_prefix}:supervisor",
            f"{stream_prefix}:supervisor:dlq",
            f"{stream_prefix}:dedupe:{message.idempotency_key}",
        )
        await store.close()


@pytest.mark.asyncio
async def test_lost_lease_and_new_fence_reject_stale_worker_commit() -> None:
    suffix = uuid4().hex[:10]
    user_id, planning_run_id, event_id = await _create_event(suffix=suffix)
    store = await _redis_store()
    lock_name = f"fault-fence:{suffix}"
    old_db = SessionLocal()
    guard = None
    new_token: str | None = None
    try:
        old_token = await store.acquire_lock(lock_name, 5)
        assert old_token is not None
        old_fence = lock_fencing_token(old_token)
        assert await _claim_worker_fence(old_db, event_id=event_id, fencing_token=old_fence)
        lease_lost = asyncio.Event()
        guard = _install_worker_fence_guard(
            old_db,
            event_id=event_id,
            fencing_token=old_fence,
            lease_lost=lease_lost,
        )
        stale_event = await old_db.get(TripEvent, event_id)
        assert stale_event is not None

        assert await store.release_lock(lock_name, old_token)
        new_token = await store.acquire_lock(lock_name, 5)
        assert new_token is not None
        new_fence = lock_fencing_token(new_token)
        assert new_fence > old_fence
        async with SessionLocal() as new_db:
            assert await _claim_worker_fence(new_db, event_id=event_id, fencing_token=new_fence)

        lease_task = asyncio.create_task(
            _maintain_lock_lease(
                store,
                lock_name,
                old_token,
                ttl_seconds=5,
                interval_seconds=1,
                lease_lost=lease_lost,
            )
        )
        await asyncio.wait_for(lease_lost.wait(), timeout=2)
        await lease_task
        stale_event.decision_json = json.dumps({"stale_worker_committed": True})
        with pytest.raises(WorkerFenceRejectedError):
            await old_db.commit()
        await old_db.rollback()

        async with SessionLocal() as db:
            current = await db.get(TripEvent, event_id)
            assert current is not None
            assert current.worker_fencing_token == new_fence
            assert "stale_worker_committed" not in current.decision_json
    finally:
        if guard is not None:
            sqlalchemy_event.remove(old_db.sync_session, "before_commit", guard)
        await old_db.close()
        if new_token is not None:
            await store.release_lock(lock_name, new_token)
        await store.close()
        await _delete_event_fixture(user_id, planning_run_id)


@pytest.mark.asyncio
async def test_retry_promotion_is_atomic_under_competing_workers() -> None:
    suffix = uuid4().hex
    queue = f"fault:retry:{suffix}"
    store = await _redis_store()
    try:
        for job_id in range(100):
            assert (
                await store.enqueue_retry(
                    queue,
                    {"job_id": job_id},
                    attempt=1,
                    max_attempts=3,
                    delay_seconds=0,
                )
                == "retry"
            )
        moved = await asyncio.gather(*(store.promote_retries(queue, limit=100) for _ in range(8)))
        assert sum(moved) == 100
        assert await store.client.zcard(f"{queue}:retry") == 0
        payloads = [json.loads(item) for item in await store.client.lrange(queue, 0, -1)]
        assert len(payloads) == 100
        assert {item["job_id"] for item in payloads} == set(range(100))
    finally:
        await store.client.delete(queue, f"{queue}:retry", f"{queue}:dlq")
        await store.close()
