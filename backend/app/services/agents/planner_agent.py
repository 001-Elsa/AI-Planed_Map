"""Executable itinerary Agent backed by deterministic route tools."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import Field

from backend.app.clients.amap_client import MapProvider
from backend.app.core.config import Settings
from backend.app.schemas.agent_artifacts import (
    AgentBudget,
    AgentSpec,
    AgentType,
    ArtifactEnvelope,
)
from backend.app.schemas.ai_intent import (
    AIPlanResult,
    CandidateReview,
    ClarificationQuestion,
    Coordinate,
    PlannedStop,
    PlanningIntent,
    PlanningState,
    TransportMode,
    UncertaintySummary,
)
from backend.app.schemas.common import StrictModel
from backend.app.services.agent_context import PlanningContext
from backend.app.services.agent_planning_tools import (
    OptimizeRouteTool,
    RouteMatrixTool,
    VerifyTransitEdgesTool,
    reevaluate_selected_route,
)
from backend.app.services.agent_tool_contracts import ToolResultEnvelope
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, InvocationMode
from backend.app.services.agents.base import AgentExecution, canonical_hash
from backend.app.services.agents.search_agent import SearchArtifact
from backend.app.services.route_optimizer import CandidateNode
from backend.app.services.uncertainty import heuristic_envelope

SHANGHAI = ZoneInfo("Asia/Shanghai")


class PlannerAgentInput(StrictModel):
    """Legacy direct-call adapter; orchestration uses ``PlanningContext``."""

    intent: PlanningIntent
    origin: Coordinate
    city: str | None = Field(default=None, max_length=50)
    max_candidates_per_task: int = Field(default=3, ge=1, le=5)
    search: SearchArtifact


PlannerRunContext = PlanningContext | PlannerAgentInput


PLANNER_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.planner,
    prompt_version="planner-stage-v2-executable",
    context_view="route_planning_minimal",
    allowed_tools=frozenset(),
    allowed_internal_capabilities=TOOL_REGISTRY.names_for(
        AgentType.planner, InvocationMode.internal_stage
    ),
    input_artifact_types=frozenset({"search_artifact", "safety_report"}),
    output_artifact_type="plan_candidate",
    budget=AgentBudget(
        max_steps=1, max_input_tokens=4_000, max_output_tokens=1_000, max_cost_usd=0
    ),
)


class PlannerAgent:
    """Chooses deterministic planning tools and produces the formal plan candidate."""

    spec = PLANNER_AGENT_SPEC

    def __init__(self, provider: MapProvider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings
        self.matrix_tool = RouteMatrixTool(provider)
        self.optimizer_tool = OptimizeRouteTool()
        self.transit_tool = VerifyTransitEdgesTool(provider)

    async def run(self, context: PlannerRunContext) -> AgentExecution[AIPlanResult]:
        started = time.perf_counter()
        if context.search.clarification_questions:
            result = AIPlanResult(
                status="need_clarification",
                planning_state=PlanningState.need_clarification,
                intent=context.intent,
                origin=context.origin,
                questions=context.search.clarification_questions,
            )
            return self._execution(result, context, started, tool_results=[])

        task_count = len(context.intent.tasks)
        per_task_limit = min(
            context.max_candidates_per_task,
            max(1, (self.settings.max_route_matrix_points - 1) // task_count),
        )
        candidates = [group[:per_task_limit] for group in context.search.candidate_groups]
        flattened = [candidate for group in candidates for candidate in group]
        points = [context.origin, *(candidate.location for candidate in flattened)]
        matrix_call = await self.matrix_tool.execute(points, context.intent.transport_mode)
        tool_results = [matrix_call.result]
        if matrix_call.data is None:
            result = self._tool_unavailable_result(
                context,
                field="route_matrix",
                error_code=matrix_call.result.error_code or "UPSTREAM_ERROR",
            )
            return self._execution(result, context, started, tool_results=tool_results)
        matrix = matrix_call.data

        candidate_groups: list[list[CandidateNode]] = []
        matrix_index = 1
        for task_index, group in enumerate(candidates):
            nodes: list[CandidateNode] = []
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

        departure = context.intent.departure_time or datetime.now(SHANGHAI).replace(
            second=0, microsecond=0
        )
        if departure.tzinfo is None:
            departure = departure.replace(tzinfo=SHANGHAI)
        safety_buffer = max(
            (item.safety_buffer_minutes for item in context.intent.constraints.uncertain),
            default=0,
        )
        optimize_call = await self.optimizer_tool.execute(
            departure=departure,
            tasks=context.intent.tasks,
            candidate_groups=candidate_groups,
            matrix=matrix,
            preferences=context.intent.preferences,
            constraints=context.intent.constraints.hard,
            transport_mode=context.intent.transport_mode,
            safety_buffer_minutes=safety_buffer,
        )
        tool_results.append(optimize_call.result)
        if optimize_call.data is None:
            result = self._tool_unavailable_result(
                context,
                field="route_optimizer",
                error_code=optimize_call.result.error_code or "UPSTREAM_ERROR",
            )
            return self._execution(result, context, started, tool_results=tool_results)
        evaluation, algorithm = optimize_call.data

        transit_warning: str | None = None
        if context.intent.transport_mode == TransportMode.transit and evaluation.selected_nodes:
            sequence_points = [
                context.origin,
                *(
                    candidates[node.task_index][node.candidate_rank].location
                    for node in evaluation.selected_nodes
                ),
            ]
            if context.intent.constraints.hard.must_return_to_origin:
                sequence_points.append(context.origin)
            transit_call = await self.transit_tool.execute(sequence_points, context.city)
            tool_results.append(transit_call.result)
            if transit_call.data is None:
                transit_warning = (
                    "公共交通精修暂不可用，保留路线矩阵中的可审计估算结果"
                    f"（{transit_call.result.error_code}）。"
                )
            else:
                transit_edges = transit_call.data
                refined_matrix = matrix.model_copy(deep=True)
                previous_matrix_index = 0
                for node, edge in zip(evaluation.selected_nodes, transit_edges, strict=False):
                    refined_matrix.edges[previous_matrix_index][node.matrix_index] = (
                        edge.model_copy(
                            update={
                                "origin_index": previous_matrix_index,
                                "destination_index": node.matrix_index,
                            }
                        )
                    )
                    previous_matrix_index = node.matrix_index
                if context.intent.constraints.hard.must_return_to_origin and len(
                    transit_edges
                ) > len(evaluation.selected_nodes):
                    refined_matrix.edges[previous_matrix_index][0] = transit_edges[-1].model_copy(
                        update={
                            "origin_index": previous_matrix_index,
                            "destination_index": 0,
                        }
                    )
                matrix = refined_matrix
                evaluation = await reevaluate_selected_route(
                    evaluation=evaluation,
                    departure=departure,
                    tasks=context.intent.tasks,
                    matrix=matrix,
                    preferences=context.intent.preferences,
                    constraints=context.intent.constraints.hard,
                    transport_mode=context.intent.transport_mode,
                    safety_buffer_minutes=safety_buffer,
                )
                algorithm += "+amap-transit-refinement"

        planned_stops: list[PlannedStop] = []
        previous = 0
        confidences: list[float] = []
        estimated_edges = 0
        for position, node in enumerate(evaluation.selected_nodes):
            task = context.intent.tasks[node.task_index]
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
                task_description=context.intent.tasks[task_index].description,
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
        envelope = heuristic_envelope(
            expected_seconds=evaluation.total_travel_seconds,
            mean_confidence=confidence,
            fallback_used=bool(estimated_edges),
            safety_buffer_minutes=safety_buffer,
            has_deadline=any(task.deadline for task in context.intent.tasks),
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
        if transit_warning:
            warnings.append(transit_warning)
        if context.intent.constraints.uncertain:
            warnings.extend(item.reason for item in context.intent.constraints.uncertain)
        if evaluation.feasible and evaluation.conflicts:
            warnings.extend(evaluation.conflicts)

        common = {
            "intent": context.intent,
            "origin": context.origin,
            "departure_time": departure,
            "stops": planned_stops,
            "total_distance_meters": evaluation.total_distance,
            "total_travel_seconds": evaluation.total_travel_seconds,
            "algorithm": algorithm,
            "warnings": warnings,
            "score": evaluation.score,
            "confidence": confidence,
            "candidate_count": len(flattened),
            "candidate_reviews": candidate_reviews,
            "uncertainty": uncertainty,
        }
        if not evaluation.feasible:
            result = AIPlanResult(
                status="infeasible",
                planning_state=PlanningState.infeasible,
                explanation=("联合求解器已尝试候选地点和访问顺序，但没有方案满足全部硬约束。"),
                conflicts=evaluation.conflicts,
                **common,
            )
        else:
            minutes = round(evaluation.total_travel_seconds / 60)
            result = AIPlanResult(
                status="success",
                planning_state=PlanningState.plan_ready,
                explanation=(
                    f"已联合比较 {len(flattened)} 个候选地点、访问顺序与时间约束，"
                    f"生成可验证方案，纯交通时间约 {minutes} 分钟。"
                ),
                **common,
            )
        return self._execution(result, context, started, tool_results=tool_results)

    @staticmethod
    def _tool_unavailable_result(
        context: PlannerRunContext, *, field: str, error_code: str
    ) -> AIPlanResult:
        return AIPlanResult(
            status="need_clarification",
            planning_state=PlanningState.need_clarification,
            intent=context.intent,
            origin=context.origin,
            questions=[
                ClarificationQuestion(
                    field=field,
                    reason=error_code,
                    question="路线数据暂不可用，是否稍后重试规划？",
                )
            ],
            warnings=[f"确定性规划工具未完成：{error_code}"],
        )

    def _execution(
        self,
        result: AIPlanResult,
        context: PlannerRunContext,
        started: float,
        *,
        tool_results: list[ToolResultEnvelope],
    ) -> AgentExecution[AIPlanResult]:
        payload = {
            "workflow_state": "plan_candidate_ready",
            "status": result.status,
            "algorithm": result.algorithm,
            "candidate_count": result.candidate_count,
            "stop_count": len(result.stops),
            "conflict_count": len(result.conflicts),
            "warning_count": len(result.warnings),
            "tool_error_codes": [
                item.error_code for item in tool_results if not item.success and item.error_code
            ],
        }
        artifact = ArtifactEnvelope(
            artifact_type=self.spec.output_artifact_type,
            producer_agent=AgentType.planner,
            payload=payload,
            confidence=result.confidence if result.status == "success" else 0.6,
            evidence_refs=[
                item.artifact_ref
                for item in tool_results
                if item.success and item.artifact_ref is not None
            ],
            input_hash=canonical_hash(context.model_dump(mode="json")),
        )
        return AgentExecution(
            spec=self.spec,
            output=result,
            artifact=artifact,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=any(not item.success for item in tool_results),
            reason=(
                "deterministic_tool_degraded"
                if any(not item.success for item in tool_results)
                else None
            ),
        )
