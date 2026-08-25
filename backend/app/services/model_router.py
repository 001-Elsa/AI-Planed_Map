"""Cost-aware, fail-closed model routing for isolated Agent roles."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import Field

from backend.app.core.config import Settings
from backend.app.core.observability import metrics
from backend.app.schemas.agent_artifacts import AgentType
from backend.app.schemas.common import StrictModel


class ModelTier(str, Enum):
    rule = "rule"
    small = "small"
    strong = "strong"
    deterministic = "deterministic"


class RoutingRisk(str, Enum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ModelRoutingContext(StrictModel):
    agent_type: AgentType
    task_count: int = Field(default=0, ge=0, le=100)
    hard_constraint_count: int = Field(default=0, ge=0, le=100)
    uncertainty_count: int = Field(default=0, ge=0, le=100)
    text_length: int = Field(default=0, ge=0, le=100_000)
    risk: RoutingRisk = RoutingRisk.none
    structured_output_required: bool = False
    model_available: bool = False
    budget_remaining_usd: float | None = Field(default=None, ge=0)
    failure_count: int = Field(default=0, ge=0, le=20)


class ModelRouteDecision(StrictModel):
    agent_type: AgentType
    tier: ModelTier
    model_name: str | None = Field(default=None, max_length=100)
    complexity_score: int = Field(ge=0, le=20)
    risk: RoutingRisk
    reason_codes: list[str] = Field(min_length=1, max_length=12)
    requires_critic: bool = False
    requires_hitl: bool = False
    fallback_tier: ModelTier | None = None
    estimated_input_cost_per_million_usd: float = Field(default=0, ge=0)
    estimated_output_cost_per_million_usd: float = Field(default=0, ge=0)


_TASK_SPLIT = re.compile(r"(?:然后|再去|接着|之后|最后|、|，|,|;|；|\n)")
_HARD_SIGNAL = re.compile(
    r"(?:必须|务必|之前|最晚|不能|不要|避开|预算|无障碍|轮椅|老人|儿童|孩子|"
    r"deadline|before|avoid|budget|wheelchair)",
    re.IGNORECASE,
)
_UNCERTAIN_SIGNAL = re.compile(
    r"(?:随便|看情况|差不多|大概|可能|尽量|合适|附近|最好|不确定|"
    r"maybe|roughly|nearby|if possible)",
    re.IGNORECASE,
)
_HIGH_RISK_SIGNAL = re.compile(
    r"(?:暴雨|台风|雷暴|封路|闭馆|偏航|延误|赶不上|"
    r"hospital|emergency|closed|off.?route|storm)",
    re.IGNORECASE,
)
_SENSITIVE_PARTY_SIGNAL = re.compile(
    r"(?:老人|轮椅|儿童|孩子|elderly|wheelchair|child)", re.IGNORECASE
)


def routing_context_from_text(
    text: str,
    *,
    agent_type: AgentType,
    model_available: bool,
) -> ModelRoutingContext:
    fragments = [item.strip() for item in _TASK_SPLIT.split(text) if item.strip()]
    task_count = max(1, len(fragments)) if text.strip() else 0
    hard_count = len(_HARD_SIGNAL.findall(text))
    uncertainty_count = len(_UNCERTAIN_SIGNAL.findall(text))
    risk = (
        RoutingRisk.high
        if _HIGH_RISK_SIGNAL.search(text)
        else RoutingRisk.medium
        if _SENSITIVE_PARTY_SIGNAL.search(text)
        else RoutingRisk.none
    )
    return ModelRoutingContext(
        agent_type=agent_type,
        task_count=task_count,
        hard_constraint_count=hard_count,
        uncertainty_count=uncertainty_count,
        text_length=len(text),
        risk=risk,
        structured_output_required=agent_type == AgentType.intent,
        model_available=model_available,
    )


def routing_context_from_plan(
    plan: dict[str, Any],
    *,
    agent_type: AgentType,
    model_available: bool,
) -> ModelRoutingContext:
    intent = plan.get("intent") or {}
    constraints = intent.get("constraints") or {}
    hard = constraints.get("hard") or {}
    uncertain = constraints.get("uncertain") or []
    risk = RoutingRisk.none
    if plan.get("status") != "success" or plan.get("conflicts"):
        risk = RoutingRisk.high
    party = hard.get("party") or {}
    if any(int(party.get(key) or 0) for key in ("elderly", "children", "wheelchair_users")):
        risk = RoutingRisk.high
    return ModelRoutingContext(
        agent_type=agent_type,
        task_count=len(intent.get("tasks") or plan.get("stops") or []),
        hard_constraint_count=sum(
            value not in (None, False, [], {}, "") for value in hard.values()
        ),
        uncertainty_count=len(uncertain),
        risk=risk,
        structured_output_required=True,
        model_available=model_available,
    )


class ModelRouter:
    """Select execution tier; it never grants tools or bypasses HITL."""

    _DETERMINISTIC_ROLES = frozenset(
        {
            AgentType.supervisor,
            AgentType.search,
            AgentType.safety,
            AgentType.planner,
            AgentType.replanner,
        }
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def small_model(self) -> str:
        return self.settings.llm_small_model or self.settings.llm_model

    @property
    def strong_model(self) -> str:
        return self.settings.llm_strong_model or self.settings.llm_model

    def route(self, context: ModelRoutingContext) -> ModelRouteDecision:
        score = min(
            20,
            max(0, context.task_count - 1)
            + min(context.hard_constraint_count, 5)
            + 2 * min(context.uncertainty_count, 3)
            + int(context.text_length > 300)
            + int(context.risk in {RoutingRisk.high, RoutingRisk.critical}) * 2,
        )
        reasons: list[str] = []
        tier = ModelTier.rule
        model_name: str | None = None

        if context.agent_type in self._DETERMINISTIC_ROLES:
            tier = ModelTier.deterministic
            reasons.append("role_has_deterministic_execution")
        elif not self.settings.model_router_enabled:
            tier = ModelTier.small if context.model_available else ModelTier.rule
            reasons.append("dynamic_routing_disabled")
        elif not context.model_available:
            tier = ModelTier.rule
            reasons.append("model_credentials_unavailable")
        elif context.budget_remaining_usd is not None and context.budget_remaining_usd < 0.001:
            tier = ModelTier.rule
            reasons.append("model_budget_exhausted")
        elif context.failure_count > 0:
            tier = ModelTier.rule
            reasons.append("model_failure_circuit_fallback")
        elif context.agent_type == AgentType.companion:
            tier = ModelTier.small
            reasons.append("companion_bounded_small_model")
        elif context.agent_type == AgentType.critic and (
            score >= self.settings.model_router_strong_min_complexity
            or context.uncertainty_count >= self.settings.model_router_strong_min_uncertainty
            or context.risk in {RoutingRisk.high, RoutingRisk.critical}
        ):
            tier = ModelTier.strong
            reasons.append("critic_complex_or_high_risk_review")
        elif score <= self.settings.model_router_rule_max_complexity:
            tier = ModelTier.rule
            reasons.append("simple_request_rule_path")
        elif (
            score >= self.settings.model_router_strong_min_complexity
            or context.uncertainty_count >= self.settings.model_router_strong_min_uncertainty
        ):
            tier = ModelTier.strong
            reasons.append("complex_or_uncertain_request")
        else:
            tier = ModelTier.small
            reasons.append("bounded_structured_small_model")

        if tier == ModelTier.small:
            model_name = self.small_model
        elif tier == ModelTier.strong:
            model_name = self.strong_model
        requires_critic = bool(
            score >= self.settings.model_router_strong_min_complexity
            or context.uncertainty_count > 0
            or context.risk in {RoutingRisk.high, RoutingRisk.critical}
        )
        requires_hitl = context.risk in {RoutingRisk.high, RoutingRisk.critical}
        decision = ModelRouteDecision(
            agent_type=context.agent_type,
            tier=tier,
            model_name=model_name,
            complexity_score=score,
            risk=context.risk,
            reason_codes=reasons,
            requires_critic=requires_critic,
            requires_hitl=requires_hitl,
            fallback_tier=ModelTier.rule if tier in {ModelTier.small, ModelTier.strong} else None,
            estimated_input_cost_per_million_usd=(
                self.settings.llm_small_input_cost_per_million_usd
                if tier == ModelTier.small
                else self.settings.llm_strong_input_cost_per_million_usd
                if tier == ModelTier.strong
                else 0
            ),
            estimated_output_cost_per_million_usd=(
                self.settings.llm_small_output_cost_per_million_usd
                if tier == ModelTier.small
                else self.settings.llm_strong_output_cost_per_million_usd
                if tier == ModelTier.strong
                else 0
            ),
        )
        metrics.increment(
            "mapgo_model_router_decisions_total",
            {
                "agent": context.agent_type.value,
                "tier": tier.value,
                "risk": context.risk.value,
            },
        )
        metrics.observe(
            "mapgo_model_router_complexity_score",
            score,
            {"agent": context.agent_type.value, "tier": tier.value},
        )
        return decision

    def public_policy(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.model_router_enabled,
            "tiers": [item.value for item in ModelTier],
            "model_credentials_available": bool(self.settings.llm_api_key),
            "small_model_override_configured": bool(self.settings.llm_small_model),
            "strong_model_override_configured": bool(self.settings.llm_strong_model),
            "compatibility_model_configured": bool(self.settings.llm_model),
            "rule_max_complexity": self.settings.model_router_rule_max_complexity,
            "strong_min_complexity": self.settings.model_router_strong_min_complexity,
            "strong_min_uncertainty": self.settings.model_router_strong_min_uncertainty,
            "role_policy": {
                "intent": "rule_or_small_or_strong_structured_output",
                "supervisor": "deterministic",
                "search": "deterministic_query_and_map_tool",
                "safety": "deterministic_rule",
                "planner": "deterministic_route_optimizer",
                "critic": "rule_or_strong_hybrid",
                "companion": "rule_or_small",
                "replanner": "deterministic_strategy",
            },
            "high_risk_action": "hitl",
        }
