"""Event-driven Companion -> Supervisor -> Replanner -> Planner -> Critic loop."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.clients.amap_client import MapProvider
from backend.app.models import PlanPatch, PlanVersion, TripSession
from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessageType,
    AgentStepTrace,
    AgentWorkflowMode,
    AgentWorkflowTrace,
    ArtifactEnvelope,
)
from backend.app.schemas.ai_intent import PlanPatchOperation
from backend.app.schemas.dynamic_replanning import (
    DynamicPatchReview,
    PlanPatchArtifact,
    TripEventArtifact,
)
from backend.app.services.agent_orchestrator import persist_agent_workflow
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agents.base import AgentExecution, canonical_hash
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC
from backend.app.services.agents.critic_agent import CRITIC_AGENT_SPEC
from backend.app.services.agents.planner_agent import PLANNER_AGENT_SPEC
from backend.app.services.agents.replanner_agent import ReplannerAgent
from backend.app.services.agents.supervisor_agent import SUPERVISOR_AGENT_SPEC
from backend.app.services.plan_versioning import apply_plan_patch_cas
from backend.app.services.replanning import PendingReplanRequest, create_pending_replan


def review_dynamic_patch(
    *,
    base_snapshot: dict[str, Any],
    patch: PlanPatchArtifact,
) -> DynamicPatchReview:
    """Fail closed on hard-constraint or evidence gaps and classify mutation risk."""
    findings: list[str] = []
    blocking = False
    risk_rank = 0
    required_by_id = {
        str((stop.get("poi") or {}).get("id")): bool((stop.get("task") or {}).get("required", True))
        for stop in base_snapshot.get("stops") or []
    }
    completed = {
        str(item) for item in ((patch.impact.get("changes") or {}).get("completed_stop_ids") or [])
    }
    conflicts = list((patch.impact.get("after") or {}).get("constraint_conflicts") or [])
    if conflicts:
        findings.append("deterministic_constraint_conflict")
        blocking = True
    before = patch.impact.get("before") or {}
    if before and patch.base_version != int(before.get("plan_version", -1)):
        findings.append("base_version_evidence_mismatch")
        blocking = True

    for operation in patch.operations:
        if operation.operation == "remove_stop":
            risk_rank = max(risk_rank, 2)
            if (
                required_by_id.get(str(operation.stop_id), True)
                and str(operation.stop_id) not in completed
            ):
                findings.append("required_stop_removal_forbidden")
                blocking = True
        elif operation.operation == "replace_stop":
            risk_rank = max(risk_rank, 2)
            poi = (operation.replacement_stop or {}).get("poi") or {}
            if not poi.get("id") or not poi.get("source"):
                findings.append("replacement_poi_evidence_missing")
                blocking = True
        elif operation.operation == "change_transport_mode":
            risk_rank = max(risk_rank, 2)
            findings.append("transport_mode_changed")
        elif operation.operation == "change_departure_time":
            risk_rank = max(risk_rank, 1)

    before_cost = float((patch.impact.get("before") or {}).get("estimated_cost_yuan") or 0)
    after_cost = float((patch.impact.get("after") or {}).get("estimated_cost_yuan") or 0)
    if after_cost > before_cost + max(20, before_cost * 0.1):
        findings.append("material_cost_increase")
        risk_rank = max(risk_rank, 2)
    risk = ("low", "medium", "high", "critical")[3 if blocking else risk_rank]
    if not findings:
        findings.append("deterministic_patch_checks_passed")
    return DynamicPatchReview(
        verdict="blocked" if blocking else ("approved_with_warnings" if risk_rank else "approved"),
        risk_level=risk,
        requires_confirmation=risk_rank >= 2 or blocking,
        findings=findings,
        checked_base_version=patch.base_version,
        operation_count=len(patch.operations),
        confidence=1,
    )


class DynamicReplanningOrchestrator:
    """Coordinates the dynamic workflow; no role may overwrite PlanVersion."""

    def __init__(self, db: AsyncSession, provider: MapProvider) -> None:
        self.db = db
        self.provider = provider
        self.router = AgentMessageRouter()
        self.replanner = ReplannerAgent()

    def _message(self, trace, *, sender, receiver, kind, artifact_type, content, prior=None):
        message = self.router.build(
            task_id=trace.task_id,
            sender=sender,
            receiver=receiver,
            message_type=kind,
            artifact_type=artifact_type,
            content=content,
            correlation_id=prior.correlation_id if prior else None,
            causation_id=prior.message_id if prior else None,
        )
        delivered, status = self.router.deliver(message)
        trace.messages.append(self.router.audit(delivered, status))
        return delivered

    @staticmethod
    def _execution(spec, artifact_type, payload, input_payload, *, confidence=1):
        artifact = ArtifactEnvelope(
            artifact_type=artifact_type,
            producer_agent=spec.agent_type,
            payload=payload,
            confidence=confidence,
            evidence_refs=[],
            input_hash=canonical_hash(input_payload),
        )
        return AgentExecution(spec=spec, output=payload, artifact=artifact, latency_ms=0)

    @staticmethod
    def _step(trace, execution, input_type, inbound=None, outbound=None):
        trace.steps.append(
            AgentStepTrace(
                agent_type=execution.spec.agent_type,
                status="succeeded",
                prompt_version=execution.spec.prompt_version,
                budget=execution.spec.budget,
                input_artifact_type=input_type,
                output_artifact=execution.artifact,
                latency_ms=execution.latency_ms,
                input_message_id=inbound.message_id if inbound else None,
                output_message_id=outbound.message_id if outbound else None,
            )
        )
        trace.handoff_count += int(outbound is not None)

    async def run(
        self,
        *,
        trip: TripSession,
        event: TripEventArtifact,
        current_location,
        completed_stop_ids: list[str],
        event_payload: dict[str, Any],
        weather: dict[str, Any] | None,
        trace_id: str | None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        trace = AgentWorkflowTrace(
            mode=AgentWorkflowMode.enforce,
            task_id=f"trip-{trip.id}-event-{event.event_id or uuid4()}",
        )
        event_payload_typed = event.model_dump(mode="json")
        companion_execution = self._execution(
            COMPANION_AGENT_SPEC,
            "trip_event_artifact",
            event_payload_typed,
            event_payload_typed,
        )
        m1 = self._message(
            trace,
            sender=AgentEndpoint.companion,
            receiver=AgentEndpoint.supervisor,
            kind=AgentMessageType.artifact,
            artifact_type="trip_event_artifact",
            content=event_payload_typed,
        )
        self._step(trace, companion_execution, "trip_observation", outbound=m1)

        supervisor_execution = self._execution(
            SUPERVISOR_AGENT_SPEC,
            "workflow_control",
            {
                "workflow_state": "dynamic_replan_scheduled",
                "base_plan_version": event.base_plan_version,
                "tasks": ["replanner", "planner", "critic", "hitl_or_cas"],
            },
            event_payload_typed,
        )
        m2 = self._message(
            trace,
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.replanner,
            kind=AgentMessageType.command,
            artifact_type="trip_event_artifact",
            content=event_payload_typed,
            prior=m1,
        )
        self._step(trace, supervisor_execution, "trip_event_artifact", m1, m2)

        replanner_execution = await self.replanner.run(
            event,
            current_location=current_location,
            completed_stop_ids=completed_stop_ids,
            event_payload=event_payload,
            weather=weather,
        )
        directive = replanner_execution.output
        m3 = self._message(
            trace,
            sender=AgentEndpoint.replanner,
            receiver=AgentEndpoint.planner,
            kind=AgentMessageType.artifact,
            artifact_type="replan_directive",
            content=directive.model_dump(mode="json"),
            prior=m2,
        )
        self._step(trace, replanner_execution, "trip_event_artifact", m2, m3)

        if directive.base_plan_version != trip.current_plan_version:
            raise ValueError("dynamic replan directive references a stale plan version")
        result = await create_pending_replan(
            db=self.db,
            trip=trip,
            provider=self.provider,
            request=PendingReplanRequest(
                current_location=directive.current_location,
                current_time=directive.current_time,
                completed_stop_ids=directive.completed_stop_ids,
                reason=directive.reason,
                source_event_id=event.event_id,
                event_type=directive.event_type,
                event_payload=directive.event_payload,
                weather=directive.weather,
            ),
            trace_id=trace_id,
        )
        operations = [
            PlanPatchOperation.model_validate(item) for item in result.get("operations", [])
        ]
        patch_artifact = PlanPatchArtifact(
            patch_id=result.get("patch_id"),
            base_version=directive.base_plan_version,
            operations=operations,
            impact=result.get("impact") or {},
            status=str(result.get("status") or "unknown"),
        )
        planner_execution = self._execution(
            PLANNER_AGENT_SPEC,
            "plan_patch_candidate",
            patch_artifact.model_dump(mode="json"),
            directive.model_dump(mode="json"),
            confidence=float((result.get("impact") or {}).get("after", {}).get("feasible", 1)),
        )
        m4 = self._message(
            trace,
            sender=AgentEndpoint.planner,
            receiver=AgentEndpoint.critic,
            kind=AgentMessageType.artifact,
            artifact_type="plan_patch_candidate",
            content=patch_artifact.model_dump(mode="json"),
            prior=m3,
        )
        self._step(trace, planner_execution, "replan_directive", m3, m4)

        # The current formal version remains the only Critic baseline.
        version = await self.db.scalar(
            select(PlanVersion).where(
                PlanVersion.planning_run_id == trip.planning_run_id,
                PlanVersion.version == directive.base_plan_version,
            )
        )
        if version is None:
            raise ValueError("formal plan version disappeared during dynamic review")
        review = review_dynamic_patch(
            base_snapshot=json.loads(version.snapshot_json), patch=patch_artifact
        )
        critic_execution = self._execution(
            CRITIC_AGENT_SPEC,
            "dynamic_patch_review",
            review.model_dump(mode="json"),
            patch_artifact.model_dump(mode="json"),
        )
        m5 = self._message(
            trace,
            sender=AgentEndpoint.critic,
            receiver=AgentEndpoint.supervisor,
            kind=AgentMessageType.result,
            artifact_type="dynamic_patch_review",
            content=review.model_dump(mode="json"),
            prior=m4,
        )
        self._step(trace, critic_execution, "plan_patch_candidate", m4, m5)

        patch_artifact.review = review
        trip_context = json.loads(trip.context_json or "{}")
        auto_apply_opt_in = bool(trip_context.get("auto_apply_low_risk_patches"))
        auto_apply_eligible = bool(review.risk_level == "low" and review.verdict == "approved")
        requires_confirmation = bool(
            patch_artifact.patch_id is not None
            and review.verdict != "blocked"
            and not (auto_apply_eligible and auto_apply_opt_in)
        )
        if patch_artifact.patch_id is not None:
            patch_record = await self.db.get(PlanPatch, patch_artifact.patch_id)
            if patch_record is not None:
                impact = json.loads(patch_record.impact_json or "{}")
                impact["critic_review"] = review.model_dump(mode="json")
                impact["requires_confirmation"] = requires_confirmation
                impact["auto_apply_opt_in"] = auto_apply_opt_in
                patch_record.impact_json = json.dumps(impact, ensure_ascii=False)
                if review.verdict == "blocked":
                    patch_record.status = "blocked"
                    trip.state = "AT_RISK"
                await self.db.commit()
        result["review"] = review.model_dump(mode="json")
        result["requires_confirmation"] = requires_confirmation
        result["auto_apply_eligible"] = auto_apply_eligible
        result["status"] = (
            "patch_blocked_by_critic" if review.verdict == "blocked" else result["status"]
        )

        final_execution = self._execution(
            SUPERVISOR_AGENT_SPEC,
            "plan_patch_proposal",
            {**patch_artifact.model_dump(mode="json"), "status": result["status"]},
            review.model_dump(mode="json"),
        )
        m6 = self._message(
            trace,
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.final_answer,
            kind=AgentMessageType.result,
            artifact_type="plan_patch_proposal",
            content={**patch_artifact.model_dump(mode="json"), "status": result["status"]},
            prior=m5,
        )
        self._step(trace, final_execution, "dynamic_patch_review", m5, m6)
        trace.status = "blocked" if review.verdict == "blocked" else "succeeded"
        workflow = await persist_agent_workflow(
            self.db,
            user_id=trip.user_id,
            trace_id=trace_id,
            trace=trace,
            trigger_type=f"trip_event:{event.event_type}",
            planning_run_id=trip.planning_run_id,
            trip_session_id=trip.id,
        )
        await self.db.commit()
        result["workflow_id"] = workflow.id
        result["workflow_task_id"] = trace.task_id
        if (
            result["auto_apply_eligible"]
            and auto_apply_opt_in
            and patch_artifact.patch_id is not None
        ):
            applied = await apply_plan_patch_cas(
                db=self.db,
                patch_id=patch_artifact.patch_id,
                provider=self.provider,
                trace_id=trace_id,
                policy_result="validated_low_risk_auto_apply_opt_in",
            )
            result["status"] = "patch_applied"
            result["requires_confirmation"] = False
            result["applied_plan_version"] = applied["plan_version"]
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return result
