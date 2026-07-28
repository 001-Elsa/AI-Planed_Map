import random
from datetime import datetime, timezone

from backend.app.schemas.ai_intent import PlanningTask
from backend.app.services.route_optimizer import optimize_route


def test_generated_routes_preserve_core_invariants():
    for seed in range(40):
        randomizer = random.Random(seed)
        count = randomizer.randint(2, 8)
        tasks = [PlanningTask(description=f"task-{index}") for index in range(count)]
        coordinates = sorted(randomizer.uniform(0, 10_000) for _ in range(count + 1))
        distances = [
            [abs(coordinates[i] - coordinates[j]) for j in range(count + 1)]
            for i in range(count + 1)
        ]
        durations = [[value / 1.2 for value in row] for row in distances]
        result, _ = optimize_route(
            datetime(2026, 7, 28, tzinfo=timezone.utc),
            tasks,
            distances,
            durations,
        )
        assert sorted(result.order) == list(range(count))
        assert len(set(result.order)) == count
        assert result.feasible
        assert result.arrivals == sorted(result.arrivals)
        assert all(
            arrival <= departure
            for arrival, departure in zip(result.arrivals, result.departures, strict=True)
        )
