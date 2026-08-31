import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.clients.amap_client import build_map_provider
from backend.app.clients.weather_client import build_weather_provider
from backend.app.core.config import get_settings
from backend.app.core.observability import metrics
from backend.app.core.privacy import read_location
from backend.app.db.session import SessionLocal, engine
from backend.app.infrastructure.runtime_store import (
    RedisRuntimeStore,
    build_runtime_store,
    lock_fencing_token,
)
from backend.app.models import (
    AgentSession,
    LocationSnapshot,
    PlanVersion,
    TripEvent,
    TripSession,
    UserConsent,
)
from backend.app.schemas.agent_artifacts import AgentEndpoint, AgentMessageType
from backend.app.schemas.companion import ConsentScope
from backend.app.schemas.dynamic_replanning import TripEventArtifact
from backend.app.services.agent_controller import AgentController
from backend.app.services.agent_decider import AgentDecider, build_agent_decider
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.agent_transport import (
    AgentTaskWorker,
    RecoverableAgentMessageBus,
    build_agent_message_bus,
)
from backend.app.services.agents.replanner_agent import ReplannerAgent
from backend.app.services.dynamic_replanning import DynamicReplanningOrchestrator
from backend.app.services.notifications import NotificationService, render_event_notification
from backend.app.services.trip_stream import publish_trip_stream

logger = logging.getLogger("mapgo.worker")
TRIP_EVENTS_QUEUE = "mapgo:trip-events"
NOTIFICATION_QUEUE = "mapgo:notifications"


class WorkerFenceRejectedError(RuntimeError):
    """Raised when a stale Worker attempts to commit after losing its lease."""


def _install_worker_fence_guard(
    db: AsyncSession,
    *,
    event_id: int,
    fencing_token: int,
    lease_lost: asyncio.Event,
):
    def guard(sync_session) -> None:
        if lease_lost.is_set():
            metrics.increment("mapgo_worker_fence_rejections_total", {"reason": "lease_lost"})
            raise WorkerFenceRejectedError("worker lease was lost before commit")
        with sync_session.no_autoflush:
            current = sync_session.execute(
                select(TripEvent.worker_fencing_token)
                .where(TripEvent.id == event_id)
                .with_for_update()
            ).scalar_one_or_none()
        if current != fencing_token:
            metrics.increment("mapgo_worker_fence_rejections_total", {"reason": "superseded"})
            raise WorkerFenceRejectedError(
                f"worker fence {fencing_token} was superseded by {current}"
            )

    sqlalchemy_event.listen(db.sync_session, "before_commit", guard)
    return guard


async def _claim_worker_fence(
    db: AsyncSession,
    *,
    event_id: int,
    fencing_token: int,
) -> bool:
    result = await db.execute(
        update(TripEvent)
        .where(
            TripEvent.id == event_id,
            TripEvent.status != "worker_processed",
            TripEvent.worker_fencing_token < fencing_token,
        )
        .values(
            status="worker_processing",
            worker_fencing_token=fencing_token,
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def _handle_replanner_message(message, router):
    event = TripEventArtifact.model_validate(message.content)
    runtime = (event.payload_summary or {}).get("_runtime") or {}
    from backend.app.schemas.ai_intent import Coordinate

    current_location = Coordinate.model_validate(runtime.get("current_location"))
    execution = await ReplannerAgent().run(
        event,
        current_location=current_location,
        completed_stop_ids=[str(item) for item in runtime.get("completed_stop_ids") or []],
        event_payload=runtime.get("event_payload") or {},
        weather=runtime.get("weather"),
    )
    return router.build(
        task_id=message.task_id,
        sender=AgentEndpoint.replanner,
        receiver=AgentEndpoint.planner,
        message_type=AgentMessageType.result,
        artifact_type="replan_directive",
        content={
            "directive": execution.output.model_dump(mode="json"),
            "execution": {
                "latency_ms": execution.latency_ms,
                "input_tokens": execution.input_tokens,
                "output_tokens": execution.output_tokens,
                "estimated_cost_usd": execution.estimated_cost_usd,
            },
        },
        correlation_id=message.correlation_id,
        causation_id=message.message_id,
    )


async def run_replanner_agent_worker(bus: RecoverableAgentMessageBus, settings) -> None:
    """Own the Replanner role in distributed mode; Planner remains a stage owner."""
    worker = AgentTaskWorker(
        bus=bus,
        endpoint=AgentEndpoint.replanner,
        consumer=f"replanner-{uuid4().hex[:12]}",
        handler=lambda message: _handle_replanner_message(message, bus.router),
        max_attempts=settings.agent_message_max_attempts,
        reclaim_idle_ms=settings.agent_message_reclaim_idle_ms,
    )
    while True:
        result = await worker.run_once(block_ms=1_000)
        if result == "idle":
            await asyncio.sleep(0)


async def _maintain_lock_lease(
    store,
    lock_name: str,
    token: str,
    *,
    ttl_seconds: int,
    interval_seconds: int,
    lease_lost: asyncio.Event | None = None,
) -> None:
    lost = lease_lost or asyncio.Event()
    while True:
        await asyncio.sleep(max(1, interval_seconds))
        try:
            renewed = await store.renew_lock(lock_name, token, ttl_seconds)
        except Exception:  # noqa: BLE001 - lease uncertainty must fail closed
            renewed = False
            logger.exception("worker_lock_renewal_errored lock=%s", lock_name)
        if not renewed:
            lost.set()
            metrics.increment("mapgo_worker_lock_renewal_failed_total")
            logger.warning("worker_lock_renewal_failed lock=%s", lock_name)
            return
        metrics.increment("mapgo_worker_lock_renewals_total")


async def cleanup_expired_locations() -> int:
    async with SessionLocal() as db:
        result = await db.execute(
            delete(LocationSnapshot).where(
                LocationSnapshot.expires_at <= datetime.now(timezone.utc)
            )
        )
        await db.commit()
        return result.rowcount


async def _consents_for_trip(db, trip: TripSession) -> set[ConsentScope]:
    rows = (
        await db.scalars(
            select(UserConsent).where(
                UserConsent.trip_session_id == trip.id,
                UserConsent.user_id == trip.user_id,
                UserConsent.granted.is_(True),
                UserConsent.revoked_at.is_(None),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    scopes: set[ConsentScope] = set()
    for row in rows:
        expires = row.expires_at
        if expires and not expires.tzinfo:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is None or expires > now:
            try:
                scopes.add(ConsentScope(row.scope))
            except ValueError:
                continue
    return scopes


async def process_trip_event(
    store,
    payload: dict[str, Any],
    *,
    map_provider=None,
    weather_provider=None,
    decider: AgentDecider | None = None,
    message_bus: RecoverableAgentMessageBus | None = None,
) -> None:
    trip_id = int(payload["trip_id"])
    event_id = payload.get("event_id")
    event_type = str(payload.get("event_type") or "")
    attempt = int(payload.get("_attempt") or 0)
    settings = get_settings()
    lock_name = f"agent-run:trip:{trip_id}"
    token = await store.acquire_lock(lock_name, settings.worker_lock_ttl_seconds)
    if token is None:
        await store.enqueue_retry(TRIP_EVENTS_QUEUE, payload, attempt=attempt + 1, max_attempts=8)
        metrics.increment("mapgo_worker_lock_contention_total", {"queue": "trip-events"})
        return
    fencing_token = lock_fencing_token(token)
    lease_lost = asyncio.Event()
    lease_task = asyncio.create_task(
        _maintain_lock_lease(
            store,
            lock_name,
            token,
            ttl_seconds=settings.worker_lock_ttl_seconds,
            interval_seconds=settings.worker_lock_renew_interval_seconds,
            lease_lost=lease_lost,
        )
    )
    fenced_db: AsyncSession | None = None
    fence_guard = None

    try:
        async with SessionLocal() as db:
            trip = await db.get(TripSession, trip_id)
            if trip is None:
                logger.warning("trip_missing trip_id=%s", trip_id)
                return
            event = None
            if event_id is not None:
                event = await db.get(TripEvent, int(event_id))
            if event is not None and event.status == "worker_processed":
                metrics.increment(
                    "mapgo_worker_events_total", {"type": event_type, "result": "deduplicated"}
                )
                return
            if event is not None:
                if lease_lost.is_set() or not await store.is_lock_owner(lock_name, token):
                    raise WorkerFenceRejectedError("worker lost lock before fencing claim")
                if not await _claim_worker_fence(
                    db,
                    event_id=event.id,
                    fencing_token=fencing_token,
                ):
                    await db.refresh(event)
                    metrics.increment(
                        "mapgo_worker_events_total",
                        {"type": event_type, "result": "fenced"},
                    )
                    return
                await db.refresh(event)
                fenced_db = db
                fence_guard = _install_worker_fence_guard(
                    db,
                    event_id=event.id,
                    fencing_token=fencing_token,
                    lease_lost=lease_lost,
                )
            decision = json.loads(event.decision_json) if event and event.decision_json else {}
            event_payload = json.loads(event.payload_json) if event and event.payload_json else {}
            should_notify = bool(decision.get("should_notify"))
            impact = event.impact_level if event else "none"

            notifications = NotificationService(store)
            if should_notify:
                title, body = render_event_notification(event_type, decision)
                await notifications.enqueue(
                    trip_id=trip.id,
                    user_id=trip.user_id,
                    channel="in_app",
                    event_type=event_type,
                    title=title,
                    body=body,
                    payload={"impact_level": impact, "decision": decision},
                    template_key=f"{event_type}:{impact}",
                )

            agent = await db.scalar(
                select(AgentSession).where(AgentSession.trip_session_id == trip.id)
            )
            if agent is not None and impact in {"high", "critical"}:
                consents = await _consents_for_trip(db, trip)
                latest_location = await db.scalar(
                    select(LocationSnapshot)
                    .where(
                        LocationSnapshot.trip_session_id == trip.id,
                        LocationSnapshot.expires_at > datetime.now(timezone.utc),
                    )
                    .order_by(LocationSnapshot.captured_at.desc())
                )
                current_location = None
                if latest_location is not None and ConsentScope.precise_location in consents:
                    lng, lat = read_location(latest_location)
                    if lng is not None and lat is not None:
                        from backend.app.schemas.ai_intent import Coordinate

                        current_location = Coordinate(lng=lng, lat=lat)
                version = await db.scalar(
                    select(PlanVersion).where(
                        PlanVersion.planning_run_id == trip.planning_run_id,
                        PlanVersion.version == trip.current_plan_version,
                    )
                )
                if current_location is None and version is not None:
                    from backend.app.schemas.ai_intent import Coordinate

                    current_location = Coordinate.model_validate(
                        json.loads(version.snapshot_json)["origin"]
                    )
                weather_observation: dict[str, Any] | None = None
                replan_result: dict[str, Any] | None = None

                async def tool_executor(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "get_trip_state":
                        return {"state": trip.state, "plan_version": trip.current_plan_version}
                    if tool == "get_current_location":
                        snap = await db.scalar(
                            select(LocationSnapshot)
                            .where(
                                LocationSnapshot.trip_session_id == trip.id,
                                LocationSnapshot.expires_at > datetime.now(timezone.utc),
                            )
                            .order_by(LocationSnapshot.captured_at.desc())
                        )
                        return {"has_location": snap is not None}
                    if tool == "get_weather":
                        nonlocal weather_observation
                        if weather_provider is None or current_location is None:
                            return {"status": "weather_unavailable"}
                        weather_observation = (
                            await weather_provider.current(current_location)
                        ).model_dump(mode="json")
                        return weather_observation
                    if tool == "propose_replan":
                        nonlocal replan_result
                        if map_provider is None or current_location is None:
                            return {
                                "status": "replan_unavailable",
                                "reason": "missing_provider_or_location",
                            }
                        trip_context = json.loads(trip.context_json or "{}")
                        replan_result = await DynamicReplanningOrchestrator(
                            db,
                            map_provider,
                            execution_mode=settings.agent_execution_mode,
                            message_bus=message_bus,
                            distributed_timeout_seconds=settings.agent_stage_timeout_seconds,
                        ).run(
                            trip=trip,
                            event=TripEventArtifact(
                                trip_id=trip.id,
                                event_id=event.id if event else None,
                                event_type=event_type,
                                occurred_at=(
                                    event.occurred_at if event else datetime.now(timezone.utc)
                                ),
                                impact_level=impact,
                                reason=str(
                                    arguments.get("reason") or decision.get("reason") or event_type
                                ),
                                payload_summary=event_payload,
                                base_plan_version=trip.current_plan_version,
                            ),
                            current_location=current_location,
                            completed_stop_ids=[
                                str(item) for item in trip_context.get("completed_stop_ids", [])
                            ],
                            event_payload=event_payload,
                            weather=weather_observation,
                            trace_id=str(payload.get("trace_id") or ""),
                        )
                        return replan_result
                    return {"status": "unsupported_in_worker", "tool": tool}

                controller = AgentController(
                    db,
                    decider=decider,
                    shared_state=AgentSharedStateManager(store, get_settings()),
                )
                result = await controller.run_once(
                    trip=trip,
                    agent=agent,
                    observation={
                        "trigger": "worker_event",
                        "event_type": event_type,
                        "reason": decision.get("reason"),
                        "impact_level": impact,
                        "has_precise_location": current_location is not None,
                        "event_payload": event_payload,
                    },
                    consents=consents,
                    tool_executor=tool_executor,
                    trace_id=str(payload.get("trace_id") or ""),
                    route_plan=json.loads(version.snapshot_json) if version is not None else None,
                )
                metrics.increment(
                    "mapgo_worker_agent_runs_total",
                    {"status": result.get("status") or "unknown"},
                )
                metrics.increment(
                    "mapgo_agent_role_runs_total",
                    {"agent": "companion", "status": result.get("status") or "unknown"},
                )
                if event is not None:
                    decision["agent_run"] = {
                        "run_id": result.get("run_id"),
                        "workflow_id": result.get("workflow_id"),
                        "agent_type": "companion",
                        "status": result.get("status"),
                    }
                    if replan_result is not None:
                        decision["replan"] = replan_result
                    event.decision_json = json.dumps(decision, ensure_ascii=False, default=str)
                    await db.commit()

            # Refresh weather/risk snapshot metadata into trip stream.
            version = await db.scalar(
                select(PlanVersion).where(
                    PlanVersion.planning_run_id == trip.planning_run_id,
                    PlanVersion.version == trip.current_plan_version,
                )
            )
            stream_payload = {
                "event_id": event.event_id if event else payload.get("client_event_id"),
                "type": event_type,
                "state": trip.state,
                "impact_level": impact,
                "decision": decision,
                "plan_patch": (decision.get("replan") if isinstance(decision, dict) else None),
                "worker": {"processed": True, "plan_present": version is not None},
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
            await publish_trip_stream(store, trip.id, stream_payload)

            if event is not None:
                event.status = "worker_processed"
                event.processed_at = datetime.now(timezone.utc)
                await db.commit()
            metrics.increment("mapgo_worker_events_total", {"type": event_type, "result": "ok"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("trip_event_failed trip_id=%s error=%s", trip_id, exc)
        metrics.increment("mapgo_worker_events_total", {"type": event_type, "result": "error"})
        disposition = await store.enqueue_retry(
            TRIP_EVENTS_QUEUE,
            payload,
            attempt=attempt + 1,
            max_attempts=5,
        )
        metrics.increment("mapgo_worker_retries_total", {"disposition": disposition})
        return
    finally:
        if fenced_db is not None and fence_guard is not None:
            sqlalchemy_event.remove(fenced_db.sync_session, "before_commit", fence_guard)
        lease_task.cancel()
        try:
            await lease_task
        except asyncio.CancelledError:
            pass
        await store.release_lock(lock_name, token)


async def process_notification(store, payload: dict[str, Any]) -> None:
    service = NotificationService(store)
    channel = str(payload.get("channel") or "in_app")
    try:
        # Channels beyond in-app are recorded as deferred integrations.
        if channel == "in_app":
            await service.mark_delivered(payload["id"], {"channel": "in_app", "delivered": True})
        elif channel in {"web_push", "email", "app_push"}:
            await service.mark_delivered(
                payload["id"],
                {"channel": channel, "delivered": False, "reason": "channel_adapter_pending"},
            )
        else:
            await service.mark_failed(payload, f"unknown_channel:{channel}")
        metrics.increment("mapgo_notifications_total", {"channel": channel, "result": "ok"})
    except Exception as exc:  # noqa: BLE001
        disposition = await service.mark_failed(payload, str(exc))
        metrics.increment(
            "mapgo_notifications_total",
            {"channel": channel, "result": disposition},
        )


async def run_worker() -> None:
    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError("Worker requires REDIS_URL")
    store = await build_runtime_store(settings.redis_url)
    agent_bus = build_agent_message_bus(
        mode=settings.agent_message_transport,
        runtime_store=store,
        stream_prefix=settings.agent_stream_prefix,
        group_prefix=settings.agent_consumer_group_prefix,
        idempotency_ttl_seconds=settings.idempotency_ttl_seconds,
        max_stream_length=settings.agent_stream_max_length,
    )
    timeout = httpx.Timeout(
        settings.external_timeout_seconds,
        connect=settings.external_connect_timeout_seconds,
    )
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    map_provider = build_map_provider(settings, client)
    weather_provider = build_weather_provider(client, settings.mock_weather_provider)
    decider = build_agent_decider(settings, client)
    replanner_task = None
    if settings.agent_execution_mode == "distributed":
        replanner_task = asyncio.create_task(run_replanner_agent_worker(agent_bus, settings))
    recovered_trip_events = await store.recover_processing(
        TRIP_EVENTS_QUEUE, settings.worker_recover_processing_limit
    )
    recovered_notifications = await store.recover_processing(
        NOTIFICATION_QUEUE, settings.worker_recover_processing_limit
    )
    if recovered_trip_events or recovered_notifications:
        logger.warning(
            "recovered_inflight trip_events=%s notifications=%s",
            recovered_trip_events,
            recovered_notifications,
        )
    logger.info("MapGo worker started")
    try:
        while True:
            if isinstance(store, RedisRuntimeStore):
                await store.promote_retries(TRIP_EVENTS_QUEUE)
                await store.promote_retries(NOTIFICATION_QUEUE)

            reserved = await store.reserve(TRIP_EVENTS_QUEUE, timeout_seconds=2)
            if reserved:
                handled = False
                try:
                    await process_trip_event(
                        store,
                        reserved.payload,
                        map_provider=map_provider,
                        weather_provider=weather_provider,
                        decider=decider,
                        message_bus=agent_bus,
                    )
                    handled = True
                except Exception:
                    logger.exception("trip event handler crashed")
                if handled:
                    await store.acknowledge(TRIP_EVENTS_QUEUE, reserved.receipt)

            reserved_note = await store.reserve(NOTIFICATION_QUEUE, timeout_seconds=1)
            if reserved_note:
                handled = False
                try:
                    await process_notification(store, reserved_note.payload)
                    handled = True
                except Exception:
                    logger.exception("notification handler crashed")
                if handled:
                    await store.acknowledge(NOTIFICATION_QUEUE, reserved_note.receipt)

            removed = await cleanup_expired_locations()
            if removed:
                logger.info("expired_locations_removed count=%s", removed)
                metrics.increment("mapgo_worker_location_cleanup_total", value=removed)
            await asyncio.sleep(0)
    finally:
        if replanner_task is not None:
            replanner_task.cancel()
            try:
                await replanner_task
            except asyncio.CancelledError:
                pass
        await client.aclose()
        await store.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
