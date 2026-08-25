from typing import Any

import pytest

from backend.app.core.config import Settings
from backend.app.schemas.agent_artifacts import AgentBudget, AgentSpec, AgentType
from backend.app.services.agent_controller import AgentController
from backend.app.services.agent_decider import AgentDecision, DecisionResult
from backend.app.services.agent_runtime import AgentRuntime, AgentRuntimeRequest
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC


class SequenceDecider:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = decisions
        self.calls = 0

    async def decide(
        self,
        *,
        trip_state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any] | None = None,
    ) -> DecisionResult:
        index = min(self.calls, len(self.decisions) - 1)
        self.calls += 1
        return DecisionResult(self.decisions[index], model_name="sequence-test")


class FailingDecider:
    async def decide(self, **_kwargs) -> DecisionResult:
        raise RuntimeError("private model endpoint and secret must not leak")


class SpecAwareDecider:
    def __init__(self) -> None:
        self.agent_type = None

    async def decide(self, **_kwargs) -> DecisionResult:
        raise AssertionError("runtime should prefer decide_for_spec")

    async def decide_for_spec(
        self, *, spec, state, observation, tool_history, tools, tool_schemas
    ) -> DecisionResult:
        self.agent_type = spec.agent_type
        return DecisionResult(
            AgentDecision(action="finish", reason=f"handled {state}"),
            model_name="spec-aware-test",
        )


def _search_runtime_spec(*, tools: frozenset[str] = frozenset()) -> AgentSpec:
    return AgentSpec(
        agent_type=AgentType.search,
        prompt_version="runtime-test-v1",
        context_view="search-runtime-test",
        allowed_tools=tools,
        input_artifact_types=frozenset({"intent_artifact"}),
        output_artifact_type="search_artifact",
        budget=AgentBudget(max_steps=3, max_cost_usd=0.01),
    )


@pytest.mark.asyncio
async def test_runtime_executes_non_companion_spec_and_all_lifecycle_ports():
    spec = _search_runtime_spec()
    emitted = []
    validated = []
    context_views = []

    async def context_loader(request, observation, history):
        context_views.append(request.spec.context_view)
        return {"observation": observation, "history": history}

    def artifact_validator(artifact):
        validated.append((artifact.producer_agent, artifact.artifact_type))

    async def state_updater(result):
        return {"revision": 2, "artifact_type": result.artifact.artifact_type}

    async def trace_emitter(event):
        emitted.append(event.event_type)

    async def executor(_tool, _arguments):
        raise AssertionError("finish decision must not execute a tool")

    runtime = AgentRuntime(
        decider=SequenceDecider(
            [AgentDecision(action="finish", reason="typed search artifact ready")]
        ),
        settings=Settings(mock_map_provider=True),
        context_loader=context_loader,
        artifact_validator=artifact_validator,
        shared_state_updater=state_updater,
        trace_emitter=trace_emitter,
    )
    result = await runtime.execute(
        AgentRuntimeRequest(
            spec=spec,
            state="searching",
            observation={"intent_artifact_ref": "artifact:intent:1"},
            input_artifact_type="intent_artifact",
            task_id="runtime-search-test",
        ),
        tool_executor=executor,
    )

    assert result.status == "succeeded"
    assert result.artifact.producer_agent == AgentType.search
    assert result.artifact.artifact_type == "search_artifact"
    assert result.shared_state == {"revision": 2, "artifact_type": "search_artifact"}
    assert context_views == ["search-runtime-test"]
    assert validated == [(AgentType.search, "search_artifact")]
    assert emitted == [
        "input_loaded",
        "decision",
        "state_updated",
        "artifact_emitted",
    ]


@pytest.mark.asyncio
async def test_runtime_registry_denies_cross_role_tool_even_if_spec_claims_it():
    spec = _search_runtime_spec(tools=frozenset({"get_weather"}))
    executed = False

    async def executor(_tool, _arguments):
        nonlocal executed
        executed = True
        return {"ok": True}

    runtime = AgentRuntime(
        decider=SequenceDecider(
            [
                AgentDecision(action="call_tool", tool="get_weather", reason="attempt"),
                AgentDecision(action="finish", reason="denied safely"),
            ]
        ),
        settings=Settings(mock_map_provider=True),
    )
    result = await runtime.execute(
        AgentRuntimeRequest(
            spec=spec,
            state="searching",
            observation={"intent_artifact_ref": "artifact:intent:1"},
            input_artifact_type="intent_artifact",
            task_id="runtime-isolation-test",
        ),
        tool_executor=executor,
    )

    assert result.status == "succeeded"
    assert result.steps[0].status == "policy_denied"
    assert result.steps[0].reason == "tool_not_allowed_for_agent"
    assert executed is False


@pytest.mark.asyncio
async def test_runtime_uses_role_neutral_fallback_and_redacts_model_failure():
    spec = _search_runtime_spec()

    async def executor(_tool, _arguments):
        raise AssertionError("safe fallback must finish")

    result = await AgentRuntime(
        decider=FailingDecider(), settings=Settings(mock_map_provider=True)
    ).execute(
        AgentRuntimeRequest(
            spec=spec,
            state="searching",
            observation={"intent_artifact_ref": "artifact:intent:1"},
            input_artifact_type="intent_artifact",
            task_id="runtime-fallback-test",
        ),
        tool_executor=executor,
    )

    serialized = result.model_dump_json()
    assert result.status == "succeeded"
    assert result.fallback_used is True
    assert result.model_name == "runtime-safe-finish-v1"
    assert "private model endpoint" not in serialized
    assert "secret" not in serialized


@pytest.mark.asyncio
async def test_runtime_rejects_input_artifact_outside_agent_spec():
    runtime = AgentRuntime(
        decider=SequenceDecider([AgentDecision(action="finish", reason="done")]),
        settings=Settings(mock_map_provider=True),
    )

    with pytest.raises(ValueError, match="cannot consume"):
        await runtime.execute(
            AgentRuntimeRequest(
                spec=_search_runtime_spec(),
                state="searching",
                observation={},
                input_artifact_type="trip_observation",
                task_id="runtime-artifact-test",
            ),
            tool_executor=lambda _tool, _args: None,  # type: ignore[arg-type,return-value]
        )


@pytest.mark.asyncio
async def test_runtime_prefers_spec_aware_model_port_for_non_companion_role():
    decider = SpecAwareDecider()

    async def executor(_tool, _arguments):
        raise AssertionError("finish decision must not execute a tool")

    result = await AgentRuntime(
        decider=decider, settings=Settings(mock_map_provider=True)
    ).execute(
        AgentRuntimeRequest(
            spec=_search_runtime_spec(),
            state="searching",
            observation={"intent_artifact_ref": "artifact:intent:2"},
            input_artifact_type="intent_artifact",
            task_id="runtime-spec-aware-test",
        ),
        tool_executor=executor,
    )

    assert decider.agent_type == AgentType.search
    assert result.model_name == "spec-aware-test"


@pytest.mark.asyncio
async def test_runtime_validates_and_executes_authorized_tool_with_result_envelope():
    called = []

    async def executor(tool, arguments):
        called.append((tool, arguments))
        return {"trip_state": "active_trip", "source": "runtime-test"}

    runtime = AgentRuntime(
        decider=SequenceDecider(
            [
                AgentDecision(
                    action="call_tool", tool="get_trip_state", reason="read state"
                ),
                AgentDecision(action="finish", reason="state loaded"),
            ]
        ),
        settings=Settings(mock_map_provider=True),
    )
    result = await runtime.execute(
        AgentRuntimeRequest(
            spec=COMPANION_AGENT_SPEC,
            state="active_trip",
            observation={"event_type": "ScheduleDelayDetected"},
            input_artifact_type="trip_observation",
            task_id="runtime-tool-success",
        ),
        tool_executor=executor,
    )

    assert called == [("get_trip_state", {})]
    assert result.steps[0].status == "succeeded"
    assert result.steps[0].output["success"] is True
    assert result.steps[0].output["source"] == "runtime-test"


def test_agent_controller_is_compatibility_facade_over_generic_runtime():
    assert hasattr(AgentController, "execute")
    assert hasattr(AgentController, "run_once")
