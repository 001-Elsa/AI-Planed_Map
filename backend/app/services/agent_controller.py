"""Observation → LLM decision → Policy → Tool → Observation controller."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.models import (
    AgentArtifact,
    AgentRun,
    AgentSession,
    AgentSharedStateSnapshot,
    AgentToolCall,
    AgentWorkflowRun,
    TripSession,
)
from backend.app.models import (
    AgentMessage as AgentMessageRecord,
)
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
from backend.app.services.agent_policy import TOOL_POLICIES, evaluate_tool_policy
from backend.app.services.agent_protocol import AgentMessageRouter
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.agent_tool_contracts import (
    stable_tool_error,
    tool_argument_schemas_for,
    tool_result_error,
    tool_result_success,
    validate_tool_arguments,
)
from backend.app.services.agent_tool_registry import (
    TOOL_REGISTRY,
    CapabilityAuthorizationError,
    InvocationMode,
)
from backend.app.services.agents.base import canonical_hash
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class AgentController:
    """Executes a bounded, auditable tool loop without plan mutation powers."""

    def __init__(
        self,
        db: AsyncSession,
        decider: AgentDecider | None = None,
        spec: AgentSpec = COMPANION_AGENT_SPEC,
        shared_state: AgentSharedStateManager | None = None,
    ) -> None:
        self.db = db
        self.decider = decider or RuleBasedAgentDecider()
        if spec.agent_type != AgentType.companion:
            raise ValueError("AgentController only accepts the isolated companion spec")
        self.spec = spec
        self.shared_state = shared_state

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
        settings = get_settings()
        started = time.perf_counter()
        limit = min(
            max_steps or settings.max_agent_steps,
            settings.max_agent_steps,
            self.spec.budget.max_steps,
        )
        steps: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        fallback_used = False
        router = AgentMessageRouter()
        task_id = f"trip-{trip.id}-state" if self.shared_state else f"trip-{uuid4()}"
        protocol_message_count = 0
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
        workflow = AgentWorkflowRun(
            user_id=trip.user_id,
            trip_session_id=trip.id,
            trigger_type=str(observation.get("trigger") or "controller"),
            mode="enforce",
            status="running",
            trace_id=trace_id,
        )
        self.db.add(workflow)
        await self.db.flush()
        observation_artifact = ArtifactEnvelope(
            artifact_type="trip_observation",
            producer_agent=AgentType.companion,
            payload=observation,
            confidence=1,
            evidence_refs=[f"trip:{trip.id}"],
            input_hash=canonical_hash(observation),
        )
        self.db.add(
            AgentArtifact(
                workflow_run_id=workflow.id,
                agent_run_id=None,
                artifact_type=observation_artifact.artifact_type,
                schema_version=observation_artifact.schema_version,
                producer_agent=observation_artifact.producer_agent.value,
                payload_json=json.dumps(
                    minimize_agent_payload(observation), ensure_ascii=False, default=str
                ),
                confidence=observation_artifact.confidence,
                evidence_refs_json=json.dumps(observation_artifact.evidence_refs),
                input_hash=observation_artifact.input_hash,
                created_at=observation_artifact.created_at,
            )
        )
        run = AgentRun(
            agent_session_id=agent.id,
            workflow_run_id=workflow.id,
            agent_type=self.spec.agent_type.value,
            prompt_version=self.spec.prompt_version,
            budget_json=self.spec.budget.model_dump_json(),
            trigger_type=str(observation.get("trigger") or "controller"),
            status="running",
            trace_id=trace_id,
        )
        self.db.add(run)
        last_message = router.build(
            task_id=task_id,
            sender=AgentEndpoint.system,
            receiver=AgentEndpoint.companion,
            message_type=AgentMessageType.event,
            artifact_type="trip_observation",
            content=observation,
        )
        last_message, delivery_status = router.deliver(last_message)
        self.db.add(
            self._message_record(
                last_message,
                router,
                agent_session_id=agent.id,
                workflow_run_id=workflow.id,
                delivery_status=delivery_status,
            )
        )
        protocol_message_count += 1
        await self.db.flush()

        status = "succeeded"
        allowed_tool_names = sorted(set(TOOL_POLICIES) & set(self.spec.allowed_tools))
        tool_schemas = tool_argument_schemas_for(allowed_tool_names)
        for step_index in range(limit):
            try:
                decision_observation = build_companion_context(
                    trip=trip,
                    observation=observation,
                    route_plan=route_plan,
                    tool_history=history,
                )
                result = await self.decider.decide(
                    trip_state=trip.state,
                    observation=decision_observation,
                    tool_history=history,
                    tools=allowed_tool_names,
                    tool_schemas=tool_schemas,
                )
            except Exception as exc:  # noqa: BLE001 - preserve the operational loop on LLM fault
                fallback_used = True
                decision_observation = build_companion_context(
                    trip=trip,
                    observation=observation,
                    route_plan=route_plan,
                    tool_history=history,
                )
                result = await RuleBasedAgentDecider().decide(
                    trip_state=trip.state,
                    observation=decision_observation,
                    tool_history=history,
                    tools=sorted(set(TOOL_POLICIES) & set(self.spec.allowed_tools)),
                    tool_schemas=tool_schemas,
                )
                fallback_message = router.build(
                    task_id=task_id,
                    sender=AgentEndpoint.system,
                    receiver=AgentEndpoint.companion,
                    message_type=AgentMessageType.error,
                    artifact_type="recovery_event",
                    content={"error_type": type(exc).__name__, "reason": str(exc)[:300]},
                    correlation_id=last_message.correlation_id,
                    causation_id=last_message.message_id,
                )
                fallback_message, delivery_status = router.deliver(fallback_message)
                self.db.add(
                    self._message_record(
                        fallback_message,
                        router,
                        agent_session_id=agent.id,
                        workflow_run_id=workflow.id,
                        delivery_status=delivery_status,
                    )
                )
                last_message = fallback_message
                protocol_message_count += 1
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            agent.model_name = result.model_name
            estimated_cost = self._estimated_cost(input_tokens, output_tokens)
            if (
                input_tokens
                > min(settings.max_agent_input_tokens, self.spec.budget.max_input_tokens)
                or output_tokens
                > min(settings.max_agent_output_tokens, self.spec.budget.max_output_tokens)
                or estimated_cost
                > min(settings.max_agent_run_cost_usd, self.spec.budget.max_cost_usd)
            ):
                status = "budget_exceeded"
                steps.append({"status": status, "reason": "agent_token_or_cost_budget"})
                break

            decision = result.decision
            if decision.action == "finish":
                steps.append({"status": "finished", "reason": decision.reason})
                break

            tool = str(decision.tool)
            raw_arguments = decision.arguments
            tool_request_message = router.build(
                task_id=task_id,
                sender=AgentEndpoint.companion,
                receiver=AgentEndpoint.tool_runtime,
                message_type=AgentMessageType.tool_request,
                artifact_type="tool_request",
                content={"tool": tool, "arguments": raw_arguments, "step_index": step_index},
                correlation_id=last_message.correlation_id,
                causation_id=last_message.message_id,
            )
            tool_request_message, delivery_status = router.deliver(tool_request_message)
            self.db.add(
                self._message_record(
                    tool_request_message,
                    router,
                    agent_session_id=agent.id,
                    workflow_run_id=workflow.id,
                    delivery_status=delivery_status,
                )
            )
            protocol_message_count += 1
            state = TripState(trip.state)
            capability = TOOL_REGISTRY.get(tool)
            try:
                TOOL_REGISTRY.authorize(
                    agent_type=self.spec.agent_type,
                    capability=tool,
                    invocation_mode=InvocationMode.agent_callable,
                    requested_scopes=(
                        capability.data_scopes if capability is not None else frozenset()
                    ),
                )
            except CapabilityAuthorizationError as exc:
                allowed, policy_reason, confirmation_required = False, exc.reason, False
            else:
                allowed, policy_reason, confirmation_required = evaluate_tool_policy(
                    tool, state, consents
                )
            run_calls = int(
                await self.db.scalar(
                    select(func.count(AgentToolCall.id)).where(AgentToolCall.agent_run_id == run.id)
                )
                or 0
            )
            task_calls = int(
                await self.db.scalar(
                    select(func.count(AgentToolCall.id))
                    .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
                    .where(AgentRun.workflow_run_id == workflow.id)
                )
                or 0
            )
            trip_calls = int(
                await self.db.scalar(
                    select(func.count(AgentToolCall.id))
                    .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
                    .where(AgentRun.agent_session_id == agent.id)
                )
                or 0
            )
            if (
                run_calls >= settings.max_agent_tool_calls_per_run
                or task_calls >= settings.max_agent_tool_calls_per_task
                or trip_calls >= settings.max_agent_tool_calls_per_trip
            ):
                status = "budget_exceeded"
                steps.append(
                    {
                        "tool": tool,
                        "status": status,
                        "reason": "agent_tool_call_budget",
                        "budgets": {
                            "run_calls": run_calls,
                            "task_calls": task_calls,
                            "trip_calls": trip_calls,
                        },
                    }
                )
                break
            if not allowed or confirmation_required:
                denial = policy_reason if not allowed else "requires_user_confirmation"
                await self._record_tool_call(
                    run, tool, raw_arguments, {"reason": denial}, "policy_denied", denial, trace_id
                )
                steps.append({"tool": tool, "status": "policy_denied", "reason": denial})
                history.append(
                    {"tool": tool, "status": "policy_denied", "output": {"reason": denial}}
                )
                tool_result_message = router.build(
                    task_id=task_id,
                    sender=AgentEndpoint.tool_runtime,
                    receiver=AgentEndpoint.companion,
                    message_type=AgentMessageType.tool_result,
                    artifact_type="tool_result",
                    content={"tool": tool, "status": "policy_denied", "reason": denial},
                    correlation_id=tool_request_message.correlation_id,
                    causation_id=tool_request_message.message_id,
                )
                tool_result_message, delivery_status = router.deliver(tool_result_message)
                self.db.add(
                    self._message_record(
                        tool_result_message,
                        router,
                        agent_session_id=agent.id,
                        workflow_run_id=workflow.id,
                        delivery_status=delivery_status,
                    )
                )
                last_message = tool_result_message
                protocol_message_count += 1
                # Let the LLM observe a refusal once, then it must make a new decision.
                observation = {**observation, "last_tool": tool, "last_output": {"reason": denial}}
                continue

            try:
                arguments = validate_tool_arguments(tool, raw_arguments)
            except Exception:
                output = tool_result_error(
                    "INVALID_TOOL_ARGUMENTS",
                    retryable=False,
                    data={"tool": tool},
                ).model_dump(mode="json")
                await self._record_tool_call(
                    run,
                    tool,
                    raw_arguments,
                    output,
                    "validation_failed",
                    "invalid_tool_arguments",
                    trace_id,
                )
                steps.append({"tool": tool, "status": "validation_failed", "output": output})
                history.append({"tool": tool, "status": "validation_failed", "output": output})
                observation = {**observation, "last_tool": tool, "last_output": output}
                continue

            tool_started = time.perf_counter()
            try:
                raw_output = await tool_executor(tool, arguments)
                output = tool_result_success(
                    tool,
                    raw_output,
                    source=str(raw_output.get("source") or "mapgo"),
                    confidence=float(raw_output.get("confidence") or 1.0),
                    artifact_ref=(
                        str(raw_output.get("artifact_ref"))
                        if raw_output.get("artifact_ref") is not None
                        else None
                    ),
                ).model_dump(mode="json")
                call_status, error_type = "succeeded", None
            except Exception as exc:  # noqa: BLE001 - must audit tool failures
                error_type = stable_tool_error(exc)
                output = tool_result_error(error_type, retryable=error_type == "UPSTREAM_TIMEOUT").model_dump(
                    mode="json"
                )
                call_status = "failed"
            await self._record_tool_call(
                run,
                tool,
                arguments,
                output,
                call_status,
                error_type,
                trace_id,
                latency_ms=int((time.perf_counter() - tool_started) * 1000),
            )
            steps.append({"tool": tool, "status": call_status, "output": output})
            history.append({"tool": tool, "status": call_status, "output": output})
            tool_result_message = router.build(
                task_id=task_id,
                sender=AgentEndpoint.tool_runtime,
                receiver=AgentEndpoint.companion,
                message_type=AgentMessageType.tool_result,
                artifact_type="tool_result",
                content={"tool": tool, "status": call_status, "output": output},
                correlation_id=tool_request_message.correlation_id,
                causation_id=tool_request_message.message_id,
            )
            tool_result_message, delivery_status = router.deliver(tool_result_message)
            self.db.add(
                self._message_record(
                    tool_result_message,
                    router,
                    agent_session_id=agent.id,
                    workflow_run_id=workflow.id,
                    delivery_status=delivery_status,
                )
            )
            last_message = tool_result_message
            protocol_message_count += 1
            observation = {**observation, "last_tool": tool, "last_output": output}
            if call_status == "failed":
                status = "tool_failed"
                break
        else:
            status = "step_limit_reached"

        run.status = status
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.estimated_cost_usd = self._estimated_cost(input_tokens, output_tokens)
        run.latency_ms = int((time.perf_counter() - started) * 1000)
        run.fallback_used = fallback_used
        run.output_summary_json = json.dumps(
            minimize_agent_payload({"status": status, "steps": steps}),
            ensure_ascii=False,
            default=str,
        )[:4000]
        workflow.status = status
        workflow.handoff_count = protocol_message_count + 1
        workflow.estimated_cost_usd = run.estimated_cost_usd or 0
        workflow.completed_at = datetime.now(timezone.utc)
        result_artifact = ArtifactEnvelope(
            artifact_type=self.spec.output_artifact_type,
            producer_agent=AgentType.companion,
            payload={"status": status, "steps": steps},
            confidence=1 if status == "succeeded" else 0.5,
            evidence_refs=[f"trip:{trip.id}", f"agent_run:{run.id}"],
            input_hash=observation_artifact.input_hash,
        )
        self.db.add(
            AgentArtifact(
                workflow_run_id=workflow.id,
                agent_run_id=run.id,
                artifact_type=result_artifact.artifact_type,
                schema_version=result_artifact.schema_version,
                producer_agent=result_artifact.producer_agent.value,
                payload_json=json.dumps(
                    minimize_agent_payload(result_artifact.payload),
                    ensure_ascii=False,
                    default=str,
                ),
                confidence=result_artifact.confidence,
                evidence_refs_json=json.dumps(result_artifact.evidence_refs),
                input_hash=result_artifact.input_hash,
                created_at=result_artifact.created_at,
            )
        )
        if self.shared_state is not None and shared_state_revision is not None:
            current_shared = await self.shared_state.read(task_id)
            action = (
                "trip_completed"
                if trip.state == TripState.completed.value
                else "trip_started"
                if current_shared.phase.value in {"plan_ready", "finalized"}
                else "trip_event_processed"
            )
            updated_shared = await self.shared_state.update(
                task_id,
                actor=AgentType.companion,
                expected_revision=shared_state_revision,
                action=action,
                changes={
                    "execution_context": {
                        "trip_id": trip.id,
                        "trip_state": trip.state,
                        "plan_version": trip.current_plan_version,
                        "last_run_status": status,
                        "last_trigger": observation.get("trigger"),
                        "tool_step_count": len(steps),
                    }
                },
                message_id=last_message.message_id,
            )
            shared_state_revision = updated_shared.revision
            shared_state_hash = updated_shared.state_hash
        final_content: dict[str, Any] = {"status": status, "steps": steps}
        if shared_state_revision is not None:
            final_content.update(
                {
                    "shared_state_ref": task_id,
                    "state_revision": shared_state_revision,
                    "state_hash": shared_state_hash,
                }
            )
        final_message = router.build(
            task_id=task_id,
            sender=AgentEndpoint.companion,
            receiver=AgentEndpoint.final_answer,
            message_type=AgentMessageType.result,
            artifact_type="companion_action_report",
            content=final_content,
            correlation_id=last_message.correlation_id,
            causation_id=last_message.message_id,
        )
        final_message, delivery_status = router.deliver(final_message)
        self.db.add(
            self._message_record(
                final_message,
                router,
                agent_session_id=agent.id,
                workflow_run_id=workflow.id,
                delivery_status=delivery_status,
            )
        )
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
            await asyncio.shield(
                self.shared_state.delete(task_id, reason="trip_completed")
            )
        return {
            "status": status,
            "steps": steps,
            "run_id": run.id,
            "workflow_id": workflow.id,
            "agent_type": self.spec.agent_type.value,
        }

    @staticmethod
    def _estimated_cost(input_tokens: int, output_tokens: int) -> float:
        settings = get_settings()
        return (
            input_tokens * settings.llm_input_cost_per_million_usd
            + output_tokens * settings.llm_output_cost_per_million_usd
        ) / 1_000_000

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
