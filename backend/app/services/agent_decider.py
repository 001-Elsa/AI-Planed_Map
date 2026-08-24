"""Constrained LLM decisions for the companion Agent tool loop.

The model never receives a mutation capability.  It can only return one
allow-listed tool name or finish; the controller applies Policy before any
executor is reached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import Field, ValidationError, model_validator

from backend.app.core.config import Settings
from backend.app.schemas.ai_intent import StrictModel
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC


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
    """Offline-safe fallback that still exercises the same tool loop."""

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
                    reason="重规划需要最新位置",
                )
            )
        if risk_event and "propose_replan" not in used:
            return DecisionResult(
                AgentDecision(
                    action="call_tool",
                    tool="propose_replan",
                    arguments={"reason": event_observation.get("reason") or event_type},
                    reason="风险事件需要生成待确认重规划方案",
                )
            )
        return DecisionResult(AgentDecision(action="finish", reason="已完成必要的观察与建议"))


class OpenAICompatibleAgentDecider:
    model_name: str

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.model_name = settings.llm_model

    async def decide(
        self,
        *,
        trip_state: str,
        observation: dict[str, Any],
        tool_history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: dict[str, Any] | None = None,
    ) -> DecisionResult:
        schema = AgentDecision.model_json_schema()
        # OpenAI-compatible strict JSON schema mode requires every declared
        # property to be present. `tool: null` is therefore the explicit
        # representation for a finish decision.
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties") or {})
        payload = {
            "model": self.settings.llm_model,
            "max_tokens": min(
                self.settings.max_agent_output_tokens, self.settings.max_llm_output_tokens
            ),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 MapGo 伴游 Agent 的工具决策器。只能输出一次工具调用或结束。"
                        "你不能修改正式计划；propose_replan 只会创建待用户确认的补丁。"
                        "优先根据 Observation 和已返回的工具结果判断是否继续。"
                        f"可用工具白名单：{', '.join(tools)}。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "trip_state": trip_state,
                            "observation": observation,
                            "tool_history": tool_history,
                            "tool_argument_schemas": tool_schemas or {},
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "agent_decision", "strict": True, "schema": schema},
            },
        }
        response = await self.client.post(
            f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            timeout=min(
                self.settings.external_timeout_seconds,
                COMPANION_AGENT_SPEC.budget.timeout_seconds,
            ),
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
        )


def build_agent_decider(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> AgentDecider:
    if settings.llm_api_key and client is not None:
        return OpenAICompatibleAgentDecider(settings, client)
    return RuleBasedAgentDecider()
