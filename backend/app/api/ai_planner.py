import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, Db
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.core.observability import metrics
from backend.app.models import (
    DecisionAuditLog,
    IdempotencyRecord,
    PlanningConversation,
    PlanningRun,
    PlanPatch,
    PlanVersion,
    TripSession,
)
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    ContinuePlanningConversationRequest,
    CreatePlanPatchRequest,
    DecidePlanPatchRequest,
)
from backend.app.services.intent_parser import build_intent_parser
from backend.app.services.planning_service import PlanningService, request_fingerprint

router = APIRouter(prefix="/ai", tags=["ai-planner"])


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _enforce_ai_budget(request: Request, user_id: int, text: str) -> None:
    settings = get_settings()
    day = f"{datetime.now(timezone.utc):%Y%m%d}"
    plan_count = await request.app.state.runtime_store.increment(
        f"quota:ai-plans:{user_id}:{day}", 86_400
    )
    if plan_count > settings.ai_plans_per_day:
        raise AppError(
            429,
            "AI_DAILY_QUOTA_EXCEEDED",
            "今日 AI 规划配额已用完",
            {"limit": settings.ai_plans_per_day},
        )
    used_tokens = await request.app.state.runtime_store.increment(
        f"quota:ai-tokens:{user_id}:{day}", 86_400, 0
    )
    if used_tokens >= settings.daily_ai_token_quota:
        raise AppError(
            429,
            "AI_TOKEN_QUOTA_EXCEEDED",
            "今日 AI Token 配额已用完",
            {"limit": settings.daily_ai_token_quota, "used": used_tokens},
        )
    # Conservative pre-flight estimate prevents a single request from crossing
    # the configured financial boundary before contacting the LLM.
    estimated_input_tokens = max(1, len(text) // 2)
    estimated_cost = (
        estimated_input_tokens * settings.llm_input_cost_per_million_usd
        + settings.max_llm_output_tokens * settings.llm_output_cost_per_million_usd
    ) / 1_000_000
    if estimated_cost > settings.max_ai_request_cost_usd:
        raise AppError(
            422,
            "AI_REQUEST_COST_LIMIT",
            "请求的最坏情况模型成本超过单次上限",
            {
                "estimated_max_cost_usd": round(estimated_cost, 6),
                "limit_usd": settings.max_ai_request_cost_usd,
            },
        )


async def _record_ai_usage(request: Request, user_id: int, input_tokens: int, output_tokens: int):
    total = input_tokens + output_tokens
    if not total:
        return
    day = f"{datetime.now(timezone.utc):%Y%m%d}"
    used = await request.app.state.runtime_store.increment(
        f"quota:ai-tokens:{user_id}:{day}", 86_400, total
    )
    metrics.increment("mapgo_llm_tokens_total", {"kind": "input"}, value=input_tokens)
    metrics.increment("mapgo_llm_tokens_total", {"kind": "output"}, value=output_tokens)
    if used > get_settings().daily_ai_token_quota:
        metrics.increment("mapgo_ai_token_quota_overshoot_total")


async def _execute_conversation_plan(
    body: AIPlanRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
    conversation: PlanningConversation | None = None,
) -> dict:
    settings = get_settings()
    await _enforce_ai_budget(request, user.id, body.text)
    parser = build_intent_parser(settings, request.app.state.http_client)
    result = await PlanningService(parser, request.app.state.map_provider, settings).plan(body)
    if getattr(parser, "fallback_used", False):
        metrics.increment(
            "mapgo_llm_fallback_total", {"parser": getattr(parser, "last_parser", "unknown")}
        )
    data = result.model_dump(mode="json")
    input_tokens = int(getattr(parser, "input_tokens", 0))
    output_tokens = int(getattr(parser, "output_tokens", 0))
    await _record_ai_usage(request, user.id, input_tokens, output_tokens)
    if conversation is None:
        conversation = PlanningConversation(
            user_id=user.id,
            state=result.planning_state.value,
            revision=1,
            request_json=body.model_dump_json(),
            intent_json=json.dumps(data["intent"], ensure_ascii=False),
            questions_json=json.dumps(data["questions"], ensure_ascii=False),
            result_json=json.dumps(data, ensure_ascii=False),
        )
        db.add(conversation)
    else:
        conversation.state = result.planning_state.value
        conversation.revision += 1
        conversation.request_json = body.model_dump_json()
        conversation.intent_json = json.dumps(data["intent"], ensure_ascii=False)
        conversation.questions_json = json.dumps(data["questions"], ensure_ascii=False)
        conversation.result_json = json.dumps(data, ensure_ascii=False)
    await db.flush()
    data["conversation_id"] = conversation.id
    data["conversation_revision"] = conversation.revision
    if result.status != "need_clarification":
        run = PlanningRun(
            user_id=user.id,
            input_text=body.text,
            intent_json=json.dumps(data["intent"], ensure_ascii=False),
            result_json=json.dumps(data, ensure_ascii=False),
            status=result.status,
            model_name=parser.name,
            prompt_version=settings.prompt_version,
            map_provider=request.app.state.map_provider.name,
            trace_id=request.state.trace_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=(
                input_tokens * settings.llm_input_cost_per_million_usd
                + output_tokens * settings.llm_output_cost_per_million_usd
            )
            / 1_000_000,
        )
        db.add(run)
        await db.flush()
        data["planning_run_id"] = run.id
        version = PlanVersion(
            planning_run_id=run.id,
            user_id=user.id,
            version=1,
            snapshot_json=json.dumps(data, ensure_ascii=False),
            change_reason="conversation_constraints_confirmed",
        )
        db.add(version)
        await db.flush()
        data["plan_version"] = 1
        data["plan_version_id"] = version.id
        run.result_json = json.dumps(data, ensure_ascii=False)
    await db.commit()
    return data


@router.post("/conversations")
async def start_planning_conversation(
    body: AIPlanRequest, request: Request, user: CurrentUser, db: Db
):
    data = await _execute_conversation_plan(body, request, user, db)
    return {"ok": True, "data": data}


@router.patch("/conversations/{conversation_id}")
async def continue_planning_conversation(
    conversation_id: int,
    body: ContinuePlanningConversationRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    conversation = await db.scalar(
        select(PlanningConversation).where(
            PlanningConversation.id == conversation_id,
            PlanningConversation.user_id == user.id,
        )
    )
    if conversation is None:
        raise AppError(404, "PLANNING_CONVERSATION_NOT_FOUND", "规划会话不存在")
    if conversation.revision != body.base_revision:
        raise AppError(
            409,
            "CONVERSATION_REVISION_CONFLICT",
            "规划会话已更新，请基于最新版本回答",
            {"current_revision": conversation.revision},
        )
    request_data = json.loads(conversation.request_json)
    from backend.app.services.clarification import apply_clarification_answer

    unsupported = [
        field
        for field in body.answers
        if field not in {"origin", "departure_time", "transport_mode"}
        and not field.startswith(("constraints.hard.", "preferences.", "tasks."))
    ]
    if unsupported:
        raise AppError(
            422,
            "UNSUPPORTED_CLARIFICATION_FIELD",
            "包含不支持的澄清字段",
            {"fields": unsupported},
        )
    for field, value in body.answers.items():
        try:
            apply_clarification_answer(request_data, field, value)
        except (KeyError, ValueError, IndexError) as exc:
            raise AppError(
                422,
                "UNSUPPORTED_CLARIFICATION_FIELD",
                "澄清字段无法应用",
                {"field": field, "reason": str(exc)},
            ) from exc
    known_fields = set(AIPlanRequest.model_fields)
    extras = {key: request_data.pop(key) for key in list(request_data) if key not in known_fields}
    updated_request = AIPlanRequest.model_validate(request_data)
    if extras:
        conversation.request_json = json.dumps(
            {**updated_request.model_dump(mode="json"), **extras},
            ensure_ascii=False,
        )
    data = await _execute_conversation_plan(updated_request, request, user, db, conversation)
    return {"ok": True, "data": data}


@router.post("/plans")
async def create_ai_plan(
    body: AIPlanRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    settings = get_settings()
    parser = build_intent_parser(settings, request.app.state.http_client)
    fingerprint = request_fingerprint(str(user.id), body, parser.name, settings.prompt_version)
    record = None
    budget_checked = False
    now = datetime.now(timezone.utc)
    if idempotency_key:
        if len(idempotency_key) > 200:
            raise AppError(400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key 过长")
        record = await db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_key == str(user.id),
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if record and _aware(record.expires_at) <= now:
            record.status = "expired"
            await db.commit()
            await db.delete(record)
            await db.commit()
            record = None
        if record:
            if record.request_fingerprint != fingerprint:
                raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "同一个幂等键不能用于不同请求")
            if record.status in {"succeeded", "completed"} and record.response_json:
                return {
                    "ok": True,
                    "data": json.loads(record.response_json),
                    "replayed": True,
                }
            if record.status == "failed":
                raise AppError(
                    409,
                    "PREVIOUS_REQUEST_FAILED",
                    "相同请求此前执行失败，请使用新的幂等键重试",
                    {"error_code": record.error_code},
                )
            raise AppError(409, "REQUEST_IN_PROGRESS", "相同请求正在处理中")
        await _enforce_ai_budget(request, user.id, body.text)
        budget_checked = True
        record = IdempotencyRecord(
            owner_key=str(user.id),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            status="processing",
            expires_at=now + timedelta(seconds=settings.idempotency_ttl_seconds),
        )
        db.add(record)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise AppError(409, "REQUEST_IN_PROGRESS", "相同请求正在处理中") from exc

    if not budget_checked:
        await _enforce_ai_budget(request, user.id, body.text)
    started = time.perf_counter()
    service = PlanningService(parser, request.app.state.map_provider, settings)
    try:
        result = await service.plan(body)
    except Exception as exc:
        if record:
            record.status = "failed"
            record.error_code = exc.code if isinstance(exc, AppError) else "UNEXPECTED_ERROR"
            await db.commit()
        raise
    if getattr(parser, "fallback_used", False):
        metrics.increment(
            "mapgo_llm_fallback_total",
            {"parser": getattr(parser, "last_parser", "unknown")},
        )
    response_data = result.model_dump(mode="json")
    planning_seconds = time.perf_counter() - started
    metrics.observe(
        "mapgo_planning_duration_seconds",
        planning_seconds,
        {"algorithm": result.algorithm or "none"},
    )
    metrics.increment(
        "mapgo_planning_results_total",
        {"status": result.status, "provider": request.app.state.map_provider.name},
    )
    metrics.increment(
        "mapgo_route_fallback_edges_total",
        value=sum(1 for stop in result.stops if stop.travel.fallback_used),
    )
    input_tokens = int(getattr(parser, "input_tokens", 0))
    output_tokens = int(getattr(parser, "output_tokens", 0))
    await _record_ai_usage(request, user.id, input_tokens, output_tokens)
    estimated_cost = (
        input_tokens * settings.llm_input_cost_per_million_usd
        + output_tokens * settings.llm_output_cost_per_million_usd
    ) / 1_000_000
    run = PlanningRun(
        user_id=user.id,
        input_text=body.text,
        intent_json=json.dumps(response_data["intent"], ensure_ascii=False),
        result_json=json.dumps(response_data, ensure_ascii=False),
        status=result.status,
        model_name=parser.name,
        prompt_version=settings.prompt_version,
        map_provider=request.app.state.map_provider.name,
        trace_id=request.state.trace_id,
        latency_ms=round(planning_seconds * 1000),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
    )
    db.add(run)
    await db.flush()
    response_data["planning_run_id"] = run.id
    if result.status != "need_clarification":
        version = PlanVersion(
            planning_run_id=run.id,
            user_id=user.id,
            version=1,
            snapshot_json=json.dumps(response_data, ensure_ascii=False),
            change_reason="initial_plan",
        )
        db.add(version)
        await db.flush()
        response_data["plan_version"] = 1
        response_data["plan_version_id"] = version.id
        run.result_json = json.dumps(response_data, ensure_ascii=False)
    if record:
        record.status = "succeeded"
        record.response_json = json.dumps(response_data, ensure_ascii=False)
    await db.commit()
    return {"ok": True, "data": response_data}


async def _owned_run(db: Db, run_id: int, user_id: int) -> PlanningRun:
    run = await db.scalar(
        select(PlanningRun).where(PlanningRun.id == run_id, PlanningRun.user_id == user_id)
    )
    if not run:
        raise AppError(404, "PLAN_RUN_NOT_FOUND", "规划记录不存在")
    return run


@router.get("/plans/{run_id}/versions")
async def list_plan_versions(run_id: int, user: CurrentUser, db: Db):
    await _owned_run(db, run_id, user.id)
    versions = (
        await db.scalars(
            select(PlanVersion)
            .where(PlanVersion.planning_run_id == run_id)
            .order_by(PlanVersion.version.desc())
        )
    ).all()
    return {
        "ok": True,
        "data": [
            {
                "id": item.id,
                "version": item.version,
                "change_reason": item.change_reason,
                "snapshot": json.loads(item.snapshot_json),
                "created_at": item.created_at,
            }
            for item in versions
        ],
    }


@router.post("/plans/{run_id}/patches")
async def create_plan_patch(
    run_id: int,
    body: CreatePlanPatchRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    await _owned_run(db, run_id, user.id)
    current = await db.scalar(
        select(func.max(PlanVersion.version)).where(PlanVersion.planning_run_id == run_id)
    )
    if current is None:
        raise AppError(409, "PLAN_NOT_VERSIONED", "该规划尚未生成正式版本")
    if body.base_version != current:
        raise AppError(
            409,
            "PLAN_VERSION_CONFLICT",
            "计划已更新，请基于最新版本重新生成补丁",
            {"current_version": current},
        )
    patch = PlanPatch(
        planning_run_id=run_id,
        user_id=user.id,
        base_version=body.base_version,
        operations_json=json.dumps(
            [item.model_dump(mode="json") for item in body.operations],
            ensure_ascii=False,
        ),
        reason=body.reason,
        impact_json=json.dumps(body.impact, ensure_ascii=False),
        status="pending",
    )
    db.add(patch)
    db.add(
        DecisionAuditLog(
            planning_run_id=run_id,
            user_id=user.id,
            action="create_plan_patch",
            reason=body.reason,
            evidence_json=patch.impact_json,
            policy_result="requires_confirmation",
            trace_id=request.state.trace_id,
        )
    )
    await db.commit()
    await db.refresh(patch)
    return {
        "ok": True,
        "data": {
            "patch_id": patch.id,
            "status": patch.status,
            "requires_confirmation": True,
        },
    }


def _apply_structure(snapshot: dict, operations: list[dict]) -> list[dict]:
    stops = list(snapshot.get("stops") or [])
    for operation in operations:
        if operation["operation"] == "remove_stop":
            stop_id = operation.get("stop_id")
            position = next(
                (i for i, stop in enumerate(stops) if stop["poi"]["id"] == stop_id),
                None,
            )
            if position is None:
                raise AppError(422, "PATCH_STOP_NOT_FOUND", f"找不到站点 {stop_id!r}")
            stops.pop(position)
        elif operation["operation"] == "move_stop":
            source = operation.get("from_position")
            target = operation.get("to_position")
            if source is None or target is None or source >= len(stops) or target >= len(stops):
                raise AppError(422, "PATCH_POSITION_INVALID", "移动站点的位置无效")
            stops.insert(target, stops.pop(source))
    if not stops:
        raise AppError(422, "PATCH_EMPTY_PLAN", "正式计划至少需要保留一个站点")
    return stops


async def _recalculate_snapshot(
    snapshot: dict,
    stops: list[dict],
    provider,
    replan_context: dict | None = None,
) -> tuple[dict, list[str]]:
    context = replan_context or {}
    origin = context.get("origin") or snapshot.get("origin")
    departure_raw = context.get("departure_time") or snapshot.get("departure_time")
    if not origin or not departure_raw:
        raise AppError(409, "PATCH_CONTEXT_MISSING", "原计划缺少可重算的起点或出发时间")
    from backend.app.schemas.ai_intent import Coordinate, TransportMode

    coordinates = [
        Coordinate.model_validate(origin),
        *(Coordinate.model_validate(stop["poi"]["location"]) for stop in stops),
    ]
    mode = TransportMode(snapshot["intent"]["transport_mode"])
    matrix = await provider.route_matrix(coordinates, mode)
    cursor = datetime.fromisoformat(departure_raw)
    total_distance = 0.0
    total_seconds = 0.0
    conflicts = []
    for index, stop in enumerate(stops):
        edge = matrix.edges[index][index + 1]
        cursor += timedelta(seconds=edge.duration_seconds)
        task = stop["task"]
        earliest = (
            datetime.fromisoformat(task["earliest_arrival"])
            if task.get("earliest_arrival")
            else None
        )
        if earliest and cursor < earliest:
            cursor = earliest
        arrival = cursor
        deadline = datetime.fromisoformat(task["deadline"]) if task.get("deadline") else None
        if deadline and arrival > deadline:
            conflicts.append(
                f"“{task['description']}”预计 {arrival:%H:%M} 到达，超过截止时间 {deadline:%H:%M}"
            )
        service_minutes = max(
            task.get("service_duration_minutes") or 0,
            task.get("min_service_duration_minutes") or 0,
        )
        cursor += timedelta(minutes=service_minutes)
        stop["arrival_time"] = arrival.isoformat()
        stop["departure_time"] = cursor.isoformat()
        stop["travel"] = edge.model_dump(mode="json")
        stop["constraint_satisfied"] = not bool(deadline and arrival > deadline)
        total_distance += edge.distance_meters
        total_seconds += edge.duration_seconds
    hard = snapshot["intent"].get("constraints", {}).get("hard", {})
    latest = (
        datetime.fromisoformat(hard["latest_return_time"])
        if hard.get("latest_return_time")
        else None
    )
    if latest and cursor > latest:
        conflicts.append(f"调整后预计 {cursor:%H:%M} 完成，超过最晚返回时间 {latest:%H:%M}")
    max_walk = hard.get("max_walking_meters")
    if mode == TransportMode.walking and max_walk is not None and total_distance > max_walk:
        conflicts.append(f"调整后步行 {total_distance:.0f} 米，超过上限 {max_walk:.0f} 米")
    snapshot["stops"] = stops
    snapshot["total_distance_meters"] = total_distance
    snapshot["total_travel_seconds"] = total_seconds
    snapshot["confidence"] = min(
        (stop["travel"]["confidence"] for stop in stops),
        default=0,
    )
    snapshot["algorithm"] = "validated-plan-patch"
    return snapshot, conflicts


@router.post("/plans/{run_id}/patches/{patch_id}/decision")
async def decide_plan_patch(
    run_id: int,
    patch_id: int,
    body: DecidePlanPatchRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
):
    await _owned_run(db, run_id, user.id)
    patch = await db.scalar(
        select(PlanPatch).where(
            PlanPatch.id == patch_id,
            PlanPatch.planning_run_id == run_id,
            PlanPatch.user_id == user.id,
        )
    )
    if not patch:
        raise AppError(404, "PLAN_PATCH_NOT_FOUND", "计划补丁不存在")
    if patch.status != "pending":
        raise AppError(409, "PLAN_PATCH_ALREADY_DECIDED", "该补丁已经处理")
    if not body.accept:
        patch.status = "rejected"
        patch.decided_at = datetime.now(timezone.utc)
        db.add(
            DecisionAuditLog(
                planning_run_id=run_id,
                user_id=user.id,
                action="reject_plan_patch",
                reason=patch.reason,
                evidence_json=patch.impact_json,
                policy_result="user_rejected",
                trace_id=request.state.trace_id,
            )
        )
        await db.commit()
        return {"ok": True, "data": {"status": "rejected"}}

    current = await db.scalar(
        select(func.max(PlanVersion.version)).where(PlanVersion.planning_run_id == run_id)
    )
    if current != patch.base_version:
        raise AppError(
            409,
            "PLAN_VERSION_CONFLICT",
            "计划已更新，不能再应用旧补丁",
            {"current_version": current},
        )
    base = await db.scalar(
        select(PlanVersion).where(
            PlanVersion.planning_run_id == run_id,
            PlanVersion.version == patch.base_version,
        )
    )
    if base is None:
        raise AppError(409, "PLAN_VERSION_MISSING", "补丁引用的计划版本不存在")
    snapshot = json.loads(base.snapshot_json)
    operations = json.loads(patch.operations_json)
    stops = _apply_structure(snapshot, operations)
    impact = json.loads(patch.impact_json)
    snapshot, conflicts = await _recalculate_snapshot(
        snapshot,
        stops,
        request.app.state.map_provider,
        impact.get("replan_context"),
    )
    if conflicts:
        db.add(
            DecisionAuditLog(
                planning_run_id=run_id,
                user_id=user.id,
                action="apply_plan_patch",
                reason=patch.reason,
                evidence_json=json.dumps({"conflicts": conflicts}, ensure_ascii=False),
                policy_result="blocked_by_constraint_validator",
                trace_id=request.state.trace_id,
            )
        )
        await db.commit()
        raise AppError(
            409,
            "PATCH_INFEASIBLE",
            "补丁会破坏硬约束，未修改正式计划",
            {"conflicts": conflicts},
        )

    new_version = PlanVersion(
        planning_run_id=run_id,
        user_id=user.id,
        version=current + 1,
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        change_reason=patch.reason,
    )
    patch.status = "accepted"
    patch.decided_at = datetime.now(timezone.utc)
    db.add(new_version)
    trips = (
        await db.scalars(
            select(TripSession).where(
                TripSession.planning_run_id == run_id,
                TripSession.current_plan_version == current,
            )
        )
    ).all()
    for trip in trips:
        trip.current_plan_version = current + 1
        if trip.state == "REPLANNING":
            trip.state = "ACTIVE_TRIP"
    db.add(
        DecisionAuditLog(
            planning_run_id=run_id,
            user_id=user.id,
            action="apply_plan_patch",
            reason=patch.reason,
            evidence_json=json.dumps(
                {"base_version": current, "operations": operations},
                ensure_ascii=False,
            ),
            policy_result="validated_and_user_confirmed",
            trace_id=request.state.trace_id,
        )
    )
    await db.commit()
    return {
        "ok": True,
        "data": {
            "status": "accepted",
            "plan_version": current + 1,
            "snapshot": snapshot,
        },
    }
