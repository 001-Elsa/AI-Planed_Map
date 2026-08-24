import asyncio

import pytest

from backend.app.core.config import Settings
from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore
from backend.app.schemas.agent_artifacts import AgentType, ReviewReport
from backend.app.schemas.ai_intent import (
    Coordinate,
    PlanningIntent,
    PlanningPreferences,
    PlanningTask,
    PoiCandidate,
)
from backend.app.services.agent_shared_state import (
    AgentSharedStateManager,
    SharedStateAccessError,
    SharedStateConflictError,
    SharedStateError,
)


@pytest.mark.asyncio
async def test_runtime_store_compare_set_json_is_atomic_by_revision():
    store = InMemoryRuntimeStore()

    assert await store.compare_set_json("state:1", -1, {"revision": 0}, 60) is True
    assert await store.compare_set_json("state:1", -1, {"revision": 0}, 60) is False
    assert await store.compare_set_json("state:1", 1, {"revision": 2}, 60) is False
    assert await store.compare_set_json("state:1", 0, {"revision": 1}, 60) is True
    assert await store.get_json("state:1") == {"revision": 1}
    assert await store.delete_json("state:1") is True
    assert await store.delete_json("state:1") is False


@pytest.mark.asyncio
async def test_shared_state_lifecycle_role_views_and_optimistic_conflicts():
    manager = AgentSharedStateManager(
        InMemoryRuntimeStore(),
        Settings(
            mock_map_provider=True,
            agent_shared_state_ttl_seconds=300,
            agent_shared_state_max_history=20,
        ),
    )
    task_id = "plan-shared-state-test"
    state = await manager.initialize(task_id)
    intent = PlanningIntent(
        tasks=[PlanningTask(description="quiet museum", location_name="museum")],
        preferences=PlanningPreferences(minimize_walking=True),
    )

    state = await manager.update(
        task_id,
        actor=AgentType.intent,
        expected_revision=state.revision,
        action="intent_analyzed",
        changes={"user_requirement": intent, "clarification_questions": []},
    )
    search_view = await manager.read_for_agent(task_id, AgentType.search)
    assert search_view.user_requirement
    assert search_view.user_requirement.preferences.minimize_walking is True
    assert search_view.poi_candidates is None

    candidate = PoiCandidate(
        id="poi-1",
        name="Quiet Museum",
        location=Coordinate(lng=120.62, lat=31.32),
        source="mock",
    )
    state = await manager.update(
        task_id,
        actor=AgentType.search,
        expected_revision=state.revision,
        action="search_completed",
        changes={"poi_candidates": [[candidate]]},
    )
    planner_view = await manager.read_for_agent(task_id, AgentType.planner)
    assert planner_view.poi_candidates and planner_view.poi_candidates[0][0].id == "poi-1"

    with pytest.raises(SharedStateAccessError, match="cannot write"):
        await manager.update(
            task_id,
            actor=AgentType.search,
            expected_revision=state.revision,
            action="search_completed",
            changes={"route_plan": {"status": "forged"}},
        )

    stale_revision = state.revision
    state = await manager.update(
        task_id,
        actor=AgentType.planner,
        expected_revision=state.revision,
        action="plan_completed",
        changes={"route_plan": {"status": "success", "stops": [{"poi": "poi-1"}]}},
    )
    with pytest.raises(SharedStateConflictError, match="expected"):
        await manager.update(
            task_id,
            actor=AgentType.critic,
            expected_revision=stale_revision,
            action="critic_reviewed",
            changes={"evaluation_result": ReviewReport(verdict="approved", summary="ok")},
        )

    critic_view = await manager.read_for_agent(task_id, AgentType.critic)
    assert critic_view.route_plan and critic_view.route_plan["status"] == "success"
    assert critic_view.poi_candidates is None
    state = await manager.update(
        task_id,
        actor=AgentType.critic,
        expected_revision=state.revision,
        action="critic_reviewed",
        changes={"evaluation_result": ReviewReport(verdict="approved", summary="ok")},
    )
    state = await manager.update(
        task_id,
        actor=AgentType.supervisor,
        expected_revision=state.revision,
        action="workflow_finalized",
        changes={"execution_context": {"status": "success"}},
    )

    audit = await manager.audit(task_id)
    assert audit.revision == state.revision == 5
    assert audit.phase.value == "finalized"
    assert audit.preference_flags == ["minimize_walking"]
    assert audit.candidate_count == 1
    assert audit.stop_count == 1
    assert audit.evaluation_verdict == "approved"
    assert audit.history_count == 6


@pytest.mark.asyncio
async def test_trip_shared_state_refreshes_only_forward_formal_plan_versions():
    manager = AgentSharedStateManager(InMemoryRuntimeStore(), Settings(mock_map_provider=True))
    task_id = "trip-formal-plan-state"
    state = await manager.initialize(
        task_id,
        route_plan={"status": "success", "plan_version": 1, "stops": []},
    )

    refreshed = await manager.initialize(
        task_id,
        route_plan={"status": "success", "plan_version": 2, "stops": [{"id": "new"}]},
    )
    assert refreshed.revision == state.revision + 1
    assert refreshed.route_plan and refreshed.route_plan["plan_version"] == 2
    assert refreshed.execution_history[-1].action == "formal_plan_refreshed"

    with pytest.raises(SharedStateAccessError, match="cannot move backwards"):
        await manager.sync_formal_route_plan(
            task_id,
            expected_revision=refreshed.revision,
            route_plan={"status": "success", "plan_version": 1, "stops": []},
        )


@pytest.mark.asyncio
async def test_shared_state_detects_out_of_band_content_tampering():
    store = InMemoryRuntimeStore()
    manager = AgentSharedStateManager(store, Settings(mock_map_provider=True))
    task_id = "plan-state-tamper-test"
    await manager.initialize(task_id)
    key = manager.key(task_id)
    raw = await store.get_json(key)
    raw["phase"] = "completed"
    await store.set_json(key, raw, 60)

    with pytest.raises(SharedStateError, match="content hash mismatch"):
        await manager.read(task_id)


@pytest.mark.asyncio
async def test_shared_state_rejects_oversized_runtime_context():
    manager = AgentSharedStateManager(
        InMemoryRuntimeStore(),
        Settings(mock_map_provider=True, agent_shared_state_max_bytes=16_384),
    )
    with pytest.raises(SharedStateError, match="byte limit"):
        await manager.initialize(
            "trip-oversized-state",
            route_plan={
                "status": "success",
                "plan_version": 1,
                "stops": [],
                "oversized": "x" * 20_000,
            },
        )


@pytest.mark.asyncio
async def test_concurrent_agents_cannot_both_commit_the_same_revision():
    manager = AgentSharedStateManager(InMemoryRuntimeStore(), Settings(mock_map_provider=True))
    task_id = "plan-concurrent-state-test"
    state = await manager.initialize(task_id)
    state = await manager.update(
        task_id,
        actor=AgentType.intent,
        expected_revision=state.revision,
        action="intent_analyzed",
        changes={
            "user_requirement": PlanningIntent(
                tasks=[PlanningTask(description="museum", location_name="museum")]
            ),
            "clarification_questions": [],
        },
    )
    candidate_a = PoiCandidate(
        id="a",
        name="A",
        location=Coordinate(lng=120.61, lat=31.31),
    )
    candidate_b = PoiCandidate(
        id="b",
        name="B",
        location=Coordinate(lng=120.62, lat=31.32),
    )

    async def commit(candidate: PoiCandidate):
        return await manager.update(
            task_id,
            actor=AgentType.search,
            expected_revision=state.revision,
            action="search_completed",
            changes={"poi_candidates": [[candidate]]},
        )

    results = await asyncio.gather(commit(candidate_a), commit(candidate_b), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, SharedStateConflictError) for result in results) == 1
    committed = await manager.read(task_id)
    assert committed.revision == state.revision + 1
