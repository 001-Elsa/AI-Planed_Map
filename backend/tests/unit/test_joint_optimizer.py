from datetime import datetime, timezone

from backend.app.clients.amap_client import MockMapProvider
from backend.app.schemas.ai_intent import (
    Coordinate,
    HardConstraints,
    PlanningPreferences,
    PlanningTask,
    TransportMode,
)
from backend.app.services.route_optimizer import CandidateNode, optimize_joint_route


def test_joint_optimizer_can_choose_lower_rated_but_globally_better_candidate():
    import asyncio

    provider = MockMapProvider()
    points = [
        Coordinate(lng=0, lat=0),
        Coordinate(lng=0.20, lat=0),  # high-rated but very far
        Coordinate(lng=0.001, lat=0),  # slightly lower-rated and nearby
        Coordinate(lng=0.002, lat=0),
    ]
    matrix = asyncio.run(provider.route_matrix(points, TransportMode.walking))
    tasks = [
        PlanningTask(description="蛋糕店"),
        PlanningTask(description="医院"),
    ]
    groups = [
        [
            CandidateNode(0, 0, 1, 4.9, 0.9),
            CandidateNode(0, 1, 2, 4.7, 0.9),
        ],
        [CandidateNode(1, 0, 3, 4.6, 0.9)],
    ]
    result, algorithm = optimize_joint_route(
        datetime(2026, 7, 28, 8, tzinfo=timezone.utc),
        tasks,
        groups,
        matrix,
        PlanningPreferences(prefer_high_rating=True),
        HardConstraints(),
        TransportMode.walking,
    )
    assert algorithm == "joint-exact-enumeration"
    selected = {node.task_index: node.candidate_rank for node in result.selected_nodes}
    assert selected[0] == 1


def test_ortools_solver_handles_larger_time_window_problem():
    import asyncio

    provider = MockMapProvider()
    points = [Coordinate(lng=0, lat=0)] + [
        Coordinate(lng=index * 0.001, lat=0) for index in range(1, 8)
    ]
    matrix = asyncio.run(provider.route_matrix(points, TransportMode.walking))
    departure = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
    tasks = [
        PlanningTask(
            description=f"任务 {index}",
            deadline=departure.replace(hour=18),
        )
        for index in range(7)
    ]
    groups = [[CandidateNode(index, 0, index + 1, 4.5, 0.9)] for index in range(7)]
    result, algorithm = optimize_joint_route(
        departure,
        tasks,
        groups,
        matrix,
        PlanningPreferences(),
        HardConstraints(),
        TransportMode.walking,
    )
    assert algorithm == "ortools-routing-time-windows"
    assert result.feasible
    assert len(result.order) == 7
