import httpx
import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.schemas.agent_artifacts import AgentType
from backend.app.services.agent_decider import RoutedAgentDecider
from backend.app.services.agents.critic_agent import RoutedCriticAgent
from backend.app.services.intent_parser import RoutedIntentParser
from backend.app.services.model_router import (
    ModelRouter,
    ModelRoutingContext,
    ModelTier,
    RoutingRisk,
    routing_context_from_text,
)


class FailingModelClient:
    async def post(self, *_args, **_kwargs):
        raise httpx.ConnectError("offline test")


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }


class RecordingModelClient:
    def __init__(self, content):
        self.content = content
        self.payloads = []

    async def post(self, *_args, **kwargs):
        self.payloads.append(kwargs["json"])
        return FakeResponse(self.content)


def _settings(**updates):
    values = {
        "llm_api_key": "test-key",
        "llm_small_model": "small-test-model",
        "llm_strong_model": "strong-test-model",
        "model_router_rule_max_complexity": 1,
        "model_router_strong_min_complexity": 5,
        "model_router_strong_min_uncertainty": 2,
        **updates,
    }
    return Settings(**values)


def test_router_uses_rule_small_and_strong_for_intent_complexity():
    router = ModelRouter(_settings())
    simple = router.route(
        routing_context_from_text(
            "去公园",
            agent_type=AgentType.intent,
            model_available=True,
        )
    )
    moderate = router.route(
        ModelRoutingContext(
            agent_type=AgentType.intent,
            task_count=2,
            hard_constraint_count=1,
            model_available=True,
            structured_output_required=True,
        )
    )
    complex_route = router.route(
        ModelRoutingContext(
            agent_type=AgentType.intent,
            task_count=4,
            hard_constraint_count=3,
            uncertainty_count=2,
            risk=RoutingRisk.high,
            model_available=True,
            structured_output_required=True,
        )
    )
    assert simple.tier == ModelTier.rule
    assert moderate.tier == ModelTier.small
    assert moderate.model_name == "small-test-model"
    assert complex_route.tier == ModelTier.strong
    assert complex_route.model_name == "strong-test-model"
    assert complex_route.requires_critic is True
    assert complex_route.requires_hitl is True
    assert complex_route.estimated_input_cost_per_million_usd > 0


def test_router_configuration_rejects_overlapping_thresholds():
    with pytest.raises(ValidationError):
        _settings(
            model_router_rule_max_complexity=5,
            model_router_strong_min_complexity=5,
        )


def test_router_never_sends_deterministic_roles_to_an_llm():
    router = ModelRouter(_settings())
    deterministic_roles = {
        AgentType.supervisor,
        AgentType.search,
        AgentType.safety,
        AgentType.planner,
        AgentType.replanner,
    }
    for role in deterministic_roles:
        decision = router.route(
            ModelRoutingContext(
                agent_type=role,
                task_count=20,
                hard_constraint_count=10,
                uncertainty_count=10,
                risk=RoutingRisk.critical,
                model_available=True,
            )
        )
        assert decision.tier == ModelTier.deterministic
        assert decision.model_name is None
        assert decision.requires_hitl is True


def test_companion_stays_small_and_critic_escalates_to_strong():
    router = ModelRouter(_settings())
    companion = router.route(
        ModelRoutingContext(
            agent_type=AgentType.companion,
            risk=RoutingRisk.critical,
            uncertainty_count=3,
            model_available=True,
        )
    )
    critic = router.route(
        ModelRoutingContext(
            agent_type=AgentType.critic,
            risk=RoutingRisk.high,
            task_count=5,
            model_available=True,
        )
    )
    assert companion.tier == ModelTier.small
    assert companion.requires_hitl is True
    assert critic.tier == ModelTier.strong


@pytest.mark.asyncio
async def test_routed_intent_strong_failure_degrades_to_rules():
    parser = RoutedIntentParser(_settings(), FailingModelClient())
    intent = await parser.parse(
        "明天下午先去博物馆，然后去公园，最后去医院；必须六点前回酒店，"
        "老人和孩子同行，预算有限，附近地点尽量少走路，看情况避开下雨"
    )
    assert intent.tasks
    assert parser.last_route is not None
    assert parser.last_route.tier == ModelTier.strong
    assert parser.fallback_used is True
    assert parser.last_parser == "rule-based-v2"


def test_missing_credentials_fail_closed_to_rule():
    settings = _settings(llm_api_key="")
    decision = ModelRouter(settings).route(
        ModelRoutingContext(
            agent_type=AgentType.critic,
            task_count=10,
            risk=RoutingRisk.critical,
            model_available=False,
        )
    )
    assert decision.tier == ModelTier.rule
    assert decision.fallback_tier is None
    budgeted = ModelRouter(_settings()).route(
        ModelRoutingContext(
            agent_type=AgentType.intent,
            task_count=10,
            model_available=True,
            budget_remaining_usd=0,
        )
    )
    assert budgeted.tier == ModelTier.rule
    assert budgeted.reason_codes == ["model_budget_exhausted"]


@pytest.mark.asyncio
async def test_companion_runtime_wiring_uses_small_model_even_for_high_risk():
    client = RecordingModelClient(
        '{"action":"finish","tool":null,"arguments":{},"reason":"handled"}'
    )
    decider = RoutedAgentDecider(_settings(), client)
    result = await decider.decide(
        trip_state="AT_RISK",
        observation={
            "event_type": "WeatherAlertReceived",
            "impact_level": "critical",
            "has_precise_location": True,
        },
        tool_history=[],
        tools=["get_weather", "propose_replan"],
        tool_schemas={},
    )
    assert client.payloads[0]["model"] == "small-test-model"
    assert result.model_name == "small:small-test-model"
    assert result.input_cost_per_million_usd == _settings().llm_small_input_cost_per_million_usd


@pytest.mark.asyncio
async def test_critic_runtime_wiring_escalates_complex_plan_to_strong_model():
    client = RecordingModelClient(
        '{"verdict":"approved","summary":"reviewed","findings":[],"suggested_adjustments":null,'
        '"route_evaluation":null,"confidence":0.9}'
    )
    critic = RoutedCriticAgent(_settings(), client)
    plan = {
        "status": "infeasible",
        "conflicts": ["deadline"],
        "intent": {
            "tasks": [{"description": f"task-{index}"} for index in range(5)],
            "constraints": {
                "hard": {"latest_return_time": "18:00"},
                "uncertain": [
                    {"field": "weather", "reason": "unknown"},
                    {"field": "traffic", "reason": "unknown"},
                ],
            },
        },
    }
    execution = await critic.run(plan)
    assert client.payloads[0]["model"] == "strong-test-model"
    assert execution.artifact.payload["model_route"]["tier"] == "strong"
    assert execution.estimated_cost_usd > 0
