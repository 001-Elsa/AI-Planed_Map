"""Safety Check Agent: deterministic review for accessibility and walking risk."""

from __future__ import annotations

import time

from backend.app.schemas.agent_artifacts import (
    AgentBudget,
    AgentSpec,
    AgentType,
    ArtifactEnvelope,
    ReviewFinding,
    SafetyCheckReport,
)
from backend.app.schemas.ai_intent import PlanningIntent, PoiCandidate
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, DataScope, InvocationMode
from backend.app.services.agents.base import AgentExecution, canonical_hash

SAFETY_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.safety,
    prompt_version="safety-check-v1",
    allowed_tools=frozenset(),
    allowed_internal_capabilities=TOOL_REGISTRY.names_for(
        AgentType.safety, InvocationMode.internal_stage
    ),
    input_artifact_types=frozenset({"search_artifact"}),
    output_artifact_type="safety_report",
    budget=AgentBudget(
        max_steps=1, max_input_tokens=2_000, max_output_tokens=800, max_cost_usd=0
    ),
)


class SafetyAgent:
    spec = SAFETY_AGENT_SPEC

    async def run(
        self, *, intent: PlanningIntent, candidates: list[list[PoiCandidate]]
    ) -> AgentExecution[SafetyCheckReport]:
        started = time.perf_counter()
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.safety,
            capability="check_travel_safety",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.safety_review}),
        )
        findings: list[ReviewFinding] = []
        hard = intent.constraints.hard
        party = hard.party
        safety_sensitive = (
            party.elderly > 0
            or party.wheelchair_users > 0
            or hard.wheelchair_accessible
            or intent.preferences.minimize_walking
            or intent.preferences.travel_style == "relaxed"
            or hard.max_walking_meters is not None
        )
        if safety_sensitive and hard.max_walking_meters is None:
            findings.append(
                ReviewFinding(
                    code="walking_budget_missing",
                    severity="warning",
                    message="行程对步行敏感，但尚未设置明确步行上限；Planner 将按软偏好控制距离。",
                    evidence_refs=["intent.constraints.hard.max_walking_meters"],
                )
            )
        if party.wheelchair_users > 0 or hard.wheelchair_accessible:
            unknown_groups = [
                index
                for index, group in enumerate(candidates)
                if group and not any(item.wheelchair_accessible is True for item in group)
            ]
            if unknown_groups:
                findings.append(
                    ReviewFinding(
                        code="accessibility_unverified",
                        severity="warning",
                        message="部分候选地点缺少明确无障碍证据，Planner 会继续执行硬约束筛选。",
                        evidence_refs=[f"candidate_group:{index}" for index in unknown_groups[:5]],
                    )
                )
        verdict = "passed_with_warnings" if findings else "passed"
        report = SafetyCheckReport(
            verdict=verdict,
            summary=(
                "已执行老人/无障碍/少步行安全检查。"
                if safety_sensitive
                else "该请求未触发额外安全检查。"
            ),
            findings=findings,
            confidence=0.8 if findings else 0.95,
        )
        artifact = ArtifactEnvelope(
            artifact_type=self.spec.output_artifact_type,
            producer_agent=AgentType.safety,
            payload=report.model_dump(mode="json"),
            confidence=report.confidence,
            evidence_refs=["intent:user_requirement", "search:poi_candidates"],
            input_hash=canonical_hash(
                {
                    "intent": intent.model_dump(mode="json"),
                    "candidates": [
                        [item.model_dump(mode="json") for item in group]
                        for group in candidates
                    ],
                }
            ),
        )
        return AgentExecution(
            spec=self.spec,
            output=report,
            artifact=artifact,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
