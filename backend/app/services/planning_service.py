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
    ClarificationQuestion,
    PlannedStop,
    PlanningState,
    UncertaintySummary,
)
from backend.app.services.intent_parser import IntentParser
from backend.app.services.route_optimizer import CandidateNode, optimize_joint_route

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

    async def plan(self, request: AIPlanRequest) -> AIPlanResult:
        intent = await self.parser.parse(request.text)
        if request.departure_time:
            intent.departure_time = request.departure_time
        if request.transport_mode:
            intent.transport_mode = request.transport_mode
        if request.constraints:
            intent.constraints = request.constraints
        for task in intent.tasks:
            if task.service_duration_minutes == 0:
                task.service_duration_minutes = request.default_service_duration_minutes

        questions: list[ClarificationQuestion] = []
        if request.origin is None:
            questions.append(
                ClarificationQuestion(
                    field="origin",
                    reason="路线矩阵和候选地点召回必须有可信起点",
                    question="请提供出发位置，或允许使用当前定位。",
                )
            )
        if (
            intent.preferences.minimize_walking
            and intent.constraints.hard.max_walking_meters is None
        ):
            questions.append(
                ClarificationQuestion(
                    field="constraints.hard.max_walking_meters",
                    reason="用户要求少走路，但没有可验证的步行上限",
                    question="本次行程最多能接受多少米步行？",
                )
            )
        if questions:
            return AIPlanResult(
                status="need_clarification",
                planning_state=PlanningState.need_clarification,
                intent=intent,
                origin=request.origin,
                questions=questions,
            )
        origin = request.origin
        if origin is None:
            raise RuntimeError("澄清阶段结束后仍缺少起点")

        keywords = [
            task.location_name or task.category or task.description for task in intent.tasks
        ]
        search_results = await asyncio.gather(
            *(self.map_provider.search_poi(keyword, origin, request.city) for keyword in keywords)
        )
        missing = [
            ClarificationQuestion(
                field=f"tasks.{index}.location",
                reason="地图 Provider 未返回可验证的真实候选地点",
                question=f"没有找到“{keywords[index]}”，可以提供更具体的名称或区域吗？",
            )
            for index, found in enumerate(search_results)
            if not found
        ]
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
        evaluation, algorithm = optimize_joint_route(
            departure,
            intent.tasks,
            candidate_groups,
            matrix,
            intent.preferences,
            intent.constraints.hard,
            intent.transport_mode,
        )

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

        confidence = sum(confidences) / len(confidences) if confidences else 0
        spread = 1 - confidence
        uncertainty = UncertaintySummary(
            expected_duration_seconds=evaluation.total_travel_seconds,
            lower_duration_seconds=max(0, evaluation.total_travel_seconds * (1 - 0.15 * spread)),
            upper_duration_seconds=evaluation.total_travel_seconds * (1 + 0.60 * spread),
            on_time_probability=(
                confidence if any(task.deadline for task in intent.tasks) else None
            ),
            method="provider-confidence-safety-envelope-v1",
        )
        warnings = []
        if estimated_edges:
            warnings.append(
                f"{estimated_edges} 段路线使用估算数据，时间仅供参考，前端应显示估算标记。"
            )
        if intent.constraints.uncertain:
            warnings.extend(item.reason for item in intent.constraints.uncertain)

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
            uncertainty=uncertainty,
        )
