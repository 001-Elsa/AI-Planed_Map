from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    AIPlanResult,
    Coordinate,
    HardConstraints,
    PartyProfile,
    PlanningIntent,
    PlanningPreferences,
    PlanningState,
    PlanningTask,
    TransportMode,
    TripConstraintSet,
)
from backend.app.services.clarification import apply_clarification_answer
from backend.app.services.human_in_loop import select_human_confirmation_questions


def _walking_result(
    *,
    distance_meters: float = 8500,
    elderly: int = 0,
    minimize_walking: bool = False,
) -> AIPlanResult:
    return AIPlanResult(
        status="success",
        planning_state=PlanningState.plan_ready,
        origin=Coordinate(lng=120.0, lat=30.0),
        total_distance_meters=distance_meters,
        intent=PlanningIntent(
            origin="hotel",
            transport_mode=TransportMode.walking,
            tasks=[PlanningTask(description="museum")],
            preferences=PlanningPreferences(minimize_walking=minimize_walking),
            constraints=TripConstraintSet(
                hard=HardConstraints(party=PartyProfile(elderly=elderly))
            ),
        ),
    )


def test_human_confirmation_triggers_for_long_walking_plan():
    request = AIPlanRequest(
        text="plan a relaxed walking trip",
        origin=Coordinate(lng=120.0, lat=30.0),
        transport_mode=TransportMode.walking,
    )

    questions = select_human_confirmation_questions(
        request=request,
        result=_walking_result(distance_meters=8500),
    )

    assert [question.field for question in questions] == [
        "human_confirmation.walking_distance"
    ]
    assert questions[0].kind == "confirmation"


def test_human_confirmation_uses_lower_threshold_for_sensitive_party():
    request = AIPlanRequest(
        text="elderly relaxed trip",
        origin=Coordinate(lng=120.0, lat=30.0),
        transport_mode=TransportMode.walking,
    )

    questions = select_human_confirmation_questions(
        request=request,
        result=_walking_result(distance_meters=6500, elderly=1),
    )

    assert questions
    assert questions[0].field == "human_confirmation.walking_distance"


def test_accepted_confirmation_is_not_repeated():
    request = AIPlanRequest(
        text="plan a long walk",
        origin=Coordinate(lng=120.0, lat=30.0),
        transport_mode=TransportMode.walking,
        human_confirmations={"walking_distance": True},
    )

    questions = select_human_confirmation_questions(
        request=request,
        result=_walking_result(distance_meters=9000),
    )

    assert questions == []


def test_rejected_walking_confirmation_rewrites_request_for_replan():
    data = {
        "text": "plan a long walk",
        "origin": {"lng": 120.0, "lat": 30.0},
        "transport_mode": "walking",
    }

    apply_clarification_answer(data, "human_confirmation.walking_distance", False)

    assert data["human_confirmations"] == {"walking_distance": False}
    assert data["preferences_answers"]["minimize_walking"] is True
    assert data["preferences_answers"]["travel_style"] == "relaxed"
    assert data["constraints"]["hard"]["max_walking_meters"] == 6000
