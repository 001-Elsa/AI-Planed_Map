"""Event-driven Companion -> Supervisor -> Replanner -> Planner -> Critic loop."""

from __future__ import annotations

import json
import time
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.clients.amap_client import MapProvider
from backend.app.models import PlanPatch, PlanVersion, TripSession
from backend.app.schemas.agent_artifacts import (
    AgentBudget,
    AgentEndpoint,
    AgentExecutionMode,
    AgentExecutionPlan,
    AgentMessageType,
    AgentStageTrace,
    AgentStepTrace,
    AgentType,
    AgentWorkflowMode,
    AgentWorkflowTrace,
    ArtifactEnvelope,
)
from backend.app.schemas.ai_intent import PlanPatchOperation
from backend.app.schemas.dynamic_replanning import (
    DynamicPatchReview,
    PlanPatchArtifact,
    ReplanDirective,
    TripEventArtifact,
)
from backend.app.services.agent_orchestrator import persist_agent_workflow
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_tool_contracts import stable_tool_error
from backend.app.services.agent_transport import new_agent_reply_inbox
from backend.app.services.agent_workflow_state import DurableWorkflowCheckpointStore
from backend.app.services.agents.base import AgentExecution, canonical_hash
from backend.app.services.agents.replanner_agent import REPLANNER_AGENT_SPEC, ReplannerAgent
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

    def __init__(
        self,
        db: AsyncSession,
        provider: MapProvider,
        *,
        execution_mode: AgentExecutionMode | str = AgentExecutionMode.sync,
        message_bus=None,
        distributed_timeout_seconds: float = 15,
    ) -> None:
        self.db = db
        self.provider = provider
        self.execution_mode = AgentExecutionMode(execution_mode)
        self.message_bus = message_bus
        self.distributed_timeout_seconds = max(1, distributed_timeout_seconds)
        self.router = AgentMessageRouter()
        self.replanner = ReplannerAgent()

    async def _message(
        self,
        trace,
        *,
        sender,
        receiver,
        kind,
        artifact_type,
        content,
        prior=None,
        durable=False,
        reply_to=None,
        inbox=None,
    ):
        message = self.router.build(
            task_id=trace.task_id,
            sender=sender,
            receiver=receiver,
            message_type=kind,
            artifact_type=artifact_type,
            content=content,
            correlation_id=prior.correlation_id if prior else None,
            causation_id=prior.message_id if prior else None,
            reply_to=reply_to,
            inbox=inbox,
        )
        if durable:
            if self.message_bus is None:
                raise RuntimeError("distributed Agent execution requires a message bus")
            published = await self.message_bus.publish(message)
            status = "delivered" if published.status == "published" else "duplicate"
            trace.messages.append(self.router.audit(message, status))
            trace.handoff_count += 1
            return message
        delivered, status = self.router.deliver(message)
        trace.messages.append(self.router.audit(delivered, status))
        trace.handoff_count += 1
        return delivered

    @staticmethod
    def _stage(
        trace: AgentWorkflowTrace,
        *,
        stage_key: str,
        stage_type: str,
        owner_agent,
        depends_on: list[str],
        input_artifact_type: str,
        output_artifact_type: str,
        input_payload,
        output_payload,
        latency_ms: int = 0,
        reason: str | None = None,
        status: str = "succeeded",
    ) -> None:
        trace.stages.append(
            AgentStageTrace(
                stage_key=stage_key,
                stage_type=stage_type,
                owner_agent=owner_agent,
                depends_on=depends_on,
                input_artifact_type=input_artifact_type,
                output_artifact_type=output_artifact_type,
                input_hash=canonical_hash(input_payload),
                output_hash=canonical_hash(output_payload),
                summary=output_payload if isinstance(output_payload, dict) else {},
                latency_ms=max(0, latency_ms),
                reason=reason,
                status=status,
            )
        )

    @staticmethod
    def _step(trace, execution, input_type, inbound=None, outbound=None, task_key=None):
        trace.steps.append(
            AgentStepTrace(
                task_key=task_key,
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
        trace.total_cost_usd += execution.estimated_cost_usd

    @staticmethod
    def _execution_plan(status: str) -> Any:
        step_status = {
            "event_ingest": "succeeded",
            "supervisor_dispatch": "succeeded",
            "replanner": "succeeded",
            "deterministic_replan": "succeeded",
            "deterministic_review": "blocked" if status == "blocked" else "succeeded",
            "final_answer": "succeeded",
        }
        if status == "pending":
            step_status = {key: "pending" for key in step_status}
        return {
            "plan_kind": "safety_sensitive_trip",
            "steps": [
                {
                    "step_id": "event_ingest",
                    "agent_type": "companion",
                    "execution_kind": "stage",
                    "responsibility": "accept_trip_event_from_companion",
                    "status": step_status["event_ingest"],
                    "depends_on": [],
                    "input_artifact_type": "trip_observation",
                    "output_artifact_type": "trip_event_artifact",
                    "output_schema_ref": "TripEventArtifact",
                    "budget": AgentBudget(max_steps=1, max_cost_usd=0).model_dump(mode="json"),
                    "required": True,
                },
                {
                    "step_id": "supervisor_dispatch",
                    "agent_type": "supervisor",
                    "execution_kind": "stage",
                    "responsibility": "route_event_to_replanner",
                    "status": step_status["supervisor_dispatch"],
                    "depends_on": ["event_ingest"],
                    "input_artifact_type": "trip_event_artifact",
                    "output_artifact_type": "trip_event_artifact",
                    "output_schema_ref": "TripEventArtifact",
                    "budget": AgentBudget(max_steps=1, max_cost_usd=0).model_dump(mode="json"),
                    "required": True,
                },
                {
                    "step_id": "replanner",
                    "agent_type": "replanner",
                    "execution_kind": "agent",
                    "responsibility": "select_bounded_recovery_strategy",
                    "status": step_status["replanner"],
                    "depends_on": ["supervisor_dispatch"],
                    "input_artifact_type": "trip_event_artifact",
                    "output_artifact_type": "replan_directive",
                    "output_schema_ref": "ReplanDirective",
                    "budget": AgentBudget(
                        max_steps=1,
                        max_input_tokens=1000,
                        max_output_tokens=400,
                        max_cost_usd=0,
                        timeout_seconds=5,
                    ).model_dump(mode="json"),
                    "required": True,
                },
                {
                    "step_id": "deterministic_replan",
                    "agent_type": "planner",
                    "execution_kind": "stage",
                    "responsibility": "recompute_patch_with_provider_and_constraints",
                    "status": step_status["deterministic_replan"],
                    "depends_on": ["replanner"],
                    "input_artifact_type": "replan_directive",
                    "output_artifact_type": "plan_patch_candidate",
                    "output_schema_ref": "PlanPatchArtifact",
                    "budget": AgentBudget(max_steps=1, max_cost_usd=0).model_dump(mode="json"),
                    "required": True,
                },
                {
                    "step_id": "deterministic_review",
                    "agent_type": "critic",
                    "execution_kind": "stage",
                    "responsibility": "validate_patch_risk_and_hard_constraints",
                    "status": step_status["deterministic_review"],
                    "depends_on": ["deterministic_replan"],
                    "input_artifact_type": "plan_patch_candidate",
                    "output_artifact_type": "dynamic_patch_review",
                    "output_schema_ref": "DynamicPatchReview",
                    "budget": AgentBudget(max_steps=1, max_cost_usd=0).model_dump(mode="json"),
                    "required": True,
                },
                {
                    "step_id": "final_answer",
                    "agent_type": "supervisor",
                    "execution_kind": "stage",
                    "responsibility": "assemble_patch_proposal",
                    "status": step_status["final_answer"],
                    "depends_on": ["deterministic_review"],
                    "input_artifact_type": "dynamic_patch_review",
                    "output_artifact_type": "plan_patch_proposal",
                    "output_schema_ref": "PlanPatchArtifact",
                    "budget": AgentBudget(max_steps=1, max_cost_usd=0).model_dump(mode="json"),
                    "required": True,
                },
            ],
            "rationale": ["dynamic_event_recovery"],
            "skipped_optional_steps": [],
        }

    async def _run_distributed_replanner(self, trace, request_message):
        """Send one role task to Redis Streams and wait for its typed result."""
        if self.message_bus is None:
            raise RuntimeError("distributed Agent execution requires a message bus")
        consumer = f"workflow-{trace.task_id}"
        inbox = request_message.reply_to
        if inbox is None:
            raise RuntimeError("distributed replanner request is missing its reply inbox")
        deadline = time.monotonic() + self.distributed_timeout_seconds
        delivery = None
        while time.monotonic() < deadline:
            delivery = await self.message_bus.receive(
                AgentEndpoint.planner,
                consumer,
                block_ms=min(1_000, max(1, int((deadline - time.monotonic()) * 1000))),
                inbox=inbox,
            )
            if delivery is not None:
                break
        if delivery is None:
            raise TimeoutError("distributed replanner did not return before workflow timeout")
        try:
            if delivery.message.inbox != inbox:
                raise ValueError("distributed replanner response targeted the wrong inbox")
            if delivery.message.task_id != request_message.task_id:
                raise ValueError("distributed replanner response has the wrong task id")
            if delivery.message.correlation_id != request_message.correlation_id:
                raise ValueError("distributed replanner response has the wrong correlation id")
            if delivery.message.causation_id != request_message.message_id:
                raise ValueError("distributed replanner response has the wrong causation id")
            content = delivery.message.content
            directive = ReplanDirective.model_validate(content.get("directive", content))
            metadata = content.get("execution") or {}
            artifact = ArtifactEnvelope(
                artifact_type="replan_directive",
                producer_agent=AgentType.replanner,
                payload={
                    "strategy": directive.strategy,
                    "event_type": directive.event_type,
                    "execution_mode": AgentExecutionMode.distributed.value,
                },
                confidence=1,
                evidence_refs=["distributed:replanner"],
                input_hash=canonical_hash(request_message.content),
            )
            execution = AgentExecution(
                spec=REPLANNER_AGENT_SPEC,
                output=directive,
                artifact=artifact,
                latency_ms=int(metadata.get("latency_ms") or 0),
                input_tokens=int(metadata.get("input_tokens") or 0),
                output_tokens=int(metadata.get("output_tokens") or 0),
                estimated_cost_usd=float(metadata.get("estimated_cost_usd") or 0),
            )
            trace.messages.append(self.router.audit(delivery.message, "delivered"))
            trace.handoff_count += 1
            self._step(
                trace,
                execution,
                "trip_event_artifact",
                request_message,
                delivery.message,
                task_key="replanner",
            )
            if not await self.message_bus.transport.acknowledge(delivery):
                raise RuntimeError("distributed replanner response ACK failed")
            return delivery.message, directive, execution
        except Exception as exc:
            await self.message_bus.transport.retry(
                delivery,
                error_code=stable_tool_error(exc),
                max_attempts=1,
            )
            raise

    @staticmethod
    async def _transition(
        trace: AgentWorkflowTrace,
        checkpoint_store: DurableWorkflowCheckpointStore,
        step_id: str,
        status: Literal[
            "pending", "running", "retrying", "succeeded", "failed", "blocked", "skipped"
        ],
        *,
        begin: bool = False,
    ) -> None:
        if trace.execution_plan is None:
            raise RuntimeError("dynamic workflow DAG is missing")
        step = next(item for item in trace.execution_plan.steps if item.step_id == step_id)
        if step.status == "succeeded":
            return
        step.status = status
        if begin:
            step.attempt_count += 1
        await checkpoint_store.checkpoint(trace)

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
        trace = AgentWorkflowTrace(
            mode=AgentWorkflowMode.enforce,
            execution_mode=self.execution_mode,
            task_id=f"trip-{trip.id}-event-{event.event_id or uuid4()}",
        )
        trace.execution_plan = AgentExecutionPlan.model_validate(self._execution_plan("pending"))
        trigger_type = f"trip_event:{event.event_id or event.event_type[:40]}"
        checkpoint_store, resume_from_task = (
            await DurableWorkflowCheckpointStore.resume_or_start(
                self.db,
                trace=trace,
                user_id=trip.user_id,
                trigger_type=trigger_type,
                trace_id=trace_id,
                planning_run_id=trip.planning_run_id,
                trip_session_id=trip.id,
            )
        )
        try:
            return await self._execute(
                trace=trace,
                checkpoint_store=checkpoint_store,
                trip=trip,
                event=event,
                current_location=current_location,
                completed_stop_ids=completed_stop_ids,
                event_payload=event_payload,
                weather=weather,
                trace_id=trace_id,
                trigger_type=trigger_type,
                resume_from_task=resume_from_task,
            )
        except Exception as exc:
            await checkpoint_store.fail(trace, reason=stable_tool_error(exc))
            raise

    async def _execute(
        self,
        *,
        trace: AgentWorkflowTrace,
        checkpoint_store: DurableWorkflowCheckpointStore,
        trip: TripSession,
        event: TripEventArtifact,
        current_location,
        completed_stop_ids: list[str],
        event_payload: dict[str, Any],
        weather: dict[str, Any] | None,
        trace_id: str | None,
        trigger_type: str,
        resume_from_task: str | None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        await self._transition(trace, checkpoint_store, "event_ingest", "running", begin=True)
        event_payload_typed = event.model_dump(mode="json")
        m1 = await self._message(
            trace,
            sender=AgentEndpoint.companion,
            receiver=AgentEndpoint.supervisor,
            kind=AgentMessageType.artifact,
            artifact_type="trip_event_artifact",
            content=event_payload_typed,
        )
        self._stage(
            trace,
            stage_key="event_ingest",
            stage_type="orchestration",
            owner_agent=None,
            depends_on=[],
            input_artifact_type="trip_observation",
            output_artifact_type="trip_event_artifact",
            input_payload=event_payload_typed,
            output_payload={"event_type": event.event_type, "event_id": event.event_id},
        )
        await self._transition(trace, checkpoint_store, "event_ingest", "succeeded")

        runtime_summary = {
            "current_location": current_location.model_dump(mode="json"),
            "completed_stop_ids": completed_stop_ids,
            "event_payload": event_payload,
            "weather": weather,
        }
        dispatch_event = event.model_copy(
            update={"payload_summary": {**event.payload_summary, "_runtime": runtime_summary}}
        )
        reply_inbox = (
            new_agent_reply_inbox()
            if self.execution_mode == AgentExecutionMode.distributed
            else None
        )
        await self._transition(
            trace, checkpoint_store, "supervisor_dispatch", "running", begin=True
        )
        m2 = await self._message(
            trace,
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.replanner,
            kind=AgentMessageType.command,
            artifact_type="trip_event_artifact",
            content=dispatch_event.model_dump(mode="json"),
            prior=m1,
            durable=self.execution_mode == AgentExecutionMode.distributed,
            reply_to=reply_inbox,
        )
        self._stage(
            trace,
            stage_key="supervisor_dispatch",
            stage_type="orchestration",
            owner_agent=None,
            depends_on=["event_ingest"],
            input_artifact_type="trip_event_artifact",
            output_artifact_type="trip_event_artifact",
            input_payload=event_payload_typed,
            output_payload={
                "workflow_state": "dynamic_replan_scheduled",
                "base_plan_version": event.base_plan_version,
                "execution_mode": self.execution_mode.value,
            },
        )
        await self._transition(trace, checkpoint_store, "supervisor_dispatch", "succeeded")

        await self._transition(trace, checkpoint_store, "replanner", "running", begin=True)
        if self.execution_mode == AgentExecutionMode.distributed:
            m3, directive, replanner_execution = await self._run_distributed_replanner(trace, m2)
        else:
            replanner_execution = await self.replanner.run(
                event,
                current_location=current_location,
                completed_stop_ids=completed_stop_ids,
                event_payload=event_payload,
                weather=weather,
            )
            directive = replanner_execution.output
            m3 = await self._message(
                trace,
                sender=AgentEndpoint.replanner,
                receiver=AgentEndpoint.planner,
                kind=AgentMessageType.artifact,
                artifact_type="replan_directive",
                content=directive.model_dump(mode="json"),
                prior=m2,
            )
            self._step(
                trace,
                replanner_execution,
                "trip_event_artifact",
                m2,
                m3,
                task_key="replanner",
            )
        await self._transition(trace, checkpoint_store, "replanner", "succeeded")
        if directive.base_plan_version != trip.current_plan_version:
            raise ValueError("dynamic replan directive references a stale plan version")
        await self._transition(
            trace, checkpoint_store, "deterministic_replan", "running", begin=True
        )
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
        m4 = await self._message(
            trace,
            sender=AgentEndpoint.planner,
            receiver=AgentEndpoint.critic,
            kind=AgentMessageType.artifact,
            artifact_type="plan_patch_candidate",
            content=patch_artifact.model_dump(mode="json"),
            prior=m3,
        )
        self._stage(
            trace,
            stage_key="deterministic_replan",
            stage_type="deterministic",
            owner_agent=None,
            depends_on=["replanner"],
            input_artifact_type="replan_directive",
            output_artifact_type="plan_patch_candidate",
            input_payload=directive.model_dump(mode="json"),
            output_payload={
                "patch_id": patch_artifact.patch_id,
                "status": patch_artifact.status,
                "operation_count": len(patch_artifact.operations),
                "impact": patch_artifact.impact,
            },
        )
        await self._transition(
            trace, checkpoint_store, "deterministic_replan", "succeeded"
        )

        # The current formal version remains the only Critic baseline.
        await self._transition(
            trace, checkpoint_store, "deterministic_review", "running", begin=True
        )
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
        m5 = await self._message(
            trace,
            sender=AgentEndpoint.critic,
            receiver=AgentEndpoint.supervisor,
            kind=AgentMessageType.result,
            artifact_type="dynamic_patch_review",
            content=review.model_dump(mode="json"),
            prior=m4,
        )
        self._stage(
            trace,
            stage_key="deterministic_review",
            stage_type="deterministic",
            owner_agent=None,
            depends_on=["deterministic_replan"],
            input_artifact_type="plan_patch_candidate",
            output_artifact_type="dynamic_patch_review",
            input_payload=patch_artifact.model_dump(mode="json"),
            output_payload=review.model_dump(mode="json"),
            status="blocked" if review.verdict == "blocked" else "succeeded",
            reason="critic_blocked_patch" if review.verdict == "blocked" else None,
        )
        await self._transition(
            trace,
            checkpoint_store,
            "deterministic_review",
            "blocked" if review.verdict == "blocked" else "succeeded",
        )

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

        await self._transition(trace, checkpoint_store, "final_answer", "running", begin=True)
        await self._message(
            trace,
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.final_answer,
            kind=AgentMessageType.result,
            artifact_type="plan_patch_proposal",
            content={**patch_artifact.model_dump(mode="json"), "status": result["status"]},
            prior=m5,
        )
        self._stage(
            trace,
            stage_key="final_answer",
            stage_type="orchestration",
            owner_agent=None,
            depends_on=["deterministic_review"],
            input_artifact_type="dynamic_patch_review",
            output_artifact_type="plan_patch_proposal",
            input_payload=review.model_dump(mode="json"),
            output_payload={
                "status": result["status"],
                "requires_confirmation": requires_confirmation,
                "patch_id": patch_artifact.patch_id,
            },
        )
        await self._transition(trace, checkpoint_store, "final_answer", "succeeded")
        trace.status = "blocked" if review.verdict == "blocked" else "succeeded"
        await checkpoint_store.checkpoint(trace)
        workflow = await persist_agent_workflow(
            self.db,
            user_id=trip.user_id,
            trace_id=trace_id,
            trace=trace,
            trigger_type=trigger_type,
            planning_run_id=trip.planning_run_id,
            trip_session_id=trip.id,
        )
        await self.db.commit()
        result["workflow_id"] = workflow.id
        result["workflow_task_id"] = trace.task_id
        result["resumed_from_task"] = resume_from_task
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
