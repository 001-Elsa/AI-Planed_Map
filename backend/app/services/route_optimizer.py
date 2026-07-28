from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import permutations, product

from backend.app.schemas.ai_intent import (
    HardConstraints,
    PlanningPreferences,
    PlanningTask,
    RouteMatrix,
    ScoreBreakdown,
    TransportMode,
)


@dataclass(frozen=True)
class CandidateNode:
    task_index: int
    candidate_rank: int
    matrix_index: int
    rating: float | None
    confidence: float
    estimated_cost_yuan: float | None = None
    open_now: bool | None = None
    wheelchair_accessible: bool | None = None
    district: str | None = None


@dataclass(frozen=True)
class RouteEvaluation:
    order: list[int]
    selected_nodes: list[CandidateNode]
    arrivals: list[datetime]
    departures: list[datetime]
    total_distance: float
    total_travel_seconds: float
    feasible: bool
    conflicts: list[str]
    cost: float
    score: ScoreBreakdown


def _relative_order_satisfied(order: list[int], required: list[int]) -> bool:
    present = [item for item in required if item in order]
    return present == sorted(present, key=order.index)


def evaluate_joint_order(
    order: list[int],
    selected_by_task: dict[int, CandidateNode],
    departure: datetime,
    tasks: list[PlanningTask],
    matrix: RouteMatrix,
    preferences: PlanningPreferences,
    constraints: HardConstraints,
    mode: TransportMode,
) -> RouteEvaluation:
    cursor = departure
    previous = 0
    arrivals: list[datetime] = []
    departures: list[datetime] = []
    conflicts: list[str] = []
    selected_nodes: list[CandidateNode] = []
    total_distance = 0.0
    total_seconds = 0.0
    confidence_penalty = 0.0
    rating_penalty = 0.0
    total_cost = 0.0
    cost_unknown = False

    if constraints.required_task_order and not _relative_order_satisfied(
        order, constraints.required_task_order
    ):
        conflicts.append("方案违反了必须保持的任务先后顺序")

    for task_index in order:
        node = selected_by_task[task_index]
        edge = matrix.edges[previous][node.matrix_index]
        total_seconds += edge.duration_seconds
        total_distance += edge.distance_meters
        cursor += timedelta(seconds=edge.duration_seconds)
        task = tasks[task_index]
        if task.appointment_time:
            if cursor > task.appointment_time:
                conflicts.append(
                    f"“{task.description}”预计 {cursor:%H:%M} 到达，错过预约时间 "
                    f"{task.appointment_time:%H:%M}"
                )
            elif cursor < task.appointment_time:
                cursor = task.appointment_time
        if task.earliest_arrival and cursor < task.earliest_arrival:
            cursor = task.earliest_arrival
        arrivals.append(cursor)
        if task.deadline and cursor > task.deadline:
            conflicts.append(
                f"“{task.description}”预计 {cursor:%H:%M} 到达，超过截止时间 {task.deadline:%H:%M}"
            )
        if task.min_rating is not None and (node.rating is None or node.rating < task.min_rating):
            conflicts.append(f"“{task.description}”没有满足最低评分 {task.min_rating:g} 的候选地点")
        if task.require_open and node.open_now is False:
            conflicts.append(f"“{task.description}”候选地点当前未营业")
        requires_accessible = (
            task.require_wheelchair_accessible
            or constraints.wheelchair_accessible
            or constraints.party.wheelchair_users > 0
        )
        if requires_accessible and node.wheelchair_accessible is not True:
            conflicts.append(f"“{task.description}”缺少可验证的无障碍信息")
        if constraints.allowed_districts and node.district not in constraints.allowed_districts:
            conflicts.append(f"“{task.description}”不在允许区域内")
        if node.district and any(area in node.district for area in constraints.avoid_areas):
            conflicts.append(f"“{task.description}”位于需要避开的区域")
        if node.estimated_cost_yuan is None:
            cost_unknown = True
        else:
            total_cost += node.estimated_cost_yuan
            if task.max_cost_yuan is not None and node.estimated_cost_yuan > task.max_cost_yuan:
                conflicts.append(f"“{task.description}”预计费用超过单站预算")
        service_minutes = max(
            task.service_duration_minutes,
            task.min_service_duration_minutes or 0,
        )
        cursor += timedelta(minutes=service_minutes)
        departures.append(cursor)
        selected_nodes.append(node)
        confidence_penalty += 1 - min(edge.confidence, node.confidence)
        rating_penalty += max(0.0, 5.0 - (node.rating or 3.0))
        previous = node.matrix_index

    if constraints.must_return_to_origin and selected_nodes:
        return_edge = matrix.edges[previous][0]
        total_seconds += return_edge.duration_seconds
        total_distance += return_edge.distance_meters
        cursor += timedelta(seconds=return_edge.duration_seconds)
        confidence_penalty += 1 - return_edge.confidence

    elapsed_minutes = (cursor - departure).total_seconds() / 60
    if (
        constraints.max_total_duration_minutes is not None
        and elapsed_minutes > constraints.max_total_duration_minutes
    ):
        conflicts.append(
            f"总行程约 {elapsed_minutes:.0f} 分钟，超过上限 {constraints.max_total_duration_minutes} 分钟"
        )
    if constraints.latest_return_time is not None and cursor > constraints.latest_return_time:
        conflicts.append(
            f"预计 {cursor:%H:%M} 完成，超过最晚返回时间 {constraints.latest_return_time:%H:%M}"
        )
    if (
        constraints.max_walking_meters is not None
        and mode == TransportMode.walking
        and total_distance > constraints.max_walking_meters
    ):
        conflicts.append(
            f"步行约 {total_distance:.0f} 米，超过上限 {constraints.max_walking_meters:.0f} 米"
        )
    if constraints.max_total_cost_yuan is not None:
        if cost_unknown:
            conflicts.append("存在缺少价格来源的候选地点，无法验证总预算硬约束")
        elif total_cost > constraints.max_total_cost_yuan:
            conflicts.append(
                f"预计费用 {total_cost:.2f} 元，超过总预算 {constraints.max_total_cost_yuan:.2f} 元"
            )
    if constraints.must_pass_areas:
        conflicts.append("当前路线矩阵不含完整路径几何，无法验证必须经过区域")
    if constraints.max_detour_meters is not None:
        conflicts.append("缺少无绕行基准路线，无法验证最大绕行距离")
    if any(
        budget is not None
        for budget in (
            constraints.transport_budget_yuan,
            constraints.dining_budget_yuan,
            constraints.ticket_budget_yuan,
        )
    ):
        conflicts.append("Provider 尚未返回分类费用，无法验证分类预算硬约束")

    weights = preferences.weights
    distance_component = total_distance / 10
    walking_component = total_seconds if mode == TransportMode.walking else 0
    rating_component = rating_penalty * 600
    uncertainty_component = confidence_penalty * 600
    distance_weight = weights.distance * (2 if preferences.minimize_distance else 1)
    walking_weight = weights.walking_time * (2 if preferences.minimize_walking else 1)
    rating_weight = weights.low_rating * (2 if preferences.prefer_high_rating else 1)
    breakdown = ScoreBreakdown(
        travel_time=total_seconds * weights.travel_time,
        walking_time=walking_component * walking_weight,
        distance=distance_component * distance_weight,
        low_rating=rating_component * rating_weight,
        uncertainty=uncertainty_component * weights.uncertainty,
        monetary_cost=total_cost * 60 * weights.monetary_cost,
    )
    breakdown.total = (
        breakdown.travel_time
        + breakdown.walking_time
        + breakdown.distance
        + breakdown.low_rating
        + breakdown.uncertainty
        + breakdown.monetary_cost
    )
    return RouteEvaluation(
        order=order,
        selected_nodes=selected_nodes,
        arrivals=arrivals,
        departures=departures,
        total_distance=total_distance,
        total_travel_seconds=total_seconds,
        feasible=not conflicts,
        conflicts=conflicts,
        cost=breakdown.total,
        score=breakdown,
    )


def _evaluation_key(item: RouteEvaluation) -> tuple:
    return (not item.feasible, len(item.conflicts), item.cost, item.total_distance)


def _beam_joint(
    candidate_groups: list[list[CandidateNode]],
    matrix: RouteMatrix,
    beam_width: int = 250,
) -> list[tuple[list[int], dict[int, CandidateNode]]]:
    # State: visited task order, selected candidates, last matrix point, cheap path score.
    states: list[tuple[list[int], dict[int, CandidateNode], int, float]] = [([], {}, 0, 0.0)]
    task_count = len(candidate_groups)
    for _ in range(task_count):
        expanded = []
        for order, selected, previous, score in states:
            for task_index, group in enumerate(candidate_groups):
                if task_index in selected:
                    continue
                for node in group:
                    edge = matrix.edges[previous][node.matrix_index]
                    uncertainty = (1 - min(edge.confidence, node.confidence)) * 600
                    rating = max(0, 4.8 - (node.rating or 3)) * 180
                    monetary = (node.estimated_cost_yuan or 0) * 60
                    expanded.append(
                        (
                            [*order, task_index],
                            {**selected, task_index: node},
                            node.matrix_index,
                            score + edge.duration_seconds + uncertainty + rating + monetary,
                        )
                    )
        expanded.sort(key=lambda state: state[3])
        states = expanded[:beam_width]
    return [(order, selected) for order, selected, _, _ in states]


def _ortools_joint(
    departure: datetime,
    tasks: list[PlanningTask],
    candidate_groups: list[list[CandidateNode]],
    matrix: RouteMatrix,
) -> tuple[list[int], dict[int, CandidateNode]] | None:
    """Solve candidate selection + order + time windows as one RoutingModel."""
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError:
        return None

    nodes = [node for group in candidate_groups for node in group]
    # Routing node 0 is the origin; 1..N map to candidate nodes.
    manager = pywrapcp.RoutingIndexManager(len(nodes) + 1, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def physical(routing_node: int) -> int:
        return 0 if routing_node == 0 else nodes[routing_node - 1].matrix_index

    def service_seconds(routing_node: int) -> int:
        if routing_node == 0:
            return 0
        task = tasks[nodes[routing_node - 1].task_index]
        return int(max(task.service_duration_minutes, task.min_service_duration_minutes or 0) * 60)

    def travel_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        # An open itinerary does not pay an artificial return-to-depot cost.
        if to_node == 0:
            return service_seconds(from_node)
        edge = matrix.edges[physical(from_node)][physical(to_node)]
        target = nodes[to_node - 1]
        rating_penalty = int(max(0, 4.8 - (target.rating or 3.0)) * 180)
        uncertainty_penalty = int((1 - min(edge.confidence, target.confidence)) * 600)
        monetary_penalty = int((target.estimated_cost_yuan or 0) * 60)
        return (
            int(edge.duration_seconds)
            + service_seconds(from_node)
            + rating_penalty
            + uncertainty_penalty
            + monetary_penalty
        )

    cost_index = routing.RegisterTransitCallback(travel_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_index)

    def time_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        if to_node == 0:
            return service_seconds(from_node)
        return int(
            matrix.edges[physical(from_node)][physical(to_node)].duration_seconds
        ) + service_seconds(from_node)

    time_index = routing.RegisterTransitCallback(time_callback)
    horizon = 7 * 24 * 3600
    routing.AddDimension(time_index, horizon, horizon, True, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")
    solver = routing.solver()
    for task_index, group in enumerate(candidate_groups):
        route_indices = [manager.NodeToIndex(nodes.index(node) + 1) for node in group]
        for index in route_indices:
            routing.AddDisjunction([index], 0)
        solver.Add(sum(routing.ActiveVar(index) for index in route_indices) == 1)
        task = tasks[task_index]
        earliest = task.appointment_time or task.earliest_arrival
        latest = task.appointment_time or task.deadline
        lower = max(0, int((earliest - departure).total_seconds())) if earliest else 0
        upper = min(horizon, int((latest - departure).total_seconds())) if latest else horizon
        if upper < 0 or lower > upper:
            return None
        for index in route_indices:
            time_dimension.CumulVar(index).SetRange(lower, upper)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = 2
    solution = routing.SolveWithParameters(params)
    if solution is None:
        return None
    order: list[int] = []
    selected: dict[int, CandidateNode] = {}
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node_index = manager.IndexToNode(index)
        if node_index:
            node = nodes[node_index - 1]
            order.append(node.task_index)
            selected[node.task_index] = node
        index = solution.Value(routing.NextVar(index))
    return order, selected


def optimize_joint_route(
    departure: datetime,
    tasks: list[PlanningTask],
    candidate_groups: list[list[CandidateNode]],
    matrix: RouteMatrix,
    preferences: PlanningPreferences,
    constraints: HardConstraints,
    mode: TransportMode,
) -> tuple[RouteEvaluation, str]:
    count = len(tasks)
    search_size = 1
    for group in candidate_groups:
        search_size *= len(group)
    exact = count <= 6 and search_size * max(1, _factorial(count)) <= 60_000

    evaluations: list[RouteEvaluation] = []
    if exact:
        for selection in product(*candidate_groups):
            selected = {node.task_index: node for node in selection}
            for permutation_order in permutations(range(count)):
                evaluations.append(
                    evaluate_joint_order(
                        list(permutation_order),
                        selected,
                        departure,
                        tasks,
                        matrix,
                        preferences,
                        constraints,
                        mode,
                    )
                )
        algorithm = "joint-exact-enumeration"
    else:
        ortools_solution = _ortools_joint(departure, tasks, candidate_groups, matrix)
        candidates_to_evaluate = (
            [ortools_solution]
            if ortools_solution is not None
            else _beam_joint(candidate_groups, matrix)
        )
        for beam_order, selected in candidates_to_evaluate:
            evaluations.append(
                evaluate_joint_order(
                    beam_order,
                    selected,
                    departure,
                    tasks,
                    matrix,
                    preferences,
                    constraints,
                    mode,
                )
            )
        algorithm = (
            "ortools-routing-time-windows"
            if ortools_solution is not None
            else "joint-beam-search-fallback"
        )

    if not evaluations:
        raise ValueError("没有可用于联合求解的候选地点")
    return min(evaluations, key=_evaluation_key), algorithm


def _factorial(value: int) -> int:
    result = 1
    for number in range(2, value + 1):
        result *= number
    return result


# Compatibility entry point retained for focused optimizer tests and benchmarks.
def optimize_route(
    departure: datetime,
    tasks: list[PlanningTask],
    distances: list[list[float]],
    durations: list[list[float]],
) -> tuple[RouteEvaluation, str]:
    from datetime import timezone

    from backend.app.schemas.ai_intent import DataQuality, RouteEdge

    generated = datetime.now(timezone.utc)
    edges = [
        [
            RouteEdge(
                origin_index=i,
                destination_index=j,
                distance_meters=distances[i][j],
                duration_seconds=durations[i][j],
                source="compatibility_matrix",
                quality=DataQuality.estimated,
                confidence=0.5,
                fallback_used=True,
            )
            for j in range(len(row))
        ]
        for i, row in enumerate(distances)
    ]
    matrix = RouteMatrix(edges=edges, provider="compatibility", generated_at=generated)
    groups = [
        [
            CandidateNode(
                task_index=i, candidate_rank=0, matrix_index=i + 1, rating=None, confidence=0.5
            )
        ]
        for i in range(len(tasks))
    ]
    result, algorithm = optimize_joint_route(
        departure,
        tasks,
        groups,
        matrix,
        PlanningPreferences(),
        HardConstraints(),
        TransportMode.walking,
    )
    return (
        result,
        "exact-permutation" if algorithm == "joint-exact-enumeration" else "nearest-neighbor+2-opt",
    )
