import pytest

from backend.app.schemas.agent_artifacts import AgentType
from backend.app.services.agent_policy import TOOL_POLICIES
from backend.app.services.agent_tool_contracts import (
    tool_argument_schemas_for,
    validate_tool_arguments,
)
from backend.app.services.agent_tool_registry import (
    TOOL_REGISTRY,
    CapabilityAuthorizationError,
    DataScope,
    InvocationMode,
)
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC
from backend.app.services.agents.critic_agent import CRITIC_AGENT_SPEC
from backend.app.services.agents.intent_agent import INTENT_AGENT_SPEC
from backend.app.services.agents.planner_agent import PLANNER_AGENT_SPEC
from backend.app.services.agents.safety_agent import SAFETY_AGENT_SPEC
from backend.app.services.agents.search_agent import SEARCH_AGENT_SPEC
from backend.app.services.agents.supervisor_agent import SUPERVISOR_AGENT_SPEC


def _authorize(
    agent_type: AgentType,
    capability: str,
    mode: InvocationMode,
    *scopes: DataScope,
):
    return TOOL_REGISTRY.authorize(
        agent_type=agent_type,
        capability=capability,
        invocation_mode=mode,
        requested_scopes=frozenset(scopes),
    )


def test_registry_is_the_single_source_for_agent_manifests():
    assert INTENT_AGENT_SPEC.allowed_internal_capabilities == frozenset(
        {"parse_requirement"}
    )
    assert SEARCH_AGENT_SPEC.allowed_internal_capabilities == frozenset({"search_poi"})
    assert SAFETY_AGENT_SPEC.allowed_internal_capabilities == frozenset(
        {"check_travel_safety"}
    )
    assert PLANNER_AGENT_SPEC.allowed_internal_capabilities == frozenset(
        {"get_route_matrix", "optimize_route", "verify_transit_edges"}
    )
    assert COMPANION_AGENT_SPEC.allowed_tools == frozenset(
        {"get_trip_state", "get_current_location", "get_weather", "propose_replan"}
    )
    assert SUPERVISOR_AGENT_SPEC.allowed_internal_capabilities == frozenset()
    assert CRITIC_AGENT_SPEC.allowed_internal_capabilities == frozenset()
    assert COMPANION_AGENT_SPEC.allowed_tools == frozenset(TOOL_POLICIES)
    assert "search_poi" not in TOOL_POLICIES
    assert "create_plan_patch" not in TOOL_POLICIES


def test_internal_capabilities_are_not_exposed_as_llm_tools():
    assert not INTENT_AGENT_SPEC.allowed_tools
    assert not SEARCH_AGENT_SPEC.allowed_tools
    assert not SAFETY_AGENT_SPEC.allowed_tools
    assert not PLANNER_AGENT_SPEC.allowed_tools
    assert not CRITIC_AGENT_SPEC.allowed_tools
    assert not SUPERVISOR_AGENT_SPEC.allowed_tools

    with pytest.raises(CapabilityAuthorizationError) as caught:
        _authorize(
            AgentType.planner,
            "optimize_route",
            InvocationMode.agent_callable,
            DataScope.route_optimization,
        )
    assert caught.value.reason == "invocation_mode_not_allowed"


def test_agent_tool_contracts_expose_and_validate_strict_arguments():
    schemas = tool_argument_schemas_for(COMPANION_AGENT_SPEC.allowed_tools)
    assert set(schemas) == {
        "get_trip_state",
        "get_current_location",
        "get_weather",
        "propose_replan",
    }
    assert validate_tool_arguments("propose_replan", {"reason": "delay"}) == {
        "reason": "delay"
    }
    with pytest.raises(ValueError):
        validate_tool_arguments("get_weather", {"location": {"lng": 200, "lat": 30}})


def test_expected_internal_stage_capabilities_are_allowed():
    assert (
        _authorize(
            AgentType.intent,
            "parse_requirement",
            InvocationMode.internal_stage,
            DataScope.planning_request,
        ).capability.name
        == "parse_requirement"
    )
    assert (
        _authorize(
            AgentType.search,
            "search_poi",
            InvocationMode.internal_stage,
            DataScope.map_search,
        ).capability.name
        == "search_poi"
    )
    assert (
        _authorize(
            AgentType.planner,
            "get_route_matrix",
            InvocationMode.internal_stage,
            DataScope.route_matrix,
        ).capability.name
        == "get_route_matrix"
    )
    assert (
        _authorize(
            AgentType.safety,
            "check_travel_safety",
            InvocationMode.internal_stage,
            DataScope.safety_review,
        ).capability.name
        == "check_travel_safety"
    )


@pytest.mark.parametrize(
    ("agent_type", "capability"),
    [
        (AgentType.intent, "search_poi"),
        (AgentType.intent, "get_route_matrix"),
        (AgentType.search, "optimize_route"),
        (AgentType.safety, "search_poi"),
        (AgentType.safety, "optimize_route"),
        (AgentType.planner, "parse_requirement"),
        (AgentType.critic, "get_route_matrix"),
        (AgentType.companion, "search_poi"),
        (AgentType.companion, "optimize_route"),
    ],
)
def test_cross_role_capability_escalation_is_denied(
    agent_type: AgentType, capability: str
):
    registered = TOOL_REGISTRY.get(capability)
    assert registered is not None
    with pytest.raises(CapabilityAuthorizationError) as caught:
        _authorize(
            agent_type,
            capability,
            registered.invocation_mode,
            *registered.data_scopes,
        )
    assert caught.value.reason == "tool_not_allowed_for_agent"


def test_planner_cannot_request_user_or_location_data_scopes():
    with pytest.raises(CapabilityAuthorizationError) as caught:
        _authorize(
            AgentType.planner,
            "get_route_matrix",
            InvocationMode.internal_stage,
            DataScope.route_matrix,
            DataScope.user_preferences,
        )
    assert caught.value.reason == "data_scope_not_allowed"

    with pytest.raises(CapabilityAuthorizationError) as caught:
        _authorize(
            AgentType.planner,
            "get_route_matrix",
            InvocationMode.internal_stage,
        )
    assert caught.value.reason == "data_scope_not_allowed"

    with pytest.raises(CapabilityAuthorizationError) as caught:
        _authorize(
            AgentType.planner,
            "optimize_route",
            InvocationMode.internal_stage,
            DataScope.route_optimization,
            DataScope.precise_location,
        )
    assert caught.value.reason == "data_scope_not_allowed"


def test_unknown_and_workflow_only_operations_fail_closed_for_agents():
    with pytest.raises(CapabilityAuthorizationError) as caught:
        _authorize(
            AgentType.companion,
            "read_user_database",
            InvocationMode.agent_callable,
        )
    assert caught.value.reason == "tool_not_registered"

    with pytest.raises(CapabilityAuthorizationError) as caught:
        _authorize(
            AgentType.companion,
            "create_plan_patch",
            InvocationMode.agent_callable,
            DataScope.trip_state,
            DataScope.replan_proposal,
        )
    assert caught.value.reason == "tool_not_allowed_for_agent"

    for capability_name in (
        "load_confirmed_preferences",
        "save_explicit_preference",
        "delete_explicit_preference",
    ):
        capability = TOOL_REGISTRY.get(capability_name)
        assert capability is not None
        assert capability.invocation_mode == InvocationMode.workflow_only
        assert capability.agents == frozenset()
