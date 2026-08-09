from datetime import datetime

import pytest

from backend.app.clients.amap_client import MockMapProvider
from backend.app.core.config import Settings
from backend.app.core.exceptions import UpstreamError
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


class MultiStopParser:
    name = "multi-stop-test-parser"

    async def parse(self, _text):
        names = ["花园酒店", "友谊商店", "盒马鲜生", "万国广场"]
        return PlanningIntent(
            transport_mode="walking",
            tasks=[PlanningTask(description=name, location_name=name) for name in names],
        )


class OneTransientRecallFailure(MockMapProvider):
    def __init__(self):
        self.attempts: dict[str, int] = {}

    async def search_poi(self, keyword, origin, city):
        self.attempts[keyword] = self.attempts.get(keyword, 0) + 1
        if keyword == "友谊商店" and self.attempts[keyword] == 1:
            raise UpstreamError("transient connect timeout")
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


@pytest.mark.asyncio
async def test_multi_stop_plan_retries_one_transient_poi_recall_failure():
    provider = OneTransientRecallFailure()
    result = await PlanningService(
        MultiStopParser(), provider, Settings(mock_map_provider=True)
    ).plan(
        AIPlanRequest(
            text="花园酒店 友谊商店 盒马鲜生 万国广场",
            origin=Coordinate(lng=113.3, lat=23.135),
        )
    )

    assert result.status == "success"
    assert len(result.stops) == 4
    assert provider.attempts["友谊商店"] == 2


@pytest.mark.asyncio
async def test_public_transit_uses_same_planner_and_refines_selected_legs():
    result = await PlanningService(
        MultiStopParser(), MockMapProvider(), Settings(mock_map_provider=True)
    ).plan(
        AIPlanRequest(
            text="花园酒店 友谊商店 盒马鲜生 万国广场",
            origin=Coordinate(lng=113.3, lat=23.135),
            city="广州市",
            transport_mode="transit",
        )
    )

    assert result.status == "success"
    assert result.intent.transport_mode == "transit"
    assert len(result.stops) == 4
    assert result.algorithm and result.algorithm.endswith("+amap-transit-refinement")
    assert all(stop.travel.source == "transit_network_estimate" for stop in result.stops)
