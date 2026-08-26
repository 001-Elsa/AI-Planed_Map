"""Offline gates for ModelRouter quality, cost and safety boundaries."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.app.core.config import Settings  # noqa: E402
from backend.app.schemas.agent_artifacts import AgentType  # noqa: E402
from backend.app.services.model_router import (  # noqa: E402
    ModelRouter,
    ModelRoutingContext,
    ModelTier,
    RoutingRisk,
)


def main() -> int:
    settings = Settings(
        llm_api_key="offline-eval",
        llm_small_model="small-eval",
        llm_strong_model="strong-eval",
        model_router_rule_max_complexity=1,
        model_router_strong_min_complexity=5,
        model_router_strong_min_uncertainty=2,
    )
    router = ModelRouter(settings)
    cases = [
        ("simple_intent", AgentType.intent, 1, 0, 0, RoutingRisk.none, ModelTier.rule),
        ("small_intent", AgentType.intent, 2, 1, 0, RoutingRisk.none, ModelTier.small),
        ("complex_intent", AgentType.intent, 5, 2, 1, RoutingRisk.none, ModelTier.strong),
        ("uncertain_intent", AgentType.intent, 1, 0, 2, RoutingRisk.none, ModelTier.strong),
        ("simple_critic", AgentType.critic, 1, 0, 0, RoutingRisk.none, ModelTier.rule),
        ("risky_critic", AgentType.critic, 3, 1, 0, RoutingRisk.high, ModelTier.strong),
        ("companion", AgentType.companion, 1, 0, 3, RoutingRisk.high, ModelTier.small),
        ("supervisor", AgentType.supervisor, 10, 5, 3, RoutingRisk.high, ModelTier.deterministic),
        ("search", AgentType.search, 10, 5, 3, RoutingRisk.high, ModelTier.deterministic),
        ("planner", AgentType.planner, 10, 5, 3, RoutingRisk.high, ModelTier.deterministic),
        ("safety", AgentType.safety, 10, 5, 3, RoutingRisk.high, ModelTier.deterministic),
        ("replanner", AgentType.replanner, 10, 5, 3, RoutingRisk.high, ModelTier.deterministic),
    ]
    results = []
    for name, role, tasks, hard, uncertainty, risk, expected in cases:
        decision = router.route(
            ModelRoutingContext(
                agent_type=role,
                task_count=tasks,
                hard_constraint_count=hard,
                uncertainty_count=uncertainty,
                risk=risk,
                model_available=True,
            )
        )
        results.append(
            {
                "case": name,
                "expected": expected.value,
                "actual": decision.tier.value,
                "passed": decision.tier == expected,
                "requires_hitl": decision.requires_hitl,
            }
        )
    unavailable = router.route(
        ModelRoutingContext(
            agent_type=AgentType.critic,
            task_count=10,
            risk=RoutingRisk.critical,
            model_available=False,
        )
    )
    deterministic_zero_model_calls = all(
        item[6] != ModelTier.deterministic
        or next(result for result in results if result["case"] == item[0])["actual"]
        == "deterministic"
        for item in cases
    )
    high_risk_hitl_rate = sum(
        bool(item["requires_hitl"])
        for item in results
        if next(case for case in cases if case[0] == item["case"])[5]
        in {RoutingRisk.high, RoutingRisk.critical}
    ) / sum(case[5] in {RoutingRisk.high, RoutingRisk.critical} for case in cases)
    output = {
        "cases": len(cases),
        "route_accuracy": sum(item["passed"] for item in results) / len(results),
        "deterministic_roles_never_call_model": deterministic_zero_model_calls,
        "high_risk_hitl_rate": high_risk_hitl_rate,
        "missing_credentials_fallback": unavailable.tier.value,
        "small_input_price": settings.llm_small_input_cost_per_million_usd,
        "strong_input_price": settings.llm_strong_input_cost_per_million_usd,
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    passed = (
        output["route_accuracy"] == 1
        and deterministic_zero_model_calls
        and high_risk_hitl_rate == 1
        and unavailable.tier == ModelTier.rule
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
