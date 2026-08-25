"""Constrained model decisions for the generic Agent Runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import Field, ValidationError, model_validator

from backend.app.core.config import Settings
from backend.app.schemas.agent_artifacts import AgentSpec, AgentType
from backend.app.schemas.common import StrictModel
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC
from backend.app.services.model_router import (
    ModelRouter,
    ModelRoutingContext,
    ModelTier,
    RoutingRisk,
)


class AgentDecision(StrictModel):
    action: str
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str

    @model_validator(mode="after")
    def validate_action(self) -> AgentDecision:
        if self.action not in {"call_tool", "finish"}:
            raise ValueError("action must be call_tool or finish")
        if self.action == "call_tool" and not self.tool:
            raise ValueError("tool is required for call_tool")
        if self.action == "finish" and self.tool is not None:
            raise ValueError("finish must not include a tool")
        return self


@dataclass(frozen=True)
class DecisionResult:
    decision: AgentDecision
    input_tokens: int = 0
    output_tokens: int = 0
    model_name: str = "rule-agent-v1"
    input_cost_per_million_usd: float = 0
    output_cost_per_million_usd: float = 0


class AgentDecider(Protocol):
    async def decide(
        self,
        *,
        trip_state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any] | None = None,
    ) -> DecisionResult: ...


class RuleBasedAgentDecider:
    """Companion-specific offline fallback used by the Companion adapter only."""

    model_name = "rule-agent-v1"

    async def decide(
        self,
        *,
        trip_state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any] | None = None,
    ) -> DecisionResult:
        used = [item.get("tool") for item in tool_history]
        current_observation = observation.get("current_observation")
        event_observation = (
            current_observation if isinstance(current_observation, dict) else observation
        )
        event_type = str(event_observation.get("event_type") or "")
        risk_event = event_type in {
            "UserOffRoute",
            "ScheduleDelayDetected",
            "TrafficChanged",
            "DeadlineRiskDetected",
            "PoiStatusChanged",
            "WeatherAlertReceived",
        }
        if event_type == "WeatherAlertReceived" and "get_weather" not in used:
            return DecisionResult(
                AgentDecision(
                    action="call_tool",
                    tool="get_weather",
                    reason="天气事件需要先读取实时降雨风险",
                )
            )
        if risk_event and "get_trip_state" not in used:
            return DecisionResult(
                AgentDecision(
                    action="call_tool", tool="get_trip_state", reason="先确认当前行程状态"
                )
            )
        if (
            risk_event
            and event_observation.get("has_precise_location")
            and "get_current_location" not in used
        ):
            return DecisionResult(
                AgentDecision(
                    action="call_tool",
                    tool="get_current_location",
                    reason="局部重规划需要最新位置",
                )
            )
        if risk_event and "propose_replan" not in used:
            return DecisionResult(
                AgentDecision(
                    action="call_tool",
                    tool="propose_replan",
                    arguments={"reason": event_observation.get("reason") or event_type},
                    reason="风险事件需要生成待确认的重规划方案",
                )
            )
        return DecisionResult(AgentDecision(action="finish", reason="已完成必要的观察和建议"))


class OpenAICompatibleAgentDecider:
    model_name: str

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        model_name: str | None = None,
        input_cost_per_million_usd: float | None = None,
        output_cost_per_million_usd: float | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.model_name = model_name or settings.llm_model
        self.input_cost_per_million_usd = (
            settings.llm_input_cost_per_million_usd
            if input_cost_per_million_usd is None
            else input_cost_per_million_usd
        )
        self.output_cost_per_million_usd = (
            settings.llm_output_cost_per_million_usd
            if output_cost_per_million_usd is None
            else output_cost_per_million_usd
        )

    async def decide(
        self,
        *,
        trip_state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any] | None = None,
    ) -> DecisionResult:
        return await self.decide_for_spec(
            spec=COMPANION_AGENT_SPEC,
            state=trip_state,
            observation=observation,
            tool_history=tool_history,
            tools=tools,
            tool_schemas=tool_schemas or {},
        )

    async def decide_for_spec(
        self,
        *,
        spec: AgentSpec,
        state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any],
    ) -> DecisionResult:
        schema = AgentDecision.model_json_schema()
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties") or {})
        role_boundary = (
            "不能覆盖正式计划；propose_replan 只能创建待用户确认的补丁。"
            if spec.agent_type == AgentType.companion
            else "只能使用 AgentSpec 白名单中的工具，不能越过制品和权限边界。"
        )
        payload = {
            "model": self.model_name,
            "max_tokens": min(
                self.settings.max_agent_output_tokens,
                self.settings.max_llm_output_tokens,
                spec.budget.max_output_tokens,
            ),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"你是 MapGo {spec.agent_type.value} Agent 的工具决策器。"
                        "每次只能选择一次工具调用或结束。"
                        f"上下文视图为 {spec.context_view}；输出制品为 "
                        f"{spec.output_artifact_type}。{role_boundary}"
                        "根据当前观察和已有工具结果决定是否继续。"
                        f"可用工具白名单：{', '.join(tools) or '无'}。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "runtime_state": state,
                            "agent_type": spec.agent_type.value,
                            "input_artifact_types": sorted(spec.input_artifact_types),
                            "output_artifact_type": spec.output_artifact_type,
                            "observation": observation,
                            "tool_history": tool_history,
                            "tool_argument_schemas": tool_schemas,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_decision",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        response = await self.client.post(
            f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            timeout=min(self.settings.external_timeout_seconds, spec.budget.timeout_seconds),
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        try:
            decision = AgentDecision.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"invalid_agent_decision:{exc}") from exc
        return DecisionResult(
            decision=decision,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model_name=self.model_name,
            input_cost_per_million_usd=self.input_cost_per_million_usd,
            output_cost_per_million_usd=self.output_cost_per_million_usd,
        )


class RoutedAgentDecider:
    """Route runtime decisions without changing tool or state authorization."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.router = ModelRouter(settings)
        self.rule = RuleBasedAgentDecider()
        self.small = (
            OpenAICompatibleAgentDecider(
                settings,
                client,
                model_name=self.router.small_model,
                input_cost_per_million_usd=settings.llm_small_input_cost_per_million_usd,
                output_cost_per_million_usd=settings.llm_small_output_cost_per_million_usd,
            )
            if settings.llm_api_key and client is not None
            else None
        )
        self.strong = (
            OpenAICompatibleAgentDecider(
                settings,
                client,
                model_name=self.router.strong_model,
                input_cost_per_million_usd=settings.llm_strong_input_cost_per_million_usd,
                output_cost_per_million_usd=settings.llm_strong_output_cost_per_million_usd,
            )
            if settings.llm_api_key and client is not None
            else None
        )

    async def decide(
        self,
        *,
        trip_state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any] | None = None,
    ) -> DecisionResult:
        return await self.decide_for_spec(
            spec=COMPANION_AGENT_SPEC,
            state=trip_state,
            observation=observation,
            tool_history=tool_history,
            tools=tools,
            tool_schemas=tool_schemas or {},
        )

    async def decide_for_spec(
        self,
        *,
        spec: AgentSpec,
        state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any],
    ) -> DecisionResult:
        current = observation.get("current_observation")
        event = current if isinstance(current, dict) else observation
        impact = str(event.get("impact_level") or "none").lower()
        risk = (
            RoutingRisk.critical
            if impact == "critical"
            else RoutingRisk.high
            if impact == "high"
            else RoutingRisk.medium
            if impact == "medium"
            else RoutingRisk.low
            if impact == "low"
            else RoutingRisk.none
        )
        uncertainty = int(not bool(event.get("event_type"))) + int(
            event.get("has_precise_location") is False
        )
        decision = self.router.route(
            ModelRoutingContext(
                agent_type=spec.agent_type,
                task_count=1,
                uncertainty_count=uncertainty,
                text_length=len(json.dumps(event, ensure_ascii=False, default=str)),
                risk=risk,
                structured_output_required=True,
                model_available=self.small is not None,
            )
        )
        if decision.tier == ModelTier.small and self.small is not None:
            result = await self.small.decide_for_spec(
                spec=spec,
                state=state,
                observation=observation,
                tool_history=tool_history,
                tools=tools,
                tool_schemas=tool_schemas,
            )
        elif decision.tier == ModelTier.strong and self.strong is not None:
            result = await self.strong.decide_for_spec(
                spec=spec,
                state=state,
                observation=observation,
                tool_history=tool_history,
                tools=tools,
                tool_schemas=tool_schemas,
            )
        elif spec.agent_type == AgentType.companion:
            result = await self.rule.decide(
                trip_state=state,
                observation=observation,
                tool_history=tool_history,
                tools=tools,
                tool_schemas=tool_schemas,
            )
        else:
            result = DecisionResult(
                AgentDecision(
                    action="finish",
                    reason="model router selected deterministic execution for this role",
                ),
                model_name="deterministic-router-v1",
            )
        return DecisionResult(
            decision=result.decision,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_name=f"{decision.tier.value}:{result.model_name}"[:100],
            input_cost_per_million_usd=result.input_cost_per_million_usd,
            output_cost_per_million_usd=result.output_cost_per_million_usd,
        )


def build_agent_decider(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> AgentDecider:
    return RoutedAgentDecider(settings, client)
