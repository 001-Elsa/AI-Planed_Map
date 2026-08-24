from copy import deepcopy

import pytest

from backend.app.clients.amap_client import MockMapProvider
from backend.app.core.config import Settings
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    Coordinate,
    PlanningIntent,
    PlanningPreferences,
    PlanningTask,
    PoiCandidate,
)
from backend.app.services.agent_evaluation import (
    ExpectedRoutePreferences,
    RouteEvaluationPolicy,
    evaluate_route_plan,
)
from backend.app.services.agents.critic_agent import RuleBasedCriticAgent
from backend.app.services.planning_service import PlanningService


class AvoidHikingParser:
    name = "avoid-hiking-parser"
    input_tokens = 0
    output_tokens = 0

    async def parse(self, _text: str) -> PlanningIntent:
        return PlanningIntent(
            tasks=[PlanningTask(description="游玩景点", location_name="景点")],
            preferences=PlanningPreferences(avoid_hiking=True),
        )


class MixedHikingProvider(MockMapProvider):
    async def search_poi(self, keyword, origin, city):
        return [
            PoiCandidate(
                id="trail-1",
                name="天目山登山步道",
                location=Coordinate(lng=origin.lng + 0.001, lat=origin.lat + 0.001),
                source="mock",
            ),
            PoiCandidate(
                id="museum-1",
                name="城市博物馆",
                location=Coordinate(lng=origin.lng + 0.002, lat=origin.lat + 0.002),
                source="mock",
            ),
        ]


def _plan() -> dict:
    return {
        "status": "success",
        "total_distance_meters": 4_000,
        "total_travel_seconds": 3_600,
        "estimated_cost_yuan": 20,
        "intent": {
            "transport_mode": "walking",
            "preferences": {
                "avoid_hiking": True,
                "travel_style": "relaxed",
                "minimize_walking": True,
                "prefer_high_rating": False,
            },
            "constraints": {
                "hard": {
                    "max_walking_meters": 6_000,
                    "max_total_duration_minutes": 120,
                }
            },
        },
        "stops": [
            {
                "constraint_satisfied": True,
                "arrival_time": "2026-08-21T10:00:00+08:00",
                "departure_time": "2026-08-21T11:00:00+08:00",
                "poi": {"id": "museum-1", "name": "城市博物馆", "rating": 4.6},
                "task": {"description": "参观博物馆", "deadline": None},
                "travel": {"distance_meters": 4_000, "mode": "walking"},
            }
        ],
    }


def _policy() -> RouteEvaluationPolicy:
    return RouteEvaluationPolicy(
        max_reasonable_distance_meters=8_000,
        max_reasonable_travel_seconds=7_200,
        expected_preferences=ExpectedRoutePreferences(
            avoid_hiking=True,
            travel_style="relaxed",
            minimize_walking=True,
        ),
    )


def test_good_route_uses_documented_40_30_30_weighting():
    report = evaluate_route_plan(_plan(), _policy())
    assert report.passed is True
    assert report.final_score == 100
    assert report.weights == {"distance": 0.4, "time": 0.3, "preference": 0.3}
    assert report.hard_failures == []


def test_duplicate_poi_and_deadline_violation_are_hard_failures():
    plan = _plan()
    duplicate = deepcopy(plan["stops"][0])
    duplicate["arrival_time"] = "2026-08-21T12:00:00+08:00"
    duplicate["task"]["deadline"] = "2026-08-21T11:30:00+08:00"
    plan["stops"].append(duplicate)
    report = evaluate_route_plan(plan, _policy())
    assert report.passed is False
    assert report.final_score == 0
    assert {"duplicate_poi", "task_deadline_exceeded"}.issubset(report.hard_failures)


def test_preference_mismatch_cannot_be_hidden_by_distance_and_time_scores():
    plan = _plan()
    plan["stops"][0]["poi"]["name"] = "天目山登山步道"
    report = evaluate_route_plan(plan, _policy())
    assert report.hard_failures == []
    assert report.distance_score == 100
    assert report.time_score == 100
    assert report.preference_score == 0
    assert report.final_score == 70
    assert report.passed is False


def test_time_reasonability_contributes_thirty_percent_without_a_hard_limit():
    plan = _plan()
    plan["total_travel_seconds"] = 9_000
    plan["intent"]["constraints"]["hard"].pop("max_total_duration_minutes")
    policy = _policy().model_copy(update={"hard_time_limit_seconds": None})
    report = evaluate_route_plan(plan, policy)
    assert report.time_score == 75
    assert report.final_score == 92.5
    assert report.passed is True


def test_twice_the_reasonable_distance_is_blocking():
    plan = _plan()
    plan["total_distance_meters"] = 16_000
    report = evaluate_route_plan(plan, _policy())
    assert report.final_score == 0
    assert "distance_excessive" in report.hard_failures


def test_inconsistent_timezones_fail_closed_instead_of_crashing():
    plan = _plan()
    plan["stops"][0]["arrival_time"] = "2026-08-21T13:00:00"
    plan["stops"][0]["task"]["deadline"] = "2026-08-21T12:00:00+08:00"
    report = evaluate_route_plan(plan, _policy())
    assert report.final_score == 0
    assert "time_evidence_invalid" in report.hard_failures


@pytest.mark.asyncio
async def test_runtime_critic_embeds_deterministic_score_and_blocks_duplicates():
    plan = _plan()
    plan["explanation"] = "provider-backed route"
    plan["stops"].append(deepcopy(plan["stops"][0]))
    execution = await RuleBasedCriticAgent().run(plan)

    report = execution.output
    assert report.verdict == "needs_clarification"
    assert report.route_evaluation is not None
    assert report.route_evaluation.final_score == 0
    assert "duplicate_poi" in report.route_evaluation.hard_failures
    assert any(item.code == "route_eval_duplicate_poi" for item in report.findings)


@pytest.mark.asyncio
async def test_planner_filters_hiking_candidates_before_shared_state_handoff():
    result = await PlanningService(
        AvoidHikingParser(),
        MixedHikingProvider(),
        Settings(mock_map_provider=True, plan_critic_mode="off"),
    ).plan(
        AIPlanRequest(
            text="不想爬山，安排一个景点",
            origin=Coordinate(lng=120.62, lat=31.32),
        )
    )

    assert result.status == "success"
    assert result.candidate_count == 1
    assert [stop.poi.id for stop in result.stops] == ["museum-1"]
