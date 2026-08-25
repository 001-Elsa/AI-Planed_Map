"""Permission-checked deterministic tools used by planning Agents.

These classes are execution adapters, not Agents. They validate a stable input
contract, enforce the capability registry at the call boundary, and return a
model-safe result envelope alongside typed runtime data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from backend.app.clients.amap_client import MapProvider
from backend.app.schemas.agent_artifacts import AgentType
from backend.app.schemas.ai_intent import (
    Coordinate,
    HardConstraints,
    PlanningPreferences,
    PlanningTask,
    PoiCandidate,
    RouteEdge,
    RouteMatrix,
    TransportMode,
)
from backend.app.services.agent_tool_contracts import (
    OptimizeRouteArgs,
    RouteMatrixArgs,
    SearchPoiArgs,
    ToolResultEnvelope,
    default_tool_expiry,
    stable_tool_error,
    tool_result_error,
    tool_result_success,
)
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, DataScope, InvocationMode
from backend.app.services.agents.base import canonical_hash
from backend.app.services.route_optimizer import (
    CandidateNode,
    RouteEvaluation,
    evaluate_joint_order,
    optimize_joint_route,
)

T = TypeVar("T")


@dataclass(frozen=True)
class ToolExecution(Generic[T]):
    data: T | None
    result: ToolResultEnvelope


class SearchPoiTool:
    def __init__(self, provider: MapProvider, *, timeout_seconds: float) -> None:
        self.provider = provider
        self.timeout_seconds = timeout_seconds

    async def execute(self, arguments: SearchPoiArgs) -> ToolExecution[list[PoiCandidate]]:
        arguments = SearchPoiArgs.model_validate(arguments)
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.search,
            capability="search_poi",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.map_search}),
        )
        artifact_ref = f"poi:{canonical_hash(arguments.model_dump(mode='json'))[:24]}"
        try:
            candidates = await asyncio.wait_for(
                self.provider.search_poi(arguments.keyword, arguments.origin, arguments.city),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return ToolExecution(
                data=None,
                result=tool_result_error(
                    stable_tool_error(exc),
                    retryable=True,
                    source=self.provider.name,
                ),
            )
        return ToolExecution(
            data=candidates,
            result=tool_result_success(
                "search_poi",
                {"candidate_count": len(candidates)},
                source=self.provider.name,
                expires_at=default_tool_expiry(),
                confidence=min((item.confidence for item in candidates), default=0.5),
                artifact_ref=artifact_ref,
            ),
        )


class RouteMatrixTool:
    def __init__(self, provider: MapProvider) -> None:
        self.provider = provider

    async def execute(
        self, points: list[Coordinate], transport_mode: TransportMode
    ) -> ToolExecution[RouteMatrix]:
        arguments = RouteMatrixArgs(points=points, transport_mode=transport_mode)
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.planner,
            capability="get_route_matrix",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.route_matrix}),
        )
        try:
            matrix = await self.provider.route_matrix(arguments.points, arguments.transport_mode)
        except Exception as exc:
            return ToolExecution(
                data=None,
                result=tool_result_error(
                    stable_tool_error(exc), retryable=True, source=self.provider.name
                ),
            )
        return ToolExecution(
            data=matrix,
            result=tool_result_success(
                "get_route_matrix",
                {"point_count": len(points), "provider": matrix.provider},
                source=self.provider.name,
                expires_at=default_tool_expiry(),
                confidence=min(
                    (edge.confidence for row in matrix.edges for edge in row), default=0.5
                ),
                artifact_ref=f"matrix:{canonical_hash(arguments.model_dump(mode='json'))[:24]}",
            ),
        )


class OptimizeRouteTool:
    async def execute(
        self,
        *,
        departure: datetime,
        tasks: list[PlanningTask],
        candidate_groups: list[list[CandidateNode]],
        matrix: RouteMatrix,
        preferences: PlanningPreferences,
        constraints: HardConstraints,
        transport_mode: TransportMode,
        safety_buffer_minutes: int,
    ) -> ToolExecution[tuple[RouteEvaluation, str]]:
        arguments = OptimizeRouteArgs(
            candidate_group_count=len(candidate_groups),
            transport_mode=transport_mode,
            hard_constraints=constraints.model_dump(mode="json"),
        )
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.planner,
            capability="optimize_route",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.route_optimization}),
        )
        try:
            output = await asyncio.to_thread(
                optimize_joint_route,
                departure,
                tasks,
                candidate_groups,
                matrix,
                preferences,
                constraints,
                transport_mode,
                safety_buffer_minutes=safety_buffer_minutes,
            )
        except Exception as exc:
            return ToolExecution(
                data=None,
                result=tool_result_error(stable_tool_error(exc), retryable=False),
            )
        evaluation, algorithm = output
        return ToolExecution(
            data=output,
            result=tool_result_success(
                "optimize_route",
                {"algorithm": algorithm, "feasible": evaluation.feasible},
                artifact_ref=f"solution:{canonical_hash(arguments.model_dump(mode='json'))[:24]}",
            ),
        )


class VerifyTransitEdgesTool:
    def __init__(self, provider: MapProvider) -> None:
        self.provider = provider

    async def execute(
        self, points: list[Coordinate], city: str | None
    ) -> ToolExecution[list[RouteEdge]]:
        arguments = RouteMatrixArgs(points=points, transport_mode=TransportMode.transit)
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.planner,
            capability="verify_transit_edges",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.transit_routes}),
        )
        try:
            edges = await self.provider.transit_route_edges(arguments.points, city)
        except Exception as exc:
            return ToolExecution(
                data=None,
                result=tool_result_error(
                    stable_tool_error(exc), retryable=True, source=self.provider.name
                ),
            )
        return ToolExecution(
            data=edges,
            result=tool_result_success(
                "verify_transit_edges",
                {"edge_count": len(edges)},
                source=self.provider.name,
                expires_at=default_tool_expiry(),
                confidence=min((edge.confidence for edge in edges), default=0.5),
                artifact_ref=f"transit:{canonical_hash(arguments.model_dump(mode='json'))[:24]}",
            ),
        )


async def reevaluate_selected_route(
    *,
    evaluation: RouteEvaluation,
    departure: datetime,
    tasks: list[PlanningTask],
    matrix: RouteMatrix,
    preferences: PlanningPreferences,
    constraints: HardConstraints,
    transport_mode: TransportMode,
    safety_buffer_minutes: int,
) -> RouteEvaluation:
    selected_by_task = {node.task_index: node for node in evaluation.selected_nodes}
    return await asyncio.to_thread(
        evaluate_joint_order,
        evaluation.order,
        selected_by_task,
        departure,
        tasks,
        matrix,
        preferences,
        constraints,
        transport_mode,
        safety_buffer_minutes=safety_buffer_minutes,
    )
