import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select

from backend.app.clients.amap_client import build_map_provider
from backend.app.clients.weather_client import build_weather_provider
from backend.app.core.config import get_settings
from backend.app.core.observability import metrics
from backend.app.core.privacy import read_location
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
from backend.app.schemas.companion import ConsentScope
from backend.app.services.agent_controller import AgentController
from backend.app.services.agent_decider import AgentDecider, build_agent_decider
from backend.app.services.notifications import NotificationService, render_event_notification
from backend.app.services.replanning import PendingReplanRequest, create_pending_replan
from backend.app.services.trip_stream import publish_trip_stream

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


async def process_trip_event(
    store,
    payload: dict[str, Any],
    *,
    map_provider=None,
    weather_provider=None,
    decider: AgentDecider | None = None,
) -> None:
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
            if event is not None and event.status == "worker_processed":
                metrics.increment(
                    "mapgo_worker_events_total", {"type": event_type, "result": "deduplicated"}
                )
                return
            decision = json.loads(event.decision_json) if event and event.decision_json else {}
            event_payload = json.loads(event.payload_json) if event and event.payload_json else {}
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
                        replan_result = await create_pending_replan(
                            db=db,
                            trip=trip,
                            provider=map_provider,
                            request=PendingReplanRequest(
                                current_location=current_location,
                                current_time=(
                                    event.occurred_at if event else datetime.now(timezone.utc)
                                ),
                                reason=str(
                                    arguments.get("reason") or decision.get("reason") or event_type
                                ),
                                source_event_id=event.id if event else None,
                                event_type=event_type,
                                event_payload=event_payload,
                                weather=weather_observation,
                            ),
                            trace_id=str(payload.get("trace_id") or ""),
                        )
                        return replan_result
                    return {"status": "unsupported_in_worker", "tool": tool}

                controller = AgentController(db, decider=decider)
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
                )
                metrics.increment(
                    "mapgo_worker_agent_runs_total",
                    {"status": result.get("status") or "unknown"},
                )
                if event is not None:
                    decision["agent_run"] = {
                        "run_id": result.get("run_id"),
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
    timeout = httpx.Timeout(
        settings.external_timeout_seconds,
        connect=settings.external_connect_timeout_seconds,
    )
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    map_provider = build_map_provider(settings, client)
    weather_provider = build_weather_provider(client, settings.mock_weather_provider)
    decider = build_agent_decider(settings, client)
    logger.info("MapGo worker started")
    try:
        while True:
            if isinstance(store, RedisRuntimeStore):
                await store.promote_retries(TRIP_EVENTS_QUEUE)
                await store.promote_retries(NOTIFICATION_QUEUE)

            item = await store.dequeue(TRIP_EVENTS_QUEUE, timeout_seconds=2)
            if item:
                try:
                    await process_trip_event(
                        store,
                        item,
                        map_provider=map_provider,
                        weather_provider=weather_provider,
                        decider=decider,
                    )
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
        await client.aclose()
        await store.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
