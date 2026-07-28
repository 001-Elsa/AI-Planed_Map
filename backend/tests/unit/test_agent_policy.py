import pytest

from backend.app.core.exceptions import AppError
from backend.app.schemas.companion import ConsentScope, TripEventType, TripState
from backend.app.services.agent_policy import evaluate_tool_policy
from backend.app.services.agent_state import validate_transition
from backend.app.services.trip_events import evaluate_trip_event


def test_location_tool_requires_state_and_consent():
    allowed, reason, confirmation = evaluate_tool_policy(
        "get_current_location", TripState.active_trip, set()
    )
    assert not allowed
    assert reason == "missing_consent:precise_location"
    assert not confirmation
    allowed, _, _ = evaluate_tool_policy(
        "get_current_location",
        TripState.active_trip,
        {ConsentScope.precise_location},
    )
    assert allowed


def test_state_machine_rejects_skipping_confirmation_states():
    with pytest.raises(AppError) as captured:
        validate_transition(TripState.plan_ready, TripState.completed)
    assert captured.value.code == "TRIP_STATE_TRANSITION_DENIED"


def test_critical_event_bypasses_notification_cooldown():
    decision = evaluate_trip_event(
        TripState.active_trip,
        TripEventType.deadline_risk,
        {},
        None,
        15,
    )
    assert decision.next_state == TripState.at_risk
    assert decision.should_notify
    assert all(item["action"] != "apply_plan_patch" for item in decision.proposals)
