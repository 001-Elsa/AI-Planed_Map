import asyncio
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.clients.amap_client import MapProvider
from backend.app.core.config import Settings
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    AIPlanResult,
    CandidateReview,
    ClarificationQuestion,
    Coordinate,
    PlannedStop,
    PlanningState,
    PoiCandidate,
    TransportMode,
    UncertaintySummary,
)
from backend.app.services.clarification import select_clarification_questions
from backend.app.services.intent_parser import IntentParser
from backend.app.services.route_optimizer import (
    CandidateNode,
    evaluate_joint_order,
    optimize_joint_route,
)
from backend.app.services.uncertainty import heuristic_envelope

SHANGHAI = ZoneInfo("Asia/Shanghai")


def request_fingerprint(
    owner: str,
    request: AIPlanRequest,
    model: str,
    prompt_version: str,
) -> str:
    normalized = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{owner}|{normalized}|{model}|{prompt_version}".encode()).hexdigest()


class PlanningService:
    def __init__(
        self,
        parser: IntentParser,
        map_provider: MapProvider,
        settings: Settings,
    ) -> None:
        self.parser = parser
        self.map_provider = map_provider
        self.settings = settings

    async def _search_candidates(
        self,
        keywords: list[str],
        origin: Coordinate,
        city: str | None,
    ) -> list[list[PoiCandidate]]:
        recalled = await asyncio.gather(
            *(self.map_provider.search_poi(keyword, origin, city) for keyword in keywords),
            return_exceptions=True,
        )
        results: list[list[PoiCandidate] | None] = [None] * len(keywords)
        failures: list[tuple[int, Exception]] = []
        for index, item in enumerate(recalled):
            if isinstance(item, BaseException):
                if isinstance(item, Exception):
                    failures.append((index, item))
                else:
                    raise item
            else:
                results[index] = item

        # AMap's JS credential bridge can occasionally time out when several
        # input-tip requests establish connections simultaneously. Retry only
        # the failed recalls sequentially so one transient timeout cannot abort
        # an otherwise valid multi-stop plan.
        for index, _ in failures:
            await asyncio.sleep(0.15)
            try:
                results[index] = await self.map_provider.search_poi(keywords[index], origin, city)
            except Exception:
                results[index] = None

        if results and all(item is None for item in results) and failures:
            raise failures[0][1]
        return [item or [] for item in results]

    async def plan(self, request: AIPlanRequest) -> AIPlanResult:
        intent = await self.parser.parse(request.text)
        if request.departure_time:
            intent.departure_time = request.departure_time
        if request.transport_mode:
            intent.transport_mode = request.transport_mode
        if request.constraints:
            intent.constraints = request.constraints
        # Conversation answers must modify the typed planning intent before
        # candidate recall and optimisation.  Persisting them alone made a
        # successful answer look accepted while silently planning the old trip.
        for key, value in request.preferences_answers.items():
            if key == "dietary_restrictions":
                values = value if isinstance(value, list) else [value]
                intent.preferences.dietary_restrictions = [str(item) for item in values if item]
            elif key == "optimization_goal" and value in {
                "balanced",
                "shortest_time",
                "shortest_distance",
            }:
                intent.preferences.optimization_goal = value
            elif key in {
                "minimize_distance",
                "minimize_walking",
                "minimize_cost",
                "prefer_high_rating",
            }:
                setattr(intent.preferences, key, bool(value))
        for raw_index, location in request.task_location_overrides.items():
            index = int(raw_index)
            if not 0 <= index < len(intent.tasks):
                raise ValueError(f"task location override index out of range: {index}")
            intent.tasks[index].location_name = location
            intent.tasks[index].location_hint = location
        for raw_index, field_overrides in request.task_field_overrides.items():
            index = int(raw_index)
            if not 0 <= index < len(intent.tasks):
                raise ValueError(f"task field override index out of range: {index}")
            if "appointment_time" in field_overrides:
                appointment = field_overrides["appointment_time"]
                if isinstance(appointment, str):
                    appointment = datetime.fromisoformat(appointment)
                if appointment.tzinfo is None:
                    appointment = appointment.replace(tzinfo=SHANGHAI)
                # An appointment is an arrival-time constraint, not merely
                # display metadata.  The optimizer may wait for it but cannot
                # schedule the stop after it.
                intent.tasks[index] = intent.tasks[index].model_copy(
                    update={
                        "appointment_time": appointment,
                        "earliest_arrival": appointment,
                        "deadline": appointment,
                    }
                )
        for task in intent.tasks:
            if task.service_duration_minutes == 0:
                task.service_duration_minutes = request.default_service_duration_minutes

        questions: list[ClarificationQuestion] = select_clarification_questions(
            request=request,
            intent=intent,
            text=request.text,
            max_questions=3,
        )
        # Keep the critical origin gate even if dynamic selector omitted it.
        if request.origin is None and not any(item.field == "origin" for item in questions):
            questions.insert(
                0,
                ClarificationQuestion(
                    field="origin",
                    reason="路线矩阵和候选地点召回必须有可信起点",
                    question="请提供出发位置，或允许使用当前定位。",
                ),
            )
        # Prefer required questions before optional preference probes.
        required_questions = [item for item in questions if item.required]
        if required_questions:
            return AIPlanResult(
                status="need_clarification",
                planning_state=PlanningState.need_clarification,
                intent=intent,
                origin=request.origin,
                questions=required_questions[:3],
            )
        origin = request.origin
        if origin is None:
            raise RuntimeError("澄清阶段结束后仍缺少起点")

        keywords = [self._recall_keyword(task, intent) for task in intent.tasks]
        search_results = await self._search_candidates(keywords, origin, request.city)
        ambiguous = {
            index: found[:5]
            for index, found in enumerate(search_results)
            if found
            and len(found) >= 2
            and len({item.name.strip().casefold() for item in found[:3]}) == 1
            and str(index) not in request.task_poi_overrides
        }
        selected_missing: list[ClarificationQuestion] = []
        for raw_index, poi_id in request.task_poi_overrides.items():
            index = int(raw_index)
            if not 0 <= index < len(search_results):
                raise ValueError(f"task POI override index out of range: {index}")
            candidates_for_task = search_results[index]
            selected = next((item for item in candidates_for_task if item.id == poi_id), None)
            if selected is None:
                selected_missing.append(
                    ClarificationQuestion(
                        field=f"tasks.{index}.selected_poi_id",
                        reason="The selected POI is no longer returned by the map provider",
                        question="该地点已无法验证，请重新选择一个候选地点。",
                        candidates=candidates_for_task[:5],
                    )
                )
            else:
                # A resolved ambiguity is a hard user choice, not a ranking
                # hint.  Keep exactly that POI so the joint solver cannot
                # silently select another same-name result for a lower score.
                search_results[index] = [selected]
        missing = [
            ClarificationQuestion(
                field=f"tasks.{index}.location",
                reason="地图 Provider 未返回可验证的真实候选地点",
                question=f"没有找到“{keywords[index]}”，可以提供更具体的名称或区域吗？",
            )
            for index, found in enumerate(search_results)
            if not found
        ]
        if selected_missing:
            missing = selected_missing
        elif not missing and ambiguous:
            missing = select_clarification_questions(
                request=request,
                intent=intent,
                ambiguous_pois=ambiguous,
                text=request.text,
                max_questions=2,
            )
        if missing:
            return AIPlanResult(
                status="need_clarification",
                planning_state=PlanningState.need_clarification,
                intent=intent,
                origin=request.origin,
                questions=missing,
            )

        # Reserve one matrix point for the origin and share the remaining budget
        # fairly across tasks. This prevents a single request from exploding into
        # hundreds of upstream calls.
        per_task_limit = min(
            request.max_candidates_per_task,
            max(1, (self.settings.max_route_matrix_points - 1) // len(intent.tasks)),
        )
        candidates = [items[:per_task_limit] for items in search_results]
        flattened = [candidate for group in candidates for candidate in group]
        points = [origin, *(candidate.location for candidate in flattened)]
        matrix = await self.map_provider.route_matrix(points, intent.transport_mode)

        candidate_groups: list[list[CandidateNode]] = []
        matrix_index = 1
        for task_index, group in enumerate(candidates):
            nodes = []
            for rank, candidate in enumerate(group):
                nodes.append(
                    CandidateNode(
                        task_index=task_index,
                        candidate_rank=rank,
                        matrix_index=matrix_index,
                        rating=candidate.rating,
                        confidence=candidate.confidence,
                        estimated_cost_yuan=candidate.estimated_cost_yuan,
                        open_now=candidate.open_now,
                        wheelchair_accessible=candidate.wheelchair_accessible,
                        district=candidate.district,
                    )
                )
                matrix_index += 1
            candidate_groups.append(nodes)

        departure = intent.departure_time or datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        if departure.tzinfo is None:
            departure = departure.replace(tzinfo=SHANGHAI)
        safety_buffer = max(
            (item.safety_buffer_minutes for item in intent.constraints.uncertain),
            default=0,
        )
        evaluation, algorithm = optimize_joint_route(
            departure,
            intent.tasks,
            candidate_groups,
            matrix,
            intent.preferences,
            intent.constraints.hard,
            intent.transport_mode,
            safety_buffer_minutes=safety_buffer,
        )

        # Public transit uses the same candidate/order solver, then verifies the
        # chosen sequence with real AMap transfer routes. This keeps upstream
        # calls linear in the number of stops instead of querying every pair.
        if intent.transport_mode == TransportMode.transit and evaluation.selected_nodes:
            selected_by_task_nodes = {node.task_index: node for node in evaluation.selected_nodes}
            sequence_points = [
                origin,
                *(
                    candidates[node.task_index][node.candidate_rank].location
                    for node in evaluation.selected_nodes
                ),
            ]
            if intent.constraints.hard.must_return_to_origin:
                sequence_points.append(origin)
            transit_edges = await self.map_provider.transit_route_edges(
                sequence_points, request.city
            )
            refined_matrix = matrix.model_copy(deep=True)
            previous_matrix_index = 0
            for node, edge in zip(
                evaluation.selected_nodes,
                transit_edges,
                strict=False,
            ):
                refined_matrix.edges[previous_matrix_index][node.matrix_index] = edge.model_copy(
                    update={
                        "origin_index": previous_matrix_index,
                        "destination_index": node.matrix_index,
                    }
                )
                previous_matrix_index = node.matrix_index
            if intent.constraints.hard.must_return_to_origin and len(transit_edges) > len(
                evaluation.selected_nodes
            ):
                refined_matrix.edges[previous_matrix_index][0] = transit_edges[-1].model_copy(
                    update={
                        "origin_index": previous_matrix_index,
                        "destination_index": 0,
                    }
                )
            matrix = refined_matrix
            evaluation = evaluate_joint_order(
                evaluation.order,
                selected_by_task_nodes,
                departure,
                intent.tasks,
                matrix,
                intent.preferences,
                intent.constraints.hard,
                intent.transport_mode,
                safety_buffer_minutes=safety_buffer,
            )
            algorithm += "+amap-transit-refinement"

        planned_stops: list[PlannedStop] = []
        previous = 0
        confidences: list[float] = []
        estimated_edges = 0
        for position, node in enumerate(evaluation.selected_nodes):
            task = intent.tasks[node.task_index]
            candidate = candidates[node.task_index][node.candidate_rank]
            edge = matrix.edges[previous][node.matrix_index]
            if edge.fallback_used:
                estimated_edges += 1
            confidences.append(min(edge.confidence, candidate.confidence))
            planned_stops.append(
                PlannedStop(
                    task_index=node.task_index,
                    candidate_rank=node.candidate_rank,
                    task=task,
                    poi=candidate,
                    arrival_time=evaluation.arrivals[position],
                    departure_time=evaluation.departures[position],
                    travel=edge,
                    constraint_satisfied=not (
                        task.deadline and evaluation.arrivals[position] > task.deadline
                    ),
                )
            )
            previous = node.matrix_index

        selected_by_task = {node.task_index: node for node in evaluation.selected_nodes}
        candidate_reviews = [
            CandidateReview(
                task_index=task_index,
                task_description=intent.tasks[task_index].description,
                considered_count=len(group),
                selected_poi_id=(
                    group[selected_by_task[task_index].candidate_rank].id
                    if task_index in selected_by_task
                    else None
                ),
                candidates=group,
            )
            for task_index, group in enumerate(candidates)
        ]

        confidence = sum(confidences) / len(confidences) if confidences else 0
        # ETA observations are collected for post-trip analysis but are not
        # yet queried as a statistically representative cohort at plan time.
        # Do not claim historical calibration until that data path exists.
        envelope = heuristic_envelope(
            expected_seconds=evaluation.total_travel_seconds,
            mean_confidence=confidence,
            fallback_used=bool(estimated_edges),
            safety_buffer_minutes=safety_buffer,
            has_deadline=any(task.deadline for task in intent.tasks),
        )
        uncertainty = UncertaintySummary(
            expected_duration_seconds=envelope.expected_seconds,
            lower_duration_seconds=envelope.lower_seconds,
            upper_duration_seconds=envelope.upper_seconds,
            on_time_probability=envelope.on_time_probability,
            method=envelope.method,
        )
        warnings = list(envelope.warnings)
        if estimated_edges:
            warnings.append(
                f"{estimated_edges} 段路线使用估算数据，时间仅供参考，前端应显示估算标记。"
            )
        if intent.constraints.uncertain:
            warnings.extend(item.reason for item in intent.constraints.uncertain)
        if evaluation.feasible and evaluation.conflicts:
            warnings.extend(evaluation.conflicts)

        if not evaluation.feasible:
            return AIPlanResult(
                status="infeasible",
                planning_state=PlanningState.infeasible,
                intent=intent,
                origin=origin,
                departure_time=departure,
                stops=planned_stops,
                total_distance_meters=evaluation.total_distance,
                total_travel_seconds=evaluation.total_travel_seconds,
                algorithm=algorithm,
                explanation="联合求解器已尝试候选地点和访问顺序，但没有方案满足全部硬约束。",
                conflicts=evaluation.conflicts,
                warnings=warnings,
                score=evaluation.score,
                confidence=confidence,
                candidate_count=len(flattened),
                candidate_reviews=candidate_reviews,
                uncertainty=uncertainty,
            )

        minutes = round(evaluation.total_travel_seconds / 60)
        return AIPlanResult(
            status="success",
            planning_state=PlanningState.plan_ready,
            intent=intent,
            origin=origin,
            departure_time=departure,
            stops=planned_stops,
            total_distance_meters=evaluation.total_distance,
            total_travel_seconds=evaluation.total_travel_seconds,
            algorithm=algorithm,
            explanation=(
                f"已联合比较 {len(flattened)} 个候选地点、访问顺序与时间约束，"
                f"生成可验证方案，纯交通时间约 {minutes} 分钟。"
            ),
            warnings=warnings,
            score=evaluation.score,
            confidence=confidence,
            candidate_count=len(flattened),
            candidate_reviews=candidate_reviews,
            uncertainty=uncertainty,
        )

    @staticmethod
    def _recall_keyword(task, intent) -> str:
        """Attach confirmed dining requirements to the actual POI recall query."""
        base = task.location_name or task.category or task.description
        dining_terms = ("餐", "饭", "吃", "咖啡", "茶", "food", "restaurant")
        task_text = f"{task.description} {task.location_name or ''} {task.category or ''}".lower()
        if intent.preferences.dietary_restrictions and any(
            term in task_text for term in dining_terms
        ):
            restrictions = " ".join(intent.preferences.dietary_restrictions)
            return f"{base} {restrictions}"
        return base
