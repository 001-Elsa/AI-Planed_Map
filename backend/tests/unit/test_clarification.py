from backend.app.schemas.ai_intent import AIPlanRequest, PlanningIntent, PlanningTask
from backend.app.services.clarification import (
    apply_clarification_answer,
    select_clarification_questions,
)


def test_dynamic_clarification_covers_party_and_poi_choice():
    request = AIPlanRequest(text="带老人轮椅去拙政园，别太累")
    intent = PlanningIntent(tasks=[PlanningTask(description="拙政园", location_name="拙政园")])
    questions = select_clarification_questions(
        request=request,
        intent=intent,
        text=request.text,
        max_questions=5,
    )
    fields = {item.field for item in questions}
    assert (
        "constraints.hard.wheelchair_accessible" in fields
        or "constraints.hard.party.elderly" in fields
    )
    assert any(
        "max_walking" in field or field == "constraints.hard.max_walking_meters" for field in fields
    ) or any("别太累" in item.reason or "步行" in item.question for item in questions)

    data = {"text": "去公园"}
    apply_clarification_answer(data, "constraints.hard.max_walking_meters", 2000)
    assert data["constraints"]["hard"]["max_walking_meters"] == 2000
    apply_clarification_answer(data, "tasks.0.selected_poi_id", "poi-1")
    assert data["task_poi_overrides"]["0"] == "poi-1"
