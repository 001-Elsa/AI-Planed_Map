"""Role-scoped context views for isolated Agent runs.

Shared State remains the source of truth. This module turns an authorized
state view plus explicit hand-off artifacts into the minimum typed input an
Agent needs; it never reconstructs a conversation transcript.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import Field

from backend.app.models import TripSession
from backend.app.schemas.agent_artifacts import (
    AgentMessage,
    AgentType,
    SafetyCheckReport,
    minimize_agent_payload,
)
from backend.app.schemas.agent_state import AgentSharedStateView
from backend.app.schemas.ai_intent import (
    Coordinate,
    HardConstraints,
    PlanningIntent,
    PlanningPreferences,
    PoiCandidate,
)
from backend.app.schemas.common import StrictModel
from backend.app.services.agent_protocol import AgentProtocolError
from backend.app.services.agents.base import canonical_hash
from backend.app.services.agents.search_agent import SearchArtifact

_INJECTION_PATTERN = re.compile(
    r"(?i)(ignore\s+(all\s+)?previous|system\s*prompt|developer\s*message|"
    r"call\s+(a\s+)?tool|execute\s+(this\s+)?command|忽略.{0,12}(指令|提示)|"
    r"系统提示|调用.{0,8}工具|执行.{0,8}命令)"
)


class ContextStateRef(StrictModel):
    task_id: str
    revision: int = Field(ge=0)
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContextSecurity(StrictModel):
    conversation_history_included: bool = False
    trusted_sources: list[str] = Field(
        default_factory=lambda: ["shared_state", "validated_artifact", "tool_registry"]
    )
    untrusted_data_fields: list[str] = Field(default_factory=list)
    suspicious_text_redacted: bool = False


class ToolExecutionSummary(StrictModel):
    tool_name: str = Field(min_length=1, max_length=80)
    success: bool
    error_code: str | None = Field(default=None, max_length=80)
    source: str | None = Field(default=None, max_length=120)
    confidence: float | None = Field(default=None, ge=0, le=1)
    artifact_ref: str | None = Field(default=None, max_length=300)


class SearchArtifactView(StrictModel):
    """Planner projection; retry internals and recovery reasons stay private."""

    candidate_groups: list[list[PoiCandidate]]
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)
    provider_name: str = Field(max_length=120)
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_execution_summary: list[ToolExecutionSummary] = Field(default_factory=list)


class PlanningContext(StrictModel):
    intent_artifact: PlanningIntent
    search_artifact: SearchArtifactView
    safety_artifact: SafetyCheckReport | None = None
    user_hard_constraints: HardConstraints
    user_soft_preferences: PlanningPreferences
    origin: Coordinate
    city: str | None = Field(default=None, max_length=50)
    max_candidates_per_task: int = Field(default=3, ge=1, le=5)
    current_state: ContextStateRef
    security: ContextSecurity

    @property
    def intent(self) -> PlanningIntent:
        return self.intent_artifact

    @property
    def search(self) -> SearchArtifactView:
        return self.search_artifact


class CriticContext(StrictModel):
    original_requirement: PlanningIntent
    plan_artifact: dict[str, Any]
    constraint_evidence: dict[str, Any]
    tool_execution_summary: list[ToolExecutionSummary] = Field(default_factory=list)
    current_state: ContextStateRef
    security: ContextSecurity


def _validate_view(
    *, view: AgentSharedStateView, message: AgentMessage, actor: AgentType, required: set[str]
) -> ContextStateRef:
    if message.task_id != view.task_id or message.content.get("shared_state_ref") != view.task_id:
        raise AgentProtocolError(f"{actor.value} context references a different task")
    if view.revision != int(message.content.get("state_revision", -1)):
        raise AgentProtocolError(f"{actor.value} context references a stale shared-state revision")
    if view.state_hash != message.content.get("state_hash"):
        raise AgentProtocolError(f"{actor.value} context references a different state hash")
    missing = required - set(view.visible_fields)
    if missing:
        raise AgentProtocolError(
            f"{actor.value} context view is missing authorized fields {sorted(missing)}"
        )
    return ContextStateRef(task_id=view.task_id, revision=view.revision, state_hash=view.state_hash)


def _tool_summaries(search: SearchArtifact) -> list[ToolExecutionSummary]:
    summaries: list[ToolExecutionSummary] = []
    for item in search.tool_results:
        payload = item.model_dump(mode="json")
        summaries.append(
            ToolExecutionSummary(
                tool_name=str(payload.get("tool_name") or "search_poi"),
                success=item.success,
                error_code=item.error_code,
                source=payload.get("source"),
                confidence=payload.get("confidence"),
                artifact_ref=item.artifact_ref,
            )
        )
    return summaries


def build_planning_context(
    *,
    view: AgentSharedStateView,
    message: AgentMessage,
    search: SearchArtifact,
    origin: Coordinate,
    city: str | None,
    max_candidates_per_task: int,
    fallback_intent: PlanningIntent,
) -> PlanningContext:
    state_ref = _validate_view(
        view=view,
        message=message,
        actor=AgentType.planner,
        required={"user_requirement", "poi_candidates", "execution_context"},
    )
    if view.user_requirement is None:
        raise AgentProtocolError("planner context is missing structured intent")
    intent = view.user_requirement.model_copy(deep=True)
    if view.soft_adjustments is not None:
        for key, value in view.soft_adjustments.updates().items():
            setattr(intent.preferences.weights, key, value)
    if canonical_hash(intent.model_dump(mode="json")) != canonical_hash(
        fallback_intent.model_dump(mode="json")
    ):
        raise AgentProtocolError("planner input intent does not match workflow state")
    groups = view.poi_candidates or []
    state_candidates = [[item.model_dump(mode="json") for item in group] for group in groups]
    artifact_candidates = [
        [item.model_dump(mode="json") for item in group] for group in search.candidate_groups
    ]
    if canonical_hash(state_candidates) != canonical_hash(artifact_candidates):
        raise AgentProtocolError("planner candidates do not match search output")
    expected_search_hash = message.content.get("search_artifact_hash") or message.content.get(
        "artifact_hash"
    )
    actual_search_hash = canonical_hash(search.model_dump(mode="json"))
    if expected_search_hash != actual_search_hash:
        raise AgentProtocolError("planner search artifact hash does not match hand-off")
    safety_payload = (view.execution_context or {}).get("safety_check")
    safety = SafetyCheckReport.model_validate(safety_payload) if safety_payload else None
    search_view = SearchArtifactView(
        candidate_groups=groups,
        clarification_questions=[
            item.model_dump(mode="json") for item in search.clarification_questions
        ],
        provider_name=search.provider_name,
        artifact_hash=actual_search_hash,
        tool_execution_summary=_tool_summaries(search),
    )
    return PlanningContext(
        intent_artifact=intent,
        search_artifact=search_view,
        safety_artifact=safety,
        user_hard_constraints=intent.constraints.hard,
        user_soft_preferences=intent.preferences,
        origin=origin,
        city=city,
        max_candidates_per_task=max_candidates_per_task,
        current_state=state_ref,
        security=ContextSecurity(
            untrusted_data_fields=[
                "search_artifact.candidate_groups.*.name",
                "search_artifact.candidate_groups.*.address",
            ]
        ),
    )


def build_critic_context(
    *, view: AgentSharedStateView, message: AgentMessage, plan: dict[str, Any]
) -> CriticContext:
    state_ref = _validate_view(
        view=view,
        message=message,
        actor=AgentType.critic,
        required={"user_requirement", "route_plan"},
    )
    if view.user_requirement is None:
        raise AgentProtocolError("critic context is missing structured requirement")
    if view.route_plan is None or canonical_hash(view.route_plan) != canonical_hash(plan):
        raise AgentProtocolError("critic shared state does not match planner output")
    if message.content.get("plan_hash") != canonical_hash(plan):
        raise AgentProtocolError("critic plan artifact hash does not match hand-off")
    evidence = {
        "hard_constraints": view.user_requirement.constraints.hard.model_dump(mode="json"),
        "uncertain_constraints": [
            item.model_dump(mode="json") for item in view.user_requirement.constraints.uncertain
        ],
        "plan_status": plan.get("status"),
        "stop_count": len(plan.get("stops") or []),
        "plan_hash": canonical_hash(plan),
    }
    tool_summary: list[ToolExecutionSummary] = []
    if plan.get("algorithm"):
        tool_summary.append(
            ToolExecutionSummary(
                tool_name="optimize_route",
                success=plan.get("status") in {"success", "infeasible"},
                error_code=None
                if plan.get("status") in {"success", "infeasible"}
                else "NO_SOLUTION",
                source=str(plan.get("algorithm"))[:120],
                confidence=plan.get("confidence"),
            )
        )
    route_sources = {
        str((stop.get("travel") or {}).get("source"))
        for stop in plan.get("stops") or []
        if isinstance(stop, dict) and (stop.get("travel") or {}).get("source")
    }
    for source in sorted(route_sources)[:5]:
        tool_summary.append(
            ToolExecutionSummary(
                tool_name="route_evidence",
                success=True,
                source=source[:120],
            )
        )
    return CriticContext(
        original_requirement=view.user_requirement,
        plan_artifact=plan,
        constraint_evidence=evidence,
        tool_execution_summary=tool_summary,
        current_state=state_ref,
        security=ContextSecurity(
            untrusted_data_fields=[
                "plan_artifact.stops.*.poi.name",
                "plan_artifact.stops.*.poi.address",
            ]
        ),
    )


def critic_model_payload(context: CriticContext, *, max_chars: int = 16_000) -> str:
    """Serialize a bounded Critic view and neutralize instruction-like provider text."""
    redacted = False

    def scrub(value: Any) -> Any:
        nonlocal redacted
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str) and _INJECTION_PATTERN.search(value):
            redacted = True
            return "[UNTRUSTED_INSTRUCTION_LIKE_TEXT_REDACTED]"
        return value

    payload = scrub(context.model_dump(mode="json"))
    payload["security"]["suspicious_text_redacted"] = redacted
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > max_chars:
        payload["plan_artifact"].pop("candidate_reviews", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > max_chars:
        raise AgentProtocolError("critic context exceeds its bounded context budget")
    return encoded


def build_companion_context(
    *,
    trip: TripSession,
    observation: dict[str, Any],
    route_plan: dict[str, Any] | None,
    tool_history: list[dict[str, Any]],
    max_chars: int = 6000,
) -> dict[str, Any]:
    plan = route_plan or {}
    stops = plan.get("stops") or []
    memory = plan.get("memory") or {}
    context = {
        "trip": {
            "trip_id": trip.id,
            "state": trip.state,
            "plan_version": trip.current_plan_version,
        },
        "current_observation": minimize_agent_payload(observation),
        "formal_plan_snapshot": {
            "planning_run_id": trip.planning_run_id,
            "plan_version": plan.get("plan_version") or trip.current_plan_version,
            "status": plan.get("status"),
            "transport_mode": (plan.get("intent") or {}).get("transport_mode"),
            "stop_count": len(stops),
            "stop_refs": [
                {
                    "index": index,
                    "poi_id": (stop.get("poi") or {}).get("id"),
                    "name": (stop.get("poi") or {}).get("name"),
                    "arrival_time": stop.get("arrival_time"),
                    "departure_time": stop.get("departure_time"),
                }
                for index, stop in enumerate(stops[:12])
                if isinstance(stop, dict)
            ],
            "warnings": (plan.get("warnings") or [])[:5],
        },
        "confirmed_memory": {
            "applied_keys": memory.get("applied_keys") or [],
            "values_included": False,
        },
        "recent_tool_results": [minimize_agent_payload(item) for item in tool_history[-5:]],
        "security": ContextSecurity().model_dump(mode="json"),
    }
    if len(str(context)) <= max_chars:
        return context
    context["formal_plan_snapshot"]["stop_refs"] = context["formal_plan_snapshot"]["stop_refs"][:5]
    context["recent_tool_results"] = context["recent_tool_results"][-2:]
    context["truncated"] = True
    return context
