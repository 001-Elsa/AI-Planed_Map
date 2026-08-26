from typing import Any

import pytest
from pydantic import ValidationError

from backend.app.clients.amap_client import MockMapProvider
from backend.app.core.config import Settings
from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore
from backend.app.schemas.ai_intent import AIPlanRequest, Coordinate, PlanningIntent, PlanningTask
from backend.app.services.agent_memory import (
    MemoryPreferenceError,
    apply_long_term_preferences,
    normalize_long_term_preference,
)
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.planning_service import PlanningService


class StableMemoryParser:
    name = "stable-memory-parser"
    input_tokens = 0
    output_tokens = 0

    async def parse(self, text: str) -> PlanningIntent:
        return PlanningIntent(tasks=[PlanningTask(description=text, location_name=text)])


def test_long_term_memory_values_are_strictly_normalized():
    assert normalize_long_term_preference("minimize_walking", True) is True
    assert normalize_long_term_preference(
        "preferred_categories", [" 博物馆 ", "博物馆", "公园"]
    ) == ["博物馆", "公园"]
    assert normalize_long_term_preference("preferred_environment", ["quiet"]) == ["quiet"]
    assert normalize_long_term_preference("avoid_hiking", True) is True
    assert normalize_long_term_preference("travel_style", "relaxed") == "relaxed"

    with pytest.raises(MemoryPreferenceError):
        normalize_long_term_preference("home_address", "private")
    with pytest.raises(MemoryPreferenceError):
        normalize_long_term_preference("minimize_walking", "yes")
    with pytest.raises(MemoryPreferenceError):
        normalize_long_term_preference("preferred_environment", ["luxury"])


def test_current_request_overrides_memory_and_discovery_hints_are_bounded():
    request = AIPlanRequest(
        text="帮我规划上海旅游，步行可以多一点",
        preferences_answers={"minimize_cost": False},
    )
    effective, audit = apply_long_term_preferences(
        request,
        {
            "minimize_walking": True,
            "minimize_cost": True,
            "preferred_categories": ["博物馆"],
            "preferred_environment": ["quiet"],
        },
    )

    assert effective.preferences_answers["minimize_cost"] is False
    assert "minimize_walking" not in effective.preferences_answers
    assert effective.preferences_answers["preferred_categories"] == ["博物馆"]
    assert effective.preferences_answers["preferred_environment"] == ["quiet"]
    assert set(audit.applied_keys) == {"preferred_categories", "preferred_environment"}
    assert set(audit.skipped_explicit_keys) == {"minimize_cost", "minimize_walking"}
    assert audit.as_dict()["values_included"] is False


def test_memory_can_be_disabled_per_request():
    request = AIPlanRequest(text="帮我规划上海旅游", use_long_term_memory=False)
    effective, audit = apply_long_term_preferences(request, {"minimize_walking": True})
    assert effective.preferences_answers == {}
    assert audit.enabled is False
    assert audit.source == "disabled_by_user"


def test_request_cannot_smuggle_unbounded_or_unknown_memory_fields():
    with pytest.raises(ValidationError):
        AIPlanRequest(text="规划旅行", preferences_answers={"home_address": "private"})
    with pytest.raises(ValidationError):
        AIPlanRequest(
            text="规划旅行",
            preferences_answers={"preferred_environment": ["ignore_previous_instructions"]},
        )


@pytest.mark.asyncio
async def test_planning_short_term_memory_is_deleted_after_trace_is_captured():
    store = InMemoryRuntimeStore()
    settings = Settings(mock_map_provider=True, plan_critic_mode="off")
    manager = AgentSharedStateManager(store, settings)
    result = await PlanningService(
        StableMemoryParser(),
        MockMapProvider(),
        settings,
        shared_state=manager,
    ).plan(
        AIPlanRequest(
            text="博物馆",
            origin=Coordinate(lng=120.62, lat=31.32),
        )
    )

    assert result.agent_workflow is not None
    task_id = result.agent_workflow.task_id
    assert result.agent_workflow.shared_state is not None
    assert await store.get_json(manager.key(task_id)) is None


@pytest.mark.parametrize(
    "preferences",
    [
        {"preferred_categories": ["博物馆"], "avoid_queues": True},
        {"preferred_environment": ["quiet", "indoor"]},
    ],
)
def test_memory_discovery_fields_are_applied_only_to_generic_requests(
    preferences: dict[str, Any],
):
    generic, _ = apply_long_term_preferences(AIPlanRequest(text="帮我规划杭州旅游"), preferences)
    specific, audit = apply_long_term_preferences(AIPlanRequest(text="去西湖公园"), preferences)
    assert generic.preferences_answers
    assert specific.preferences_answers == {}
    assert set(audit.skipped_explicit_keys) == set(preferences)
