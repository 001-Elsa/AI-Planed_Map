"""Intent Agent: typed requirement extraction and clarification only.

This role has no Provider/tool access and therefore cannot generate or select
POIs. Candidate recall remains a deterministic PlanningService responsibility.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.core.observability import metrics
from backend.app.schemas.agent_artifacts import AgentBudget, AgentSpec, AgentType, ArtifactEnvelope
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    ClarificationQuestion,
    PlanningIntent,
)
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, DataScope, InvocationMode
from backend.app.services.agents.base import AgentExecution, canonical_hash
from backend.app.services.clarification import select_clarification_questions
from backend.app.services.intent_parser import IntentParser

SHANGHAI = ZoneInfo("Asia/Shanghai")

INTENT_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.intent,
    prompt_version="intent-agent-v1",
    allowed_tools=frozenset(),
    allowed_internal_capabilities=TOOL_REGISTRY.names_for(
        AgentType.intent, InvocationMode.internal_stage
    ),
    input_artifact_types=frozenset({"planning_request"}),
    output_artifact_type="intent_artifact",
    budget=AgentBudget(
        max_steps=1, max_input_tokens=6_000, max_output_tokens=2_000, max_cost_usd=0.04
    ),
)


class IntentAgent:
    spec = INTENT_AGENT_SPEC

    def __init__(self, parser: IntentParser) -> None:
        self.parser = parser

    async def run(
        self, request: AIPlanRequest
    ) -> AgentExecution[tuple[PlanningIntent, list[ClarificationQuestion]]]:
        started = time.perf_counter()
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.intent,
            capability="parse_requirement",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.planning_request}),
        )
        intent = await self.parser.parse(request.text)
        if request.departure_time:
            intent.departure_time = request.departure_time
        if request.transport_mode:
            intent.transport_mode = request.transport_mode
        if request.constraints:
            intent.constraints = request.constraints

        for key, value in request.preferences_answers.items():
            if key == "dietary_restrictions":
                values = value if isinstance(value, list) else [value]
                intent.preferences.dietary_restrictions = [str(item) for item in values if item]
            elif key == "optimization_goal" and value in {
                "balanced",
                "shortest_time",
                "shortest_distance",
            }:
                intent.preferences.optimization_goal = value
            elif key in {
                "minimize_distance",
                "minimize_walking",
                "minimize_cost",
                "prefer_high_rating",
                "avoid_queues",
                "avoid_hiking",
            }:
                setattr(intent.preferences, key, bool(value))
            elif key == "travel_style" and value in {"balanced", "relaxed", "intensive"}:
                intent.preferences.travel_style = value
            elif key in {"preferred_categories", "preferred_environment"}:
                values = value if isinstance(value, list) else [value]
                setattr(intent.preferences, key, [str(item) for item in values if item])

        for raw_index, location in request.task_location_overrides.items():
            index = int(raw_index)
            if not 0 <= index < len(intent.tasks):
                raise ValueError(f"task location override index out of range: {index}")
            intent.tasks[index].location_name = location
            intent.tasks[index].location_hint = location
        for raw_index, field_overrides in request.task_field_overrides.items():
            index = int(raw_index)
            if not 0 <= index < len(intent.tasks):
                raise ValueError(f"task field override index out of range: {index}")
            if "appointment_time" in field_overrides:
                appointment = field_overrides["appointment_time"]
                if isinstance(appointment, str):
                    appointment = datetime.fromisoformat(appointment)
                if appointment.tzinfo is None:
                    appointment = appointment.replace(tzinfo=SHANGHAI)
                intent.tasks[index] = intent.tasks[index].model_copy(
                    update={
                        "appointment_time": appointment,
                        "earliest_arrival": appointment,
                        "deadline": appointment,
                    }
                )
        for task in intent.tasks:
            if task.service_duration_minutes == 0:
                task.service_duration_minutes = request.default_service_duration_minutes

        questions = select_clarification_questions(
            request=request, intent=intent, text=request.text, max_questions=3
        )
        if request.origin is None and not any(item.field == "origin" for item in questions):
            questions.insert(
                0,
                ClarificationQuestion(
                    field="origin",
                    reason="路线矩阵和候选地点召回必须有可信起点",
                    question="请提供出发位置，或允许使用当前定位。",
                ),
            )
        required_questions = [item for item in questions if item.required][:3]
        payload = {
            "intent": intent.model_dump(mode="json"),
            "questions": [item.model_dump(mode="json") for item in required_questions],
            "parser": getattr(self.parser, "name", type(self.parser).__name__),
        }
        input_payload = request.model_dump(mode="json")
        artifact = ArtifactEnvelope(
            artifact_type=self.spec.output_artifact_type,
            producer_agent=AgentType.intent,
            payload=payload,
            confidence=0.45 if getattr(self.parser, "fallback_used", False) else 0.9,
            evidence_refs=[],
            input_hash=canonical_hash(input_payload),
        )
        input_tokens = int(getattr(self.parser, "input_tokens", 0) or 0)
        output_tokens = int(getattr(self.parser, "output_tokens", 0) or 0)
        route = getattr(self.parser, "last_route", None)
        route_reason = (
            "model_route:"
            f"{route.tier.value}:score={route.complexity_score}:"
            f"{','.join(route.reason_codes)}"
            if route is not None
            else None
        )
        fallback_reason = getattr(self.parser, "fallback_reason", None)
        estimated_cost = 0.0
        if route is not None:
            estimated_cost = (
                input_tokens * route.estimated_input_cost_per_million_usd
                + output_tokens * route.estimated_output_cost_per_million_usd
            ) / 1_000_000
            metrics.observe(
                "mapgo_model_router_actual_cost_usd",
                estimated_cost,
                {"agent": "intent", "tier": route.tier.value},
            )
            metrics.observe(
                "mapgo_model_router_latency_ms",
                int((time.perf_counter() - started) * 1000),
                {"agent": "intent", "tier": route.tier.value},
            )
        return AgentExecution(
            spec=self.spec,
            output=(intent, required_questions),
            artifact=artifact,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost,
            fallback_used=bool(getattr(self.parser, "fallback_used", False)),
            reason=(
                f"{route_reason};fallback={fallback_reason}"
                if route_reason and fallback_reason
                else fallback_reason or route_reason
            ),
        )
