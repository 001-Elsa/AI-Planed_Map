from backend.app.core.exceptions import AppError
from backend.app.schemas.companion import TripState

ALLOWED_TRANSITIONS: dict[TripState, set[TripState]] = {
    TripState.idle: {TripState.discovering, TripState.plan_ready, TripState.cancelled},
    TripState.discovering: {TripState.clarifying, TripState.planning, TripState.cancelled},
    TripState.clarifying: {TripState.planning, TripState.cancelled},
    TripState.planning: {TripState.plan_ready, TripState.clarifying, TripState.cancelled},
    TripState.plan_ready: {TripState.active_trip, TripState.cancelled},
    TripState.active_trip: {
        TripState.paused,
        TripState.off_route,
        TripState.at_risk,
        TripState.completed,
        TripState.cancelled,
    },
    TripState.paused: {TripState.active_trip, TripState.completed, TripState.cancelled},
    TripState.off_route: {
        TripState.replanning,
        TripState.active_trip,
        TripState.completed,
        TripState.cancelled,
    },
    TripState.at_risk: {
        TripState.replanning,
        TripState.active_trip,
        TripState.completed,
        TripState.cancelled,
    },
    TripState.replanning: {
        TripState.active_trip,
        TripState.at_risk,
        TripState.completed,
        TripState.cancelled,
    },
    TripState.completed: set(),
    TripState.cancelled: set(),
}


def validate_transition(current: TripState, target: TripState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise AppError(
            409,
            "TRIP_STATE_TRANSITION_DENIED",
            f"不能从 {current.value} 转换到 {target.value}",
            {"allowed": sorted(item.value for item in ALLOWED_TRANSITIONS[current])},
        )
