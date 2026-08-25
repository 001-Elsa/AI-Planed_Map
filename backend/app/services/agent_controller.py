"""Compatibility adapter from Companion workflows to the generic AgentRuntime."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    AgentArtifact,
    AgentRun,
    AgentSession,
    AgentSharedStateSnapshot,
    AgentToolCall,
    AgentWorkflowRun,
    TripSession,
)
from backend.app.models import AgentMessage as AgentMessageRecord
from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessage,
    AgentMessageType,
    AgentSpec,
    AgentType,
    ArtifactEnvelope,
    minimize_agent_payload,
)
from backend.app.schemas.companion import ConsentScope, TripState
from backend.app.services.agent_context import build_companion_context
from backend.app.services.agent_decider import AgentDecider, RuleBasedAgentDecider
from backend.app.services.agent_policy import evaluate_tool_policy
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_runtime import (
    AgentRuntime,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    ArtifactValidator,
    ContextLoader,
    SharedStateUpdater,
    ToolBudgetSnapshot,
    ToolExecutor,
    ToolPolicyEvaluator,
    TraceEmitter,
)
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC


class AgentController:
    """Legacy facade; new roles should call `execute` with their own AgentSpec and ports."""

    def __init__(
        self,
        db: AsyncSession,
        decider: AgentDecider | None = None,
        spec: AgentSpec = COMPANION_AGENT_SPEC,
        shared_state: AgentSharedStateManager | None = None,
    ) -> None:
        self.db = db
        self.decider = decider or RuleBasedAgentDecider()
        self.spec = spec
        self.shared_state = shared_state

    async def execute(
        self,
        request: AgentRuntimeRequest,
        *,
        tool_executor: ToolExecutor,
        fallback_decider: AgentDecider | None = None,
        context_loader: ContextLoader | None = None,
        policy_evaluator: ToolPolicyEvaluator | None = None,
        artifact_validator: ArtifactValidator | None = None,
        shared_state_updater: SharedStateUpdater | None = None,
        trace_emitter: TraceEmitter | None = None,
    ) -> AgentRuntimeResult:
        """Execute any role through the common runtime without Companion assumptions."""

        if request.spec.agent_type != self.spec.agent_type:
            raise ValueError("runtime request AgentSpec does not match controller AgentSpec")
        runtime = AgentRuntime(
            decider=self.decider,
            fallback_decider=fallback_decider,
            context_loader=context_loader,
            policy_evaluator=policy_evaluator,
            artifact_validator=artifact_validator,
            shared_state_updater=shared_state_updater,
            trace_emitter=trace_emitter,
        )
        return await runtime.execute(request, tool_executor=tool_executor)

    async def run_once(
        self,
        *,
        trip: TripSession,
        agent: AgentSession,
        observation: dict[str, Any],
        consents: set[ConsentScope],
        tool_executor: ToolExecutor,
        trace_id: str | None = None,
        max_steps: int | None = None,
        route_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Companion adapter retained for Worker/API compatibility."""

        if self.spec.agent_type != AgentType.companion:
            raise ValueError("run_once is the Companion adapter; use execute for other roles")
        task_id = f"trip-{trip.id}-state"
        shared_state_revision: int | None = None
        shared_state_hash: str | None = None
        if self.shared_state is not None:
            shared = await self.shared_state.initialize(task_id, route_plan=route_plan)
            shared_state_revision = shared.revision
            shared_state_hash = shared.state_hash
            view = await self.shared_state.read_for_agent(task_id, AgentType.companion)
            route = view.route_plan or {}
            observation = {
                **observation,
                "shared_state": {
                    "revision": view.revision,
                    "phase": view.phase.value,
                    "route_status": route.get("status"),
                    "stop_count": len(route.get("stops") or []),
                    "evaluation_verdict": (
                        view.evaluation_result.verdict if view.evaluation_result else None
                    ),
                    "execution_context": view.execution_context,
                },
            }

        trigger_type = str(observation.get("trigger") or "controller")
        workflow = AgentWorkflowRun(
            user_id=trip.user_id,
            trip_session_id=trip.id,
            trigger_type=trigger_type,
            mode="enforce",
            status="running",
            trace_id=trace_id,
        )
        self.db.add(workflow)
        await self.db.flush()
        input_artifact = ArtifactEnvelope(
            artifact_type="trip_observation",
            producer_agent=AgentType.companion,
            payload=observation,
            confidence=1,
            evidence_refs=[f"trip:{trip.id}"],
            input_hash=self._input_hash(observation),
        )
        self._add_artifact(workflow.id, None, input_artifact)
        run = AgentRun(
            agent_session_id=agent.id,
            workflow_run_id=workflow.id,
            agent_type=self.spec.agent_type.value,
            prompt_version=self.spec.prompt_version,
            budget_json=self.spec.budget.model_dump_json(),
            trigger_type=trigger_type,
            status="running",
            trace_id=trace_id,
        )
        self.db.add(run)
        await self.db.flush()

        scope_calls = int(
            await self.db.scalar(
                select(func.count(AgentToolCall.id))
                .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
                .where(AgentRun.agent_session_id == agent.id)
            )
            or 0
        )

        async def context_loader(
            _request: AgentRuntimeRequest,
            current_observation: dict[str, Any],
            history: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return build_companion_context(
                trip=trip,
                observation=current_observation,
                route_plan=route_plan,
                tool_history=history,
            )

        def policy_evaluator(tool: str, state: str) -> tuple[bool, str, bool]:
            return evaluate_tool_policy(tool, TripState(state), consents)

        async def shared_state_updater(
            runtime_result: AgentRuntimeResult,
        ) -> dict[str, Any] | None:
            nonlocal shared_state_revision, shared_state_hash
            if self.shared_state is None or shared_state_revision is None:
                return None
            current_shared = await self.shared_state.read(task_id)
            action = (
                "trip_completed"
                if trip.state == TripState.completed.value
                else "trip_started"
                if current_shared.phase.value in {"plan_ready", "finalized"}
                else "trip_event_processed"
            )
            updated = await self.shared_state.update(
                task_id,
                actor=AgentType.companion,
                expected_revision=shared_state_revision,
                action=action,
                changes={
                    "execution_context": {
                        "trip_id": trip.id,
                        "trip_state": trip.state,
                        "plan_version": trip.current_plan_version,
                        "last_run_status": runtime_result.status,
                        "last_trigger": observation.get("trigger"),
                        "tool_step_count": len(runtime_result.steps),
                    }
                },
            )
            shared_state_revision = updated.revision
            shared_state_hash = updated.state_hash
            return {
                "shared_state_ref": task_id,
                "state_revision": updated.revision,
                "state_hash": updated.state_hash,
            }

        runtime_request = AgentRuntimeRequest(
            spec=self.spec,
            state=trip.state,
            observation=observation,
            input_artifact_type="trip_observation",
            task_id=task_id,
            trigger_type=trigger_type,
            evidence_refs=[f"trip:{trip.id}", f"agent_run:{run.id}"],
            max_steps=max_steps,
            tool_budget=ToolBudgetSnapshot(scope_calls=scope_calls),
        )
        runtime_result = await self.execute(
            runtime_request,
            tool_executor=tool_executor,
            fallback_decider=RuleBasedAgentDecider(),
            context_loader=context_loader,
            policy_evaluator=policy_evaluator,
            shared_state_updater=shared_state_updater,
        )

        agent.model_name = runtime_result.model_name
        run.status = runtime_result.status
        run.input_tokens = runtime_result.input_tokens
        run.output_tokens = runtime_result.output_tokens
        run.estimated_cost_usd = runtime_result.estimated_cost_usd
        run.latency_ms = runtime_result.latency_ms
        run.fallback_used = runtime_result.fallback_used
        run.output_summary_json = json.dumps(
            minimize_agent_payload(runtime_result.artifact.payload),
            ensure_ascii=False,
            default=str,
        )[:4000]
        workflow.status = runtime_result.status
        workflow.estimated_cost_usd = runtime_result.estimated_cost_usd
        workflow.completed_at = datetime.now(timezone.utc)

        for step in runtime_result.steps:
            if step.tool is None or step.status in {"finished", "budget_exceeded"}:
                continue
            error_type = (
                step.reason
                if step.status in {"policy_denied", "validation_failed", "failed"}
                else None
            )
            await self._record_tool_call(
                run,
                step.tool,
                step.arguments,
                step.output,
                step.status,
                error_type,
                trace_id,
                latency_ms=step.latency_ms,
            )

        self._add_artifact(workflow.id, run.id, runtime_result.artifact)
        message_count = self._persist_protocol_trace(
            task_id=task_id,
            agent_session_id=agent.id,
            workflow_id=workflow.id,
            observation=observation,
            runtime_result=runtime_result,
        )
        workflow.handoff_count = message_count

        if self.shared_state is not None:
            shared_audit = await self.shared_state.audit(task_id)
            self.db.add(
                AgentSharedStateSnapshot(
                    workflow_run_id=workflow.id,
                    task_id=shared_audit.task_id,
                    revision=shared_audit.revision,
                    phase=shared_audit.phase.value,
                    state_hash=shared_audit.state_hash,
                    payload_json=shared_audit.model_dump_json(),
                )
            )
        await self.db.commit()
        if self.shared_state is not None and trip.state == TripState.completed.value:
            await asyncio.shield(self.shared_state.delete(task_id, reason="trip_completed"))
        return {
            "status": runtime_result.status,
            "steps": [item.model_dump(mode="json") for item in runtime_result.steps],
            "run_id": run.id,
            "workflow_id": workflow.id,
            "agent_type": self.spec.agent_type.value,
        }

    def _persist_protocol_trace(
        self,
        *,
        task_id: str,
        agent_session_id: int,
        workflow_id: int,
        observation: dict[str, Any],
        runtime_result: AgentRuntimeResult,
    ) -> int:
        router = AgentMessageRouter()
        last_message = router.build(
            task_id=task_id,
            sender=AgentEndpoint.system,
            receiver=AgentEndpoint.companion,
            message_type=AgentMessageType.event,
            artifact_type="trip_observation",
            content=observation,
        )
        last_message, status = router.deliver(last_message)
        self.db.add(
            self._message_record(
                last_message,
                router,
                agent_session_id=agent_session_id,
                workflow_run_id=workflow_id,
                delivery_status=status,
            )
        )
        count = 1
        pending_request: AgentMessage | None = None
        for event in runtime_result.events:
            if event.event_type == "model_fallback":
                message = router.build(
                    task_id=task_id,
                    sender=AgentEndpoint.system,
                    receiver=AgentEndpoint.companion,
                    message_type=AgentMessageType.error,
                    artifact_type="recovery_event",
                    content=event.payload,
                    correlation_id=last_message.correlation_id,
                    causation_id=last_message.message_id,
                )
            elif event.event_type == "tool_request":
                message = router.build(
                    task_id=task_id,
                    sender=AgentEndpoint.companion,
                    receiver=AgentEndpoint.tool_runtime,
                    message_type=AgentMessageType.tool_request,
                    artifact_type="tool_request",
                    content={**event.payload, "step_index": event.step_index},
                    correlation_id=last_message.correlation_id,
                    causation_id=last_message.message_id,
                )
                pending_request = message
            elif event.event_type == "tool_result":
                cause = pending_request or last_message
                message = router.build(
                    task_id=task_id,
                    sender=AgentEndpoint.tool_runtime,
                    receiver=AgentEndpoint.companion,
                    message_type=AgentMessageType.tool_result,
                    artifact_type="tool_result",
                    content=event.payload,
                    correlation_id=cause.correlation_id,
                    causation_id=cause.message_id,
                )
            else:
                continue
            message, status = router.deliver(message)
            self.db.add(
                self._message_record(
                    message,
                    router,
                    agent_session_id=agent_session_id,
                    workflow_run_id=workflow_id,
                    delivery_status=status,
                )
            )
            last_message = message
            count += 1

        final_content = dict(runtime_result.artifact.payload)
        if runtime_result.shared_state:
            final_content.update(runtime_result.shared_state)
        final_message = router.build(
            task_id=task_id,
            sender=AgentEndpoint.companion,
            receiver=AgentEndpoint.final_answer,
            message_type=AgentMessageType.result,
            artifact_type=self.spec.output_artifact_type,
            content=final_content,
            correlation_id=last_message.correlation_id,
            causation_id=last_message.message_id,
        )
        final_message, status = router.deliver(final_message)
        self.db.add(
            self._message_record(
                final_message,
                router,
                agent_session_id=agent_session_id,
                workflow_run_id=workflow_id,
                delivery_status=status,
            )
        )
        return count + 1

    def _add_artifact(
        self, workflow_id: int, run_id: int | None, artifact: ArtifactEnvelope
    ) -> None:
        self.db.add(
            AgentArtifact(
                workflow_run_id=workflow_id,
                agent_run_id=run_id,
                artifact_type=artifact.artifact_type,
                schema_version=artifact.schema_version,
                producer_agent=artifact.producer_agent.value,
                payload_json=json.dumps(
                    minimize_agent_payload(artifact.payload), ensure_ascii=False, default=str
                ),
                confidence=artifact.confidence,
                evidence_refs_json=json.dumps(artifact.evidence_refs),
                input_hash=artifact.input_hash,
                created_at=artifact.created_at,
            )
        )

    @staticmethod
    def _input_hash(observation: dict[str, Any]) -> str:
        from backend.app.services.agents.base import canonical_hash

        return canonical_hash(observation)

    @staticmethod
    def _message_record(
        message: AgentMessage,
        router: AgentMessageRouter,
        *,
        agent_session_id: int,
        workflow_run_id: int,
        delivery_status: str,
    ) -> AgentMessageRecord:
        audit = router.audit(message, delivery_status)
        return AgentMessageRecord(
            agent_session_id=agent_session_id,
            workflow_run_id=workflow_run_id,
            role=message.message_type.value,
            content=message.artifact_type,
            structured_json=json.dumps(audit.content_summary, ensure_ascii=False, default=str),
            protocol_version=message.protocol_version,
            message_id=str(message.message_id),
            task_id=message.task_id,
            sender=message.sender.value,
            receiver=message.receiver.value,
            message_type=message.message_type.value,
            artifact_type=message.artifact_type,
            content_hash=message.content_hash,
            idempotency_key=message.idempotency_key,
            correlation_id=str(message.correlation_id),
            causation_id=str(message.causation_id) if message.causation_id else None,
            attempt=message.attempt,
            delivery_status=delivery_status,
            created_at=message.created_at,
        )

    async def _record_tool_call(
        self,
        run: AgentRun,
        tool: str,
        arguments: dict[str, Any],
        output: dict[str, Any],
        status: str,
        error_type: str | None,
        trace_id: str | None,
        latency_ms: int | None = None,
    ) -> None:
        self.db.add(
            AgentToolCall(
                agent_run_id=run.id,
                tool_name=tool,
                input_json=json.dumps(
                    minimize_agent_payload(arguments), ensure_ascii=False, default=str
                ),
                output_summary_json=json.dumps(
                    minimize_agent_payload(output), ensure_ascii=False, default=str
                )[:4000],
                status=status,
                error_type=error_type,
                latency_ms=latency_ms,
                trace_id=trace_id,
            )
        )


__all__ = ["AgentController", "AgentRuntime", "AgentRuntimeRequest", "AgentRuntimeResult"]
