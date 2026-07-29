from dataclasses import dataclass

from backend.app.schemas.companion import ConsentScope, TripState


@dataclass(frozen=True)
class ToolPolicy:
    allowed_states: frozenset[TripState]
    confirmation_required: bool = False
    consent_scope: ConsentScope | None = None


TOOL_POLICIES = {
    "get_trip_state": ToolPolicy(frozenset(TripState)),
    "search_poi": ToolPolicy(
        frozenset({TripState.discovering, TripState.planning, TripState.replanning})
    ),
    "get_route_matrix": ToolPolicy(
        frozenset({TripState.planning, TripState.active_trip, TripState.replanning})
    ),
    "get_weather": ToolPolicy(
        frozenset(
            {
                TripState.plan_ready,
                TripState.active_trip,
                TripState.at_risk,
                TripState.off_route,
                TripState.replanning,
            }
        )
    ),
    "generate_attraction_brief": ToolPolicy(frozenset({TripState.active_trip})),
    "get_current_location": ToolPolicy(
        frozenset(
            {
                TripState.active_trip,
                TripState.off_route,
                TripState.at_risk,
                TripState.replanning,
            }
        ),
        consent_scope=ConsentScope.precise_location,
    ),
    "propose_replan": ToolPolicy(
        frozenset(
            {TripState.active_trip, TripState.off_route, TripState.at_risk, TripState.replanning}
        ),
        confirmation_required=True,
    ),
    "create_plan_patch": ToolPolicy(
        frozenset(
            {TripState.active_trip, TripState.off_route, TripState.at_risk, TripState.replanning}
        ),
        confirmation_required=True,
    ),
    "share_trip_status": ToolPolicy(
        frozenset({TripState.active_trip}),
        confirmation_required=True,
        consent_scope=ConsentScope.share_location,
    ),
    "save_explicit_preference": ToolPolicy(
        frozenset({TripState.active_trip, TripState.completed}),
        confirmation_required=True,
        consent_scope=ConsentScope.save_preference,
    ),
}


def evaluate_tool_policy(
    tool: str,
    state: TripState,
    granted_consents: set[ConsentScope],
) -> tuple[bool, str, bool]:
    policy = TOOL_POLICIES.get(tool)
    if policy is None:
        return False, "tool_not_registered", False
    if state not in policy.allowed_states:
        return False, "tool_not_allowed_in_state", policy.confirmation_required
    if policy.consent_scope and policy.consent_scope not in granted_consents:
        return False, f"missing_consent:{policy.consent_scope.value}", policy.confirmation_required
    return True, "allowed", policy.confirmation_required
