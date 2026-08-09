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


def test_optimizer_handles_many_tasks_exactly_once_and_preserves_explicit_order():
    import asyncio

    provider = MockMapProvider()
    task_count = 16
    points = [Coordinate(lng=0, lat=0)] + [
        Coordinate(lng=((index * 7) % 17) * 0.001, lat=((index * 11) % 19) * 0.001)
        for index in range(1, task_count + 1)
    ]
    matrix = asyncio.run(provider.route_matrix(points, TransportMode.driving))
    tasks = [PlanningTask(description=f"任务 {index}") for index in range(task_count)]
    groups = [[CandidateNode(index, 0, index + 1, 4.5, 0.9)] for index in range(task_count)]
    required = list(range(task_count))
    result, algorithm = optimize_joint_route(
        datetime(2026, 7, 28, 8, tzinfo=timezone.utc),
        tasks,
        groups,
        matrix,
        PlanningPreferences(optimization_goal="shortest_time"),
        HardConstraints(required_task_order=required),
        TransportMode.driving,
    )
    assert algorithm == "ordered-layered-beam"
    assert result.feasible
    assert result.order == required
    assert {node.task_index for node in result.selected_nodes} == set(required)


def test_exact_solver_honors_shortest_time_and_shortest_distance_as_distinct_goals():
    from backend.app.schemas.ai_intent import DataQuality, RouteEdge, RouteMatrix

    generated = datetime.now(timezone.utc)
    distances = [[0, 10, 100], [10, 0, 10], [100, 10, 0]]
    durations = [[0, 100, 10], [100, 0, 100], [10, 100, 0]]
    matrix = RouteMatrix(
        provider="test",
        generated_at=generated,
        edges=[
            [
                RouteEdge(
                    origin_index=i,
                    destination_index=j,
                    distance_meters=distances[i][j],
                    duration_seconds=durations[i][j],
                    source="test",
                    quality=DataQuality.provider,
                    confidence=1,
                )
                for j in range(3)
            ]
            for i in range(3)
        ],
    )
    tasks = [PlanningTask(description="A"), PlanningTask(description="B")]
    groups = [[CandidateNode(0, 0, 1, 5, 1)], [CandidateNode(1, 0, 2, 5, 1)]]
    shortest_time, _ = optimize_joint_route(
        generated,
        tasks,
        groups,
        matrix,
        PlanningPreferences(optimization_goal="shortest_time"),
        HardConstraints(),
        TransportMode.driving,
    )
    shortest_distance, _ = optimize_joint_route(
        generated,
        tasks,
        groups,
        matrix,
        PlanningPreferences(optimization_goal="shortest_distance"),
        HardConstraints(),
        TransportMode.driving,
    )
    assert shortest_time.order == [1, 0]
    assert shortest_distance.order == [0, 1]
