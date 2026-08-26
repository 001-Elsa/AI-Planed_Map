"""Deterministic orchestration and persistence for isolated Agent roles."""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import Settings
from backend.app.models import (
    AgentArtifact,
    AgentHandoff,
    AgentRun,
    AgentSharedStateSnapshot,
    AgentWorkflowRun,
    AgentWorkflowTask,
)
from backend.app.models import AgentMessage as AgentMessageRecord
from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentExecutionPlan,
    AgentMessage,
    AgentMessageType,
    AgentRecoveryDecision,
    AgentStepTrace,
    AgentType,
    AgentWorkflowMode,
    AgentWorkflowTrace,
    ReviewReport,
    SafetyCheckReport,
    minimize_agent_payload,
)
from backend.app.schemas.agent_state import AgentSharedStateAudit
from backend.app.schemas.ai_intent import AIPlanRequest, AIPlanResult, PlanningIntent
from backend.app.services.agent_context import build_critic_context, build_planning_context
from backend.app.services.agent_protocol import AgentMessageRouter, AgentProtocolError
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.agent_tool_contracts import stable_tool_error
from backend.app.services.agents.base import AgentExecution, canonical_hash
from backend.app.services.agents.critic_agent import CriticAgent
from backend.app.services.agents.intent_agent import IntentAgent
from backend.app.services.agents.planner_agent import PlannerAgent
from backend.app.services.agents.safety_agent import SafetyAgent
from backend.app.services.agents.search_agent import SearchAgent, SearchAgentInput, SearchArtifact
from backend.app.services.agents.supervisor_agent import SupervisorAgent


class PlanningAgentOrchestrator:
    """Owns hand-offs; Intent and Critic never receive references to each other."""

    def __init__(
        self,
        *,
        settings: Settings,
        supervisor_agent: SupervisorAgent,
        intent_agent: IntentAgent,
        search_agent: SearchAgent,
        safety_agent: SafetyAgent,
        planner_agent: PlannerAgent,
        critic_agent: CriticAgent,
        shared_state: AgentSharedStateManager,
    ) -> None:
        self.settings = settings
        self.supervisor_agent = supervisor_agent
        self.intent_agent = intent_agent
        self.search_agent = search_agent
        self.safety_agent = safety_agent
        self.planner_agent = planner_agent
        self.critic_agent = critic_agent
        self.shared_state = shared_state
        try:
            configured_mode = AgentWorkflowMode(settings.plan_critic_mode.lower())
        except ValueError:
            configured_mode = AgentWorkflowMode.shadow
        self.mode = configured_mode if settings.multi_agent_enabled else AgentWorkflowMode.off
        self.router = AgentMessageRouter()
        self.trace = AgentWorkflowTrace(mode=self.mode, task_id=f"plan-{uuid4()}")
        self._pending: dict[AgentEndpoint, deque[AgentMessage]] = {}
        self._active_inputs: dict[AgentEndpoint, AgentMessage] = {}
        self.execution_plan: AgentExecutionPlan | None = None
        self.state_revision = -1

    def _dispatch(
        self,
        *,
        sender: AgentEndpoint,
        receiver: AgentEndpoint,
        message_type: AgentMessageType,
        artifact_type: str,
        content: dict[str, Any],
        causation_id=None,
        correlation_id=None,
    ) -> AgentMessage:
        message = self.router.build(
            task_id=self.trace.task_id,
            sender=sender,
            receiver=receiver,
            message_type=message_type,
            artifact_type=artifact_type,
            content=content,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        delivered, status = self.router.deliver(message)
        self.trace.messages.append(self.router.audit(delivered, status))
        self._pending.setdefault(receiver, deque()).append(delivered)
        return delivered

    def _consume(
        self, receiver: AgentEndpoint, expected_artifact_types: frozenset[str]
    ) -> AgentMessage:
        try:
            queue = self._pending[receiver]
            message = queue.popleft()
            if not queue:
                del self._pending[receiver]
        except (KeyError, IndexError) as exc:
            raise AgentProtocolError(f"no pending message for {receiver.value}") from exc
        if message.artifact_type not in expected_artifact_types:
            raise AgentProtocolError(
                f"{receiver.value} cannot consume {message.artifact_type}; "
                f"expected {sorted(expected_artifact_types)}"
            )
        self._active_inputs[receiver] = message
        return message

    def _record(
        self,
        execution: AgentExecution[Any],
        input_artifact_type: str,
        *,
        input_message: AgentMessage | None = None,
        output_message: AgentMessage | None = None,
    ) -> None:
        cost = execution.estimated_cost_usd
        if not cost and (execution.input_tokens or execution.output_tokens):
            cost = (
                execution.input_tokens * self.settings.llm_input_cost_per_million_usd
                + execution.output_tokens * self.settings.llm_output_cost_per_million_usd
            ) / 1_000_000
        self.trace.steps.append(
            AgentStepTrace(
                agent_type=execution.spec.agent_type,
                status="fallback" if execution.fallback_used else "succeeded",
                prompt_version=execution.spec.prompt_version,
                budget=execution.spec.budget,
                input_artifact_type=input_artifact_type,
                output_artifact=execution.artifact,
                latency_ms=execution.latency_ms,
                input_tokens=execution.input_tokens,
                output_tokens=execution.output_tokens,
                estimated_cost_usd=cost,
                fallback_used=execution.fallback_used,
                reason=execution.reason,
                input_message_id=input_message.message_id if input_message else None,
                output_message_id=output_message.message_id if output_message else None,
            )
        )
        self.trace.handoff_count += 1
        self.trace.total_cost_usd += cost

    async def start(self, request: AIPlanRequest) -> None:
        state = await self.shared_state.initialize(self.trace.task_id)
        self.state_revision = state.revision
        inbound = self._dispatch(
            sender=AgentEndpoint.user,
            receiver=AgentEndpoint.supervisor,
            message_type=AgentMessageType.command,
            artifact_type="planning_request",
            content=request.model_dump(mode="json"),
        )
        inbound = self._consume(AgentEndpoint.supervisor, frozenset({"planning_request"}))
        validated_request = AIPlanRequest.model_validate(inbound.content)
        execution = await self.supervisor_agent.start(validated_request)
        outbound = self._dispatch(
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.intent,
            message_type=AgentMessageType.command,
            artifact_type="planning_request",
            content=validated_request.model_dump(mode="json"),
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        self._record(
            execution,
            "planning_request",
            input_message=inbound,
            output_message=outbound,
        )

    async def understand(self, request: AIPlanRequest):
        inbound = self._consume(AgentEndpoint.intent, frozenset({"planning_request"}))
        validated_request = AIPlanRequest.model_validate(inbound.content)
        execution = await self.intent_agent.run(validated_request)
        intent, required_questions = execution.output
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.intent,
            expected_revision=self.state_revision,
            action="intent_analyzed",
            changes={
                "user_requirement": intent,
                "clarification_questions": [
                    question.model_dump(mode="json") for question in required_questions
                ],
            },
            message_id=inbound.message_id,
        )
        self.state_revision = state.revision
        receiver = AgentEndpoint.supervisor
        message_type = AgentMessageType.result if required_questions else AgentMessageType.artifact
        outbound = self._dispatch(
            sender=AgentEndpoint.intent,
            receiver=receiver,
            message_type=message_type,
            artifact_type="intent_artifact",
            content={
                "shared_state_ref": self.trace.task_id,
                "state_revision": self.state_revision,
                "state_hash": state.state_hash,
                "artifact_hash": execution.artifact.input_hash,
                "question_count": len(required_questions),
            },
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        self._record(
            execution,
            "planning_request",
            input_message=inbound,
            output_message=outbound,
        )
        return execution.output

    async def plan_next(self, fallback_intent: PlanningIntent) -> AgentExecutionPlan:
        inbound = self._consume(AgentEndpoint.supervisor, frozenset({"intent_artifact"}))
        view = await self.shared_state.read_for_agent(self.trace.task_id, AgentType.supervisor)
        if view.revision != int(inbound.content.get("state_revision", -1)):
            raise AgentProtocolError("supervisor intent message references stale shared state")
        if view.state_hash != inbound.content.get("state_hash"):
            raise AgentProtocolError("supervisor intent message references a different state hash")
        if view.user_requirement is None:
            raise AgentProtocolError("supervisor shared state is missing structured intent")
        if canonical_hash(view.user_requirement.model_dump(mode="json")) != canonical_hash(
            fallback_intent.model_dump(mode="json")
        ):
            raise AgentProtocolError("supervisor input intent does not match workflow state")
        execution = await self.supervisor_agent.plan(view.user_requirement, mode=self.mode)
        plan = AgentExecutionPlan.model_validate(execution.output)
        self.execution_plan = plan
        outbound = self._dispatch(
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.search,
            message_type=AgentMessageType.command,
            artifact_type="intent_artifact",
            content={
                "shared_state_ref": self.trace.task_id,
                "state_revision": self.state_revision,
                "state_hash": view.state_hash,
                "artifact_hash": execution.artifact.input_hash,
                "question_count": 0,
            },
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        self._record(
            execution,
            "intent_artifact",
            input_message=inbound,
            output_message=outbound,
        )
        return plan

    async def run_search(
        self, request: AIPlanRequest, fallback_intent: PlanningIntent
    ) -> SearchArtifact:
        inbound = self._consume(
            AgentEndpoint.search, frozenset({"intent_artifact", "retry_directive"})
        )
        view = await self.shared_state.read_for_agent(self.trace.task_id, AgentType.search)
        if view.revision != int(inbound.content.get("state_revision", -1)):
            raise AgentProtocolError("search message references a stale shared-state revision")
        if view.state_hash != inbound.content.get("state_hash"):
            raise AgentProtocolError("search message references a different shared-state hash")
        if view.user_requirement is None:
            raise AgentProtocolError("search shared state is missing structured intent")
        intent = view.user_requirement.model_copy(deep=True)
        if view.soft_adjustments is not None:
            for key, value in view.soft_adjustments.updates().items():
                setattr(intent.preferences.weights, key, value)
        if canonical_hash(intent.model_dump(mode="json")) != canonical_hash(
            fallback_intent.model_dump(mode="json")
        ):
            raise AgentProtocolError("search input intent does not match workflow state")
        if request.origin is None:
            raise AgentProtocolError("search context is missing the confirmed origin")
        execution = await self.search_agent.run(
            SearchAgentInput(
                intent=intent,
                origin=request.origin,
                city=request.city,
                task_poi_overrides=request.task_poi_overrides,
            ),
            recovery_handler=self.recover,
        )
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.search,
            expected_revision=self.state_revision,
            action="search_completed",
            changes={"poi_candidates": execution.output.candidate_groups},
            message_id=inbound.message_id,
        )
        self.state_revision = state.revision
        receiver = AgentEndpoint.safety if self.safety_required() else AgentEndpoint.planner
        outbound = self._dispatch(
            sender=AgentEndpoint.search,
            receiver=receiver,
            message_type=AgentMessageType.artifact,
            artifact_type="search_artifact",
            content={
                "summary": execution.artifact.payload,
                "artifact_hash": canonical_hash(execution.output.model_dump(mode="json")),
                "shared_state_ref": self.trace.task_id,
                "state_revision": self.state_revision,
                "state_hash": state.state_hash,
            },
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        self._record(
            execution,
            "intent_artifact",
            input_message=inbound,
            output_message=outbound,
        )
        return execution.output

    def safety_required(self) -> bool:
        return bool(
            self.execution_plan
            and any(step.step_id == "safety_check" for step in self.execution_plan.steps)
        )

    async def execute_planning_stages(
        self, request: AIPlanRequest, intent: PlanningIntent
    ) -> AIPlanResult:
        """Execute the Supervisor-selected planning subgraph in topological order."""

        if self.execution_plan is None:
            raise AgentProtocolError("Supervisor task graph has not been created")
        executable_steps = {step.step_id for step in self.execution_plan.steps}
        if not {"search", "planner"}.issubset(executable_steps):
            raise AgentProtocolError("Supervisor task graph is missing required planning stages")
        search = await self.run_search(request, intent)
        if "safety_check" in executable_steps:
            await self.check_safety()
        return await self.run_planner(request, intent, search)

    async def check_safety(self) -> SafetyCheckReport:
        inbound = self._consume(AgentEndpoint.safety, frozenset({"search_artifact"}))
        view = await self.shared_state.read_for_agent(self.trace.task_id, AgentType.safety)
        if view.revision != int(inbound.content.get("state_revision", -1)):
            raise AgentProtocolError("safety message references a stale shared-state revision")
        if view.state_hash != inbound.content.get("state_hash"):
            raise AgentProtocolError("safety message references a different shared-state hash")
        if view.user_requirement is None:
            raise AgentProtocolError("safety shared state is missing structured intent")
        candidates = view.poi_candidates or []
        execution = await self.safety_agent.run(
            intent=view.user_requirement,
            candidates=candidates,
        )
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.safety,
            expected_revision=self.state_revision,
            action="safety_checked",
            changes={
                "execution_context": {"safety_check": execution.output.model_dump(mode="json")}
            },
            message_id=inbound.message_id,
        )
        self.state_revision = state.revision
        outbound = self._dispatch(
            sender=AgentEndpoint.safety,
            receiver=AgentEndpoint.planner,
            message_type=AgentMessageType.artifact,
            artifact_type="safety_report",
            content={
                "summary": execution.output.model_dump(mode="json"),
                "artifact_hash": canonical_hash(execution.output.model_dump(mode="json")),
                "search_artifact_hash": inbound.content["artifact_hash"],
                "shared_state_ref": self.trace.task_id,
                "state_revision": self.state_revision,
                "state_hash": state.state_hash,
            },
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        self._record(
            execution,
            "search_artifact",
            input_message=inbound,
            output_message=outbound,
        )
        return execution.output

    async def run_planner(
        self,
        request: AIPlanRequest,
        fallback_intent: PlanningIntent,
        search: SearchArtifact,
    ) -> AIPlanResult:
        inbound = self._consume(
            AgentEndpoint.planner, frozenset({"search_artifact", "safety_report"})
        )
        view = await self.shared_state.read_for_agent(self.trace.task_id, AgentType.planner)
        if request.origin is None:
            raise AgentProtocolError("planner context is missing the confirmed origin")
        context = build_planning_context(
            view=view,
            message=inbound,
            search=search,
            origin=request.origin,
            city=request.city,
            max_candidates_per_task=request.max_candidates_per_task,
            fallback_intent=fallback_intent,
        )
        execution = await self.planner_agent.run(context)
        result = execution.output
        formal_plan = result.model_dump(mode="json", exclude={"critic_review", "agent_workflow"})
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.planner,
            expected_revision=self.state_revision,
            action="plan_completed",
            changes={"route_plan": formal_plan},
            message_id=inbound.message_id,
        )
        self.state_revision = state.revision
        run_critic = result.status != "need_clarification" and self.may_run_critic()
        receiver = AgentEndpoint.critic if run_critic else AgentEndpoint.supervisor
        outbound = self._dispatch(
            sender=AgentEndpoint.planner,
            receiver=receiver,
            message_type=(AgentMessageType.artifact if run_critic else AgentMessageType.result),
            artifact_type="plan_candidate",
            content={
                "shared_state_ref": self.trace.task_id,
                "state_revision": self.state_revision,
                "state_hash": state.state_hash,
                "plan_hash": canonical_hash(formal_plan),
                **execution.artifact.payload,
            },
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        self._record(
            execution,
            inbound.artifact_type,
            input_message=inbound,
            output_message=outbound,
        )
        return result

    def may_run_critic(self) -> bool:
        return (
            self.mode != AgentWorkflowMode.off
            and self.trace.handoff_count < self.settings.max_agent_handoffs
            and self.trace.total_cost_usd < self.settings.max_agent_workflow_cost_usd
        )

    async def review(self, plan: dict[str, Any]) -> ReviewReport | None:
        if not self.may_run_critic():
            return None
        inbound = self._consume(AgentEndpoint.critic, frozenset({"plan_candidate"}))
        view = await self.shared_state.read_for_agent(self.trace.task_id, AgentType.critic)
        context = build_critic_context(view=view, message=inbound, plan=plan)
        execution = await self.critic_agent.run(context)
        projected = self.trace.total_cost_usd + execution.estimated_cost_usd
        if projected > self.settings.max_agent_workflow_cost_usd:
            self.trace.status = "budget_exceeded"
            return None
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.critic,
            expected_revision=self.state_revision,
            action="critic_reviewed",
            changes={"evaluation_result": execution.output},
            message_id=inbound.message_id,
        )
        self.state_revision = state.revision
        outbound = self._dispatch(
            sender=AgentEndpoint.critic,
            receiver=AgentEndpoint.supervisor,
            message_type=AgentMessageType.result,
            artifact_type="review_report",
            content={
                "shared_state_ref": self.trace.task_id,
                "state_revision": self.state_revision,
                "state_hash": state.state_hash,
                "verdict": execution.output.verdict,
                "confidence": execution.output.confidence,
                "finding_count": len(execution.output.findings),
            },
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        self._record(
            execution,
            "plan_candidate",
            input_message=inbound,
            output_message=outbound,
        )
        return execution.output

    async def apply_soft_adjustments(
        self, intent: PlanningIntent, review: ReviewReport
    ) -> PlanningIntent:
        if review.suggested_adjustments is None:
            return intent
        adjusted = intent.model_copy(deep=True)
        for key, value in review.suggested_adjustments.updates().items():
            setattr(adjusted.preferences.weights, key, value)
        self.trace.retry_count += 1
        inbound = self._consume(AgentEndpoint.supervisor, frozenset({"review_report"}))
        supervisor_view = await self.shared_state.read_for_agent(
            self.trace.task_id, AgentType.supervisor
        )
        if (
            supervisor_view.revision != int(inbound.content.get("state_revision", -1))
            or supervisor_view.state_hash != inbound.content.get("state_hash")
            or supervisor_view.evaluation_result != review
        ):
            raise AgentProtocolError("supervisor review does not match shared state")
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.supervisor,
            expected_revision=self.state_revision,
            action="soft_retry_scheduled",
            changes={"soft_adjustments": review.suggested_adjustments},
            message_id=inbound.message_id,
        )
        self.state_revision = state.revision
        self._dispatch(
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.search,
            message_type=AgentMessageType.command,
            artifact_type="retry_directive",
            content={
                "soft_adjustments": review.suggested_adjustments.updates(),
                "shared_state_ref": self.trace.task_id,
                "state_revision": self.state_revision,
                "state_hash": state.state_hash,
            },
            correlation_id=inbound.correlation_id,
            causation_id=inbound.message_id,
        )
        return adjusted

    def retry_allowed(self) -> bool:
        return (
            self.mode == AgentWorkflowMode.enforce
            and self.trace.retry_count < self.settings.max_critic_retries
            and self.trace.handoff_count < self.settings.max_agent_handoffs
        )

    async def recover(
        self,
        *,
        stage: str,
        exc: Exception,
        attempt: int = 1,
        max_attempts: int = 1,
        timeout_seconds: float | None = None,
        fallback_available: bool = False,
        fallback_source: str | None = None,
    ) -> AgentRecoveryDecision:
        error_code = stable_tool_error(exc)
        inbound = self._dispatch(
            sender=AgentEndpoint.system,
            receiver=AgentEndpoint.supervisor,
            message_type=AgentMessageType.error,
            artifact_type="recovery_event",
            content={
                "stage": stage,
                "error_type": error_code,
                "message": error_code,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": timeout_seconds,
                "fallback_available": fallback_available,
                "fallback_source": fallback_source,
            },
        )
        inbound = self._consume(AgentEndpoint.supervisor, frozenset({"recovery_event"}))
        execution = await self.supervisor_agent.recover(
            stage=stage,
            error_type=error_code,
            message=error_code,
            attempt=attempt,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            fallback_available=fallback_available,
            fallback_source=fallback_source,
        )
        decision = AgentRecoveryDecision.model_validate(
            {
                key: execution.output[key]
                for key in AgentRecoveryDecision.model_fields
                if key in execution.output
            }
        )
        state_action = "workflow_failed" if decision.action == "fail" else "recovery_applied"
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.supervisor,
            expected_revision=self.state_revision,
            action=state_action,
            changes={
                "execution_context": {
                    "last_recovery": decision.model_dump(mode="json"),
                }
            },
            message_id=inbound.message_id,
        )
        self.state_revision = state.revision
        self._record(execution, "recovery_event", input_message=inbound)
        return decision

    async def finalize(self, result: dict[str, Any]) -> None:
        queue = self._pending.get(AgentEndpoint.supervisor)
        inbound = queue.popleft() if queue else None
        if queue is not None and not queue:
            del self._pending[AgentEndpoint.supervisor]
        state = await self.shared_state.update(
            self.trace.task_id,
            actor=AgentType.supervisor,
            expected_revision=self.state_revision,
            action="workflow_finalized",
            changes={
                "execution_context": {
                    "status": result.get("status"),
                    "planning_state": result.get("planning_state"),
                }
            },
            message_id=inbound.message_id if inbound else None,
        )
        self.state_revision = state.revision
        execution = await self.supervisor_agent.finalize(result)
        final_content = {
            **result,
            "shared_state_ref": self.trace.task_id,
            "state_revision": self.state_revision,
            "state_hash": state.state_hash,
        }
        outbound = self._dispatch(
            sender=AgentEndpoint.supervisor,
            receiver=AgentEndpoint.final_answer,
            message_type=AgentMessageType.result,
            artifact_type="final_answer",
            content=final_content,
            correlation_id=inbound.correlation_id if inbound else None,
            causation_id=inbound.message_id if inbound else None,
        )
        self._record(
            execution,
            "plan_candidate",
            input_message=inbound,
            output_message=outbound,
        )
        self.trace.shared_state = (await self.shared_state.audit(self.trace.task_id)).model_dump(
            mode="json"
        )

    def finish(self, status: str) -> AgentWorkflowTrace:
        if self.trace.status == "running":
            self.trace.status = status
        return self.trace

    async def clear_short_term_memory(self, reason: str = "planning_task_completed") -> bool:
        return await self.shared_state.delete(self.trace.task_id, reason=reason)


async def persist_agent_workflow(
    db: AsyncSession,
    *,
    user_id: int,
    trace_id: str | None,
    trace: AgentWorkflowTrace,
    trigger_type: str,
    planning_conversation_id: int | None = None,
    planning_run_id: int | None = None,
    trip_session_id: int | None = None,
) -> AgentWorkflowRun:
    now = datetime.now(timezone.utc)
    workflow = AgentWorkflowRun(
        user_id=user_id,
        planning_conversation_id=planning_conversation_id,
        planning_run_id=planning_run_id,
        trip_session_id=trip_session_id,
        trigger_type=trigger_type,
        mode=trace.mode.value,
        status=trace.status,
        trace_id=trace_id,
        handoff_count=trace.handoff_count,
        retry_count=trace.retry_count,
        estimated_cost_usd=trace.total_cost_usd,
        completed_at=now,
    )
    db.add(workflow)
    await db.flush()
    task_keys_by_role: dict[str, str] = {}
    previous_task_key: str | None = None
    for index, step in enumerate(trace.steps, start=1):
        task_key = f"{index:02d}_{step.agent_type.value}"
        task_keys_by_role.setdefault(step.agent_type.value, task_key)
        dependency_keys = [previous_task_key] if previous_task_key else []
        db.add(
            AgentWorkflowTask(
                workflow_run_id=workflow.id,
                task_key=task_key,
                role=step.agent_type.value,
                status=step.status,
                dependency_keys_json=json.dumps(dependency_keys, ensure_ascii=False),
                attempt_count=1 + int(step.fallback_used),
                input_artifact_refs_json=json.dumps(
                    [str(step.input_message_id)] if step.input_message_id else [],
                    ensure_ascii=False,
                ),
                output_artifact_type=step.output_artifact.artifact_type,
                budget_json=step.budget.model_dump_json(),
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        previous_task_key = task_key
    parent_run_id: int | None = None
    for index, step in enumerate(trace.steps, start=1):
        minimized_payload = minimize_agent_payload(step.output_artifact.payload)
        run = AgentRun(
            agent_session_id=None,
            workflow_run_id=workflow.id,
            parent_run_id=parent_run_id,
            agent_type=step.agent_type.value,
            prompt_version=step.prompt_version,
            budget_json=step.budget.model_dump_json(),
            trigger_type=trigger_type,
            status=step.status,
            trace_id=trace_id,
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            estimated_cost_usd=step.estimated_cost_usd,
            latency_ms=step.latency_ms,
            fallback_used=step.fallback_used,
            output_summary_json=json.dumps(minimized_payload, ensure_ascii=False, default=str)[
                :4000
            ],
        )
        db.add(run)
        await db.flush()
        parent_run_id = run.id
        artifact = step.output_artifact
        artifact_plan_version = None
        if planning_run_id is not None:
            candidate_version = minimized_payload.get("base_version")
            if candidate_version is None:
                candidate_version = minimized_payload.get("base_plan_version")
            if candidate_version is None:
                candidate_version = minimized_payload.get("checked_base_version")
            artifact_plan_version = int(candidate_version) if candidate_version is not None else 1
        db.add(
            AgentArtifact(
                workflow_run_id=workflow.id,
                agent_run_id=run.id,
                artifact_type=artifact.artifact_type,
                schema_version=artifact.schema_version,
                artifact_key=f"{trace.task_id}:{index:02d}:{artifact.artifact_type}",
                artifact_version=1,
                status="active",
                plan_version=artifact_plan_version,
                producer_agent=artifact.producer_agent.value,
                payload_json=json.dumps(minimized_payload, ensure_ascii=False, default=str),
                confidence=artifact.confidence,
                evidence_refs_json=json.dumps(artifact.evidence_refs, ensure_ascii=False),
                input_hash=artifact.input_hash,
                created_at=artifact.created_at,
                expires_at=artifact.expires_at,
            )
        )
    for message in trace.messages:
        source_task_key = task_keys_by_role.get(message.sender.value)
        target_task_key = task_keys_by_role.get(message.receiver.value)
        db.add(
            AgentHandoff(
                workflow_run_id=workflow.id,
                message_id=str(message.message_id),
                source_task_key=source_task_key,
                target_task_key=target_task_key,
                sender=message.sender.value,
                receiver=message.receiver.value,
                artifact_type=message.artifact_type,
                status=message.delivery_status,
                content_hash=message.content_hash,
                created_at=message.created_at,
            )
        )
        db.add(
            AgentMessageRecord(
                agent_session_id=None,
                workflow_run_id=workflow.id,
                role=message.message_type.value,
                content=message.artifact_type,
                structured_json=json.dumps(
                    message.content_summary, ensure_ascii=False, default=str
                ),
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
                delivery_status=message.delivery_status,
                created_at=message.created_at,
            )
        )
    if trace.shared_state is not None:
        shared_state = AgentSharedStateAudit.model_validate(trace.shared_state)
        db.add(
            AgentSharedStateSnapshot(
                workflow_run_id=workflow.id,
                task_id=shared_state.task_id,
                revision=shared_state.revision,
                phase=shared_state.phase.value,
                state_hash=shared_state.state_hash,
                payload_json=shared_state.model_dump_json(),
            )
        )
    await db.flush()
    trace.workflow_id = workflow.id
    return workflow
