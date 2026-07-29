import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select

from backend.app.api.deps import CurrentUser, Db
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.core.privacy import encrypt_location, read_location
from backend.app.core.security import token_hash
from backend.app.db.session import SessionLocal
from backend.app.models import (
    AgentRun,
    AgentSession,
    AgentToolCall,
    DecisionAuditLog,
    ExternalDataSnapshot,
    LocationSnapshot,
    PlanningRun,
    PlanPatch,
    PlanVersion,
    Session,
    TripEvent,
    TripSession,
    User,
    UserConsent,
    UserPreference,
)
from backend.app.schemas.companion import (
    ConsentRequest,
    ConsentScope,
    CreateTripSessionRequest,
    ExecuteAgentToolRequest,
    ExplicitPreferenceRequest,
    LocationUpdateRequest,
    PreTripCheckRequest,
    PrivacyPurgeRequest,
    ReplanTripRequest,
    TripEventRequest,
    TripEventType,
    TripState,
    TripTransitionRequest,
)
from backend.app.services.agent_policy import evaluate_tool_policy
from backend.app.services.agent_state import validate_transition
from backend.app.services.trip_events import evaluate_trip_event

router = APIRouter(prefix="/companion", tags=["companion-agent"])


def _aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _trip(db: Db, trip_id: int, user_id: int) -> TripSession:
    trip = await db.scalar(
        select(TripSession).where(TripSession.id == trip_id, TripSession.user_id == user_id)
    )
    if trip is None:
        raise AppError(404, "TRIP_SESSION_NOT_FOUND", "行程会话不存在")
    return trip


async def _has_consent(db: Db, trip_id: int, user_id: int, scope: ConsentScope) -> bool:
    now = datetime.now(timezone.utc)
    rows = (
        await db.scalars(
            select(UserConsent)
            .where(
                UserConsent.user_id == user_id,
                UserConsent.trip_session_id == trip_id,
                UserConsent.scope == scope.value,
            )
            .order_by(UserConsent.id.desc())
        )
    ).all()
    if not rows:
        return False
    latest = rows[0]
    expires = latest.expires_at
    if expires and not expires.tzinfo:
        expires = expires.replace(tzinfo=timezone.utc)
    return latest.granted and latest.revoked_at is None and (expires is None or expires > now)


@router.post("/trips")
async def create_trip_session(body: CreateTripSessionRequest, user: CurrentUser, db: Db):
    if not get_settings().feature_companion_agent:
        raise AppError(404, "FEATURE_DISABLED", "伴游 Agent 功能当前未启用")
    run = await db.scalar(
        select(PlanningRun).where(
            PlanningRun.id == body.planning_run_id, PlanningRun.user_id == user.id
        )
    )
    if run is None:
        raise AppError(404, "PLAN_RUN_NOT_FOUND", "规划记录不存在")
    version = await db.scalar(
        select(PlanVersion)
        .where(PlanVersion.planning_run_id == run.id)
        .order_by(PlanVersion.version.desc())
    )
    if version is None:
        raise AppError(409, "PLAN_NOT_READY", "需要先生成正式计划")
    existing = await db.scalar(
        select(TripSession).where(
            TripSession.planning_run_id == run.id,
            TripSession.user_id == user.id,
            TripSession.state.notin_([TripState.completed.value, TripState.cancelled.value]),
        )
    )
    if existing:
        return {"ok": True, "data": {"trip_id": existing.id, "state": existing.state}}
    trip = TripSession(
        user_id=user.id,
        planning_run_id=run.id,
        state=TripState.plan_ready.value,
        current_plan_version=version.version,
        reminder_cooldown_minutes=body.reminder_cooldown_minutes,
    )
    db.add(trip)
    await db.flush()
    db.add(AgentSession(trip_session_id=trip.id, user_id=user.id))
    await db.commit()
    return {"ok": True, "data": {"trip_id": trip.id, "state": trip.state}}


@router.get("/trips/{trip_id}")
async def get_trip_session(trip_id: int, user: CurrentUser, db: Db):
    trip = await _trip(db, trip_id, user.id)
    return {
        "ok": True,
        "data": {
            "id": trip.id,
            "planning_run_id": trip.planning_run_id,
            "state": trip.state,
            "current_plan_version": trip.current_plan_version,
            "tracking_enabled": trip.tracking_enabled,
            "context": json.loads(trip.context_json),
        },
    }


@router.post("/trips/{trip_id}/transition")
async def transition_trip(
    trip_id: int,
    body: TripTransitionRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    trip = await _trip(db, trip_id, user.id)
    current = TripState(trip.state)
    validate_transition(current, body.target_state)
    trip.state = body.target_state.value
    now = datetime.now(timezone.utc)
    if body.target_state == TripState.active_trip and trip.started_at is None:
        trip.started_at = now
    if body.target_state in {TripState.completed, TripState.cancelled}:
        trip.ended_at = now
        trip.tracking_enabled = False
    db.add(
        DecisionAuditLog(
            planning_run_id=trip.planning_run_id,
            user_id=user.id,
            action="trip_state_transition",
            reason=body.reason,
            evidence_json=json.dumps(
                {"from": current.value, "to": body.target_state.value}, ensure_ascii=False
            ),
            policy_result="allowed_by_state_machine",
            trace_id=request.state.trace_id,
        )
    )
    await db.commit()
    return {"ok": True, "data": {"state": trip.state}}


@router.post("/trips/{trip_id}/consents")
async def set_consent(trip_id: int, body: ConsentRequest, user: CurrentUser, db: Db):
    trip = await _trip(db, trip_id, user.id)
    now = datetime.now(timezone.utc)
    consent = UserConsent(
        user_id=user.id,
        trip_session_id=trip.id,
        scope=body.scope.value,
        granted=body.granted,
        granted_at=now if body.granted else None,
        expires_at=body.expires_at,
        revoked_at=None if body.granted else now,
    )
    db.add(consent)
    if body.scope in {ConsentScope.precise_location, ConsentScope.background_location}:
        trip.tracking_enabled = body.granted
    await db.commit()
    return {"ok": True, "data": {"scope": body.scope, "granted": body.granted}}


@router.post("/trips/{trip_id}/location")
async def update_location(
    trip_id: int,
    body: LocationUpdateRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    trip = await _trip(db, trip_id, user.id)
    if TripState(trip.state) not in {
        TripState.active_trip,
        TripState.off_route,
        TripState.at_risk,
        TripState.replanning,
    }:
        raise AppError(409, "LOCATION_STATE_DENIED", "当前行程状态不允许持续定位")
    if not await _has_consent(db, trip.id, user.id, ConsentScope.precise_location):
        raise AppError(403, "LOCATION_CONSENT_REQUIRED", "精确定位需要用户明确授权")
    duplicate = await db.scalar(
        select(TripEvent.id).where(
            TripEvent.trip_session_id == trip.id, TripEvent.event_id == body.event_id
        )
    )
    if duplicate:
        return {"ok": True, "data": {"deduplicated": True}}
    settings = get_settings()
    snapshot = LocationSnapshot(
        trip_session_id=trip.id,
        latitude=None,
        longitude=None,
        encrypted_payload=encrypt_location(body.location.lng, body.location.lat),
        accuracy_meters=body.accuracy_meters,
        captured_at=body.captured_at,
        expires_at=datetime.now(timezone.utc)
        + timedelta(minutes=settings.precise_location_ttl_minutes),
    )
    db.add(snapshot)
    db.add(
        TripEvent(
            trip_session_id=trip.id,
            event_id=body.event_id,
            event_type="LocationUpdated",
            payload_json=json.dumps({"accuracy_meters": body.accuracy_meters}, ensure_ascii=False),
            occurred_at=body.captured_at,
            status="processed",
            impact_level="none",
            decision_json="{}",
            processed_at=datetime.now(timezone.utc),
        )
    )

    off_route_event = None
    version = await db.scalar(
        select(PlanVersion).where(
            PlanVersion.planning_run_id == trip.planning_run_id,
            PlanVersion.version == trip.current_plan_version,
        )
    )
    if version is not None:
        from backend.app.services.offroute import evaluate_off_route, polyline_from_plan_snapshot

        snapshot_json = json.loads(version.snapshot_json)
        polyline = polyline_from_plan_snapshot(snapshot_json)
        context = json.loads(trip.context_json or "{}")
        previous_off = float(context.get("off_route_seconds") or 0)
        interval = float(context.get("location_interval_seconds") or 5)
        verdict = evaluate_off_route(
            lng=body.location.lng,
            lat=body.location.lat,
            polyline=polyline,
            previous_off_route_seconds=previous_off,
            sample_interval_seconds=interval,
        )
        context["off_route_seconds"] = verdict.sustained_seconds
        context["last_off_route_distance_m"] = verdict.distance_meters
        trip.context_json = json.dumps(context, ensure_ascii=False)
        if verdict.off_route and TripState(trip.state) != TripState.off_route:
            now = datetime.now(timezone.utc)
            decision = evaluate_trip_event(
                TripState(trip.state),
                TripEventType.user_off_route,
                {
                    "distance_meters": verdict.distance_meters,
                    "sustained_seconds": verdict.sustained_seconds,
                },
                trip.last_notification_at,
                trip.reminder_cooldown_minutes,
                now,
            )
            trip.state = decision.next_state.value
            if decision.should_notify:
                trip.last_notification_at = now
            off_route_event = TripEvent(
                trip_session_id=trip.id,
                event_id=f"auto-offroute-{body.event_id}",
                event_type="UserOffRoute",
                payload_json=json.dumps(
                    {
                        "distance_meters": verdict.distance_meters,
                        "sustained_seconds": verdict.sustained_seconds,
                        "reason": verdict.reason,
                    },
                    ensure_ascii=False,
                ),
                occurred_at=body.captured_at,
                status="processed",
                impact_level=decision.impact_level,
                decision_json=json.dumps(
                    {
                        "reason": decision.reason,
                        "should_notify": decision.should_notify,
                        "proposals": decision.proposals,
                    },
                    ensure_ascii=False,
                ),
                processed_at=now,
            )
            db.add(off_route_event)
            await db.flush()

    await db.commit()
    if off_route_event is not None:
        await request.app.state.runtime_store.enqueue(
            "mapgo:trip-events",
            {
                "trip_id": trip.id,
                "event_id": off_route_event.id,
                "event_type": "UserOffRoute",
            },
        )
        await request.app.state.runtime_store.set_json(
            f"trip-stream:{trip.id}",
            {
                "sequence": off_route_event.id,
                "event_id": off_route_event.event_id,
                "type": "UserOffRoute",
                "state": trip.state,
                "impact_level": off_route_event.impact_level,
                "decision": json.loads(off_route_event.decision_json),
                "occurred_at": body.captured_at.isoformat(),
            },
            86_400,
        )
        await request.app.state.runtime_store.publish(
            f"trip:{trip.id}",
            {"type": "UserOffRoute", "trip_id": trip.id, "event_id": off_route_event.id},
        )
    return {
        "ok": True,
        "data": {
            "stored_until": snapshot.expires_at,
            "deduplicated": False,
            "off_route_detected": off_route_event is not None,
            "off_route_event_id": off_route_event.event_id if off_route_event else None,
        },
    }


@router.post("/trips/{trip_id}/events")
async def ingest_event(
    trip_id: int,
    body: TripEventRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    trip = await _trip(db, trip_id, user.id)
    existing = await db.scalar(
        select(TripEvent).where(
            TripEvent.trip_session_id == trip.id, TripEvent.event_id == body.event_id
        )
    )
    if existing:
        return {
            "ok": True,
            "data": {
                "deduplicated": True,
                "impact_level": existing.impact_level,
                "decision": json.loads(existing.decision_json),
            },
        }
    now = datetime.now(timezone.utc)
    decision = evaluate_trip_event(
        TripState(trip.state),
        body.type,
        body.payload,
        trip.last_notification_at,
        trip.reminder_cooldown_minutes,
        now,
    )
    trip.state = decision.next_state.value
    if decision.next_state in {TripState.completed, TripState.cancelled}:
        trip.tracking_enabled = False
        trip.ended_at = now
    if decision.should_notify:
        trip.last_notification_at = now
    decision_data = {
        "reason": decision.reason,
        "should_notify": decision.should_notify,
        "proposals": decision.proposals,
    }
    event = TripEvent(
        trip_session_id=trip.id,
        event_id=body.event_id,
        event_type=body.type.value,
        payload_json=json.dumps(body.payload, ensure_ascii=False),
        occurred_at=body.occurred_at,
        status="processed",
        impact_level=decision.impact_level,
        decision_json=json.dumps(decision_data, ensure_ascii=False),
        processed_at=now,
    )
    db.add(event)
    await db.commit()
    await request.app.state.runtime_store.enqueue(
        "mapgo:trip-events",
        {"trip_id": trip.id, "event_id": event.id, "event_type": body.type.value},
    )
    await request.app.state.runtime_store.set_json(
        f"trip-stream:{trip.id}",
        {
            "sequence": event.id,
            "event_id": body.event_id,
            "type": body.type.value,
            "state": trip.state,
            "impact_level": decision.impact_level,
            "decision": decision_data,
            "occurred_at": body.occurred_at.isoformat(),
        },
        86_400,
    )
    await request.app.state.runtime_store.publish(
        f"trip:{trip.id}",
        {
            "sequence": event.id,
            "event_id": body.event_id,
            "type": body.type.value,
            "state": trip.state,
        },
    )
    return {
        "ok": True,
        "data": {
            "deduplicated": False,
            "state": trip.state,
            "impact_level": decision.impact_level,
            "decision": decision_data,
        },
    }


@router.get("/trips/{trip_id}/stream")
async def stream_trip_events(trip_id: int, request: Request):
    """Short-lived authenticated SSE stream; clients reconnect with Last-Event-ID."""
    authorization = request.headers.get("authorization", "")
    raw_token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not raw_token:
        raise AppError(401, "AUTH_REQUIRED", "事件流需要登录")
    async with SessionLocal() as stream_db:
        session = await stream_db.get(Session, token_hash(raw_token))
        now = datetime.now(timezone.utc)
        if (
            session is None
            or session.revoked_at is not None
            or _aware_datetime(session.expires_at) <= now
        ):
            raise AppError(401, "SESSION_EXPIRED", "登录已过期，请重新登录")
        user = await stream_db.get(User, session.user_id)
        trip = await stream_db.scalar(
            select(TripSession).where(
                TripSession.id == trip_id,
                TripSession.user_id == session.user_id,
            )
        )
        if user is None or trip is None:
            raise AppError(404, "TRIP_SESSION_NOT_FOUND", "行程会话不存在")

    last_header = request.headers.get("last-event-id", "0")
    try:
        initial_sequence = int(last_header)
    except ValueError:
        initial_sequence = 0

    async def generate():
        last_sequence = initial_sequence
        yield "retry: 2000\n\n"
        # A bounded stream avoids indefinitely pinning proxy resources; the
        # browser reconnect loop resumes from the last event ID.
        for tick in range(30):
            if await request.is_disconnected():
                break
            latest = await request.app.state.runtime_store.get_json(f"trip-stream:{trip_id}")
            sequence = int((latest or {}).get("sequence") or 0)
            if latest and sequence > last_sequence:
                last_sequence = sequence
                payload = json.dumps(latest, ensure_ascii=False)
                yield f"id: {sequence}\nevent: trip-event\ndata: {payload}\n\n"
            elif tick % 5 == 0:
                yield f": heartbeat {tick}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/trips/{trip_id}/locations")
async def delete_trip_locations(trip_id: int, user: CurrentUser, db: Db):
    trip = await _trip(db, trip_id, user.id)
    result = await db.execute(
        delete(LocationSnapshot).where(LocationSnapshot.trip_session_id == trip.id)
    )
    trip.tracking_enabled = False
    await db.commit()
    return {"ok": True, "data": {"deleted": result.rowcount}}


@router.get("/trips/{trip_id}/summary")
async def trip_summary(trip_id: int, user: CurrentUser, db: Db):
    trip = await _trip(db, trip_id, user.id)
    version = await db.scalar(
        select(PlanVersion).where(
            PlanVersion.planning_run_id == trip.planning_run_id,
            PlanVersion.version == trip.current_plan_version,
        )
    )
    if version is None:
        raise AppError(409, "PLAN_VERSION_MISSING", "当前正式计划版本不存在")
    snapshot = json.loads(version.snapshot_json)
    events = (
        await db.scalars(
            select(TripEvent)
            .where(TripEvent.trip_session_id == trip.id)
            .order_by(TripEvent.occurred_at)
        )
    ).all()
    completed_ids = []
    skipped_ids = []
    arrived_at: dict[str, str] = {}
    accepted_patches = 0
    rejected_patches = 0
    eta_errors: list[float] = []
    for event in events:
        payload = json.loads(event.payload_json or "{}")
        if event.event_type == "PlanStopCompleted":
            stop_id = payload.get("stop_id")
            if stop_id:
                completed_ids.append(stop_id)
                if payload.get("arrived_at"):
                    arrived_at[stop_id] = payload["arrived_at"]
                if payload.get("planned_arrival") and payload.get("arrived_at"):
                    try:
                        planned = datetime.fromisoformat(payload["planned_arrival"])
                        actual = datetime.fromisoformat(payload["arrived_at"])
                        eta_errors.append(abs((actual - planned).total_seconds()))
                    except ValueError:
                        pass
        elif event.event_type == "PlanStopSkipped":
            if payload.get("stop_id"):
                skipped_ids.append(payload["stop_id"])
        elif event.event_type == "PlanPatchAccepted":
            accepted_patches += 1
        elif event.event_type == "PlanPatchRejected":
            rejected_patches += 1
    planned_stops = snapshot.get("stops", [])
    planned_ids = [stop["poi"]["id"] for stop in planned_stops]
    planned_walk_meters = float(snapshot.get("total_distance_meters") or 0)
    planned_cost = 0.0
    for stop in planned_stops:
        cost = (stop.get("poi") or {}).get("estimated_cost_yuan")
        if cost is not None:
            planned_cost += float(cost)
    stop_deviations = []
    for stop in planned_stops:
        stop_id = stop["poi"]["id"]
        planned_arrival = stop.get("arrival_time")
        actual = arrived_at.get(stop_id)
        deviation = None
        if planned_arrival and actual:
            try:
                deviation = (
                    datetime.fromisoformat(actual) - datetime.fromisoformat(planned_arrival)
                ).total_seconds()
            except ValueError:
                deviation = None
        stop_deviations.append(
            {
                "stop_id": stop_id,
                "name": stop["poi"].get("name"),
                "planned_arrival": planned_arrival,
                "actual_arrival": actual,
                "deviation_seconds": deviation,
                "completed": stop_id in completed_ids,
                "skipped": stop_id in skipped_ids,
            }
        )
    actual_seconds = None
    if trip.started_at and trip.ended_at:
        actual_seconds = (trip.ended_at - trip.started_at).total_seconds()
    context = json.loads(trip.context_json or "{}")
    replan_count = int(context.get("replan_count") or 0) + sum(
        1 for event in events if event.impact_level in {"high", "critical"}
    )
    mae = sum(eta_errors) / len(eta_errors) if eta_errors else None
    lessons = []
    if skipped_ids:
        lessons.append(f"跳过了 {len(set(skipped_ids))} 个地点，下次可降低必经任务密度")
    if mae and mae > 600:
        lessons.append("ETA 误差较大，建议提高不确定约束安全缓冲")
    if rejected_patches > accepted_patches and rejected_patches:
        lessons.append("多次拒绝重规划建议，说明初始偏好或约束需要更早澄清")
    if not lessons:
        lessons.append("本次行程按计划推进较好，可将显式偏好保存为长期设置")
    return {
        "ok": True,
        "data": {
            "plan_version": trip.current_plan_version,
            "planned_stops": len(planned_ids),
            "completed_stops": len(set(completed_ids)),
            "skipped_stop_ids": sorted(set(skipped_ids)),
            "uncompleted_stop_ids": [
                stop_id
                for stop_id in planned_ids
                if stop_id not in completed_ids and stop_id not in skipped_ids
            ],
            "planned_travel_seconds": snapshot.get("total_travel_seconds"),
            "actual_trip_seconds": actual_seconds,
            "planned_walking_meters": planned_walk_meters,
            "estimated_cost_yuan": planned_cost,
            "event_count": len(events),
            "replan_events": replan_count,
            "accepted_suggestions": accepted_patches,
            "rejected_suggestions": rejected_patches,
            "stop_deviations": stop_deviations,
            "eta_error_mae_seconds": mae,
            "lessons": lessons,
            "preference_followup": "这次行程中，你是否觉得步行安排仍然偏多？",
            "preference_saved": False,
        },
    }


@router.post("/preferences")
async def save_preference(body: ExplicitPreferenceRequest, user: CurrentUser, db: Db):
    if not body.confirmed:
        raise AppError(
            409,
            "EXPLICIT_CONFIRMATION_REQUIRED",
            "长期偏好只能在用户明确确认后保存",
        )
    row = await db.scalar(
        select(UserPreference).where(
            UserPreference.user_id == user.id, UserPreference.key == body.key
        )
    )
    now = datetime.now(timezone.utc)
    if row:
        row.value_json = json.dumps(body.value, ensure_ascii=False)
        row.confirmed_at = now
    else:
        db.add(
            UserPreference(
                user_id=user.id,
                key=body.key,
                value_json=json.dumps(body.value, ensure_ascii=False),
                confirmed_at=now,
            )
        )
    await db.commit()
    return {"ok": True, "data": {"key": body.key, "saved": True}}


@router.post("/trips/{trip_id}/tools/execute")
async def execute_agent_tool(
    trip_id: int,
    body: ExecuteAgentToolRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    trip = await _trip(db, trip_id, user.id)
    agent = await db.scalar(select(AgentSession).where(AgentSession.trip_session_id == trip.id))
    if agent is None:
        raise AppError(409, "AGENT_SESSION_MISSING", "Agent 会话不存在")
    consent_rows = (
        await db.scalars(
            select(UserConsent).where(
                UserConsent.trip_session_id == trip.id,
                UserConsent.user_id == user.id,
                UserConsent.granted.is_(True),
                UserConsent.revoked_at.is_(None),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    consents = {
        ConsentScope(row.scope)
        for row in consent_rows
        if row.expires_at is None or _aware_datetime(row.expires_at) > now
    }
    allowed, policy_reason, confirmation_required = evaluate_tool_policy(
        body.tool, TripState(trip.state), consents
    )
    if not allowed:
        raise AppError(
            403,
            "AGENT_TOOL_POLICY_DENIED",
            "工具调用被 Policy Engine 拒绝",
            {
                "reason": policy_reason,
            },
        )
    if confirmation_required and not body.confirmed:
        raise AppError(409, "AGENT_TOOL_CONFIRMATION_REQUIRED", "该操作需要用户二次确认")
    call_count = await db.scalar(
        select(func.count(AgentToolCall.id))
        .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
        .where(AgentRun.agent_session_id == agent.id)
    )
    if (call_count or 0) >= get_settings().max_agent_tool_calls:
        raise AppError(429, "AGENT_TOOL_BUDGET_EXCEEDED", "本次 Agent 工具调用次数已达上限")

    run = AgentRun(
        agent_session_id=agent.id,
        trigger_type="user_or_controller",
        status="running",
        trace_id=request.state.trace_id,
    )
    db.add(run)
    await db.flush()
    output: dict = {}
    status = "succeeded"
    error_type = None
    try:
        if body.tool == "get_trip_state":
            output = {
                "state": trip.state,
                "current_plan_version": trip.current_plan_version,
            }
        elif body.tool == "get_current_location":
            location = await db.scalar(
                select(LocationSnapshot)
                .where(LocationSnapshot.trip_session_id == trip.id)
                .order_by(LocationSnapshot.captured_at.desc())
            )
            output = (
                {
                    "location": {
                        "lng": read_location(location)[0],
                        "lat": read_location(location)[1],
                    },
                    "accuracy_meters": location.accuracy_meters,
                    "captured_at": location.captured_at,
                }
                if location
                else {"location": None}
            )
        elif body.tool == "search_poi":
            from backend.app.schemas.ai_intent import Coordinate

            keyword = str(body.arguments.get("keyword") or "").strip()
            origin = Coordinate.model_validate(body.arguments.get("origin"))
            found = await request.app.state.map_provider.search_poi(
                keyword, origin, body.arguments.get("city")
            )
            output = {"candidates": [item.model_dump(mode="json") for item in found[:5]]}
        elif body.tool == "get_weather":
            from backend.app.schemas.ai_intent import Coordinate

            weather_location = Coordinate.model_validate(body.arguments.get("location"))
            weather = await request.app.state.weather_provider.current(weather_location)
            output = weather.model_dump(mode="json")
        elif body.tool == "generate_attraction_brief":
            attraction_name = str(body.arguments.get("name") or "").strip()
            brief = await request.app.state.knowledge_provider.attraction_brief(attraction_name)
            if brief is None:
                raise AppError(
                    404,
                    "ATTRACTION_KNOWLEDGE_NOT_FOUND",
                    "知识库没有可追溯内容，Agent 不会编造讲解",
                )
            output = brief
        else:
            output = {
                "action_proposal": body.tool,
                "status": "proposal_only",
                "reason": "重要变更必须进入专用 Plan Patch 或授权流程",
            }
    except Exception as exc:
        status, error_type = "failed", type(exc).__name__
        run.status = status
        db.add(
            AgentToolCall(
                agent_run_id=run.id,
                tool_name=body.tool,
                input_json=json.dumps(body.arguments, ensure_ascii=False),
                output_summary_json="{}",
                status=status,
                error_type=error_type,
                trace_id=request.state.trace_id,
            )
        )
        await db.commit()
        raise
    run.status = status
    db.add(
        AgentToolCall(
            agent_run_id=run.id,
            tool_name=body.tool,
            input_json=json.dumps(body.arguments, ensure_ascii=False),
            output_summary_json=json.dumps(output, ensure_ascii=False, default=str),
            upstream_provider=(
                request.app.state.map_provider.name if body.tool == "search_poi" else None
            ),
            status=status,
            error_type=error_type,
            trace_id=request.state.trace_id,
        )
    )
    await db.commit()
    return {
        "ok": True,
        "data": {
            "tool": body.tool,
            "output": output,
            "policy": "allowed",
            "audited": True,
        },
    }


@router.post("/trips/{trip_id}/replan")
async def replan_remaining_trip(
    trip_id: int,
    body: ReplanTripRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    trip = await _trip(db, trip_id, user.id)
    if TripState(trip.state) not in {
        TripState.active_trip,
        TripState.off_route,
        TripState.at_risk,
        TripState.replanning,
    }:
        raise AppError(409, "REPLAN_STATE_DENIED", "当前状态不允许动态重规划")
    settings = get_settings()
    context = json.loads(trip.context_json or "{}")
    replan_count = int(context.get("replan_count") or 0)
    if replan_count >= settings.max_replans_per_trip:
        raise AppError(429, "REPLAN_BUDGET_EXCEEDED", "本次行程重规划次数已达上限")
    lock_token = await request.app.state.runtime_store.acquire_lock(f"trip-mutate:{trip.id}", 20)
    if lock_token is None:
        raise AppError(409, "TRIP_LOCKED", "行程正在被其他实例修改，请稍后重试")
    try:
        return await _replan_remaining_trip_locked(
            trip_id, body, request, user, db, trip, context, replan_count
        )
    finally:
        await request.app.state.runtime_store.release_lock(f"trip-mutate:{trip.id}", lock_token)


async def _replan_remaining_trip_locked(
    trip_id: int,
    body: ReplanTripRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
    trip: TripSession,
    context: dict,
    replan_count: int,
):
    version = await db.scalar(
        select(PlanVersion).where(
            PlanVersion.planning_run_id == trip.planning_run_id,
            PlanVersion.version == trip.current_plan_version,
        )
    )
    if version is None:
        raise AppError(409, "PLAN_VERSION_MISSING", "当前正式计划版本不存在")
    snapshot = json.loads(version.snapshot_json)
    completed = set(body.completed_stop_ids)
    remaining = [stop for stop in snapshot.get("stops", []) if stop["poi"]["id"] not in completed]
    if not remaining:
        raise AppError(409, "NO_REMAINING_STOPS", "没有需要重规划的剩余站点")
    from backend.app.schemas.ai_intent import Coordinate, PlanningTask, TransportMode
    from backend.app.services.route_optimizer import optimize_route

    points = [
        body.current_location,
        *(Coordinate.model_validate(stop["poi"]["location"]) for stop in remaining),
    ]
    mode = TransportMode(snapshot["intent"]["transport_mode"])
    modes_to_try = [mode]
    for alt in (TransportMode.driving, TransportMode.transit, TransportMode.walking):
        if alt not in modes_to_try:
            modes_to_try.append(alt)
    alternatives = []
    primary = None
    for candidate_mode in modes_to_try[:3]:
        try:
            matrix = await request.app.state.map_provider.route_matrix(points, candidate_mode)
        except Exception:  # noqa: BLE001
            continue
        tasks = [PlanningTask.model_validate(stop["task"]) for stop in remaining]
        # Drop optional (required=False) stops as a differentiated option.
        for drop_optional in (False, True):
            active_remaining = remaining
            active_tasks = tasks
            if drop_optional:
                keep = [
                    (stop, task)
                    for stop, task in zip(remaining, tasks, strict=False)
                    if task.required
                ]
                if len(keep) == len(remaining) or not keep:
                    continue
                active_remaining = [item[0] for item in keep]
                active_tasks = [item[1] for item in keep]
                active_points = [
                    body.current_location,
                    *(
                        Coordinate.model_validate(stop["poi"]["location"])
                        for stop in active_remaining
                    ),
                ]
                matrix = await request.app.state.map_provider.route_matrix(
                    active_points, candidate_mode
                )
            evaluation, algorithm = optimize_route(
                body.current_time, active_tasks, matrix.distances, matrix.durations
            )
            option = {
                "label": (
                    f"方案{'ABC'[len(alternatives)]}"
                    if len(alternatives) < 3
                    else f"方案{len(alternatives)+1}"
                ),
                "transport_mode": candidate_mode.value,
                "drop_optional": drop_optional,
                "feasible": evaluation.feasible,
                "total_travel_seconds": evaluation.total_travel_seconds,
                "total_distance_meters": evaluation.total_distance,
                "conflicts": evaluation.conflicts,
                "algorithm": algorithm,
                "stop_ids": [active_remaining[index]["poi"]["id"] for index in evaluation.order],
            }
            alternatives.append(option)
            if (
                primary is None
                and evaluation.feasible
                and candidate_mode == mode
                and not drop_optional
            ):
                primary = (evaluation, algorithm, active_remaining, matrix)
            if len(alternatives) >= 3:
                break
        if len(alternatives) >= 3:
            break

    if primary is None:
        # Fall back to first feasible alternative's reorder on original mode.
        matrix = await request.app.state.map_provider.route_matrix(points, mode)
        tasks = [PlanningTask.model_validate(stop["task"]) for stop in remaining]
        evaluation, algorithm = optimize_route(
            body.current_time, tasks, matrix.distances, matrix.durations
        )
        reordered = [remaining[index] for index in evaluation.order]
    else:
        evaluation, algorithm, active_remaining, matrix = primary
        reordered = [active_remaining[index] for index in evaluation.order]
        remaining = active_remaining

    original_ids = [stop["poi"]["id"] for stop in snapshot["stops"]]
    operations = [
        {
            "operation": "remove_stop",
            "stop_id": stop_id,
            "from_position": None,
            "to_position": None,
        }
        for stop_id in original_ids
        if stop_id in completed
    ]
    target_ids = [stop["poi"]["id"] for stop in reordered]
    working = [stop_id for stop_id in original_ids if stop_id not in completed]
    for target_position, stop_id in enumerate(target_ids):
        if stop_id not in working:
            continue
        source_position = working.index(stop_id)
        if source_position != target_position:
            operations.append(
                {
                    "operation": "move_stop",
                    "stop_id": None,
                    "from_position": source_position,
                    "to_position": target_position,
                }
            )
            working.insert(target_position, working.pop(source_position))
    impact = {
        "before": {
            "total_travel_seconds": snapshot.get("total_travel_seconds"),
            "plan_version": trip.current_plan_version,
        },
        "after": {
            "total_travel_seconds": evaluation.total_travel_seconds,
            "total_distance_meters": evaluation.total_distance,
            "feasible": evaluation.feasible,
            "conflicts": evaluation.conflicts,
        },
        "algorithm": algorithm,
        "alternatives": alternatives,
        "replan_context": {
            "origin": body.current_location.model_dump(mode="json"),
            "departure_time": body.current_time.isoformat(),
        },
    }
    context["replan_count"] = replan_count + 1
    trip.context_json = json.dumps(context, ensure_ascii=False)
    if not evaluation.feasible:
        trip.state = TripState.at_risk.value
        await db.commit()
        return {
            "ok": True,
            "data": {
                "status": "no_feasible_reorder",
                "patch_created": False,
                "impact": impact,
                "alternatives": alternatives,
                "options": [
                    {"action": "remove_optional_stop", "requires_confirmation": True},
                    {"action": "change_transport_mode", "requires_confirmation": True},
                    {"action": "keep_plan", "violates_hard_constraints": True},
                ],
            },
        }
    if not operations:
        trip.state = TripState.active_trip.value
        await db.commit()
        return {
            "ok": True,
            "data": {
                "status": "current_order_still_optimal",
                "patch_created": False,
                "impact": impact,
                "alternatives": alternatives,
            },
        }
    patch = PlanPatch(
        planning_run_id=trip.planning_run_id,
        user_id=user.id,
        base_version=trip.current_plan_version,
        operations_json=json.dumps(operations, ensure_ascii=False),
        reason=body.reason,
        impact_json=json.dumps(impact, ensure_ascii=False),
        status="pending",
    )
    db.add(patch)
    await db.flush()
    trip.state = TripState.replanning.value
    await db.commit()
    return {
        "ok": True,
        "data": {
            "status": "patch_pending_confirmation",
            "patch_created": True,
            "patch_id": patch.id,
            "impact": impact,
            "alternatives": alternatives,
        },
    }


@router.post("/trips/{trip_id}/pretrip-check")
async def pretrip_check(
    trip_id: int,
    body: PreTripCheckRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    trip = await _trip(db, trip_id, user.id)
    weather = await request.app.state.weather_provider.current(body.location)
    risks = []
    if weather.precipitation_probability >= 50:
        risks.append(f"未来时段降水概率约 {weather.precipitation_probability:.0f}%")
    if weather.weather_code >= 80:
        risks.append(f"天气代码 {weather.weather_code}，建议准备室内备选方案")
    db.add(
        ExternalDataSnapshot(
            trip_session_id=trip.id,
            provider=weather.source,
            data_type="weather",
            source_version="current",
            payload_json=weather.model_dump_json(),
            confidence=weather.confidence,
            observed_at=weather.observed_at,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
    )
    await db.commit()
    return {
        "ok": True,
        "data": {
            "weather": weather.model_dump(mode="json"),
            "risks": risks,
            "original_plan_changed": False,
            "recommendation": (
                "已生成室内优先备选建议，未替换正式计划" if risks else "当前天气未触发调整阈值"
            ),
        },
    }


@router.get("/privacy/export")
async def export_private_data(user: CurrentUser, db: Db):
    trips = (await db.scalars(select(TripSession).where(TripSession.user_id == user.id))).all()
    trip_ids = [item.id for item in trips]
    consents = (await db.scalars(select(UserConsent).where(UserConsent.user_id == user.id))).all()
    preferences = (
        await db.scalars(select(UserPreference).where(UserPreference.user_id == user.id))
    ).all()
    locations = (
        (
            await db.scalars(
                select(LocationSnapshot).where(LocationSnapshot.trip_session_id.in_(trip_ids))
            )
        ).all()
        if trip_ids
        else []
    )
    return {
        "ok": True,
        "data": {
            "trips": [{"id": item.id, "state": item.state} for item in trips],
            "consents": [
                {"scope": item.scope, "granted": item.granted, "expires_at": item.expires_at}
                for item in consents
            ],
            "preferences": [
                {"key": item.key, "value": json.loads(item.value_json)} for item in preferences
            ],
            "locations": [
                {
                    "trip_id": item.trip_session_id,
                    "lng": read_location(item)[0],
                    "lat": read_location(item)[1],
                    "captured_at": item.captured_at,
                    "expires_at": item.expires_at,
                }
                for item in locations
            ],
        },
    }


@router.post("/privacy/purge")
async def purge_private_data(body: PrivacyPurgeRequest, user: CurrentUser, db: Db):
    if body.confirmation != "DELETE_MY_LOCATION_AND_PREFERENCES":
        raise AppError(409, "PRIVACY_PURGE_CONFIRMATION_REQUIRED", "删除敏感数据需要明确确认")
    trip_ids = list(
        (await db.scalars(select(TripSession.id).where(TripSession.user_id == user.id))).all()
    )
    locations_deleted = 0
    if trip_ids:
        result = await db.execute(
            delete(LocationSnapshot).where(LocationSnapshot.trip_session_id.in_(trip_ids))
        )
        locations_deleted = result.rowcount
    await db.execute(delete(UserPreference).where(UserPreference.user_id == user.id))
    await db.execute(delete(UserConsent).where(UserConsent.user_id == user.id))
    await db.commit()
    return {
        "ok": True,
        "data": {
            "locations_deleted": locations_deleted,
            "preferences_deleted": True,
            "consents_revoked": True,
        },
    }
