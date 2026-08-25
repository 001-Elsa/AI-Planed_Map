import asyncio
import inspect

import pytest

from backend.app.clients.amap_client import MockMapProvider
from backend.app.core.config import Settings
from backend.app.schemas.ai_intent import Coordinate, PlanningIntent, PlanningTask
from backend.app.services.agents.planner_agent import PlannerAgent, PlannerAgentInput
from backend.app.services.agents.search_agent import SearchAgent, SearchAgentInput
from backend.app.services.planning_service import PlanningService


class ConcurrentRecordingProvider(MockMapProvider):
    def __init__(self) -> None:
        self.active_searches = 0
        self.max_active_searches = 0
        self.matrix_calls = 0

    async def search_poi(self, keyword, origin, city):
        self.active_searches += 1
        self.max_active_searches = max(self.max_active_searches, self.active_searches)
        try:
            await asyncio.sleep(0.01)
            return await super().search_poi(keyword, origin, city)
        finally:
            self.active_searches -= 1

    async def route_matrix(self, points, mode):
        self.matrix_calls += 1
        return await super().route_matrix(points, mode)


class SecretFailingProvider(MockMapProvider):
    async def search_poi(self, keyword, origin, city):
        raise RuntimeError("https://internal-map.local?token=do-not-leak")


def _intent() -> PlanningIntent:
    return PlanningIntent(
        tasks=[
            PlanningTask(description="museum", location_name="museum"),
            PlanningTask(description="park", location_name="park"),
        ]
    )


@pytest.mark.asyncio
async def test_search_agent_executes_parallel_provider_recall_and_returns_typed_artifact():
    provider = ConcurrentRecordingProvider()
    agent = SearchAgent(provider, Settings(mock_map_provider=True))

    execution = await agent.run(
        SearchAgentInput(
            intent=_intent(),
            origin=Coordinate(lng=120.62, lat=31.32),
            city="Suzhou",
        )
    )

    assert provider.max_active_searches == 2
    assert execution.output.provider_name == provider.name
    assert [len(group) for group in execution.output.candidate_groups] == [3, 3]
    assert execution.output.clarification_questions == []
    assert all(item.success for item in execution.output.tool_results)
    assert execution.artifact.artifact_type == "search_artifact"


@pytest.mark.asyncio
async def test_planner_agent_owns_matrix_solver_and_plan_assembly():
    provider = ConcurrentRecordingProvider()
    settings = Settings(mock_map_provider=True)
    intent = _intent()
    search = await SearchAgent(provider, settings).run(
        SearchAgentInput(
            intent=intent,
            origin=Coordinate(lng=120.62, lat=31.32),
        )
    )

    execution = await PlannerAgent(provider, settings).run(
        PlannerAgentInput(
            intent=intent,
            origin=Coordinate(lng=120.62, lat=31.32),
            search=search.output,
        )
    )

    assert provider.matrix_calls == 1
    assert execution.output.status == "success"
    assert execution.output.algorithm
    assert len(execution.output.stops) == 2
    assert execution.artifact.producer_agent.value == "planner"
    assert any(ref.startswith("matrix:") for ref in execution.artifact.evidence_refs)
    assert any(ref.startswith("solution:") for ref in execution.artifact.evidence_refs)


@pytest.mark.asyncio
async def test_search_agent_never_exposes_raw_upstream_exception_text():
    agent = SearchAgent(
        SecretFailingProvider(),
        Settings(mock_map_provider=True, agent_search_max_attempts=1),
    )

    execution = await agent.run(
        SearchAgentInput(
            intent=PlanningIntent(tasks=[PlanningTask(description="museum")]),
            origin=Coordinate(lng=120.62, lat=31.32),
        )
    )
    serialized = execution.output.model_dump_json()

    assert execution.output.tool_results[0].error_code == "UPSTREAM_ERROR"
    assert execution.output.recovery_actions[0].error_type == "UPSTREAM_ERROR"
    assert "internal-map.local" not in serialized
    assert "do-not-leak" not in serialized


def test_planning_service_no_longer_contains_search_or_route_execution_logic():
    source = inspect.getsource(PlanningService)

    assert "_search_candidates" not in source
    assert "optimize_joint_route" not in source
    assert "route_matrix(" not in source
    assert "transit_route_edges(" not in source
    assert "orchestrator.execute_planning_stages" in source
