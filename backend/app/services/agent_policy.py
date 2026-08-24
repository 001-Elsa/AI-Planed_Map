from dataclasses import dataclass

from backend.app.schemas.companion import ConsentScope, TripState


@dataclass(frozen=True)
class ToolPolicy:
    allowed_states: frozenset[TripState]
    confirmation_required: bool = False
    consent_scope: ConsentScope | None = None


# This table is intentionally limited to model-selectable Companion tools.
# Planning capabilities and confirmed server workflows belong to the Tool
# Registry and their dedicated execution paths, never this LLM policy surface.
TOOL_POLICIES = {
    "get_trip_state": ToolPolicy(frozenset(TripState)),
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
        )
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
