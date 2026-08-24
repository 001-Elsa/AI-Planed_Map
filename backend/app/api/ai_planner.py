import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, Db
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.core.observability import metrics
from backend.app.models import (
    AgentArtifact,
    AgentWorkflowRun,
    DecisionAuditLog,
    IdempotencyRecord,
    PlanningConversation,
    PlanningRun,
    PlanPatch,
    PlanVersion,
    TripEvent,
    TripSession,
)
from backend.app.schemas.agent_artifacts import AgentWorkflowTrace
from backend.app.schemas.ai_intent import (
    MAX_PLANNING_TASKS,
    AIPlanRequest,
    ContinuePlanningConversationRequest,
    CreatePlanPatchRequest,
    DecidePlanPatchRequest,
)
from backend.app.services.agent_memory import (
    MemoryApplication,
    apply_long_term_preferences,
    load_confirmed_preferences,
)
from backend.app.services.agent_orchestrator import persist_agent_workflow
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.agents.critic_agent import build_critic_agent
from backend.app.services.intent_parser import build_intent_parser
from backend.app.services.plan_patch_validator import (
    apply_patch_structure,
    recalculate_and_validate_snapshot,
)
from backend.app.services.planning_service import PlanningService, request_fingerprint

router = APIRouter(prefix="/ai", tags=["ai-planner"])


def _planning_service(request: Request, parser, settings):
    return PlanningService(
        parser,
        request.app.state.map_provider,
        settings,
        critic_agent=build_critic_agent(settings, request.app.state.http_client),
        shared_state=AgentSharedStateManager(request.app.state.runtime_store, settings),
    )


def _workflow_usage(result) -> tuple[int, int, float]:
    raw = result.agent_workflow or {}
    trace = AgentWorkflowTrace.model_validate(raw)
    return (
        sum(step.input_tokens for step in trace.steps),
        sum(step.output_tokens for step in trace.steps),
        trace.total_cost_usd,
    )


def _record_workflow_metrics(trace: AgentWorkflowTrace) -> None:
    metrics.increment(
        "mapgo_agent_workflows_total", {"mode": trace.mode.value, "status": trace.status}
    )
    metrics.observe(
        "mapgo_agent_workflow_cost_usd", trace.total_cost_usd, {"mode": trace.mode.value}
    )
    for step in trace.steps:
        metrics.increment(
            "mapgo_agent_role_runs_total",
            {"agent": step.agent_type.value, "status": step.status},
        )
        metrics.observe(
            "mapgo_agent_role_latency_seconds",
            step.latency_ms / 1000,
            {"agent": step.agent_type.value},
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _execution_trace(result, parser, request: Request, latency_ms: int) -> dict:
    estimated_edges = sum(1 for stop in result.stops if stop.travel.fallback_used)
    verified_edges = len(result.stops) - estimated_edges
    agent_trace = AgentWorkflowTrace.model_validate(result.agent_workflow or {})
    stages = [
        {
            "key": "intent",
            "label": "Intent Agent",
            "status": "complete",
            "detail": f"识别 {len(result.intent.tasks)} 个任务；无 POI 工具权限",
        },
        {
            "key": "poi",
            "label": "核验地点",
            "status": "complete" if result.status != "need_clarification" else "attention",
            "detail": f"比较 {result.candidate_count} 个真实候选",
        },
        {
            "key": "route",
            "label": "计算路网",
            "status": "complete" if result.stops else "pending",
            "detail": f"{verified_edges} 段 Provider 路线，{estimated_edges} 段估算",
        },
        {
            "key": "optimize",
            "label": "确定性约束求解",
            "status": (
                "blocked"
                if result.status == "infeasible"
                else "complete"
                if result.status == "success"
                else "pending"
            ),
            "detail": result.algorithm or "等待补充信息",
        },
    ]
    critic_step = next(
        (step for step in agent_trace.steps if step.agent_type.value == "critic"), None
    )
    stages.append(
        {
            "key": "critic",
            "label": "Critic Agent",
            "status": critic_step.status if critic_step else "pending",
            "detail": (
                result.critic_review.summary
                if result.critic_review
                else "方案证据审阅完成"
                if critic_step
                else "等待形成可审阅方案"
            ),
        }
    )
    return {
        "trace_id": request.state.trace_id,
        "latency_ms": latency_ms,
        "intent_parser": parser.name,
        "parser_fallback_used": bool(getattr(parser, "fallback_used", False)),
        "map_provider": request.app.state.map_provider.name,
        "verified_route_edges": verified_edges,
        "estimated_route_edges": estimated_edges,
        "formal_plan_persisted": False,
        "agent_roles": [step.agent_type.value for step in agent_trace.steps],
        "agent_workflow_id": agent_trace.workflow_id,
        "stages": stages,
    }


def _execution_trace_v2(result, parser, request: Request, latency_ms: int) -> dict:
    estimated_edges = sum(1 for stop in result.stops if stop.travel.fallback_used)
    verified_edges = len(result.stops) - estimated_edges
    agent_trace = AgentWorkflowTrace.model_validate(result.agent_workflow or {})
    role_status = {step.agent_type.value: step.status for step in agent_trace.steps}
    critic_step = next(
        (step for step in agent_trace.steps if step.agent_type.value == "critic"), None
    )
    human_questions = [
        question for question in result.questions if getattr(question, "kind", None) == "confirmation"
    ]
    stages = [
        {
            "key": "supervisor",
            "label": "Supervisor Agent",
            "status": role_status.get("supervisor", "pending"),
            "detail": "任务拆分、角色调度、状态管理和错误恢复",
        },
        {
            "key": "intent",
            "label": "Intent Agent",
            "status": role_status.get("intent", "pending"),
            "detail": f"识别 {len(result.intent.tasks)} 个任务；无 POI 生成权限",
        },
        {
            "key": "search",
            "label": "Search Stage",
            "status": role_status.get("search", "attention" if result.questions else "pending"),
            "detail": f"召回并校验 {result.candidate_count} 个真实候选地点",
        },
        {
            "key": "planner",
            "label": "Planner Stage",
            "status": (
                "blocked"
                if result.status == "infeasible"
                else role_status.get("planner", "pending")
            ),
            "detail": (
                f"{result.algorithm or 'pending'}; {verified_edges} verified edges, "
                f"{estimated_edges} estimated edges"
            ),
        },
        {
            "key": "critic",
            "label": "Critic Agent",
            "status": critic_step.status if critic_step else "pending",
            "detail": (
                result.critic_review.summary
                if result.critic_review
                else "方案证据审阅完成"
                if critic_step
                else "等待形成可审阅方案"
            ),
        },
        {
            "key": "human_confirmation",
            "label": "Human-in-the-loop",
            "status": "attention" if human_questions else "complete",
            "detail": (
                f"等待用户确认 {len(human_questions)} 个高影响决策"
                if human_questions
                else "未触发人工确认闸门"
            ),
        },
        {
            "key": "final",
            "label": "Final Answer",
            "status": result.status,
            "detail": result.explanation or "等待补充信息",
        },
    ]
    return {
        "trace_id": request.state.trace_id,
        "latency_ms": latency_ms,
        "intent_parser": parser.name,
        "parser_fallback_used": bool(getattr(parser, "fallback_used", False)),
        "map_provider": request.app.state.map_provider.name,
        "verified_route_edges": verified_edges,
        "estimated_route_edges": estimated_edges,
        "formal_plan_persisted": False,
        "agent_roles": [step.agent_type.value for step in agent_trace.steps],
        "agent_workflow_id": agent_trace.workflow_id,
        "stages": stages,
    }


@router.get("/capabilities")
async def planning_capabilities(request: Request):
    """Expose safe runtime capability metadata for the planning workspace."""
    settings = get_settings()
    parser = build_intent_parser(settings, request.app.state.http_client)
    map_provider = request.app.state.map_provider
    credential_mode = getattr(map_provider, "credential_mode", "mock")
    return {
        "ok": True,
        "data": {
            "status": "operational",
            "intent_parser": parser.name,
            "map_provider": map_provider.name,
            "map_credential_mode": credential_mode,
            "configuration_warning": None
            if credential_mode != "mock"
            else "尚未配置可用的高德凭据，AI 规划将明确使用估算数据",
            "persistence": True,
            "versioned_plans": True,
            "dynamic_replanning": True,
            "multi_agent": {
                "enabled": settings.multi_agent_enabled,
                "critic_mode": settings.plan_critic_mode,
                "isolated_roles": [
                    "supervisor",
                    "intent",
                    "search",
                    "planner",
                    "critic",
                    "companion",
                ],
                "max_handoffs": settings.max_agent_handoffs,
                "max_workflow_cost_usd": settings.max_agent_workflow_cost_usd,
                "message_protocol": {
                    "version": "1.0",
                    "structured_content": True,
                    "route_allowlist": True,
                    "causal_tracing": True,
                    "idempotent_delivery": True,
                    "audit_payload_minimized": True,
                },
                "shared_state": {
                    "version": "1.0",
                    "runtime_backend": (
                        "redis"
                        if request.app.state.runtime_store.__class__.__name__
                        == "RedisRuntimeStore"
                        else "in_memory"
                    ),
                    "ttl_seconds": settings.agent_shared_state_ttl_seconds,
                    "optimistic_concurrency": True,
                    "role_scoped_views": True,
                    "durable_minimized_snapshot": True,
                    "delete_on_task_completion": True,
                },
                "memory": {
                    "short_term": "runtime_store_delete_on_completion_with_ttl_fallback",
                    "long_term": "postgresql_explicit_confirmation_only",
                    "request_override_precedence": True,
                    "per_request_opt_out": True,
                    "agent_database_access": False,
                },
                "evaluation": {
                    "intent_golden_cases": True,
                    "route_golden_cases": True,
                    "runtime_critic_scoring": True,
                    "formula": "distance*0.40 + time*0.30 + preference*0.30",
                    "hard_fail_zero_score": True,
                    "default_quality_threshold": 75,
                },
                "human_in_the_loop": {
                    "enabled": True,
                    "protocol": "confirmation_question_over_clarification_continue",
                    "gates": [
                        "walking_distance",
                        "estimated_cost",
                    ],
                    "reject_replans": True,
                },
                "critic_enforce_thresholds": {
                    "min_shadow_samples": settings.critic_enforce_min_shadow_samples,
                    "max_fallback_rate": settings.critic_enforce_max_fallback_rate,
                    "max_blocking_rate": settings.critic_enforce_max_blocking_rate,
                    "max_budget_exceeded_rate": settings.critic_enforce_max_budget_exceeded_rate,
                    "max_p95_latency_ms": settings.critic_enforce_max_p95_latency_ms,
                },
            },
            "max_tasks": MAX_PLANNING_TASKS,
            "max_candidates_per_task": 5,
            "max_route_matrix_points": settings.max_route_matrix_points,
            "daily_plan_limit": settings.ai_plans_per_day,
            "transport_modes": ["walking", "cycling", "driving", "transit"],
            "hard_constraints": [
                "latest_return_time",
                "max_walking_meters",
                "max_total_duration_minutes",
                "max_total_cost_yuan",
                "required_task_order",
                "avoid_areas",
                "wheelchair_accessible",
            ],
        },
    }


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


async def _request_with_long_term_memory(
    db: Db, user_id: int, body: AIPlanRequest
) -> tuple[AIPlanRequest, MemoryApplication]:
    values, ignored = await load_confirmed_preferences(db, user_id)
    return apply_long_term_preferences(body, values, ignored_invalid_keys=ignored)


def _memory_execution_stage(memory: MemoryApplication) -> dict[str, str]:
    if not memory.enabled:
        status, detail = "disabled", "本次请求已关闭长期记忆"
    elif memory.applied_keys:
        status = "complete"
        detail = f"应用 {len(memory.applied_keys)} 项已确认长期软偏好；本次请求优先"
    else:
        status, detail = "complete", "未应用长期默认值；本次请求或文本意图优先"
    return {"key": "memory", "label": "Memory", "status": status, "detail": detail}


async def _execute_conversation_plan(
    body: AIPlanRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
    conversation: PlanningConversation | None = None,
) -> dict:
    settings = get_settings()
    await _enforce_ai_budget(request, user.id, body.text)
    if conversation is None:
        body, memory = await _request_with_long_term_memory(db, user.id, body)
    else:
        previous = json.loads(conversation.result_json or "{}").get("memory") or {}
        memory = MemoryApplication(
            enabled=bool(previous.get("enabled", body.use_long_term_memory)),
            source=str(previous.get("source") or "conversation_frozen"),
            applied_keys=tuple(previous.get("applied_keys") or ()),
            skipped_explicit_keys=tuple(previous.get("skipped_explicit_keys") or ()),
            ignored_invalid_count=int(previous.get("ignored_invalid_count") or 0),
        )
    parser = build_intent_parser(settings, request.app.state.http_client)
    started = time.perf_counter()
    result = await _planning_service(request, parser, settings).plan(body)
    if getattr(parser, "fallback_used", False):
        metrics.increment(
            "mapgo_llm_fallback_total", {"parser": getattr(parser, "last_parser", "unknown")}
        )
    data = result.model_dump(mode="json")
    data["memory"] = memory.as_dict()
    if set(memory.applied_keys) & {
        "preferred_categories",
        "preferred_environment",
        "avoid_queues",
    }:
        data["warnings"].append(
            "长期地点偏好仅用于候选召回；拥挤度、排队和环境特征仍需 Provider 事实验证。"
        )
    data["execution"] = _execution_trace_v2(
        result,
        parser,
        request,
        round((time.perf_counter() - started) * 1000),
    )
    data["execution"]["stages"].insert(1, _memory_execution_stage(memory))
    input_tokens, output_tokens, workflow_cost = _workflow_usage(result)
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
            estimated_cost_usd=workflow_cost,
        )
        db.add(run)
        await db.flush()
        data["planning_run_id"] = run.id
        version = PlanVersion(
            planning_run_id=run.id,
            user_id=user.id,
            version=1,
            snapshot_json="{}",
            change_reason="conversation_constraints_confirmed",
        )
        db.add(version)
        await db.flush()
        data["plan_version"] = 1
        data["plan_version_id"] = version.id
        data["execution"]["formal_plan_persisted"] = True
        data["execution"]["stages"].append(
            {
                "key": "persist",
                "label": "保存正式计划",
                "status": "complete",
                "detail": "已写入可回滚版本 v1",
            }
        )
        version.snapshot_json = json.dumps(data, ensure_ascii=False)
        run.result_json = json.dumps(data, ensure_ascii=False)
    trace = AgentWorkflowTrace.model_validate(data["agent_workflow"])
    workflow = await persist_agent_workflow(
        db,
        user_id=user.id,
        trace_id=request.state.trace_id,
        trace=trace,
        trigger_type="planning_conversation",
        planning_conversation_id=conversation.id,
        planning_run_id=data.get("planning_run_id"),
    )
    data["agent_workflow"]["workflow_id"] = workflow.id
    data["execution"]["agent_workflow_id"] = workflow.id
    _record_workflow_metrics(trace)
    conversation.result_json = json.dumps(data, ensure_ascii=False)
    if result.status != "need_clarification":
        version.snapshot_json = json.dumps(data, ensure_ascii=False)
        run.result_json = json.dumps(data, ensure_ascii=False)
    await db.commit()
    return data


@router.post("/conversations")
async def start_planning_conversation(
    body: AIPlanRequest,
    request: Request,
    user: CurrentUser,
    db: Db,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    record = None
    if idempotency_key:
        if len(idempotency_key) > 200:
            raise AppError(400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key 过长")
        settings = get_settings()
        parser = build_intent_parser(settings, request.app.state.http_client)
        fingerprint = request_fingerprint(str(user.id), body, parser.name, settings.prompt_version)
        owner_key = f"{user.id}:conversation"
        now = datetime.now(timezone.utc)
        record = await db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_key == owner_key,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if record and _aware(record.expires_at) <= now:
            await db.delete(record)
            await db.commit()
            record = None
        if record:
            if record.request_fingerprint != fingerprint:
                raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "同一个幂等键不能用于不同请求")
            if record.status == "succeeded" and record.response_json:
                return {
                    "ok": True,
                    "data": json.loads(record.response_json),
                    "replayed": True,
                }
            if record.status == "failed":
                raise AppError(
                    409, "PREVIOUS_REQUEST_FAILED", "相同规划此前执行失败，请使用新的幂等键"
                )
            raise AppError(409, "REQUEST_IN_PROGRESS", "相同规划正在处理中")
        record = IdempotencyRecord(
            owner_key=owner_key,
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
            raise AppError(409, "REQUEST_IN_PROGRESS", "相同规划正在处理中") from exc
    try:
        data = await _execute_conversation_plan(body, request, user, db)
    except Exception as exc:
        if record:
            await db.rollback()
            stored = await db.get(IdempotencyRecord, record.id)
            if stored:
                stored.status = "failed"
                stored.error_code = exc.code if isinstance(exc, AppError) else "UNEXPECTED_ERROR"
                await db.commit()
        raise
    if record:
        stored = await db.get(IdempotencyRecord, record.id)
        if stored:
            stored.status = "succeeded"
            stored.response_json = json.dumps(data, ensure_ascii=False)
            await db.commit()
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
    try:
        body, memory = await _request_with_long_term_memory(db, user.id, body)
        started = time.perf_counter()
        service = _planning_service(request, parser, settings)
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
    response_data["memory"] = memory.as_dict()
    if set(memory.applied_keys) & {
        "preferred_categories",
        "preferred_environment",
        "avoid_queues",
    }:
        response_data["warnings"].append(
            "长期地点偏好仅用于候选召回；拥挤度、排队和环境特征仍需 Provider 事实验证。"
        )
    planning_seconds = time.perf_counter() - started
    response_data["execution"] = _execution_trace_v2(
        result,
        parser,
        request,
        round(planning_seconds * 1000),
    )
    response_data["execution"]["stages"].insert(1, _memory_execution_stage(memory))
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
    input_tokens, output_tokens, workflow_cost = _workflow_usage(result)
    await _record_ai_usage(request, user.id, input_tokens, output_tokens)
    estimated_cost = workflow_cost
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
    trace = AgentWorkflowTrace.model_validate(response_data["agent_workflow"])
    workflow = await persist_agent_workflow(
        db,
        user_id=user.id,
        trace_id=request.state.trace_id,
        trace=trace,
        trigger_type="planning_request",
        planning_run_id=run.id,
    )
    response_data["agent_workflow"]["workflow_id"] = workflow.id
    response_data["execution"]["agent_workflow_id"] = workflow.id
    _record_workflow_metrics(trace)
    if result.status != "need_clarification":
        version = PlanVersion(
            planning_run_id=run.id,
            user_id=user.id,
            version=1,
            snapshot_json="{}",
            change_reason="initial_plan",
        )
        db.add(version)
        await db.flush()
        response_data["plan_version"] = 1
        response_data["plan_version_id"] = version.id
        response_data["execution"]["formal_plan_persisted"] = True
        response_data["execution"]["stages"].append(
            {
                "key": "persist",
                "label": "保存正式计划",
                "status": "complete",
                "detail": "已写入可回滚版本 v1",
            }
        )
        version.snapshot_json = json.dumps(response_data, ensure_ascii=False)
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


@router.get("/plans/overview")
async def get_plan_overview(
    user: CurrentUser,
    db: Db,
    limit: int = Query(5, ge=1, le=20),
):
    """Return the user's formal AI plans as a resumable planning workspace."""
    total_runs = int(
        await db.scalar(select(func.count(PlanningRun.id)).where(PlanningRun.user_id == user.id))
        or 0
    )
    successful_runs = int(
        await db.scalar(
            select(func.count(PlanningRun.id)).where(
                PlanningRun.user_id == user.id,
                PlanningRun.status == "success",
            )
        )
        or 0
    )
    formal_plans = int(
        await db.scalar(
            select(func.count(func.distinct(PlanVersion.planning_run_id))).where(
                PlanVersion.user_id == user.id
            )
        )
        or 0
    )
    active_trips = int(
        await db.scalar(
            select(func.count(TripSession.id)).where(
                TripSession.user_id == user.id,
                TripSession.state.in_(("ACTIVE_TRIP", "PAUSED", "REPLANNING")),
            )
        )
        or 0
    )

    runs = (
        await db.scalars(
            select(PlanningRun)
            .where(PlanningRun.user_id == user.id)
            .order_by(PlanningRun.created_at.desc(), PlanningRun.id.desc())
            .limit(limit * 3)
        )
    ).all()
    recent = []
    for run in runs:
        latest_version = await db.scalar(
            select(PlanVersion)
            .where(PlanVersion.planning_run_id == run.id)
            .order_by(PlanVersion.version.desc())
            .limit(1)
        )
        if latest_version is None:
            continue
        trip = await db.scalar(
            select(TripSession)
            .where(TripSession.planning_run_id == run.id, TripSession.user_id == user.id)
            .order_by(TripSession.updated_at.desc(), TripSession.id.desc())
            .limit(1)
        )
        snapshot = json.loads(latest_version.snapshot_json)
        snapshot["planning_run_id"] = run.id
        snapshot["plan_version"] = latest_version.version
        recent.append(
            {
                "planning_run_id": run.id,
                "input_text": run.input_text,
                "status": run.status,
                "created_at": run.created_at,
                "plan_version": latest_version.version,
                "change_reason": latest_version.change_reason,
                "trip_id": trip.id if trip else None,
                "trip_state": trip.state if trip else None,
                "summary": {
                    "stop_count": len(snapshot.get("stops") or []),
                    "total_distance_meters": snapshot.get("total_distance_meters", 0),
                    "total_travel_seconds": snapshot.get("total_travel_seconds", 0),
                    "confidence": snapshot.get("confidence", 0),
                },
                "snapshot": snapshot,
            }
        )
        if len(recent) >= limit:
            break

    return {
        "ok": True,
        "data": {
            "total_runs": total_runs,
            "successful_runs": successful_runs,
            "formal_plans": formal_plans,
            "active_trips": active_trips,
            "success_rate": successful_runs / total_runs if total_runs else None,
            "recent": recent,
        },
    }


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
    return apply_patch_structure(snapshot, operations)


async def _recalculate_snapshot(
    snapshot: dict,
    stops: list[dict],
    provider,
    replan_context: dict | None = None,
) -> tuple[dict, list[str]]:
    return await recalculate_and_validate_snapshot(snapshot, stops, provider, replan_context)


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
        now = datetime.now(timezone.utc)
        patch.status = "rejected"
        patch.decided_at = now
        trips = (
            await db.scalars(
                select(TripSession).where(
                    TripSession.planning_run_id == run_id,
                    TripSession.user_id == user.id,
                )
            )
        ).all()
        for trip in trips:
            db.add(
                TripEvent(
                    trip_session_id=trip.id,
                    event_id=f"patch-{patch.id}-rejected",
                    event_type="PlanPatchRejected",
                    payload_json=json.dumps({"patch_id": patch.id}),
                    occurred_at=now,
                    status="processed",
                    impact_level="none",
                    decision_json=json.dumps({"accepted": False}),
                    processed_at=now,
                )
            )
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
        patch.status = "blocked"
        patch.decided_at = datetime.now(timezone.utc)
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
    decided_at = datetime.now(timezone.utc)
    patch.decided_at = decided_at
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
            TripEvent(
                trip_session_id=trip.id,
                event_id=f"patch-{patch.id}-accepted",
                event_type="PlanPatchAccepted",
                payload_json=json.dumps({"patch_id": patch.id, "plan_version": current + 1}),
                occurred_at=decided_at,
                status="processed",
                impact_level="none",
                decision_json=json.dumps({"accepted": True}),
                processed_at=decided_at,
            )
        )
    await db.execute(
        update(AgentArtifact)
        .where(
            AgentArtifact.workflow_run_id.in_(
                select(AgentWorkflowRun.id).where(AgentWorkflowRun.planning_run_id == run_id)
            ),
            AgentArtifact.status == "active",
            (AgentArtifact.plan_version.is_(None)) | (AgentArtifact.plan_version < current + 1),
        )
        .values(status="stale", invalidated_at=decided_at)
    )
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
