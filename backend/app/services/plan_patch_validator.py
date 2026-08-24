"""Shared deterministic validation for initial plans and accepted patches."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from backend.app.core.exceptions import AppError
from backend.app.schemas.ai_intent import (
    Coordinate,
    HardConstraints,
    PlanningPreferences,
    PlanningTask,
    TransportMode,
)
from backend.app.services.route_optimizer import CandidateNode, evaluate_joint_order


def apply_patch_structure(
    snapshot: dict[str, Any], operations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    stops = list(snapshot.get("stops") or [])
    for operation in operations:
        if operation["operation"] == "remove_stop":
            stop_id = operation.get("stop_id")
            position = next(
                (i for i, stop in enumerate(stops) if stop["poi"]["id"] == stop_id), None
            )
            if position is None:
                raise AppError(422, "PATCH_STOP_NOT_FOUND", f"找不到站点 {stop_id!r}")
            stops.pop(position)
        elif operation["operation"] == "move_stop":
            source = operation.get("from_position")
            target = operation.get("to_position")
            if source is None or target is None or source >= len(stops) or target >= len(stops):
                raise AppError(422, "PATCH_POSITION_INVALID", "移动站点的位置无效")
            stops.insert(target, stops.pop(source))
        elif operation["operation"] == "replace_stop":
            stop_id = operation.get("stop_id")
            replacement = operation.get("replacement_stop")
            position = next(
                (i for i, stop in enumerate(stops) if stop["poi"]["id"] == stop_id), None
            )
            if position is None:
                raise AppError(422, "PATCH_STOP_NOT_FOUND", f"找不到站点 {stop_id!r}")
            if (
                not isinstance(replacement, dict)
                or not replacement.get("poi")
                or not replacement.get("task")
            ):
                raise AppError(422, "PATCH_REPLACEMENT_INVALID", "替换站点必须包含 POI 和任务")
            # Preserve the task identity. A replacement may change a Provider
            # POI, but it cannot smuggle in a different planning obligation.
            replacement = dict(replacement)
            replacement["task_index"] = stops[position].get("task_index")
            replacement["task"] = stops[position]["task"]
            stops[position] = replacement
        elif operation["operation"] == "change_transport_mode":
            try:
                snapshot.setdefault("intent", {})["transport_mode"] = TransportMode(
                    operation.get("transport_mode")
                ).value
            except (TypeError, ValueError) as exc:
                raise AppError(422, "PATCH_TRANSPORT_MODE_INVALID", "交通方式无效") from exc
    if not stops:
        raise AppError(422, "PATCH_EMPTY_PLAN", "正式计划至少需要保留一个站点")
    return stops


async def recalculate_and_validate_snapshot(
    snapshot: dict[str, Any],
    stops: list[dict[str, Any]],
    provider: Any,
    replan_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    context = replan_context or {}
    origin = context.get("origin") or snapshot.get("origin")
    departure_raw = context.get("departure_time") or snapshot.get("departure_time")
    if not origin or not departure_raw:
        raise AppError(409, "PATCH_CONTEXT_MISSING", "原计划缺少可重算的起点或出发时间")

    intent = snapshot.get("intent") or {}
    mode = TransportMode(intent["transport_mode"])
    full_tasks = [PlanningTask.model_validate(item) for item in intent.get("tasks") or []]
    preferences = PlanningPreferences.model_validate(intent.get("preferences") or {})
    constraints = HardConstraints.model_validate(
        (intent.get("constraints") or {}).get("hard") or {}
    )
    departure = datetime.fromisoformat(departure_raw)
    coordinates = [
        Coordinate.model_validate(origin),
        *(Coordinate.model_validate(stop["poi"]["location"]) for stop in stops),
    ]
    matrix = await provider.route_matrix(coordinates, mode)

    order: list[int] = []
    selected: dict[int, CandidateNode] = {}
    conflicts: list[str] = []
    for position, stop in enumerate(stops, start=1):
        task_index = int(stop.get("task_index", -1))
        if not 0 <= task_index < len(full_tasks) or task_index in selected:
            conflicts.append("补丁破坏了任务身份或包含重复任务")
            continue
        poi = stop.get("poi") or {}
        order.append(task_index)
        selected[task_index] = CandidateNode(
            task_index=task_index,
            candidate_rank=int(stop.get("candidate_rank") or 0),
            matrix_index=position,
            rating=poi.get("rating"),
            confidence=float(poi.get("confidence") or 0),
            estimated_cost_yuan=poi.get("estimated_cost_yuan"),
            open_now=poi.get("open_now"),
            wheelchair_accessible=poi.get("wheelchair_accessible"),
            district=poi.get("district"),
        )
    required_missing = [
        index for index, task in enumerate(full_tasks) if task.required and index not in selected
    ]
    if required_missing:
        conflicts.append(
            "补丁删除了必经任务：" + "、".join(str(index + 1) for index in required_missing)
        )
    if conflicts:
        return snapshot, conflicts

    safety_buffer = max(
        (
            int(item.get("safety_buffer_minutes") or 0)
            for item in (intent.get("constraints") or {}).get("uncertain") or []
        ),
        default=0,
    )
    evaluation = await asyncio.to_thread(
        evaluate_joint_order,
        order,
        selected,
        departure,
        full_tasks,
        matrix,
        preferences,
        constraints,
        mode,
        safety_buffer_minutes=safety_buffer,
    )
    conflicts.extend(evaluation.conflicts)
    by_task = {node.task_index: index for index, node in enumerate(evaluation.selected_nodes)}
    for stop in stops:
        task_index = int(stop["task_index"])
        result_index = by_task[task_index]
        node = selected[task_index]
        edge = matrix.edges[
            0 if result_index == 0 else selected[evaluation.order[result_index - 1]].matrix_index
        ][node.matrix_index]
        stop["arrival_time"] = evaluation.arrivals[result_index].isoformat()
        stop["departure_time"] = evaluation.departures[result_index].isoformat()
        stop["travel"] = edge.model_dump(mode="json")
        stop["constraint_satisfied"] = not any(
            full_tasks[task_index].description in conflict for conflict in evaluation.conflicts
        )

    snapshot["stops"] = stops
    snapshot["total_distance_meters"] = evaluation.total_distance
    snapshot["total_travel_seconds"] = evaluation.total_travel_seconds
    snapshot["estimated_cost_yuan"] = sum(
        float((stop.get("poi") or {}).get("estimated_cost_yuan") or 0) for stop in stops
    )
    snapshot["confidence"] = min(
        (float((stop.get("travel") or {}).get("confidence") or 0) for stop in stops),
        default=0,
    )
    snapshot["algorithm"] = "shared-joint-validator-plan-patch"
    return snapshot, conflicts
