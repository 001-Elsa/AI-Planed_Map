from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import permutations

from backend.app.schemas.ai_intent import PlanningTask


@dataclass(frozen=True)
class RouteEvaluation:
    order: list[int]
    arrivals: list[datetime]
    departures: list[datetime]
    total_distance: float
    total_travel_seconds: float
    feasible: bool
    conflicts: list[str]
    cost: float


def evaluate_order(
    order: list[int],
    departure: datetime,
    tasks: list[PlanningTask],
    distances: list[list[float]],
    durations: list[list[float]],
) -> RouteEvaluation:
    cursor = departure
    previous = 0
    arrivals: list[datetime] = []
    departures: list[datetime] = []
    conflicts: list[str] = []
    total_distance = 0.0
    total_seconds = 0.0
    for task_index in order:
        matrix_index = task_index + 1
        seconds = durations[previous][matrix_index]
        total_seconds += seconds
        total_distance += distances[previous][matrix_index]
        cursor += timedelta(seconds=seconds)
        arrivals.append(cursor)
        task = tasks[task_index]
        if task.deadline and cursor > task.deadline:
            conflicts.append(
                f"“{task.description}”最早预计 {cursor:%H:%M} 到达，超过截止时间 {task.deadline:%H:%M}"
            )
        cursor += timedelta(minutes=task.service_duration_minutes)
        departures.append(cursor)
        previous = matrix_index
    feasible = not conflicts
    return RouteEvaluation(
        order=order,
        arrivals=arrivals,
        departures=departures,
        total_distance=total_distance,
        total_travel_seconds=total_seconds,
        feasible=feasible,
        conflicts=conflicts,
        cost=total_seconds if feasible else float("inf"),
    )


def _nearest_neighbor(distances: list[list[float]], count: int) -> list[int]:
    remaining = set(range(count))
    previous = 0
    order: list[int] = []
    while remaining:
        chosen = min(remaining, key=lambda index: distances[previous][index + 1])
        order.append(chosen)
        remaining.remove(chosen)
        previous = chosen + 1
    return order


def _two_opt(
    initial: list[int],
    departure: datetime,
    tasks: list[PlanningTask],
    distances: list[list[float]],
    durations: list[list[float]],
) -> RouteEvaluation:
    best = evaluate_order(initial, departure, tasks, distances, durations)
    improved = True
    while improved:
        improved = False
        for start in range(len(initial) - 1):
            for end in range(start + 1, len(initial)):
                candidate_order = best.order[:start] + list(reversed(best.order[start : end + 1])) + best.order[end + 1 :]
                candidate = evaluate_order(candidate_order, departure, tasks, distances, durations)
                candidate_key = (not candidate.feasible, candidate.cost, candidate.total_distance)
                best_key = (not best.feasible, best.cost, best.total_distance)
                if candidate_key < best_key:
                    best = candidate
                    improved = True
    return best


def optimize_route(
    departure: datetime,
    tasks: list[PlanningTask],
    distances: list[list[float]],
    durations: list[list[float]],
) -> tuple[RouteEvaluation, str]:
    count = len(tasks)
    if count <= 6:
        evaluations = [
            evaluate_order(list(order), departure, tasks, distances, durations)
            for order in permutations(range(count))
        ]
        feasible = [item for item in evaluations if item.feasible]
        if feasible:
            return min(feasible, key=lambda item: (item.cost, item.total_distance)), "exact-permutation"
        # Return the least-late route instead of inventing a feasible result.
        return min(evaluations, key=lambda item: (len(item.conflicts), item.total_travel_seconds)), "exact-infeasible"
    initial = _nearest_neighbor(distances, count)
    return _two_opt(initial, departure, tasks, distances, durations), "nearest-neighbor+2-opt"

