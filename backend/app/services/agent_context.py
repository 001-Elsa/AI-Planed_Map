"""Minimal context builders for Agent runs.

Agents should receive compact, role-scoped facts and artifact references rather
than an ever-growing conversation transcript.
"""

from __future__ import annotations

from typing import Any

from backend.app.models import TripSession
from backend.app.schemas.agent_artifacts import minimize_agent_payload


def build_companion_context(
    *,
    trip: TripSession,
    observation: dict[str, Any],
    route_plan: dict[str, Any] | None,
    tool_history: list[dict[str, Any]],
    max_chars: int = 6000,
) -> dict[str, Any]:
    plan = route_plan or {}
    stops = plan.get("stops") or []
    memory = plan.get("memory") or {}
    context = {
        "trip": {
            "trip_id": trip.id,
            "state": trip.state,
            "plan_version": trip.current_plan_version,
        },
        "current_observation": minimize_agent_payload(observation),
        "formal_plan_snapshot": {
            "planning_run_id": trip.planning_run_id,
            "plan_version": plan.get("plan_version") or trip.current_plan_version,
            "status": plan.get("status"),
            "transport_mode": (plan.get("intent") or {}).get("transport_mode"),
            "stop_count": len(stops),
            "stop_refs": [
                {
                    "index": index,
                    "poi_id": ((stop.get("poi") or {}).get("id")),
                    "name": ((stop.get("poi") or {}).get("name")),
                    "arrival_time": stop.get("arrival_time"),
                    "departure_time": stop.get("departure_time"),
                }
                for index, stop in enumerate(stops[:12])
                if isinstance(stop, dict)
            ],
            "warnings": (plan.get("warnings") or [])[:5],
        },
        "confirmed_memory": {
            "applied_keys": memory.get("applied_keys") or [],
            "values_included": False,
        },
        "recent_tool_results": [
            minimize_agent_payload(item)
            for item in tool_history[-5:]
        ],
    }
    encoded = str(context)
    if len(encoded) <= max_chars:
        return context
    context["formal_plan_snapshot"]["stop_refs"] = context["formal_plan_snapshot"][
        "stop_refs"
    ][:5]
    context["recent_tool_results"] = context["recent_tool_results"][-2:]
    context["truncated"] = True
    return context
