from typing import Any

import pytest
from pydantic import ValidationError

from backend.app.clients.amap_client import MockMapProvider
from backend.app.core.config import Settings
from backend.app.models import AgentArtifact, AgentRun, AgentWorkflowRun
from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessageType,
    AgentType,
    AgentWorkflowMode,
    ArtifactEnvelope,
    CriticSoftAdjustments,
    ReviewReport,
    minimize_agent_payload,
)
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    Coordinate,
    HardConstraints,
    PartyProfile,
    PlanningIntent,
    PlanningTask,
    TripConstraintSet,
    UncertainConstraint,
)
from backend.app.services.agent_protocol import AgentMessageRouter, AgentProtocolError
from backend.app.services.agent_readiness import build_critic_readiness_report
from backend.app.services.agent_role_contracts import ROLE_CONTRACTS
from backend.app.services.agents.base import AgentExecution, canonical_hash
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC
from backend.app.services.agents.critic_agent import CRITIC_AGENT_SPEC
from backend.app.services.agents.intent_agent import INTENT_AGENT_SPEC, IntentAgent
from backend.app.services.agents.planner_agent import PLANNER_AGENT_SPEC
from backend.app.services.agents.safety_agent import SAFETY_AGENT_SPEC
from backend.app.services.agents.search_agent import _POI_RECOVERY_CACHE, SEARCH_AGENT_SPEC
from backend.app.services.agents.supervisor_agent import SUPERVISOR_AGENT_SPEC, SupervisorAgent
from backend.app.services.planning_service import PlanningService


class StableParser:
    name = "stable-isolation-parser"
    input_tokens = 10
    output_tokens = 5

    async def parse(self, _text: str) -> PlanningIntent:
        return PlanningIntent(tasks=[PlanningTask(description="博物馆", location_name="博物馆")])


class RetryOnceCritic:
    spec = CRITIC_AGENT_SPEC

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, plan: dict[str, Any]) -> AgentExecution[ReviewReport]:
        self.calls += 1
        if self.calls == 1:
            report = ReviewReport(
                verdict="retry_with_soft_adjustments",
                summary="仅提高不确定性软权重后重算",
                suggested_adjustments=CriticSoftAdjustments(uncertainty=1.5),
            )
        else:
            report = ReviewReport(verdict="approved", summary="重算后通过")
        artifact = ArtifactEnvelope(
            artifact_type="review_report",
            producer_agent=AgentType.critic,
            payload=report.model_dump(mode="json"),
            input_hash=canonical_hash(plan),
        )
        return AgentExecution(
            spec=self.spec,
            output=report,
            artifact=artifact,
            latency_ms=1,
        )


class MuseumParser:
    name = "museum-parser"
    input_tokens = 5
    output_tokens = 3

    async def parse(self, _text: str) -> PlanningIntent:
        return PlanningIntent(tasks=[PlanningTask(description="museum", location_name="museum")])


class ElderlyParser:
    name = "elderly-parser"
    input_tokens = 5
    output_tokens = 3

    async def parse(self, _text: str) -> PlanningIntent:
        return PlanningIntent(
            tasks=[PlanningTask(description="museum", location_name="museum")],
            constraints=TripConstraintSet(
                hard=HardConstraints(
                    max_walking_meters=10_000,
                    party=PartyProfile(elderly=1),
                )
            ),
        )


class ToggleFailSearchProvider(MockMapProvider):
    name = "toggle-fail-search-map"

    def __init__(self) -> None:
        self.fail_search = False

    async def search_poi(self, keyword, origin, city):
        if self.fail_search:
            raise TimeoutError("map search unavailable")
        return await super().search_poi(keyword, origin, city)


class AlwaysFailSearchProvider(MockMapProvider):
    name = "always-fail-search-map"

    async def search_poi(self, keyword, origin, city):
        raise TimeoutError("map search unavailable")


@pytest.mark.asyncio
async def test_intent_agent_has_no_tools_and_only_emits_typed_intent_and_questions():
    request = AIPlanRequest(
        text="去博物馆",
        origin=Coordinate(lng=120.62, lat=31.32),
    )
    execution = await IntentAgent(StableParser()).run(request)

    intent, questions = execution.output
    assert INTENT_AGENT_SPEC.allowed_tools == frozenset()
    assert intent.tasks[0].location_name == "博物馆"
    assert questions == []
    assert set(execution.artifact.payload) == {"intent", "questions", "parser"}
    assert "poi" not in execution.artifact.payload


def test_agent_tool_and_schema_boundaries_are_disjoint():
    assert SUPERVISOR_AGENT_SPEC.allowed_tools == frozenset()
    assert INTENT_AGENT_SPEC.allowed_tools == frozenset()
    assert SEARCH_AGENT_SPEC.allowed_tools == frozenset()
    assert SAFETY_AGENT_SPEC.allowed_tools == frozenset()
    assert PLANNER_AGENT_SPEC.allowed_tools == frozenset()
    assert CRITIC_AGENT_SPEC.allowed_tools == frozenset()
    assert COMPANION_AGENT_SPEC.allowed_tools == frozenset(
        {
            "get_trip_state",
            "get_current_location",
            "get_weather",
            "propose_replan",
        }
    )
    assert INTENT_AGENT_SPEC.allowed_internal_capabilities == frozenset(
        {"parse_requirement"}
    )
    assert SEARCH_AGENT_SPEC.allowed_internal_capabilities == frozenset({"search_poi"})
    assert SAFETY_AGENT_SPEC.allowed_internal_capabilities == frozenset(
        {"check_travel_safety"}
    )
    assert PLANNER_AGENT_SPEC.allowed_internal_capabilities == frozenset(
        {"get_route_matrix", "optimize_route", "verify_transit_edges"}
    )
    assert "search_poi" not in COMPANION_AGENT_SPEC.allowed_tools
    assert "create_plan_patch" not in COMPANION_AGENT_SPEC.allowed_tools
    with pytest.raises(ValidationError):
        CriticSoftAdjustments.model_validate({"latest_return_time": "2030-01-01T18:00:00Z"})


def test_five_role_contracts_make_responsibility_boundaries_explicit():
    assert set(ROLE_CONTRACTS) == {
        "requirement_clarification",
        "place_research",
        "itinerary_coordination",
        "plan_review",
        "runtime_companion",
    }
    assert ROLE_CONTRACTS["place_research"].allowed_internal_capabilities == frozenset(
        {"search_poi"}
    )
    assert "modify_formal_plan" in ROLE_CONTRACTS["place_research"].forbidden_actions
    assert ROLE_CONTRACTS["runtime_companion"].allowed_tools == COMPANION_AGENT_SPEC.allowed_tools
    assert "overwrite_formal_plan" in ROLE_CONTRACTS["runtime_companion"].forbidden_actions
    assert "modify_hard_constraints" in ROLE_CONTRACTS["plan_review"].forbidden_actions


@pytest.mark.asyncio
async def test_supervisor_agent_schedules_expected_topology_without_tools():
    execution = await SupervisorAgent().start(
        AIPlanRequest(text="visit museum", origin=Coordinate(lng=120.62, lat=31.32))
    )

    assert execution.spec.allowed_tools == frozenset()
    assert execution.artifact.producer_agent == AgentType.supervisor
    assert execution.output["workflow_state"] == "intent_scheduled"
    assert [item["agent_type"] for item in execution.output["tasks"]] == [
        "intent",
    ]


@pytest.mark.asyncio
async def test_supervisor_dynamic_plan_inserts_safety_for_elderly_trip():
    standard = await SupervisorAgent().plan(
        PlanningIntent(tasks=[PlanningTask(description="museum")]),
        mode=AgentWorkflowMode.shadow,
    )

    elderly_intent = await ElderlyParser().parse("elderly trip")
    execution = await SupervisorAgent().plan(
        elderly_intent,
        mode=AgentWorkflowMode.shadow,
    )

    assert "safety_check" not in [
        item["step_id"] for item in standard.output["steps"]
    ]
    assert execution.output["plan_kind"] == "safety_sensitive_trip"
    assert [item["step_id"] for item in execution.output["steps"]] == [
        "intent",
        "search",
        "safety_check",
        "planner",
        "critic",
        "final_answer",
    ]
    assert execution.output["steps"][2]["depends_on"] == ["intent", "search"]
    assert execution.output["steps"][3]["output_schema_ref"] == "RouteSolution"


@pytest.mark.asyncio
async def test_supervisor_dynamic_plan_is_explicit_acyclic_task_graph_with_weather():
    intent = PlanningIntent(
        tasks=[PlanningTask(description="museum")],
        constraints=TripConstraintSet(
            uncertain=[
                UncertainConstraint(
                    field="weather",
                    reason="avoid rain windows",
                    confidence=0.6,
                    safety_buffer_minutes=30,
                )
            ]
        ),
    )

    execution = await SupervisorAgent().plan(intent, mode=AgentWorkflowMode.shadow)
    steps = execution.output["steps"]
    seen: set[str] = set()
    for step in steps:
        assert set(step["depends_on"]) <= seen
        assert step["status"] == "pending"
        assert step["attempt_count"] == 0
        assert step["version"] == 1
        assert step["output_schema_ref"]
        assert step["budget"]
        seen.add(step["step_id"])
    assert "weather" in [item["step_id"] for item in steps]


def test_critic_retry_report_rejects_hard_constraint_adjustments():
    with pytest.raises(ValidationError):
        ReviewReport.model_validate(
            {
                "verdict": "retry_with_soft_adjustments",
                "summary": "attempts to change hard constraints",
                "findings": [],
                "suggested_adjustments": {
                    "latest_return_time": "2030-01-01T18:00:00Z",
                    "max_walking_meters": 100,
                },
                "confidence": 0.8,
            }
        )


def test_agent_payload_minimization_removes_coordinates_secrets_and_raw_text():
    minimized = minimize_agent_payload(
        {
            "origin": {"lng": 120.62, "lat": 31.32},
            "poi": {"name": "Museum", "location": {"lng": 120.63, "lat": 31.33}},
            "Authorization": "Bearer secret",
            "text": "meet me at my home address",
            "nested": [{"api_key": "secret-key"}],
        }
    )

    assert minimized["origin"]["redacted"] == "coordinate"
    assert minimized["poi"]["location"]["redacted"] == "coordinate"
    assert minimized["Authorization"] == "[REDACTED]"
    assert minimized["text"] == "[REDACTED_TEXT]"
    assert minimized["nested"][0]["api_key"] == "[REDACTED]"


def test_agent_message_protocol_enforces_routes_causality_and_idempotency():
    router = AgentMessageRouter()
    inbound = router.build(
        task_id="plan-protocol-test",
        sender=AgentEndpoint.user,
        receiver=AgentEndpoint.supervisor,
        message_type=AgentMessageType.command,
        artifact_type="planning_request",
        content={"text": "private address", "origin": {"lng": 120.62, "lat": 31.32}},
    )
    delivered, status = router.deliver(inbound)
    duplicate, duplicate_status = router.deliver(inbound)

    assert status == "delivered"
    assert duplicate_status == "duplicate"
    assert duplicate.message_id == delivered.message_id
    audit = router.audit(delivered)
    assert audit.content_summary["text"] == "[REDACTED_TEXT]"
    assert audit.content_summary["origin"]["redacted"] == "coordinate"

    outbound = router.build(
        task_id=inbound.task_id,
        sender=AgentEndpoint.supervisor,
        receiver=AgentEndpoint.intent,
        message_type=AgentMessageType.command,
        artifact_type="planning_request",
        content=inbound.content,
        correlation_id=inbound.correlation_id,
        causation_id=inbound.message_id,
    )
    router.deliver(outbound)
    assert outbound.correlation_id == inbound.correlation_id
    assert outbound.causation_id == inbound.message_id

    outbound.content["text"] = "tampered after signing"
    with pytest.raises(AgentProtocolError, match="content hash mismatch"):
        router.deliver(outbound)


@pytest.mark.parametrize(
    ("sender", "receiver", "message_type", "artifact_type"),
    [
        (
            AgentEndpoint.critic,
            AgentEndpoint.planner,
            AgentMessageType.command,
            "retry_directive",
        ),
        (
            AgentEndpoint.companion,
            AgentEndpoint.planner,
            AgentMessageType.command,
            "planning_request",
        ),
    ],
)
def test_agent_message_protocol_rejects_cross_role_escalation(
    sender: AgentEndpoint,
    receiver: AgentEndpoint,
    message_type: AgentMessageType,
    artifact_type: str,
):
    router = AgentMessageRouter()
    message = router.build(
        task_id="red-team-route-test",
        sender=sender,
        receiver=receiver,
        message_type=message_type,
        artifact_type=artifact_type,
        content={"hard_constraints": {"latest_return_time": "never"}},
    )
    with pytest.raises(AgentProtocolError, match="forbidden agent route"):
        router.deliver(message)


def test_agent_message_protocol_rejects_critic_hard_constraint_smuggling():
    router = AgentMessageRouter()
    message = router.build(
        task_id="red-team-critic-payload",
        sender=AgentEndpoint.critic,
        receiver=AgentEndpoint.supervisor,
        message_type=AgentMessageType.result,
        artifact_type="review_report",
        content={
            "verdict": "retry_with_soft_adjustments",
            "summary": "change a hard constraint",
            "suggested_adjustments": {"latest_return_time": "never"},
        },
    )
    with pytest.raises(AgentProtocolError, match="invalid review_report payload"):
        router.deliver(message)


def test_agent_message_protocol_rejects_forged_shared_state_reference():
    router = AgentMessageRouter()
    message = router.build(
        task_id="plan-state-reference-test",
        sender=AgentEndpoint.supervisor,
        receiver=AgentEndpoint.search,
        message_type=AgentMessageType.command,
        artifact_type="intent_artifact",
        content={
            "shared_state_ref": "plan-another-task",
            "state_revision": 1,
            "state_hash": "0" * 64,
            "artifact_hash": "a" * 64,
            "question_count": 0,
        },
    )
    with pytest.raises(AgentProtocolError, match="invalid intent_artifact payload"):
        router.deliver(message)


def test_critic_readiness_policy_requires_enough_clean_shadow_reviews():
    settings = Settings(
        mock_map_provider=True,
        critic_enforce_min_shadow_samples=2,
        critic_enforce_max_fallback_rate=0,
        critic_enforce_max_blocking_rate=0,
        critic_enforce_max_budget_exceeded_rate=0,
        critic_enforce_max_p95_latency_ms=200,
    )
    workflows = [
        AgentWorkflowRun(
            user_id=1, trigger_type="planning_request", mode="shadow", status="success"
        ),
        AgentWorkflowRun(
            user_id=1, trigger_type="planning_request", mode="shadow", status="success"
        ),
    ]
    runs = [
        AgentRun(
            agent_type="critic",
            trigger_type="planning_request",
            status="succeeded",
            fallback_used=False,
            latency_ms=100,
        ),
        AgentRun(
            agent_type="critic",
            trigger_type="planning_request",
            status="succeeded",
            fallback_used=False,
            latency_ms=150,
        ),
    ]
    artifacts = [
        AgentArtifact(
            workflow_run_id=1,
            artifact_type="review_report",
            producer_agent="critic",
            payload_json='{"verdict":"approved"}',
            input_hash="a" * 64,
        ),
        AgentArtifact(
            workflow_run_id=2,
            artifact_type="review_report",
            producer_agent="critic",
            payload_json='{"verdict":"approved_with_warnings"}',
            input_hash="b" * 64,
        ),
    ]

    ready = build_critic_readiness_report(
        settings=settings,
        shadow_workflows=workflows,
        critic_runs=runs,
        critic_artifacts=artifacts,
    )
    assert ready["ready"] is True
    assert ready["recommendation"] == "ready_for_enforce"

    artifacts[1].payload_json = '{"verdict":"needs_clarification"}'
    not_ready = build_critic_readiness_report(
        settings=settings,
        shadow_workflows=workflows,
        critic_runs=runs,
        critic_artifacts=artifacts,
    )
    assert not_ready["ready"] is False
    assert not_ready["checks"]["blocking_rate"] is False


@pytest.mark.asyncio
async def test_enforced_critic_can_trigger_only_one_bounded_soft_retry():
    critic = RetryOnceCritic()
    result = await PlanningService(
        StableParser(),
        MockMapProvider(),
        Settings(
            mock_map_provider=True,
            plan_critic_mode="enforce",
            max_critic_retries=1,
            max_agent_handoffs=12,
        ),
        critic_agent=critic,
    ).plan(
        AIPlanRequest(
            text="去博物馆",
            origin=Coordinate(lng=120.62, lat=31.32),
        )
    )

    assert result.status == "success"
    assert critic.calls == 2
    assert result.intent.preferences.weights.uncertainty == 1.5
    assert result.agent_workflow and result.agent_workflow.retry_count == 1
    assert result.agent_workflow.messages
    assert all(
        message.delivery_status == "delivered" for message in result.agent_workflow.messages
    )
    assert result.agent_workflow.messages[0].content_summary["text"] == "[REDACTED_TEXT]"
    assert [step.agent_type.value for step in result.agent_workflow.steps] == [
        "supervisor",
        "intent",
        "supervisor",
        "search",
        "planner",
        "critic",
        "search",
        "planner",
        "critic",
        "supervisor",
    ]


@pytest.mark.asyncio
async def test_supervisor_recovers_poi_search_from_verified_cache():
    _POI_RECOVERY_CACHE.clear()
    provider = ToggleFailSearchProvider()
    service = PlanningService(
        MuseumParser(),
        provider,
        Settings(
            mock_map_provider=True,
            agent_search_max_attempts=2,
            agent_stage_timeout_seconds=1,
        ),
    )
    request = AIPlanRequest(
        text="visit museum",
        origin=Coordinate(lng=120.62, lat=31.32),
    )

    warm = await service.plan(request)
    provider.fail_search = True
    recovered = await service.plan(request)

    assert warm.status == "success"
    assert recovered.status == "success"
    assert recovered.stops[0].poi.source.startswith("cache:")
    recovery_steps = [
        step.output_artifact.payload["action"]
        for step in recovered.agent_workflow.steps
        if step.agent_type == AgentType.supervisor
        and step.output_artifact.payload.get("workflow_state") == "recovery_applied"
    ]
    assert recovery_steps == ["retry", "fallback_cached"]


@pytest.mark.asyncio
async def test_supervisor_degrades_to_clarification_when_search_cache_is_unavailable():
    _POI_RECOVERY_CACHE.clear()
    result = await PlanningService(
        MuseumParser(),
        AlwaysFailSearchProvider(),
        Settings(
            mock_map_provider=True,
            agent_search_max_attempts=2,
            agent_stage_timeout_seconds=1,
        ),
    ).plan(
        AIPlanRequest(
            text="visit museum",
            origin=Coordinate(lng=120.62, lat=31.32),
        )
    )

    assert result.status == "need_clarification"
    recovery_steps = [
        step.output_artifact.payload["action"]
        for step in result.agent_workflow.steps
        if step.agent_type == AgentType.supervisor
        and step.output_artifact.payload.get("workflow_state") == "recovery_applied"
    ]
    assert recovery_steps == ["retry", "fallback_unavailable"]


@pytest.mark.asyncio
async def test_elderly_trip_runtime_executes_safety_before_planner():
    result = await PlanningService(
        ElderlyParser(),
        MockMapProvider(),
        Settings(mock_map_provider=True),
    ).plan(
        AIPlanRequest(
            text="elderly trip",
            origin=Coordinate(lng=120.62, lat=31.32),
        )
    )

    assert result.status == "success"
    assert [step.agent_type.value for step in result.agent_workflow.steps] == [
        "supervisor",
        "intent",
        "supervisor",
        "search",
        "safety",
        "planner",
        "critic",
        "supervisor",
    ]
    safety_step = next(
        step for step in result.agent_workflow.steps if step.agent_type == AgentType.safety
    )
    assert safety_step.output_artifact.payload["verdict"] == "passed"
