"""Offline quality gate for the isolated Agent roles.

Run: python backend/tests/evaluation/evaluate_agents.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.app.schemas.agent_artifacts import AgentType  # noqa: E402
from backend.app.services.agent_shared_state import READ_FIELDS, WRITE_FIELDS  # noqa: E402
from backend.app.services.agent_tool_registry import (  # noqa: E402
    TOOL_REGISTRY,
    CapabilityAuthorizationError,
    DataScope,
    InvocationMode,
)
from backend.app.services.agents.critic_agent import RuleBasedCriticAgent  # noqa: E402
from backend.app.services.agents.registry import AGENT_REGISTRY  # noqa: E402


def base_plan() -> dict:
    return {
        "status": "success",
        "explanation": "比较真实候选与路线后生成方案",
        "intent": {"preferences": {"prefer_high_rating": False}},
        "stops": [
            {
                "task_index": 0,
                "poi": {
                    "id": "provider-poi-1",
                    "name": "博物馆",
                    "source": "amap",
                    "rating": 4.5,
                },
                "travel": {
                    "source": "amap-walking",
                    "confidence": 0.9,
                    "fallback_used": False,
                },
            }
        ],
        "candidate_reviews": [{"candidates": [{"id": "provider-poi-1", "rating": 4.5}]}],
    }


async def main() -> int:
    cases: list[tuple[str, dict, str]] = []
    cases.append(("evidence_complete", base_plan(), "approved"))

    estimated = deepcopy(base_plan())
    estimated["stops"][0]["travel"]["fallback_used"] = True
    cases.append(("estimated_route", estimated, "approved_with_warnings"))

    missing = deepcopy(base_plan())
    missing["stops"][0]["poi"]["source"] = "unknown"
    cases.append(("missing_poi_evidence", missing, "needs_clarification"))

    rating = deepcopy(base_plan())
    rating["intent"]["preferences"]["prefer_high_rating"] = True
    rating["stops"][0]["poi"]["rating"] = 3.5
    rating["candidate_reviews"][0]["candidates"].append({"id": "provider-poi-2", "rating": 4.8})
    cases.append(("soft_rating_retry", rating, "retry_with_soft_adjustments"))

    critic = RuleBasedCriticAgent()
    passed = 0
    results = []
    for name, plan, expected in cases:
        actual = (await critic.run(plan)).output.verdict
        ok = actual == expected
        passed += int(ok)
        results.append({"case": name, "expected": expected, "actual": actual, "passed": ok})

    registry_ok = set(AGENT_REGISTRY) == {
        AgentType.supervisor,
        AgentType.intent,
        AgentType.search,
        AgentType.safety,
        AgentType.planner,
        AgentType.critic,
        AgentType.companion,
    }
    isolation_ok = (
        not AGENT_REGISTRY[AgentType.supervisor].allowed_tools
        and not AGENT_REGISTRY[AgentType.intent].allowed_tools
        and not AGENT_REGISTRY[AgentType.search].allowed_tools
        and not AGENT_REGISTRY[AgentType.safety].allowed_tools
        and not AGENT_REGISTRY[AgentType.planner].allowed_tools
        and not AGENT_REGISTRY[AgentType.critic].allowed_tools
        and "propose_replan" in AGENT_REGISTRY[AgentType.companion].allowed_tools
    )
    shared_state_isolation_ok = (
        "user_requirement" in READ_FIELDS[AgentType.search]
        and "route_plan" not in READ_FIELDS[AgentType.search]
        and "poi_candidates" in WRITE_FIELDS[AgentType.search]
        and "route_plan" not in WRITE_FIELDS[AgentType.search]
        and "poi_candidates" in READ_FIELDS[AgentType.safety]
        and "route_plan" not in READ_FIELDS[AgentType.safety]
        and "poi_candidates" not in READ_FIELDS[AgentType.critic]
        and "user_requirement" not in READ_FIELDS[AgentType.companion]
    )
    capability_isolation_ok = (
        TOOL_REGISTRY.names_for(AgentType.intent, InvocationMode.internal_stage)
        == frozenset({"parse_requirement"})
        and TOOL_REGISTRY.names_for(AgentType.search, InvocationMode.internal_stage)
        == frozenset({"search_poi"})
        and TOOL_REGISTRY.names_for(AgentType.safety, InvocationMode.internal_stage)
        == frozenset({"check_travel_safety"})
        and TOOL_REGISTRY.names_for(AgentType.planner, InvocationMode.internal_stage)
        == frozenset({"get_route_matrix", "optimize_route", "verify_transit_edges"})
    )
    try:
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.companion,
            capability="search_poi",
            invocation_mode=InvocationMode.agent_callable,
            requested_scopes=frozenset({DataScope.map_search}),
        )
    except CapabilityAuthorizationError:
        pass
    else:
        capability_isolation_ok = False
    memory_isolation_ok = all(
        (capability := TOOL_REGISTRY.get(name)) is not None
        and capability.invocation_mode == InvocationMode.workflow_only
        and not capability.agents
        for name in (
            "load_confirmed_preferences",
            "save_explicit_preference",
            "delete_explicit_preference",
        )
    )
    try:
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.planner,
            capability="get_route_matrix",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset(
                {DataScope.route_matrix, DataScope.user_preferences}
            ),
        )
    except CapabilityAuthorizationError:
        pass
    else:
        capability_isolation_ok = False
    summary = {
        "cases": results,
        "quality_rate": passed / len(cases),
        "registry_matches_supervised_topology": registry_ok,
        "tool_isolation": isolation_ok,
        "shared_state_isolation": shared_state_isolation_ok,
        "capability_isolation": capability_isolation_ok,
        "memory_isolation": memory_isolation_ok,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return (
        0
        if passed == len(cases)
        and registry_ok
        and isolation_ok
        and shared_state_isolation_ok
        and capability_isolation_ok
        and memory_isolation_ok
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
