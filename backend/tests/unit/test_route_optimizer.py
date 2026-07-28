from datetime import datetime, timedelta, timezone

from backend.app.schemas.ai_intent import PlanningTask
from backend.app.services.route_optimizer import optimize_route


def test_exact_solver_respects_deadline_over_shorter_route():
    departure = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
    tasks = [
        PlanningTask(description="普通任务", location_name="A", service_duration_minutes=0),
        PlanningTask(
            description="必须先到",
            location_name="B",
            service_duration_minutes=0,
            deadline=departure + timedelta(minutes=10),
        ),
    ]
    # Origin=0, A=1, B=2. Visiting A first is shorter by distance but misses B's deadline.
    distances = [[0, 100, 500], [100, 0, 100], [500, 100, 0]]
    durations = [[0, 60, 300], [60, 0, 900], [300, 60, 0]]
    result, algorithm = optimize_route(departure, tasks, distances, durations)
    assert algorithm == "exact-permutation"
    assert result.feasible
    assert result.order == [1, 0]


def test_two_opt_never_returns_worse_than_initial_shape():
    departure = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
    tasks = [PlanningTask(description=str(index), location_name=str(index)) for index in range(7)]
    size = 8
    distances = [[abs(i - j) * 100 for j in range(size)] for i in range(size)]
    durations = [[value / 2 for value in row] for row in distances]
    result, algorithm = optimize_route(departure, tasks, distances, durations)
    assert algorithm == "nearest-neighbor+2-opt"
    assert result.order == list(range(7))
    assert result.total_distance == 700
