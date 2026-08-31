"""Executable replay benchmark for Single-Controller versus Multi-Agent planning.

The default profile is deterministic and offline: it runs the real Agent
implementations and deterministic tools with a scripted parser/provider. It
does not pretend that mock execution has LLM token cost; those metrics remain
zero unless a future live profile supplies a metered parser.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.app.clients.amap_client import MockMapProvider  # noqa: E402
from backend.app.core.config import Settings  # noqa: E402
from backend.app.db.session import Base  # noqa: E402
from backend.app.models import (  # noqa: E402
    AgentWorkflowRun,
    AgentWorkflowTask,
    PlanningRun,
    PlanPatch,
    PlanVersion,
    TripSession,
    User,
)
from backend.app.schemas.agent_artifacts import (  # noqa: E402
    AgentEndpoint,
    AgentMessageType,
    AgentType,
    AgentWorkflowTrace,
)
from backend.app.schemas.ai_intent import (  # noqa: E402
    AIPlanRequest,
    AIPlanResult,
    Coordinate,
    HardConstraints,
    PartyProfile,
    PlanningIntent,
    PlanningPreferences,
    PlanningState,
    PlanningTask,
    PoiCandidate,
    TransportMode,
    TripConstraintSet,
)
from backend.app.schemas.dynamic_replanning import TripEventArtifact  # noqa: E402
from backend.app.services.agent_evaluation import (  # noqa: E402
    evaluate_route_plan,
    runtime_route_policy,
)
from backend.app.services.agent_protocol import AgentMessageRouter  # noqa: E402
from backend.app.services.agent_tool_registry import (  # noqa: E402
    TOOL_REGISTRY,
    CapabilityAuthorizationError,
    DataScope,
    InvocationMode,
)
from backend.app.services.agent_transport import (  # noqa: E402
    InMemoryAgentMessageTransport,
)
from backend.app.services.agents.intent_agent import IntentAgent  # noqa: E402
from backend.app.services.agents.planner_agent import (  # noqa: E402
    PlannerAgent,
    PlannerAgentInput,
)
from backend.app.services.agents.search_agent import (  # noqa: E402
    SearchAgent,
    SearchAgentInput,
)
from backend.app.services.dynamic_replanning import DynamicReplanningOrchestrator  # noqa: E402
from backend.app.services.planning_service import PlanningService  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEPARTURE = datetime(2030, 5, 20, 9, 0, tzinfo=SHANGHAI)
ORIGIN = Coordinate(lng=120.1551, lat=30.2741)

Scenario = Literal[
    "standard",
    "safety",
    "clarification",
    "search_recovery",
    "matrix_failure",
    "infeasible",
    "critic_bad_evidence",
    "weather_change",
    "poi_closed",
    "off_route",
    "duplicate_event",
    "worker_crash_recovery",
    "tool_escalation",
]

SCENARIOS: tuple[Scenario, ...] = (
    "standard",
    "safety",
    "clarification",
    "search_recovery",
    "matrix_failure",
    "infeasible",
    "critic_bad_evidence",
    "weather_change",
    "poi_closed",
    "off_route",
    "duplicate_event",
    "worker_crash_recovery",
    "tool_escalation",
)


@dataclass(frozen=True)
class ReplayCase:
    case_id: str
    scenario: Scenario
    request: AIPlanRequest
    intent: PlanningIntent
    expected_statuses: frozenset[str]
    expected_tools: frozenset[str]
    requires_replan: bool = False
    critic_should_intercept: bool = False


@dataclass
class ReplayResult:
    case_id: str
    scenario: str
    runner: str
    task_success: bool
    hard_constraints_satisfied: bool
    tool_selection_accurate: bool
    unauthorized_tool_attempts: int
    unauthorized_tool_executions: int
    handoff_success_rate: float
    recovery_required: bool
    recovery_success: bool
    replanning_required: bool
    replanning_success: bool
    critic_intercept_expected: bool
    critic_intercepted: bool
    agent_count: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    token_cost_usd: float
    latency_ms: float
    terminal_status: str
    actual_tools: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    production_replan_executed: bool = False
    workflow_graph_valid: bool = False
    execution_mode: str | None = None
    agent_task_count: int = 0
    stage_task_count: int = 0


@dataclass(frozen=True)
class DynamicReplayEvidence:
    status: str
    replanning_succeeded: bool
    workflow_graph_valid: bool
    execution_mode: str
    agent_task_count: int
    stage_task_count: int
    handoff_count: int


class ReplayIntentParser:
    name = "offline-replay-intent-v1"
    input_tokens = 0
    output_tokens = 0
    fallback_used = False
    fallback_reason = None

    def __init__(self, intent: PlanningIntent) -> None:
        self.intent = intent

    async def parse(self, _text: str) -> PlanningIntent:
        return self.intent.model_copy(deep=True)


class FaultInjectingMapProvider(MockMapProvider):
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.search_calls = 0
        self.matrix_calls = 0
        self.successful_matrix_calls = 0
        self.search_arguments: list[tuple[str, Coordinate, str | None]] = []
        self.matrix_arguments: list[tuple[int, TransportMode]] = []
        self._failed_keywords: set[str] = set()

    async def search_poi(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]:
        self.search_calls += 1
        self.search_arguments.append((keyword, origin, city))
        if self.scenario == "search_recovery" and keyword not in self._failed_keywords:
            self._failed_keywords.add(keyword)
            raise TimeoutError("scripted provider timeout")
        candidates = await super().search_poi(keyword, origin, city)
        if self.scenario == "critic_bad_evidence":
            return [item.model_copy(update={"source": "unknown"}) for item in candidates]
        return candidates

    async def route_matrix(self, points, mode):
        self.matrix_calls += 1
        self.matrix_arguments.append((len(points), mode))
        if self.scenario == "matrix_failure":
            raise TimeoutError("scripted matrix timeout")
        result = await super().route_matrix(points, mode)
        self.successful_matrix_calls += 1
        return result


def build_cases(count: int = 100) -> list[ReplayCase]:
    if count < len(SCENARIOS):
        raise ValueError(f"benchmark requires at least {len(SCENARIOS)} cases")
    return [_build_case(index, SCENARIOS[index % len(SCENARIOS)]) for index in range(count)]


def _build_case(index: int, scenario: Scenario) -> ReplayCase:
    suffix = f"{index:03d}"
    tasks = [
        PlanningTask(
            description=f"museum-{suffix}",
            location_name=f"museum-{suffix}",
            service_duration_minutes=30,
        ),
        PlanningTask(
            description=f"park-{suffix}",
            location_name=f"park-{suffix}",
            service_duration_minutes=25,
        ),
    ]
    preferences = PlanningPreferences()
    hard = HardConstraints(max_total_duration_minutes=480)
    origin: Coordinate | None = ORIGIN
    expected_statuses = frozenset({"success"})
    requires_replan = scenario in {
        "weather_change",
        "poi_closed",
        "off_route",
        "duplicate_event",
    }
    critic_should_intercept = scenario == "critic_bad_evidence"

    if scenario == "safety":
        preferences = PlanningPreferences(minimize_walking=True, travel_style="relaxed")
        hard = HardConstraints(
            max_total_duration_minutes=480,
            max_walking_meters=8_000,
            party=PartyProfile(adults=1, elderly=1),
        )
    elif scenario == "clarification":
        origin = None
        expected_statuses = frozenset({"need_clarification"})
    elif scenario == "matrix_failure":
        expected_statuses = frozenset({"need_clarification"})
    elif scenario == "infeasible":
        tasks[0] = tasks[0].model_copy(update={"deadline": DEPARTURE - timedelta(minutes=5)})
        expected_statuses = frozenset({"infeasible", "need_clarification"})
    elif scenario == "critic_bad_evidence":
        expected_statuses = frozenset({"need_clarification"})

    intent = PlanningIntent(
        origin="benchmark-origin" if origin else None,
        departure_time=DEPARTURE,
        transport_mode=TransportMode.driving,
        tasks=tasks,
        preferences=preferences,
        constraints=TripConstraintSet(hard=hard),
    )
    request = AIPlanRequest(
        text=f"replay benchmark {scenario} {suffix}",
        origin=origin,
        departure_time=DEPARTURE,
        city="Hangzhou",
        constraints=intent.constraints,
    )
    expected_tools = {"parse_requirement"}
    if scenario not in {"clarification", "worker_crash_recovery"}:
        expected_tools.add("search_poi")
    if scenario not in {"clarification", "worker_crash_recovery"}:
        expected_tools.add("get_route_matrix")
    if scenario not in {
        "clarification",
        "matrix_failure",
        "worker_crash_recovery",
    }:
        expected_tools.add("optimize_route")
    if scenario == "safety":
        expected_tools.add("check_travel_safety")
    if scenario == "worker_crash_recovery":
        expected_tools = set()
        expected_statuses = frozenset({"processed_once"})
    return ReplayCase(
        case_id=f"replay_{suffix}",
        scenario=scenario,
        request=request,
        intent=intent,
        expected_statuses=expected_statuses,
        expected_tools=frozenset(expected_tools),
        requires_replan=requires_replan,
        critic_should_intercept=critic_should_intercept,
    )


def _settings(*, multi: bool) -> Settings:
    return Settings(
        mock_map_provider=True,
        multi_agent_enabled=multi,
        plan_critic_mode="enforce" if multi else "off",
        agent_search_max_attempts=2,
        agent_stage_timeout_seconds=2,
        max_agent_workflow_cost_usd=1,
        max_agent_handoffs=20,
    )


def _tools_from_execution(
    trace: AgentWorkflowTrace | None,
    provider: FaultInjectingMapProvider,
    result: AIPlanResult | None,
) -> set[str]:
    tools: set[str] = set()
    agents = {step.agent_type for step in trace.steps} if trace else set()
    if AgentType.intent in agents:
        tools.add("parse_requirement")
    if provider.search_calls:
        tools.add("search_poi")
    if AgentType.safety in agents:
        tools.add("check_travel_safety")
    if provider.matrix_calls:
        tools.add("get_route_matrix")
    if provider.successful_matrix_calls and result and result.algorithm:
        tools.add("optimize_route")
    return tools


def _trace_metrics(
    traces: list[AgentWorkflowTrace],
) -> tuple[int, int, int, int, float, float]:
    steps = [step for trace in traces for step in trace.steps]
    messages = [message for trace in traces for message in trace.messages]
    agents = {step.agent_type for step in steps}
    llm_calls = sum(1 for step in steps if step.input_tokens or step.output_tokens)
    input_tokens = sum(step.input_tokens for step in steps)
    output_tokens = sum(step.output_tokens for step in steps)
    cost = sum(trace.total_cost_usd for trace in traces)
    handoff_rate = (
        sum(message.delivery_status in {"delivered", "duplicate"} for message in messages)
        / len(messages)
        if messages
        else 1.0
    )
    return len(agents), llm_calls, input_tokens, output_tokens, cost, handoff_rate


def _hard_constraints_satisfied(result: AIPlanResult | None) -> bool:
    if result is None or result.status != "success":
        return True
    payload = result.model_dump(mode="json", exclude={"agent_workflow"})
    return evaluate_route_plan(payload, runtime_route_policy(payload)).passed


def _attempt_tool_escalation() -> tuple[int, int]:
    try:
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.companion,
            capability="optimize_route",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.route_optimization}),
        )
    except CapabilityAuthorizationError:
        return 1, 0
    return 1, 1


async def _worker_crash_replay(case: ReplayCase, runner: str) -> ReplayResult:
    started = time.perf_counter()
    if runner == "single_agent":
        return ReplayResult(
            case_id=case.case_id,
            scenario=case.scenario,
            runner=runner,
            task_success=False,
            hard_constraints_satisfied=True,
            tool_selection_accurate=True,
            unauthorized_tool_attempts=0,
            unauthorized_tool_executions=0,
            # A single controller has no inter-Agent hand-off. Message loss is
            # scored under recovery, not as a fictitious hand-off failure.
            handoff_success_rate=1,
            recovery_required=True,
            recovery_success=False,
            replanning_required=False,
            replanning_success=False,
            critic_intercept_expected=False,
            critic_intercepted=False,
            agent_count=1,
            llm_calls=0,
            input_tokens=0,
            output_tokens=0,
            token_cost_usd=0,
            latency_ms=(time.perf_counter() - started) * 1_000,
            terminal_status="message_lost_after_crash",
        )
    router = AgentMessageRouter()
    transport = InMemoryAgentMessageTransport(router)
    message = router.build(
        task_id=f"{case.case_id}-task",
        sender=AgentEndpoint.user,
        receiver=AgentEndpoint.supervisor,
        message_type=AgentMessageType.command,
        artifact_type="planning_request",
        content=case.request.model_dump(mode="json"),
    )
    await transport.publish(message)
    crashed = await transport.receive(AgentEndpoint.supervisor, "crashed-worker", block_ms=0)
    assert crashed is not None
    reclaimed = await transport.reclaim(
        AgentEndpoint.supervisor, "recovery-worker", min_idle_ms=0, count=1
    )
    recovered = bool(reclaimed and await transport.acknowledge(reclaimed[0]))
    return ReplayResult(
        case_id=case.case_id,
        scenario=case.scenario,
        runner=runner,
        task_success=recovered,
        hard_constraints_satisfied=True,
        tool_selection_accurate=True,
        unauthorized_tool_attempts=0,
        unauthorized_tool_executions=0,
        handoff_success_rate=1 if recovered else 0,
        recovery_required=True,
        recovery_success=recovered,
        replanning_required=False,
        replanning_success=False,
        critic_intercept_expected=False,
        critic_intercepted=False,
        agent_count=2,
        llm_calls=0,
        input_tokens=0,
        output_tokens=0,
        token_cost_usd=0,
        latency_ms=(time.perf_counter() - started) * 1_000,
        terminal_status="processed_once" if recovered else "recovery_failed",
    )


def _baseline_dynamic_intent(case: ReplayCase) -> PlanningIntent:
    """Simulated updated intent used only by the Single Controller baseline."""
    intent = case.intent.model_copy(deep=True)
    if case.scenario in {"weather_change", "poi_closed"}:
        intent.tasks[1] = PlanningTask(
            description=f"indoor-museum-{case.case_id}",
            location_name=f"indoor-museum-{case.case_id}",
            service_duration_minutes=25,
        )
    return intent


def _dynamic_event(
    case: ReplayCase,
    result: AIPlanResult,
) -> tuple[TripEventArtifact, Coordinate, dict[str, Any], dict[str, Any] | None]:
    stops = result.model_dump(mode="json").get("stops") or []
    first_stop_id = str(stops[0]["poi"]["id"])
    second_stop_id = str(stops[-1]["poi"]["id"])
    event_type = {
        "weather_change": "WeatherAlertReceived",
        "poi_closed": "PoiStatusChanged",
        "off_route": "UserOffRoute",
        "duplicate_event": "TrafficChanged",
    }[case.scenario]
    payload: dict[str, Any] = {}
    weather = None
    if case.scenario == "weather_change":
        payload = {"outdoor_stop_ids": [second_stop_id]}
        weather = {"precipitation_probability": 90, "weather_code": 82}
    elif case.scenario == "poi_closed":
        payload = {"closed_poi_id": first_stop_id}
    elif case.scenario == "duplicate_event":
        payload = {"delay_minutes": 20, "allow_transport_switch": True}
    current_location = result.origin or ORIGIN
    if case.scenario == "off_route":
        current_location = Coordinate(lng=ORIGIN.lng + 0.01, lat=ORIGIN.lat + 0.01)
    event_number = int(hashlib.sha256(case.case_id.encode()).hexdigest()[:8], 16) + 1
    event = TripEventArtifact(
        trip_id=1,
        event_id=event_number,
        event_type=event_type,
        occurred_at=DEPARTURE + timedelta(minutes=30),
        impact_level="high",
        reason=f"production replay: {case.scenario}",
        payload_summary=payload,
        base_plan_version=1,
    )
    return event, current_location, payload, weather


async def _run_production_dynamic_replay(
    case: ReplayCase,
    result: AIPlanResult,
    provider: FaultInjectingMapProvider,
) -> DynamicReplayEvidence:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            user = User(
                username="agent-eval",
                nickname="Agent Eval",
                pass_hash="evaluation-only",
            )
            db.add(user)
            await db.flush()
            snapshot = result.model_dump(mode="json", exclude={"agent_workflow"})
            planning_run = PlanningRun(
                user_id=user.id,
                input_text=case.request.text,
                intent_json=json.dumps(snapshot["intent"], ensure_ascii=False),
                result_json=json.dumps(snapshot, ensure_ascii=False),
                status=result.status,
                model_name="offline-replay",
                prompt_version="evaluation-v1",
                map_provider=provider.name,
            )
            db.add(planning_run)
            await db.flush()
            db.add(
                PlanVersion(
                    planning_run_id=planning_run.id,
                    user_id=user.id,
                    version=1,
                    snapshot_json=json.dumps(snapshot, ensure_ascii=False),
                    change_reason="evaluation_initial_plan",
                )
            )
            trip = TripSession(
                user_id=user.id,
                planning_run_id=planning_run.id,
                state="ACTIVE_TRIP",
                current_plan_version=1,
                context_json=json.dumps(
                    {"completed_stop_ids": [], "auto_apply_low_risk_patches": False}
                ),
            )
            db.add(trip)
            await db.flush()

            event, current_location, payload, weather = _dynamic_event(case, result)
            event = event.model_copy(update={"trip_id": trip.id})
            orchestrator = DynamicReplanningOrchestrator(db, provider)
            first = await orchestrator.run(
                trip=trip,
                event=event,
                current_location=current_location,
                completed_stop_ids=[],
                event_payload=payload,
                weather=weather,
                trace_id=f"eval-{case.case_id}",
            )
            duplicate_valid = True
            if case.scenario == "duplicate_event":
                second = await orchestrator.run(
                    trip=trip,
                    event=event,
                    current_location=current_location,
                    completed_stop_ids=[],
                    event_payload=payload,
                    weather=weather,
                    trace_id=f"eval-{case.case_id}-duplicate",
                )
                duplicate_valid = second.get("patch_id") == first.get("patch_id")

            workflow = await db.scalar(
                select(AgentWorkflowRun)
                .where(AgentWorkflowRun.trip_session_id == trip.id)
                .order_by(AgentWorkflowRun.id.desc())
            )
            assert workflow is not None
            tasks = (
                await db.scalars(
                    select(AgentWorkflowTask)
                    .where(AgentWorkflowTask.workflow_run_id == workflow.id)
                    .order_by(AgentWorkflowTask.id)
                )
            ).all()
            expected_graph = {
                "event_ingest": [],
                "supervisor_dispatch": ["event_ingest"],
                "replanner": ["supervisor_dispatch"],
                "deterministic_replan": ["replanner"],
                "deterministic_review": ["deterministic_replan"],
                "final_answer": ["deterministic_review"],
            }
            graph_valid = len(tasks) == len(expected_graph) and all(
                json.loads(task.dependency_keys_json) == expected_graph.get(task.task_key)
                for task in tasks
            )
            patch_count = int(
                await db.scalar(
                    select(func.count(PlanPatch.id)).where(
                        PlanPatch.planning_run_id == planning_run.id
                    )
                )
                or 0
            )
            replanning_succeeded = bool(first.get("patch_id"))
            if case.scenario == "duplicate_event":
                replanning_succeeded = replanning_succeeded and duplicate_valid and patch_count == 1
            return DynamicReplayEvidence(
                status=str(first.get("status") or "unknown"),
                replanning_succeeded=replanning_succeeded,
                workflow_graph_valid=graph_valid,
                execution_mode=workflow.execution_mode,
                agent_task_count=sum(task.execution_kind == "agent" for task in tasks),
                stage_task_count=sum(task.execution_kind == "stage" for task in tasks),
                handoff_count=workflow.handoff_count,
            )
    finally:
        await engine.dispose()


async def run_multi_agent(case: ReplayCase) -> ReplayResult:
    if case.scenario == "worker_crash_recovery":
        return await _worker_crash_replay(case, "multi_agent")
    started = time.perf_counter()
    settings = _settings(multi=True)
    provider = FaultInjectingMapProvider(case.scenario)
    service = PlanningService(ReplayIntentParser(case.intent), provider, settings)
    result = await service.plan(case.request)
    traces = [AgentWorkflowTrace.model_validate(result.agent_workflow)]
    replanning_success = False
    dynamic_evidence: DynamicReplayEvidence | None = None
    if case.requires_replan and result.status == "success":
        dynamic_evidence = await _run_production_dynamic_replay(case, result, provider)
        replanning_success = dynamic_evidence.replanning_succeeded
    unauthorized_attempts = unauthorized_executions = 0
    if case.scenario == "tool_escalation":
        unauthorized_attempts, unauthorized_executions = _attempt_tool_escalation()
    actual_tools = _tools_from_execution(traces[-1], provider, result)
    agent_count, llm_calls, input_tokens, output_tokens, cost, handoff_rate = _trace_metrics(traces)
    critic_intercepted = bool(
        result.critic_review and result.critic_review.verdict == "needs_clarification"
    )
    recovery_required = case.requires_replan or case.scenario == "search_recovery"
    recovery_success = replanning_success if case.requires_replan else result.status == "success"
    task_success = result.status in case.expected_statuses
    if case.requires_replan:
        task_success = task_success and replanning_success
    return ReplayResult(
        case_id=case.case_id,
        scenario=case.scenario,
        runner="multi_agent",
        task_success=task_success,
        hard_constraints_satisfied=_hard_constraints_satisfied(result),
        tool_selection_accurate=actual_tools == set(case.expected_tools),
        unauthorized_tool_attempts=unauthorized_attempts,
        unauthorized_tool_executions=unauthorized_executions,
        handoff_success_rate=handoff_rate,
        recovery_required=recovery_required,
        recovery_success=recovery_success if recovery_required else False,
        replanning_required=case.requires_replan,
        replanning_success=replanning_success,
        critic_intercept_expected=case.critic_should_intercept,
        critic_intercepted=critic_intercepted,
        agent_count=agent_count,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_cost_usd=cost,
        latency_ms=(time.perf_counter() - started) * 1_000,
        terminal_status=result.status,
        actual_tools=sorted(actual_tools),
        expected_tools=sorted(case.expected_tools),
        production_replan_executed=dynamic_evidence is not None,
        workflow_graph_valid=bool(dynamic_evidence and dynamic_evidence.workflow_graph_valid),
        execution_mode=dynamic_evidence.execution_mode if dynamic_evidence else None,
        agent_task_count=dynamic_evidence.agent_task_count if dynamic_evidence else 0,
        stage_task_count=dynamic_evidence.stage_task_count if dynamic_evidence else 0,
    )


async def run_single_agent(case: ReplayCase) -> ReplayResult:
    if case.scenario == "worker_crash_recovery":
        return await _worker_crash_replay(case, "single_agent")
    started = time.perf_counter()
    settings = _settings(multi=False)
    provider = FaultInjectingMapProvider(case.scenario)
    actual_tools: set[str] = set()
    input_tokens = output_tokens = 0

    async def execute_once(
        intent_input: PlanningIntent, request_input: AIPlanRequest
    ) -> AIPlanResult:
        nonlocal input_tokens, output_tokens
        intent_execution = await IntentAgent(ReplayIntentParser(intent_input)).run(request_input)
        input_tokens += intent_execution.input_tokens
        output_tokens += intent_execution.output_tokens
        intent, questions = intent_execution.output
        actual_tools.add("parse_requirement")
        if questions:
            return AIPlanResult(
                status="need_clarification",
                planning_state=PlanningState.need_clarification,
                intent=intent,
                origin=request_input.origin,
                questions=questions,
            )
        search_execution = await SearchAgent(provider, settings).run(
            SearchAgentInput(
                intent=intent,
                origin=request_input.origin or ORIGIN,
                city=request_input.city,
            )
        )
        actual_tools.add("search_poi")
        planner_execution = await PlannerAgent(provider, settings).run(
            PlannerAgentInput(
                intent=intent,
                origin=request_input.origin or ORIGIN,
                city=request_input.city,
                search=search_execution.output,
            )
        )
        if provider.matrix_calls:
            actual_tools.add("get_route_matrix")
        result_once = planner_execution.output
        if provider.successful_matrix_calls and result_once.algorithm:
            actual_tools.add("optimize_route")
        return result_once

    result = await execute_once(case.intent, case.request)
    agent_count = 1
    replanning_success = False
    if case.requires_replan:
        if case.scenario == "duplicate_event":
            seen: set[str] = set()
            event_id = f"event-{case.case_id}"
            writes = 0
            for _ in range(2):
                if event_id in seen:
                    continue
                seen.add(event_id)
                writes += 1
            replanning_success = writes == 1
        else:
            updated_request = case.request.model_copy(deep=True)
            if case.scenario == "off_route":
                updated_request.origin = Coordinate(lng=ORIGIN.lng + 0.01, lat=ORIGIN.lat + 0.01)
            result = await execute_once(_baseline_dynamic_intent(case), updated_request)
            replanning_success = result.status == "success"
    unauthorized_attempts = unauthorized_executions = 0
    if case.scenario == "tool_escalation":
        unauthorized_attempts, unauthorized_executions = _attempt_tool_escalation()
    critic_intercepted = False
    task_success = result.status in case.expected_statuses
    if case.requires_replan:
        task_success = task_success and replanning_success
    if case.critic_should_intercept:
        task_success = False
    recovery_required = case.requires_replan or case.scenario == "search_recovery"
    recovery_success = replanning_success if case.requires_replan else result.status == "success"
    return ReplayResult(
        case_id=case.case_id,
        scenario=case.scenario,
        runner="single_agent",
        task_success=task_success,
        hard_constraints_satisfied=_hard_constraints_satisfied(result),
        tool_selection_accurate=actual_tools == set(case.expected_tools),
        unauthorized_tool_attempts=unauthorized_attempts,
        unauthorized_tool_executions=unauthorized_executions,
        handoff_success_rate=1,
        recovery_required=recovery_required,
        recovery_success=recovery_success if recovery_required else False,
        replanning_required=case.requires_replan,
        replanning_success=replanning_success,
        critic_intercept_expected=case.critic_should_intercept,
        critic_intercepted=critic_intercepted,
        agent_count=agent_count,
        llm_calls=int(bool(input_tokens or output_tokens)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_cost_usd=0,
        latency_ms=(time.perf_counter() - started) * 1_000,
        terminal_status=result.status,
        actual_tools=sorted(actual_tools),
        expected_tools=sorted(case.expected_tools),
    )


def _rate(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def aggregate(results: list[ReplayResult]) -> dict[str, Any]:
    recovery = [item for item in results if item.recovery_required]
    replan = [item for item in results if item.replanning_required]
    production_replans = [item for item in replan if item.production_replan_executed]
    critic = [item for item in results if item.critic_intercept_expected]
    executable = [item for item in results if item.terminal_status == "success"]
    total_tool_attempts = sum(
        max(1, len(item.actual_tools)) + item.unauthorized_tool_attempts for item in results
    )
    return {
        "case_count": len(results),
        "task_completion_rate": round(_rate([item.task_success for item in results]), 4),
        "hard_constraint_satisfaction_rate": round(
            _rate([item.hard_constraints_satisfied for item in results]), 4
        ),
        "executable_plan_constraint_satisfaction_rate": round(
            _rate([item.hard_constraints_satisfied for item in executable]), 4
        ),
        "tool_selection_accuracy": round(
            _rate([item.tool_selection_accurate for item in results]), 4
        ),
        "illegal_tool_execution_rate": round(
            sum(item.unauthorized_tool_executions for item in results) / total_tool_attempts,
            6,
        ),
        "agent_handoff_success_rate": round(
            statistics.fmean(item.handoff_success_rate for item in results), 4
        ),
        "recovery_success_rate": round(_rate([item.recovery_success for item in recovery]), 4),
        "replanning_success_rate": round(_rate([item.replanning_success for item in replan]), 4),
        "production_dynamic_replay_rate": round(
            _rate([item.production_replan_executed for item in replan]), 4
        ),
        "workflow_graph_accuracy": round(
            _rate([item.workflow_graph_valid for item in production_replans]), 4
        ),
        "average_true_agent_tasks_per_dynamic_run": round(
            statistics.fmean(item.agent_task_count for item in production_replans), 2
        )
        if production_replans
        else 0,
        "average_deterministic_stages_per_dynamic_run": round(
            statistics.fmean(item.stage_task_count for item in production_replans), 2
        )
        if production_replans
        else 0,
        "critic_bad_plan_recall": round(_rate([item.critic_intercepted for item in critic]), 4),
        "average_agent_count": round(statistics.fmean(item.agent_count for item in results), 2),
        "average_llm_calls": round(statistics.fmean(item.llm_calls for item in results), 2),
        "average_input_tokens": round(statistics.fmean(item.input_tokens for item in results), 2),
        "average_output_tokens": round(statistics.fmean(item.output_tokens for item in results), 2),
        "average_token_cost_usd": round(
            statistics.fmean(item.token_cost_usd for item in results), 6
        ),
        "latency_p50_ms": round(_percentile([item.latency_ms for item in results], 0.50), 2),
        "latency_p95_ms": round(_percentile([item.latency_ms for item in results], 0.95), 2),
    }


async def benchmark(case_count: int = 100) -> dict[str, Any]:
    cases = build_cases(case_count)
    single_results: list[ReplayResult] = []
    multi_results: list[ReplayResult] = []
    for case in cases:
        single_results.append(await run_single_agent(case))
        multi_results.append(await run_multi_agent(case))
    dataset_payload = [
        {
            "case_id": item.case_id,
            "scenario": item.scenario,
            "request": item.request.model_dump(mode="json"),
            "intent": item.intent.model_dump(mode="json"),
        }
        for item in cases
    ]
    dataset_hash = hashlib.sha256(
        json.dumps(dataset_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return {
        "benchmark": "mapgo-agent-replay-v2",
        "profile": "offline_deterministic",
        "dataset_hash": dataset_hash,
        "case_count": len(cases),
        "scenario_counts": {
            scenario: sum(case.scenario == scenario for case in cases) for scenario in SCENARIOS
        },
        "single_agent": aggregate(single_results),
        "multi_agent": aggregate(multi_results),
        "notes": {
            "llm_metrics": "zero in offline profile; no model endpoint is called",
            "latency": "local deterministic execution; not production network latency",
            "single_agent_definition": (
                "one controller using the same deterministic search and route tools, without "
                "Supervisor, Safety, Critic or role handoffs; it may replay dynamic events"
            ),
            "dynamic_replay": (
                "multi-agent dynamic cases execute the production DynamicReplanningOrchestrator "
                "against an isolated SQLite database and score the persisted workflow DAG"
            ),
        },
        "failures": {
            "single_agent": [asdict(item) for item in single_results if not item.task_success],
            "multi_agent": [asdict(item) for item in multi_results if not item.task_success],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--details", action="store_true")
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="Write the reproducible benchmark report to this JSON path.",
    )
    args = parser.parse_args()
    report = asyncio.run(benchmark(args.cases))
    if not args.details:
        report["failures"] = {key: len(value) for key, value in report["failures"].items()}
    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    multi = report["multi_agent"]
    single = report["single_agent"]
    passed = (
        report["case_count"] >= 100
        and multi["task_completion_rate"] >= 0.95
        and multi["task_completion_rate"] >= single["task_completion_rate"]
        and multi["hard_constraint_satisfaction_rate"] == 1
        and multi["executable_plan_constraint_satisfaction_rate"] == 1
        and multi["tool_selection_accuracy"] >= 0.95
        and multi["illegal_tool_execution_rate"] == 0
        and multi["agent_handoff_success_rate"] == 1
        and multi["recovery_success_rate"] >= single["recovery_success_rate"]
        and multi["critic_bad_plan_recall"] == 1
        and multi["production_dynamic_replay_rate"] == 1
        and multi["workflow_graph_accuracy"] == 1
        and multi["average_true_agent_tasks_per_dynamic_run"] == 1
        and multi["average_deterministic_stages_per_dynamic_run"] == 5
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
