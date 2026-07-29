import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from backend.app.core.config import get_settings
from backend.app.core.observability import metrics
from backend.app.db.session import SessionLocal, engine
from backend.app.infrastructure.runtime_store import RedisRuntimeStore, build_runtime_store
from backend.app.models import (
    AgentSession,
    LocationSnapshot,
    PlanVersion,
    TripEvent,
    TripSession,
    UserConsent,
)
from backend.app.schemas.companion import ConsentScope, TripState
from backend.app.services.agent_controller import AgentController
from backend.app.services.notifications import NotificationService, render_event_notification

logger = logging.getLogger("mapgo.worker")
TRIP_EVENTS_QUEUE = "mapgo:trip-events"
NOTIFICATION_QUEUE = "mapgo:notifications"


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


async def process_trip_event(store, payload: dict[str, Any]) -> None:
    trip_id = int(payload["trip_id"])
    event_id = payload.get("event_id")
    event_type = str(payload.get("event_type") or "")
    attempt = int(payload.get("_attempt") or 0)
    lock_name = f"agent-run:trip:{trip_id}"
    token = await store.acquire_lock(lock_name, 30)
    if token is None:
        await store.enqueue_retry(TRIP_EVENTS_QUEUE, payload, attempt=attempt + 1, max_attempts=8)
        metrics.increment("mapgo_worker_lock_contention_total", {"queue": "trip-events"})
        return

    try:
        async with SessionLocal() as db:
            trip = await db.get(TripSession, trip_id)
            if trip is None:
                logger.warning("trip_missing trip_id=%s", trip_id)
                return
            event = None
            if event_id is not None:
                event = await db.get(TripEvent, int(event_id))
            decision = json.loads(event.decision_json) if event and event.decision_json else {}
            should_notify = bool(decision.get("should_notify"))
            impact = event.impact_level if event else "none"

            # Persist worker processing status for observability.
            if event is not None:
                event.status = "worker_processing"
                await db.commit()

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

                async def tool_executor(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    if tool == "get_trip_state":
                        return {"state": trip.state, "plan_version": trip.current_plan_version}
                    if tool == "get_current_location":
                        snap = await db.scalar(
                            select(LocationSnapshot)
                            .where(LocationSnapshot.trip_session_id == trip.id)
                            .order_by(LocationSnapshot.captured_at.desc())
                        )
                        return {"has_location": snap is not None}
                    if tool == "get_weather":
                        return {"status": "deferred_to_api", "arguments": arguments}
                    if tool == "propose_replan":
                        # Worker proposes; user/API confirms. Enforce replan budget.
                        settings = get_settings()
                        replan_count = int(
                            (json.loads(trip.context_json or "{}") or {}).get("replan_count") or 0
                        )
                        if replan_count >= settings.max_replans_per_trip:
                            return {"status": "replan_budget_exceeded"}
                        context = json.loads(trip.context_json or "{}")
                        context["replan_proposed_at"] = datetime.now(timezone.utc).isoformat()
                        context["replan_reason"] = arguments.get("reason") or event_type
                        trip.context_json = json.dumps(context, ensure_ascii=False)
                        if trip.state not in {
                            TripState.replanning.value,
                            TripState.completed.value,
                            TripState.cancelled.value,
                        }:
                            trip.state = TripState.replanning.value
                        await db.commit()
                        return {"status": "replan_proposed", "requires_confirmation": True}
                    return {"status": "unsupported_in_worker", "tool": tool}

                controller = AgentController(db)
                result = await controller.run_once(
                    trip=trip,
                    agent=agent,
                    observation={
                        "trigger": "worker_event",
                        "event_type": event_type,
                        "reason": decision.get("reason"),
                        "impact_level": impact,
                    },
                    consents=consents,
                    tool_executor=tool_executor,
                    trace_id=str(payload.get("trace_id") or ""),
                )
                metrics.increment(
                    "mapgo_worker_agent_runs_total",
                    {"status": result.get("status") or "unknown"},
                )

            # Refresh weather/risk snapshot metadata into trip stream.
            version = await db.scalar(
                select(PlanVersion).where(
                    PlanVersion.planning_run_id == trip.planning_run_id,
                    PlanVersion.version == trip.current_plan_version,
                )
            )
            stream_payload = {
                "sequence": int(event.id) if event else int(datetime.now(timezone.utc).timestamp()),
                "event_id": event.event_id if event else payload.get("client_event_id"),
                "type": event_type,
                "state": trip.state,
                "impact_level": impact,
                "decision": decision,
                "worker": {"processed": True, "plan_present": version is not None},
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
            await store.set_json(f"trip-stream:{trip.id}", stream_payload, 86_400)
            await store.publish(f"trip:{trip.id}", stream_payload)

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
        raise
    finally:
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
    logger.info("MapGo worker started")
    try:
        while True:
            if isinstance(store, RedisRuntimeStore):
                await store.promote_retries(TRIP_EVENTS_QUEUE)
                await store.promote_retries(NOTIFICATION_QUEUE)

            item = await store.dequeue(TRIP_EVENTS_QUEUE, timeout_seconds=2)
            if item:
                try:
                    await process_trip_event(store, item)
                except Exception:
                    logger.exception("trip event handler crashed")

            note = await store.dequeue(NOTIFICATION_QUEUE, timeout_seconds=1)
            if note:
                await process_notification(store, note)

            removed = await cleanup_expired_locations()
            if removed:
                logger.info("expired_locations_removed count=%s", removed)
                metrics.increment("mapgo_worker_location_cleanup_total", value=removed)
            await asyncio.sleep(0)
    finally:
        await store.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
