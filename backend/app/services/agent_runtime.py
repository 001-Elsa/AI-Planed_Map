"""Framework-independent bounded runtime for model-driven Agent tool loops."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import Field

from backend.app.core.config import Settings, get_settings
from backend.app.core.observability import metrics
from backend.app.schemas.agent_artifacts import AgentSpec, ArtifactEnvelope, minimize_agent_payload
from backend.app.schemas.common import StrictModel
from backend.app.services.agent_decider import (
    AgentDecider,
    AgentDecision,
    DecisionResult,
)
from backend.app.services.agent_tool_contracts import (
    stable_tool_error,
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

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
ContextLoader = Callable[
    ["AgentRuntimeRequest", dict[str, Any], list[dict[str, Any]]],
    Awaitable[dict[str, Any]],
]
ToolPolicyEvaluator = Callable[[str, str], tuple[bool, str, bool]]
ArtifactValidator = Callable[[ArtifactEnvelope], None]
SharedStateUpdater = Callable[["AgentRuntimeResult"], Awaitable[dict[str, Any] | None]]
TraceEmitter = Callable[["AgentRuntimeEvent"], Awaitable[None]]


class ToolBudgetSnapshot(StrictModel):
    run_calls: int = Field(default=0, ge=0)
    task_calls: int = Field(default=0, ge=0)
    scope_calls: int = Field(default=0, ge=0)


class AgentRuntimeRequest(StrictModel):
    spec: AgentSpec
    state: str = Field(min_length=1, max_length=60)
    observation: dict[str, Any]
    input_artifact_type: str = Field(min_length=1, max_length=80)
    task_id: str = Field(default_factory=lambda: f"agent-{uuid4()}", min_length=8, max_length=128)
    trigger_type: str = Field(default="runtime", min_length=1, max_length=60)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    max_steps: int | None = Field(default=None, ge=1, le=20)
    tool_budget: ToolBudgetSnapshot = Field(default_factory=ToolBudgetSnapshot)


class AgentRuntimeStep(StrictModel):
    step_index: int = Field(ge=0)
    status: Literal[
        "finished",
        "policy_denied",
        "validation_failed",
        "succeeded",
        "failed",
        "budget_exceeded",
    ]
    reason: str | None = Field(default=None, max_length=300)
    tool: str | None = Field(default=None, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = Field(default=None, ge=0)


class AgentRuntimeEvent(StrictModel):
    event_type: Literal[
        "input_loaded",
        "model_fallback",
        "decision",
        "tool_request",
        "tool_result",
        "state_updated",
        "artifact_emitted",
    ]
    step_index: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentRuntimeResult(StrictModel):
    status: Literal["succeeded", "budget_exceeded", "tool_failed", "step_limit_reached"]
    steps: list[AgentRuntimeStep]
    artifact: ArtifactEnvelope
    events: list[AgentRuntimeEvent]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0, ge=0)
    model_name: str = Field(default="unknown", max_length=100)
    latency_ms: int = Field(default=0, ge=0)
    fallback_used: bool = False
    shared_state: dict[str, Any] | None = None


class SpecAwareDecider(Protocol):
    async def decide_for_spec(
        self,
        *,
        spec: AgentSpec,
        state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any],
    ) -> DecisionResult: ...


class SafeFinishAgentDecider:
    """Role-neutral fallback: finish safely instead of borrowing Companion policy."""

    async def decide(
        self,
        *,
        trip_state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any] | None = None,
    ) -> DecisionResult:
        return DecisionResult(
            decision=AgentDecision(
                action="finish",
                reason="model unavailable; role-neutral runtime fallback stopped safely",
            ),
            model_name="runtime-safe-finish-v1",
        )


class AgentRuntime:
    """Runs any AgentSpec through the same bounded and auditable tool loop."""

    def __init__(
        self,
        *,
        decider: AgentDecider,
        fallback_decider: AgentDecider | None = None,
        settings: Settings | None = None,
        context_loader: ContextLoader | None = None,
        policy_evaluator: ToolPolicyEvaluator | None = None,
        artifact_validator: ArtifactValidator | None = None,
        shared_state_updater: SharedStateUpdater | None = None,
        trace_emitter: TraceEmitter | None = None,
    ) -> None:
        self.decider = decider
        self.fallback_decider = fallback_decider or SafeFinishAgentDecider()
        self.settings = settings or get_settings()
        self.context_loader = context_loader
        self.policy_evaluator = policy_evaluator or self._allow_registered_tool
        self.artifact_validator = artifact_validator
        self.shared_state_updater = shared_state_updater
        self.trace_emitter = trace_emitter

    async def execute(
        self, request: AgentRuntimeRequest, *, tool_executor: ToolExecutor
    ) -> AgentRuntimeResult:
        started = time.perf_counter()
        self._validate_input(request)
        limit = min(
            request.max_steps or self.settings.max_agent_steps,
            self.settings.max_agent_steps,
            request.spec.budget.max_steps,
        )
        steps: list[AgentRuntimeStep] = []
        history: list[dict[str, Any]] = []
        events: list[AgentRuntimeEvent] = []
        observation = dict(request.observation)
        input_tokens = 0
        output_tokens = 0
        fallback_used = False
        model_name = "unknown"
        input_cost_per_million_usd = 0.0
        output_cost_per_million_usd = 0.0
        status: Literal["succeeded", "budget_exceeded", "tool_failed", "step_limit_reached"] = (
            "succeeded"
        )
        allowed_tools = sorted(request.spec.allowed_tools)
        tool_schemas = TOOL_REGISTRY.argument_schemas_for(
            request.spec.agent_type, InvocationMode.agent_callable
        )
        await self.emit_trace(
            AgentRuntimeEvent(
                event_type="input_loaded",
                payload={
                    "agent_type": request.spec.agent_type.value,
                    "context_view": request.spec.context_view,
                    "input_artifact_type": request.input_artifact_type,
                },
            ),
            events,
        )

        for step_index in range(limit):
            context = await self.load_context(request, observation, history)
            try:
                decision_result = await self.call_model(
                    self.decider,
                    request=request,
                    context=context,
                    history=history,
                    tools=allowed_tools,
                    tool_schemas=tool_schemas,
                )
            except Exception as exc:  # noqa: BLE001 - fallback is an explicit runtime boundary
                fallback_used = True
                await self.emit_trace(
                    AgentRuntimeEvent(
                        event_type="model_fallback",
                        step_index=step_index,
                        payload={"error_code": stable_tool_error(exc)},
                    ),
                    events,
                )
                decision_result = await self.call_model(
                    self.fallback_decider,
                    request=request,
                    context=context,
                    history=history,
                    tools=allowed_tools,
                    tool_schemas=tool_schemas,
                )
            input_tokens += decision_result.input_tokens
            output_tokens += decision_result.output_tokens
            model_name = decision_result.model_name
            input_cost_per_million_usd = decision_result.input_cost_per_million_usd
            output_cost_per_million_usd = decision_result.output_cost_per_million_usd
            estimated_cost = self._estimated_cost(
                input_tokens,
                output_tokens,
                input_cost_per_million_usd=input_cost_per_million_usd,
                output_cost_per_million_usd=output_cost_per_million_usd,
            )
            if self._model_budget_exceeded(
                request.spec, input_tokens, output_tokens, estimated_cost
            ):
                status = "budget_exceeded"
                steps.append(
                    AgentRuntimeStep(
                        step_index=step_index,
                        status="budget_exceeded",
                        reason="agent_token_or_cost_budget",
                    )
                )
                break

            decision = decision_result.decision
            await self.emit_trace(
                AgentRuntimeEvent(
                    event_type="decision",
                    step_index=step_index,
                    payload={
                        "action": decision.action,
                        "tool": decision.tool,
                        "reason": decision.reason,
                    },
                ),
                events,
            )
            if decision.action == "finish":
                steps.append(
                    AgentRuntimeStep(
                        step_index=step_index,
                        status="finished",
                        reason=decision.reason,
                    )
                )
                break

            tool = str(decision.tool)
            raw_arguments = dict(decision.arguments)
            await self.emit_trace(
                AgentRuntimeEvent(
                    event_type="tool_request",
                    step_index=step_index,
                    payload={"tool": tool, "arguments": minimize_agent_payload(raw_arguments)},
                ),
                events,
            )
            allowed, reason, confirmation_required = self.authorize_tool(
                request.spec, tool, request.state
            )
            if self._tool_budget_exceeded(request, len([item for item in steps if item.tool])):
                status = "budget_exceeded"
                steps.append(
                    AgentRuntimeStep(
                        step_index=step_index,
                        status="budget_exceeded",
                        reason="agent_tool_call_budget",
                        tool=tool,
                        arguments=raw_arguments,
                    )
                )
                break
            if not allowed or confirmation_required:
                denial = reason if not allowed else "requires_user_confirmation"
                output = {"reason": denial}
                step = AgentRuntimeStep(
                    step_index=step_index,
                    status="policy_denied",
                    reason=denial,
                    tool=tool,
                    arguments=raw_arguments,
                    output=output,
                )
                steps.append(step)
                history.append(step.model_dump(mode="json"))
                observation = {**observation, "last_tool": tool, "last_output": output}
                await self.emit_trace(
                    AgentRuntimeEvent(
                        event_type="tool_result",
                        step_index=step_index,
                        payload={"tool": tool, "status": step.status, "output": output},
                    ),
                    events,
                )
                continue

            try:
                arguments = validate_tool_arguments(tool, raw_arguments)
            except Exception:
                output = tool_result_error(
                    "INVALID_TOOL_ARGUMENTS", retryable=False, data={"tool": tool}
                ).model_dump(mode="json")
                step = AgentRuntimeStep(
                    step_index=step_index,
                    status="validation_failed",
                    reason="invalid_tool_arguments",
                    tool=tool,
                    arguments=raw_arguments,
                    output=output,
                )
                steps.append(step)
                history.append(step.model_dump(mode="json"))
                observation = {**observation, "last_tool": tool, "last_output": output}
                await self.emit_trace(
                    AgentRuntimeEvent(
                        event_type="tool_result",
                        step_index=step_index,
                        payload={"tool": tool, "status": step.status, "output": output},
                    ),
                    events,
                )
                continue

            call_status: Literal["succeeded", "failed"]
            error_type: str | None
            tool_started = time.perf_counter()
            try:
                output = await self.execute_tool(tool, arguments, tool_executor)
                call_status, error_type = "succeeded", None
            except Exception as exc:  # noqa: BLE001 - runtime must convert executor failures
                error_type = stable_tool_error(exc)
                output = tool_result_error(
                    error_type, retryable=error_type == "UPSTREAM_TIMEOUT"
                ).model_dump(mode="json")
                call_status = "failed"
            step = AgentRuntimeStep(
                step_index=step_index,
                status=call_status,
                reason=error_type,
                tool=tool,
                arguments=arguments,
                output=output,
                latency_ms=int((time.perf_counter() - tool_started) * 1000),
            )
            steps.append(step)
            history.append(step.model_dump(mode="json"))
            observation = {**observation, "last_tool": tool, "last_output": output}
            await self.emit_trace(
                AgentRuntimeEvent(
                    event_type="tool_result",
                    step_index=step_index,
                    payload={"tool": tool, "status": call_status, "output": output},
                ),
                events,
            )
            if call_status == "failed":
                status = "tool_failed"
                break
        else:
            status = "step_limit_reached"

        artifact = ArtifactEnvelope(
            artifact_type=request.spec.output_artifact_type,
            producer_agent=request.spec.agent_type,
            payload={
                "status": status,
                "steps": [item.model_dump(mode="json") for item in steps],
            },
            confidence=1 if status == "succeeded" else 0.5,
            evidence_refs=request.evidence_refs,
            input_hash=canonical_hash(request.observation),
        )
        self.validate_artifact(request.spec, artifact)
        result = AgentRuntimeResult(
            status=status,
            steps=steps,
            artifact=artifact,
            events=events,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=self._estimated_cost(
                input_tokens,
                output_tokens,
                input_cost_per_million_usd=input_cost_per_million_usd,
                output_cost_per_million_usd=output_cost_per_million_usd,
            ),
            model_name=model_name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=fallback_used,
        )
        route_tier = model_name.split(":", 1)[0] if ":" in model_name else "unrouted"
        metrics.observe(
            "mapgo_model_router_actual_cost_usd",
            result.estimated_cost_usd,
            {"agent": request.spec.agent_type.value, "tier": route_tier},
        )
        metrics.observe(
            "mapgo_model_router_latency_ms",
            result.latency_ms,
            {"agent": request.spec.agent_type.value, "tier": route_tier},
        )
        state_update = await self.update_shared_state(result)
        if state_update is not None:
            result.shared_state = state_update
            await self.emit_trace(
                AgentRuntimeEvent(event_type="state_updated", payload=state_update), events
            )
        await self.emit_trace(
            AgentRuntimeEvent(
                event_type="artifact_emitted",
                payload={
                    "artifact_type": artifact.artifact_type,
                    "producer_agent": artifact.producer_agent.value,
                },
            ),
            events,
        )
        result.events = list(events)
        return result

    async def load_context(
        self,
        request: AgentRuntimeRequest,
        observation: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.context_loader is not None:
            return await self.context_loader(request, observation, history)
        return {
            "agent": {
                "type": request.spec.agent_type.value,
                "context_view": request.spec.context_view,
                "task_id": request.task_id,
            },
            "current_observation": minimize_agent_payload(observation),
            "recent_tool_results": [minimize_agent_payload(item) for item in history[-5:]],
        }

    def authorize_tool(self, spec: AgentSpec, tool: str, state: str) -> tuple[bool, str, bool]:
        capability = TOOL_REGISTRY.get(tool)
        try:
            TOOL_REGISTRY.authorize(
                agent_type=spec.agent_type,
                capability=tool,
                invocation_mode=InvocationMode.agent_callable,
                requested_scopes=(capability.data_scopes if capability else frozenset()),
            )
        except CapabilityAuthorizationError as exc:
            return False, exc.reason, False
        return self.policy_evaluator(tool, state)

    async def call_model(
        self,
        decider: AgentDecider,
        *,
        request: AgentRuntimeRequest,
        context: dict[str, Any],
        history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any],
    ) -> DecisionResult:
        spec_aware = getattr(decider, "decide_for_spec", None)
        if callable(spec_aware):
            return await spec_aware(
                spec=request.spec,
                state=request.state,
                observation=context,
                tool_history=history,
                tools=tools,
                tool_schemas=tool_schemas,
            )
        return await decider.decide(
            trip_state=request.state,
            observation=context,
            tool_history=history,
            tools=tools,
            tool_schemas=tool_schemas,
        )

    @staticmethod
    async def execute_tool(
        tool: str, arguments: dict[str, Any], executor: ToolExecutor
    ) -> dict[str, Any]:
        raw_output = await executor(tool, arguments)
        return tool_result_success(
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

    def validate_artifact(self, spec: AgentSpec, artifact: ArtifactEnvelope) -> None:
        if artifact.producer_agent != spec.agent_type:
            raise ValueError("runtime artifact producer does not match AgentSpec")
        if artifact.artifact_type != spec.output_artifact_type:
            raise ValueError("runtime artifact type does not match AgentSpec")
        if self.artifact_validator is not None:
            self.artifact_validator(artifact)

    async def update_shared_state(self, result: AgentRuntimeResult) -> dict[str, Any] | None:
        if self.shared_state_updater is None:
            return None
        return await self.shared_state_updater(result)

    async def emit_trace(self, event: AgentRuntimeEvent, events: list[AgentRuntimeEvent]) -> None:
        events.append(event)
        if self.trace_emitter is not None:
            await self.trace_emitter(event)

    def _validate_input(self, request: AgentRuntimeRequest) -> None:
        if request.input_artifact_type not in request.spec.input_artifact_types:
            raise ValueError(
                f"{request.spec.agent_type.value} cannot consume {request.input_artifact_type}"
            )

    def _model_budget_exceeded(
        self,
        spec: AgentSpec,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
    ) -> bool:
        return (
            input_tokens > min(self.settings.max_agent_input_tokens, spec.budget.max_input_tokens)
            or output_tokens
            > min(self.settings.max_agent_output_tokens, spec.budget.max_output_tokens)
            or estimated_cost > min(self.settings.max_agent_run_cost_usd, spec.budget.max_cost_usd)
        )

    def _tool_budget_exceeded(self, request: AgentRuntimeRequest, local_calls: int) -> bool:
        budget = request.tool_budget
        return (
            budget.run_calls + local_calls >= self.settings.max_agent_tool_calls_per_run
            or budget.task_calls + local_calls >= self.settings.max_agent_tool_calls_per_task
            or budget.scope_calls + local_calls >= self.settings.max_agent_tool_calls_per_trip
        )

    def _estimated_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        input_cost_per_million_usd: float = 0,
        output_cost_per_million_usd: float = 0,
    ) -> float:
        input_price = input_cost_per_million_usd or self.settings.llm_input_cost_per_million_usd
        output_price = output_cost_per_million_usd or self.settings.llm_output_cost_per_million_usd
        return (input_tokens * input_price + output_tokens * output_price) / 1_000_000

    @staticmethod
    def _allow_registered_tool(_tool: str, _state: str) -> tuple[bool, str, bool]:
        return True, "allowed", False
