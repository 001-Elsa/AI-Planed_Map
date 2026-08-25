"""Fail-closed capability registry for every Agent execution boundary.

The registry distinguishes model-selectable tools from deterministic internal
capabilities and server workflows.  This prevents an internal map/optimizer
function from becoming an LLM tool merely because both are callable Python
functions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, NoReturn

from backend.app.core.observability import metrics
from backend.app.schemas.agent_artifacts import AgentType

logger = logging.getLogger(__name__)


class InvocationMode(str, Enum):
    agent_callable = "agent_callable"
    internal_stage = "internal_stage"
    workflow_only = "workflow_only"


class DataScope(str, Enum):
    planning_request = "planning_request"
    map_search = "map_search"
    safety_review = "safety_review"
    route_matrix = "route_matrix"
    route_optimization = "route_optimization"
    transit_routes = "transit_routes"
    trip_state = "trip_state"
    precise_location = "precise_location"
    weather = "weather"
    replan_proposal = "replan_proposal"
    external_share = "external_share"
    user_preferences = "user_preferences"


@dataclass(frozen=True)
class AgentCapability:
    name: str
    agents: frozenset[AgentType]
    invocation_mode: InvocationMode
    data_scopes: frozenset[DataScope]
    side_effect: str = "read_only"
    argument_schema_ref: str | None = None


@dataclass(frozen=True)
class CapabilityGrant:
    capability: AgentCapability
    agent_type: AgentType
    invocation_mode: InvocationMode
    requested_scopes: frozenset[DataScope]
    argument_schema: dict[str, Any] | None = None


class CapabilityAuthorizationError(PermissionError):
    def __init__(self, *, agent_type: AgentType, capability: str, reason: str) -> None:
        self.agent_type = agent_type
        self.capability = capability
        self.reason = reason
        super().__init__(f"{agent_type.value}:{capability}:{reason}")


CAPABILITIES = (
    AgentCapability(
        "parse_requirement",
        frozenset({AgentType.intent}),
        InvocationMode.internal_stage,
        frozenset({DataScope.planning_request}),
        argument_schema_ref="ParseRequirementArgs",
    ),
    AgentCapability(
        "search_poi",
        frozenset({AgentType.search}),
        InvocationMode.internal_stage,
        frozenset({DataScope.map_search}),
        argument_schema_ref="SearchPoiArgs",
    ),
    AgentCapability(
        "check_travel_safety",
        frozenset({AgentType.safety}),
        InvocationMode.internal_stage,
        frozenset({DataScope.safety_review}),
        argument_schema_ref="SafetyCheckArgs",
    ),
    AgentCapability(
        "get_route_matrix",
        frozenset({AgentType.planner}),
        InvocationMode.internal_stage,
        frozenset({DataScope.route_matrix}),
        argument_schema_ref="RouteMatrixArgs",
    ),
    AgentCapability(
        "optimize_route",
        frozenset({AgentType.planner}),
        InvocationMode.internal_stage,
        frozenset({DataScope.route_optimization}),
        argument_schema_ref="OptimizeRouteArgs",
    ),
    AgentCapability(
        "verify_transit_edges",
        frozenset({AgentType.planner}),
        InvocationMode.internal_stage,
        frozenset({DataScope.transit_routes}),
        argument_schema_ref="RouteMatrixArgs",
    ),
    AgentCapability(
        "get_trip_state",
        frozenset({AgentType.companion}),
        InvocationMode.agent_callable,
        frozenset({DataScope.trip_state}),
        argument_schema_ref="TripStateQueryArgs",
    ),
    AgentCapability(
        "get_current_location",
        frozenset({AgentType.companion}),
        InvocationMode.agent_callable,
        frozenset({DataScope.precise_location}),
        argument_schema_ref="CurrentLocationQueryArgs",
    ),
    AgentCapability(
        "get_weather",
        frozenset({AgentType.companion}),
        InvocationMode.agent_callable,
        frozenset({DataScope.weather}),
        argument_schema_ref="WeatherQueryArgs",
    ),
    AgentCapability(
        "propose_replan",
        frozenset({AgentType.companion}),
        InvocationMode.agent_callable,
        frozenset({DataScope.trip_state, DataScope.replan_proposal}),
        side_effect="proposal_only",
        argument_schema_ref="ReplanProposalArgs",
    ),
    # These operations exist in the business policy table but intentionally
    # have no Agent owner.  Only dedicated, server-controlled workflows may
    # execute them after confirmation/validation.
    AgentCapability(
        "generate_attraction_brief",
        frozenset(),
        InvocationMode.workflow_only,
        frozenset({DataScope.trip_state}),
    ),
    AgentCapability(
        "create_plan_patch",
        frozenset(),
        InvocationMode.workflow_only,
        frozenset({DataScope.trip_state, DataScope.replan_proposal}),
        side_effect="proposal_persist",
    ),
    AgentCapability(
        "share_trip_status",
        frozenset(),
        InvocationMode.workflow_only,
        frozenset({DataScope.trip_state, DataScope.external_share}),
        side_effect="external_write",
    ),
    AgentCapability(
        "load_confirmed_preferences",
        frozenset(),
        InvocationMode.workflow_only,
        frozenset({DataScope.user_preferences}),
    ),
    AgentCapability(
        "save_explicit_preference",
        frozenset(),
        InvocationMode.workflow_only,
        frozenset({DataScope.user_preferences}),
        side_effect="persistent_write",
    ),
    AgentCapability(
        "delete_explicit_preference",
        frozenset(),
        InvocationMode.workflow_only,
        frozenset({DataScope.user_preferences}),
        side_effect="persistent_delete",
    ),
)


class AgentToolRegistry:
    def __init__(self, capabilities: tuple[AgentCapability, ...]) -> None:
        by_name = {item.name: item for item in capabilities}
        if len(by_name) != len(capabilities):
            raise ValueError("duplicate Agent capability name")
        self._capabilities = by_name

    def names_for(self, agent_type: AgentType, mode: InvocationMode) -> frozenset[str]:
        return frozenset(
            item.name
            for item in self._capabilities.values()
            if agent_type in item.agents and item.invocation_mode == mode
        )

    def get(self, name: str) -> AgentCapability | None:
        return self._capabilities.get(name)

    def argument_schema(self, name: str) -> dict[str, Any] | None:
        from backend.app.services.agent_tool_contracts import tool_argument_schema

        return tool_argument_schema(name)

    def argument_schemas_for(self, agent_type: AgentType, mode: InvocationMode) -> dict[str, Any]:
        return {
            name: schema
            for name in self.names_for(agent_type, mode)
            if (schema := self.argument_schema(name)) is not None
        }

    def authorize(
        self,
        *,
        agent_type: AgentType,
        capability: str,
        invocation_mode: InvocationMode,
        requested_scopes: frozenset[DataScope],
    ) -> CapabilityGrant:
        registered = self._capabilities.get(capability)
        metric_capability = capability if registered is not None else "__unregistered__"
        if registered is None:
            self._deny(
                agent_type, metric_capability, capability, invocation_mode, "tool_not_registered"
            )
        if agent_type not in registered.agents:
            self._deny(
                agent_type,
                metric_capability,
                capability,
                invocation_mode,
                "tool_not_allowed_for_agent",
            )
        if registered.invocation_mode != invocation_mode:
            self._deny(
                agent_type,
                metric_capability,
                capability,
                invocation_mode,
                "invocation_mode_not_allowed",
            )
        # A caller may not under-declare its data footprint either. Requiring
        # the exact set makes scope changes visible in review.
        if requested_scopes != registered.data_scopes:
            self._deny(
                agent_type,
                metric_capability,
                capability,
                invocation_mode,
                "data_scope_not_allowed",
            )
        metrics.increment(
            "mapgo_agent_capability_authorizations_total",
            {
                "agent": agent_type.value,
                "capability": metric_capability,
                "mode": invocation_mode.value,
                "result": "allowed",
                "reason": "allowed",
            },
        )
        return CapabilityGrant(
            capability=registered,
            agent_type=agent_type,
            invocation_mode=invocation_mode,
            requested_scopes=requested_scopes,
            argument_schema=self.argument_schema(capability),
        )

    @staticmethod
    def _deny(
        agent_type: AgentType,
        metric_capability: str,
        requested_capability: str,
        invocation_mode: InvocationMode,
        reason: str,
    ) -> NoReturn:
        metrics.increment(
            "mapgo_agent_capability_authorizations_total",
            {
                "agent": agent_type.value,
                "capability": metric_capability,
                "mode": invocation_mode.value,
                "result": "denied",
                "reason": reason,
            },
        )
        logger.warning(
            "agent capability denied agent=%s capability=%s mode=%s reason=%s",
            agent_type.value,
            metric_capability,
            invocation_mode.value,
            reason,
        )
        raise CapabilityAuthorizationError(
            agent_type=agent_type,
            capability=requested_capability,
            reason=reason,
        )


TOOL_REGISTRY = AgentToolRegistry(CAPABILITIES)
