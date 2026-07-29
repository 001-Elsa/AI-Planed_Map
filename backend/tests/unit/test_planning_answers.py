from datetime import datetime

import pytest

from backend.app.clients.amap_client import MockMapProvider
from backend.app.core.config import Settings
from backend.app.schemas.ai_intent import AIPlanRequest, Coordinate, PlanningIntent, PlanningTask
from backend.app.services.planning_service import PlanningService


class StableParser:
    name = "stable-test-parser"

    async def parse(self, _text):
        return PlanningIntent(
            transport_mode="walking",
            tasks=[PlanningTask(description="午餐", location_name="餐厅")],
        )


class RecordingMap(MockMapProvider):
    def __init__(self):
        self.keywords: list[str] = []

    async def search_poi(self, keyword, origin, city):
        self.keywords.append(keyword)
        return await super().search_poi(keyword, origin, city)


@pytest.mark.asyncio
async def test_structured_clarification_answers_change_recall_intent_and_solution():
    provider = RecordingMap()
    origin = Coordinate(lng=120.62, lat=31.32)
    initial = await provider.search_poi("餐厅 素食", origin, None)
    selected_id = initial[2].id
    result = await PlanningService(StableParser(), provider, Settings(mock_map_provider=True)).plan(
        AIPlanRequest(
            text="午餐",
            origin=origin,
            task_poi_overrides={"0": selected_id},
            task_field_overrides={"0": {"appointment_time": "2030-01-01T12:00:00+08:00"}},
            preferences_answers={"dietary_restrictions": ["素食"], "minimize_cost": True},
        )
    )
    assert "餐厅 素食" in provider.keywords
    assert result.intent.preferences.dietary_restrictions == ["素食"]
    assert result.intent.preferences.minimize_cost is True
    assert result.intent.tasks[0].appointment_time == datetime.fromisoformat(
        "2030-01-01T12:00:00+08:00"
    )
    assert result.stops[0].poi.id == selected_id
    assert result.stops[0].task.deadline == result.stops[0].task.appointment_time
