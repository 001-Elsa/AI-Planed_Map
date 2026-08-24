"""Supervisor Agent: deterministic workflow scheduling and recovery bookkeeping."""

from __future__ import annotations

import time
from typing import Any

from backend.app.schemas.agent_artifacts import (
    AgentBudget,
    AgentExecutionPlan,
    AgentPlanStep,
    AgentRecoveryDecision,
    AgentSpec,
    AgentType,
    AgentWorkflowMode,
    ArtifactEnvelope,
)
from backend.app.schemas.ai_intent import AIPlanRequest, PlanningIntent
from backend.app.services.agents.base import AgentExecution, canonical_hash

SUPERVISOR_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.supervisor,
    prompt_version="supervisor-agent-v1",
    allowed_tools=frozenset(),
    input_artifact_types=frozenset({"planning_request", "workflow_state", "recovery_event"}),
    output_artifact_type="workflow_control",
    budget=AgentBudget(max_steps=1, max_input_tokens=1_000, max_output_tokens=800, max_cost_usd=0),
)


class SupervisorAgent:
    spec = SUPERVISOR_AGENT_SPEC

    async def start(self, request: AIPlanRequest) -> AgentExecution[dict[str, Any]]:
        started = time.perf_counter()
        tasks = [
            {
                "agent_type": "intent",
                "status": "pending",
                "responsibility": "parse_intent_before_dynamic_plan",
            },
        ]
        payload = {
            "workflow_state": "intent_scheduled",
            "task_count": len(request.text.strip().split()) if request.text else 0,
            "tasks": tasks,
            "dispatch_policy": "supervisor_dynamic_plan_after_intent",
            "recovery_policy": {
                "intent": {
                    "retry": False,
                    "timeout": "parser_budget",
                    "fallback": "rule_based_parser_or_clarification",
                },
                "search": {
                    "retry": "bounded_sequential_retry",
                    "timeout": "stage_budget",
                    "fallback": "provider_verified_cache_then_clarification",
                },
                "planner": {
                    "retry": False,
                    "timeout": "stage_budget",
                    "fallback": "estimated_route_edges_or_infeasible_conflicts",
                },
                "critic": {
                    "retry": "at_most_one_soft_replan",
                    "timeout": "critic_budget",
                    "fallback": "rule_based_review_or_enforce_clarification",
                },
            },
        }
        return self._execution(
            payload, "planning_request", request.model_dump(mode="json"), started
        )

    async def plan(
        self, intent: PlanningIntent, *, mode: AgentWorkflowMode
    ) -> AgentExecution[dict[str, Any]]:
        started = time.perf_counter()
        execution_plan = self._build_execution_plan(intent, mode=mode)
        return self._execution(
            execution_plan.model_dump(mode="json"),
            "intent_artifact",
            intent.model_dump(mode="json"),
            started,
        )

    async def finalize(self, result: dict[str, Any]) -> AgentExecution[dict[str, Any]]:
        started = time.perf_counter()
        payload = {
            "workflow_state": "final_answer_ready",
            "status": result.get("status"),
            "planning_state": result.get("planning_state"),
            "stop_count": len(result.get("stops") or []),
            "warning_count": len(result.get("warnings") or []),
            "has_critic_review": bool(result.get("critic_review")),
        }
        return self._execution(payload, "plan_candidate", result, started)

    async def recover(
        self,
        *,
        stage: str,
        error_type: str,
        message: str,
        attempt: int = 1,
        max_attempts: int = 1,
        timeout_seconds: float | None = None,
        fallback_available: bool = False,
        fallback_source: str | None = None,
    ) -> AgentExecution[dict[str, Any]]:
        started = time.perf_counter()
        if stage in {"search", "poi_search"} and attempt < max_attempts:
            action = "retry"
            reason = "transient search failure; bounded retry remains"
        elif stage in {"search", "poi_search"} and fallback_available:
            action = "fallback_cached"
            reason = "retry budget exhausted; using provider-verified cached POIs"
        elif stage in {"search", "poi_search"}:
            action = "fallback_unavailable"
            reason = "retry budget exhausted and no verified cache is available"
        elif fallback_available:
            action = "fallback_cached"
            reason = "using an available deterministic fallback"
        else:
            action = "clarify"
            reason = "no safe automatic recovery is available"
        decision = AgentRecoveryDecision(
            stage=stage,
            action=action,
            attempt=attempt,
            max_attempts=max_attempts,
            error_type=error_type,
            reason=reason,
            timeout_seconds=timeout_seconds,
            fallback_source=fallback_source if fallback_available else None,
        )
        payload = {
            "workflow_state": "recovery_applied",
            **decision.model_dump(mode="json"),
            "message": message[:300],
        }
        return self._execution(payload, "recovery_event", payload, started)

    def _execution(
        self,
        payload: dict[str, Any],
        input_artifact_type: str,
        input_payload: object,
        started: float,
    ) -> AgentExecution[dict[str, Any]]:
        artifact = ArtifactEnvelope(
            artifact_type=self.spec.output_artifact_type,
            producer_agent=AgentType.supervisor,
            payload=payload,
            confidence=1,
            evidence_refs=[f"stage:{input_artifact_type}"],
            input_hash=canonical_hash(input_payload),
        )
        return AgentExecution(
            spec=self.spec,
            output=payload,
            artifact=artifact,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _build_execution_plan(
        intent: PlanningIntent, *, mode: AgentWorkflowMode
    ) -> AgentExecutionPlan:
        hard = intent.constraints.hard
        party = hard.party
        safety_reasons: list[str] = []
        if party.elderly > 0:
            safety_reasons.append("elderly_party")
        if party.wheelchair_users > 0 or hard.wheelchair_accessible:
            safety_reasons.append("accessibility_required")
        if intent.preferences.minimize_walking or hard.max_walking_meters is not None:
            safety_reasons.append("walking_sensitive")
        if intent.preferences.travel_style == "relaxed":
            safety_reasons.append("relaxed_travel_style")

        steps = [
            AgentPlanStep(
                step_id="intent",
                agent_type=AgentType.intent,
                responsibility="parse_intent_and_constraints",
                input_artifact_type="planning_request",
                output_artifact_type="intent_artifact",
                trigger_reason="always_required",
            ),
            AgentPlanStep(
                step_id="search",
                agent_type=AgentType.search,
                responsibility="recall_provider_poi_candidates",
                input_artifact_type="intent_artifact",
                output_artifact_type="search_artifact",
                trigger_reason="verified_poi_required",
            ),
        ]
        if safety_reasons:
            steps.append(
                AgentPlanStep(
                    step_id="safety_check",
                    agent_type=AgentType.safety,
                    responsibility="check_party_accessibility_and_walking_risk",
                    input_artifact_type="search_artifact",
                    output_artifact_type="safety_report",
                    trigger_reason=",".join(safety_reasons),
                )
            )
        steps.append(
            AgentPlanStep(
                step_id="planner",
                agent_type=AgentType.planner,
                responsibility="solve_route_with_hard_constraints",
                input_artifact_type="safety_report" if safety_reasons else "search_artifact",
                output_artifact_type="plan_candidate",
                trigger_reason="candidate_route_required",
            )
        )
        skipped_optional_steps = []
        if mode != AgentWorkflowMode.off:
            steps.append(
                AgentPlanStep(
                    step_id="critic",
                    agent_type=AgentType.critic,
                    responsibility="review_plan_evidence_and_preferences",
                    input_artifact_type="plan_candidate",
                    output_artifact_type="review_report",
                    trigger_reason=f"critic_mode:{mode.value}",
                )
            )
        else:
            skipped_optional_steps.append("critic")
        steps.append(
            AgentPlanStep(
                step_id="final_answer",
                agent_type=AgentType.supervisor,
                responsibility="assemble_final_answer",
                input_artifact_type="plan_candidate",
                output_artifact_type="final_answer",
                trigger_reason="always_required",
            )
        )
        if not safety_reasons:
            skipped_optional_steps.append("safety_check")
        return AgentExecutionPlan(
            plan_kind="safety_sensitive_trip" if safety_reasons else "standard_trip",
            steps=steps,
            rationale=safety_reasons or ["standard_trip_without_extra_safety_gate"],
            skipped_optional_steps=skipped_optional_steps,
        )
