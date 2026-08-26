"""Enterprise-facing responsibility contracts for MAPGO Agent roles."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from backend.app.schemas.agent_artifacts import AgentType
from backend.app.schemas.common import StrictModel
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, InvocationMode

AgentResponsibilityRole = Literal[
    "requirement_clarification",
    "place_research",
    "itinerary_coordination",
    "plan_review",
    "runtime_companion",
]


class AgentRoleContract(StrictModel):
    role: AgentResponsibilityRole
    implementation_agents: frozenset[AgentType]
    responsibility: str = Field(min_length=1, max_length=240)
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    allowed_internal_capabilities: frozenset[str] = Field(default_factory=frozenset)
    forbidden_actions: frozenset[str] = Field(default_factory=frozenset)
    output_artifacts: frozenset[str] = Field(default_factory=frozenset)


ROLE_CONTRACTS: dict[AgentResponsibilityRole, AgentRoleContract] = {
    "requirement_clarification": AgentRoleContract(
        role="requirement_clarification",
        implementation_agents=frozenset({AgentType.intent}),
        responsibility="Convert natural language into structured goals, constraints and clarification questions.",
        allowed_internal_capabilities=TOOL_REGISTRY.names_for(
            AgentType.intent, InvocationMode.internal_stage
        ),
        forbidden_actions=frozenset(
            {
                "generate_poi",
                "call_map_provider",
                "modify_formal_plan",
                "create_plan_patch",
            }
        ),
        output_artifacts=frozenset({"intent_artifact", "clarification_questions"}),
    ),
    "place_research": AgentRoleContract(
        role="place_research",
        implementation_agents=frozenset({AgentType.search}),
        responsibility="Search provider-backed POI candidates in parallel and never fabricate places.",
        allowed_internal_capabilities=TOOL_REGISTRY.names_for(
            AgentType.search, InvocationMode.internal_stage
        ),
        forbidden_actions=frozenset(
            {"generate_fake_poi", "modify_formal_plan", "optimize_route", "create_plan_patch"}
        ),
        output_artifacts=frozenset({"candidate_set", "search_artifact"}),
    ),
    "itinerary_coordination": AgentRoleContract(
        role="itinerary_coordination",
        implementation_agents=frozenset(
            {AgentType.supervisor, AgentType.replanner, AgentType.planner}
        ),
        responsibility=(
            "Build the task graph, coordinate handoffs, and invoke deterministic route "
            "solvers such as OR-Tools or Beam Search without turning them into Agents."
        ),
        allowed_internal_capabilities=(
            TOOL_REGISTRY.names_for(AgentType.planner, InvocationMode.internal_stage)
            | TOOL_REGISTRY.names_for(AgentType.supervisor, InvocationMode.internal_stage)
        ),
        forbidden_actions=frozenset(
            {"fabricate_poi", "ignore_hard_constraints", "direct_user_data_export"}
        ),
        output_artifacts=frozenset({"agent_execution_plan", "route_solution", "plan_candidate"}),
    ),
    "plan_review": AgentRoleContract(
        role="plan_review",
        implementation_agents=frozenset({AgentType.critic, AgentType.safety}),
        responsibility=(
            "Read tasks, candidates and route solutions to review time windows, budget, "
            "accessibility, open status and confidence; never mutate hard constraints."
        ),
        allowed_internal_capabilities=TOOL_REGISTRY.names_for(
            AgentType.safety, InvocationMode.internal_stage
        ),
        forbidden_actions=frozenset(
            {
                "modify_hard_constraints",
                "generate_poi",
                "call_route_solver",
                "write_formal_plan",
            }
        ),
        output_artifacts=frozenset({"safety_report", "review_report"}),
    ),
    "runtime_companion": AgentRoleContract(
        role="runtime_companion",
        implementation_agents=frozenset({AgentType.companion}),
        responsibility=(
            "Handle weather, off-route, delay and closure events during execution and create "
            "pending plan patches only."
        ),
        allowed_tools=TOOL_REGISTRY.names_for(AgentType.companion, InvocationMode.agent_callable),
        forbidden_actions=frozenset(
            {
                "overwrite_formal_plan",
                "call_planning_period_search",
                "call_route_optimizer_directly",
                "bypass_user_confirmation",
            }
        ),
        output_artifacts=frozenset({"companion_action_report", "pending_plan_patch"}),
    ),
}


def public_role_contracts() -> dict[str, dict]:
    return {role: contract.model_dump(mode="json") for role, contract in ROLE_CONTRACTS.items()}
