"""Replanner Agent: select a bounded recovery strategy without solving routes."""

from __future__ import annotations

import time

from backend.app.schemas.agent_artifacts import (
    AgentBudget,
    AgentSpec,
    AgentType,
    ArtifactEnvelope,
)
from backend.app.schemas.dynamic_replanning import ReplanDirective, TripEventArtifact
from backend.app.services.agents.base import AgentExecution, canonical_hash

REPLANNER_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.replanner,
    prompt_version="replanner-agent-v1",
    context_view="event_and_current_plan_minimal",
    allowed_tools=frozenset(),
    allowed_internal_capabilities=frozenset(),
    input_artifact_types=frozenset({"trip_event_artifact"}),
    output_artifact_type="replan_directive",
    budget=AgentBudget(
        max_steps=1,
        max_input_tokens=1_000,
        max_output_tokens=400,
        max_cost_usd=0,
        timeout_seconds=5,
    ),
)


class ReplannerAgent:
    """Deterministically maps an event to strategy; Planner owns all solving."""

    spec = REPLANNER_AGENT_SPEC

    async def run(
        self,
        event: TripEventArtifact,
        *,
        current_location,
        completed_stop_ids: list[str],
        event_payload: dict,
        weather: dict | None,
    ) -> AgentExecution[ReplanDirective]:
        started = time.perf_counter()
        strategy = {
            "PoiStatusChanged": "replace_closed_poi",
            "WeatherAlertReceived": "weather_indoor_fallback",
            "TrafficChanged": "fastest_feasible_route",
            "ScheduleDelayDetected": "fastest_feasible_route",
            "DeadlineRiskDetected": "fastest_feasible_route",
            "UserOffRoute": "off_route_recovery",
        }.get(event.event_type, "reorder_remaining")
        directive = ReplanDirective(
            base_plan_version=event.base_plan_version,
            current_location=current_location,
            current_time=event.occurred_at,
            completed_stop_ids=completed_stop_ids,
            event_type=event.event_type,
            reason=event.reason,
            event_payload=event_payload,
            weather=weather,
            strategy=strategy,
        )
        artifact = ArtifactEnvelope(
            artifact_type=self.spec.output_artifact_type,
            producer_agent=AgentType.replanner,
            payload={
                "base_plan_version": directive.base_plan_version,
                "event_type": directive.event_type,
                "strategy": directive.strategy,
                "completed_stop_count": len(directive.completed_stop_ids),
            },
            confidence=1,
            evidence_refs=[f"trip_event:{event.event_id or 'direct'}"],
            input_hash=canonical_hash(event.model_dump(mode="json")),
        )
        return AgentExecution(
            spec=self.spec,
            output=directive,
            artifact=artifact,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
