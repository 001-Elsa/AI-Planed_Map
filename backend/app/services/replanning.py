"""Deterministic generation of a pending dynamic replan Patch.

This module deliberately creates a proposal only.  Applying a patch remains in
the API's version validator and requires an explicit user decision.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.clients.amap_client import MapProvider
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.models import DecisionAuditLog, PlanPatch, PlanVersion, TripSession
from backend.app.schemas.ai_intent import Coordinate, PlanningTask, TransportMode
from backend.app.schemas.companion import TripState
from backend.app.services.route_optimizer import optimize_route


@dataclass(frozen=True)
class PendingReplanRequest:
    current_location: Coordinate
    current_time: datetime
    completed_stop_ids: list[str] = field(default_factory=list)
    reason: str = "动态事件触发重规划"
    source_event_id: int | None = None
    event_type: str | None = None
    event_payload: dict[str, Any] = field(default_factory=dict)
    weather: dict[str, Any] | None = None


def _is_outdoor(stop: dict[str, Any], explicit_ids: set[str]) -> bool:
    poi = stop.get("poi") or {}
    if poi.get("id") in explicit_ids:
        return True
    if poi.get("is_outdoor") is not None:
        return bool(poi["is_outdoor"])
    task = stop.get("task") or {}
    text = " ".join(
        str(value or "")
        for value in (
            poi.get("name"),
            task.get("description"),
            task.get("location_name"),
            task.get("category"),
        )
    ).lower()
    if any(word in text for word in ("室内", "商场", "博物馆", "美术馆", "展馆", "影院", "咖啡")):
        return False
    return any(
        word in text for word in ("公园", "园林", "徒步", "户外", "广场", "山", "湖", "动物园")
    )


def _weather_requires_indoor(weather: dict[str, Any] | None) -> bool:
    if not weather:
        return False
    return (
        float(weather.get("precipitation_probability") or 0) >= 50
        or int(weather.get("weather_code") or 0) >= 80
    )


def _closed_ids(
    event_type: str | None, payload: dict[str, Any], remaining: list[dict[str, Any]]
) -> set[str]:
    values = payload.get("closed_poi_ids") or []
    if payload.get("closed_poi_id"):
        values = [*values, payload["closed_poi_id"]]
    closed = {str(value) for value in values}
    reason = str(payload.get("reason") or "").lower()
    if event_type == "PoiStatusChanged" and not closed and remaining:
        closed.add(str(remaining[0]["poi"]["id"]))
    if (
        any(word in reason for word in ("关闭", "闭馆", "closed", "close"))
        and not closed
        and remaining
    ):
        closed.add(str(remaining[0]["poi"]["id"]))
    return closed


async def _replacement_candidates(
    provider: MapProvider,
    *,
    stop: dict[str, Any],
    current_location: Coordinate,
    indoor: bool,
) -> dict[str, Any] | None:
    task = stop.get("task") or {}
    poi = stop.get("poi") or {}
    base = str(
        task.get("category")
        or task.get("location_name")
        or task.get("description")
        or poi.get("name")
    )
    keyword = f"{base} {'室内' if indoor else '替代'}"
    candidates = await provider.search_poi(keyword, current_location, poi.get("district"))
    original_id = str(poi.get("id"))
    selected = next((item for item in candidates if item.id != original_id), None)
    if selected is None:
        return None
    replacement = copy.deepcopy(stop)
    replacement["poi"] = selected.model_dump(mode="json")
    replacement["candidate_rank"] = 0
    return replacement


async def create_pending_replan(
    *,
    db: AsyncSession,
    trip: TripSession,
    provider: MapProvider,
    request: PendingReplanRequest,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Solve remaining work and persist a pending Patch, never a PlanVersion."""
    settings = get_settings()
    current_time = request.current_time
    if current_time.tzinfo is None:
        # SQLite commonly strips timezone information from DateTime columns.
        # Worker events are stored as UTC, so restore that invariant before
        # comparing them with timezone-aware task windows.
        current_time = current_time.replace(tzinfo=timezone.utc)
    context = json.loads(trip.context_json or "{}")
    replan_count = int(context.get("replan_count") or 0)
    if replan_count >= settings.max_replans_per_trip:
        return {"status": "replan_budget_exceeded", "patch_created": False}
    version = await db.scalar(
        select(PlanVersion).where(
            PlanVersion.planning_run_id == trip.planning_run_id,
            PlanVersion.version == trip.current_plan_version,
        )
    )
    if version is None:
        raise AppError(409, "PLAN_VERSION_MISSING", "当前正式计划版本不存在")
    snapshot = json.loads(version.snapshot_json)
    if request.source_event_id is not None:
        pending = (
            await db.scalars(
                select(PlanPatch).where(
                    PlanPatch.planning_run_id == trip.planning_run_id,
                    PlanPatch.base_version == trip.current_plan_version,
                    PlanPatch.status == "pending",
                )
            )
        ).all()
        for patch in pending:
            impact = json.loads(patch.impact_json or "{}")
            if impact.get("source_event_id") == request.source_event_id:
                return {
                    "status": "patch_pending_confirmation",
                    "patch_created": False,
                    "deduplicated": True,
                    "patch_id": patch.id,
                    "impact": impact,
                }

    completed = {str(item) for item in request.completed_stop_ids}
    original_stops = list(snapshot.get("stops") or [])
    remaining_items = [
        {"source_id": str(stop["poi"]["id"]), "stop": copy.deepcopy(stop)}
        for stop in original_stops
        if str(stop["poi"]["id"]) not in completed
    ]
    if not remaining_items:
        raise AppError(409, "NO_REMAINING_STOPS", "没有需要重规划的剩余站点")

    weather_indoor = _weather_requires_indoor(request.weather)
    closed = _closed_ids(
        request.event_type, request.event_payload, [item["stop"] for item in remaining_items]
    )
    explicit_outdoors = {str(item) for item in request.event_payload.get("outdoor_stop_ids") or []}
    replacements: list[dict[str, Any]] = []
    for item in remaining_items:
        source_id, stop = item["source_id"], item["stop"]
        indoors_needed = weather_indoor and _is_outdoor(stop, explicit_outdoors)
        if source_id not in closed and not indoors_needed:
            continue
        replacement = await _replacement_candidates(
            provider,
            stop=stop,
            current_location=request.current_location,
            indoor=indoors_needed,
        )
        if replacement is None:
            continue
        item["stop"] = replacement
        replacements.append(
            {
                "source_stop_id": source_id,
                "replacement_stop_id": replacement["poi"]["id"],
                "reason": "weather_indoor" if indoors_needed else "poi_closed",
            }
        )

    base_mode = TransportMode(snapshot["intent"]["transport_mode"])
    modes = [base_mode, *(item for item in TransportMode if item != base_mode)]
    options: list[dict[str, Any]] = []
    solved: list[tuple[Any, str, list[dict[str, Any]], TransportMode, bool]] = []
    for candidate_mode in modes:
        for drop_optional in (False, True):
            active = remaining_items
            if drop_optional:
                active = [
                    item
                    for item in remaining_items
                    if bool((item["stop"].get("task") or {}).get("required", True))
                ]
                if len(active) == len(remaining_items) or not active:
                    continue
            points = [
                request.current_location,
                *(Coordinate.model_validate(item["stop"]["poi"]["location"]) for item in active),
            ]
            matrix = await provider.route_matrix(points, candidate_mode)
            tasks = [PlanningTask.model_validate(item["stop"]["task"]) for item in active]
            evaluation, algorithm = optimize_route(
                current_time, tasks, matrix.distances, matrix.durations
            )
            option = {
                "label": f"方案{chr(65 + len(options))}",
                "transport_mode": candidate_mode.value,
                "drop_optional": drop_optional,
                "feasible": evaluation.feasible,
                "total_travel_seconds": evaluation.total_travel_seconds,
                "total_distance_meters": evaluation.total_distance,
                "conflicts": evaluation.conflicts,
                "algorithm": algorithm,
                "stop_ids": [active[index]["stop"]["poi"]["id"] for index in evaluation.order],
            }
            options.append(option)
            if evaluation.feasible:
                solved.append((evaluation, algorithm, active, candidate_mode, drop_optional))
            if len(options) >= 4:
                break
        if len(options) >= 4:
            break
    if not solved:
        trip.state = TripState.at_risk.value
        await db.commit()
        return {
            "status": "no_feasible_replan",
            "patch_created": False,
            "alternatives": options,
            "replacements": replacements,
        }

    # Stay with the existing mode where possible.  Delay and traffic events
    # are explicit permission to select the fastest feasible transport mode.
    switch_requested = request.event_type in {
        "ScheduleDelayDetected",
        "TrafficChanged",
        "DeadlineRiskDetected",
    } or bool(request.event_payload.get("allow_transport_switch"))
    preferred = next((entry for entry in solved if entry[3] == base_mode and not entry[4]), None)
    chosen = (
        min(solved, key=lambda entry: entry[0].total_travel_seconds)
        if switch_requested or preferred is None
        else preferred
    )
    evaluation, algorithm, active, chosen_mode, drop_optional = chosen
    ordered = [active[index] for index in evaluation.order]
    active_source_ids = {item["source_id"] for item in active}
    operations: list[dict[str, Any]] = [
        {"operation": "remove_stop", "stop_id": str(stop["poi"]["id"])}
        for stop in original_stops
        if str(stop["poi"]["id"]) not in active_source_ids
    ]
    working = [
        str(stop["poi"]["id"])
        for stop in original_stops
        if str(stop["poi"]["id"]) in active_source_ids
    ]
    for target_position, item in enumerate(ordered):
        source_id = item["source_id"]
        source_position = working.index(source_id)
        if source_position != target_position:
            operations.append(
                {
                    "operation": "move_stop",
                    "from_position": source_position,
                    "to_position": target_position,
                }
            )
            working.insert(target_position, working.pop(source_position))
    for item in ordered:
        replacement = next(
            (record for record in replacements if record["source_stop_id"] == item["source_id"]),
            None,
        )
        if replacement:
            operations.append(
                {
                    "operation": "replace_stop",
                    "stop_id": item["source_id"],
                    "replacement_stop": item["stop"],
                }
            )
    if chosen_mode != base_mode:
        operations.append(
            {"operation": "change_transport_mode", "transport_mode": chosen_mode.value}
        )

    before_cost = sum(
        float((stop.get("poi") or {}).get("estimated_cost_yuan") or 0) for stop in original_stops
    )
    after_cost = sum(
        float((item["stop"].get("poi") or {}).get("estimated_cost_yuan") or 0) for item in ordered
    )
    impact = {
        "source_event_id": request.source_event_id,
        "event_type": request.event_type,
        "weather": request.weather,
        "before": {
            "plan_version": trip.current_plan_version,
            "total_travel_seconds": snapshot.get("total_travel_seconds"),
            "total_distance_meters": snapshot.get("total_distance_meters"),
            "estimated_cost_yuan": before_cost,
            "transport_mode": base_mode.value,
        },
        "after": {
            "total_travel_seconds": evaluation.total_travel_seconds,
            "total_distance_meters": evaluation.total_distance,
            "estimated_cost_yuan": after_cost,
            "transport_mode": chosen_mode.value,
            "feasible": evaluation.feasible,
            "constraint_conflicts": evaluation.conflicts,
        },
        "changes": {"replacements": replacements, "dropped_optional": drop_optional},
        "algorithm": algorithm,
        "alternatives": options,
        "replan_context": {
            "origin": request.current_location.model_dump(mode="json"),
            "departure_time": current_time.isoformat(),
        },
    }
    if not operations:
        trip.state = TripState.active_trip.value
        await db.commit()
        return {
            "status": "current_plan_still_feasible",
            "patch_created": False,
            "impact": impact,
            "alternatives": options,
        }
    patch = PlanPatch(
        planning_run_id=trip.planning_run_id,
        user_id=trip.user_id,
        base_version=trip.current_plan_version,
        operations_json=json.dumps(operations, ensure_ascii=False),
        reason=request.reason,
        impact_json=json.dumps(impact, ensure_ascii=False),
        status="pending",
    )
    db.add(patch)
    context["replan_count"] = replan_count + 1
    context["replan_proposed_at"] = datetime.now(timezone.utc).isoformat()
    trip.context_json = json.dumps(context, ensure_ascii=False)
    trip.state = TripState.replanning.value
    db.add(
        DecisionAuditLog(
            planning_run_id=trip.planning_run_id,
            user_id=trip.user_id,
            action="create_pending_replan_patch",
            reason=request.reason,
            evidence_json=json.dumps(impact, ensure_ascii=False),
            policy_result="proposal_only_requires_user_confirmation",
            trace_id=trace_id,
        )
    )
    await db.flush()
    await db.commit()
    return {
        "status": "patch_pending_confirmation",
        "patch_created": True,
        "patch_id": patch.id,
        "operations": operations,
        "impact": impact,
        "alternatives": options,
    }
