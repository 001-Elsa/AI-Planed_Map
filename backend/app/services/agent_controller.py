"""Observation → LLM decision → Policy → Tool → Observation controller."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.models import AgentMessage, AgentRun, AgentSession, AgentToolCall, TripSession
from backend.app.schemas.companion import ConsentScope, TripState
from backend.app.services.agent_decider import AgentDecider, RuleBasedAgentDecider
from backend.app.services.agent_policy import TOOL_POLICIES, evaluate_tool_policy

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class AgentController:
    """Executes a bounded, auditable tool loop without plan mutation powers."""

    def __init__(self, db: AsyncSession, decider: AgentDecider | None = None) -> None:
        self.db = db
        self.decider = decider or RuleBasedAgentDecider()

    async def run_once(
        self,
        *,
        trip: TripSession,
        agent: AgentSession,
        observation: dict[str, Any],
        consents: set[ConsentScope],
        tool_executor: ToolExecutor,
        trace_id: str | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        started = time.perf_counter()
        limit = min(max_steps or settings.max_agent_steps, settings.max_agent_steps)
        steps: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        run = AgentRun(
            agent_session_id=agent.id,
            trigger_type=str(observation.get("trigger") or "controller"),
            status="running",
            trace_id=trace_id,
        )
        self.db.add(run)
        self.db.add(
            AgentMessage(
                agent_session_id=agent.id,
                role="observation",
                content="trip observation",
                structured_json=json.dumps(observation, ensure_ascii=False, default=str),
            )
        )
        await self.db.flush()

        status = "succeeded"
        for step_index in range(limit):
            try:
                result = await self.decider.decide(
                    trip_state=trip.state,
                    observation=observation,
                    tool_history=history,
                    tools=sorted(TOOL_POLICIES),
                )
            except Exception as exc:  # noqa: BLE001 - preserve the operational loop on LLM fault
                result = await RuleBasedAgentDecider().decide(
                    trip_state=trip.state,
                    observation=observation,
                    tool_history=history,
                    tools=sorted(TOOL_POLICIES),
                )
                self.db.add(
                    AgentMessage(
                        agent_session_id=agent.id,
                        role="system",
                        content="agent_decider_fallback",
                        structured_json=json.dumps(
                            {"error_type": type(exc).__name__, "reason": str(exc)[:300]},
                            ensure_ascii=False,
                        ),
                    )
                )
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            agent.model_name = result.model_name
            estimated_cost = self._estimated_cost(input_tokens, output_tokens)
            if (
                input_tokens > settings.max_agent_input_tokens
                or output_tokens > settings.max_agent_output_tokens
                or estimated_cost > settings.max_agent_run_cost_usd
            ):
                status = "budget_exceeded"
                steps.append({"status": status, "reason": "agent_token_or_cost_budget"})
                break

            decision = result.decision
            self.db.add(
                AgentMessage(
                    agent_session_id=agent.id,
                    role="assistant",
                    content=f"step-{step_index}:{decision.action}",
                    structured_json=json.dumps(
                        decision.model_dump(mode="json"), ensure_ascii=False
                    ),
                )
            )
            if decision.action == "finish":
                steps.append({"status": "finished", "reason": decision.reason})
                break

            tool = str(decision.tool)
            arguments = decision.arguments
            state = TripState(trip.state)
            allowed, policy_reason, confirmation_required = evaluate_tool_policy(
                tool, state, consents
            )
            historical_calls = int(
                await self.db.scalar(
                    select(func.count(AgentToolCall.id))
                    .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
                    .where(AgentRun.agent_session_id == agent.id)
                )
                or 0
            )
            if historical_calls >= settings.max_agent_tool_calls:
                status = "budget_exceeded"
                steps.append({"tool": tool, "status": status, "reason": "max_agent_tool_calls"})
                break
            if not allowed or confirmation_required:
                denial = policy_reason if not allowed else "requires_user_confirmation"
                await self._record_tool_call(
                    run, tool, arguments, {"reason": denial}, "policy_denied", denial, trace_id
                )
                steps.append({"tool": tool, "status": "policy_denied", "reason": denial})
                history.append(
                    {"tool": tool, "status": "policy_denied", "output": {"reason": denial}}
                )
                # Let the LLM observe a refusal once, then it must make a new decision.
                observation = {**observation, "last_tool": tool, "last_output": {"reason": denial}}
                continue

            tool_started = time.perf_counter()
            try:
                output = await tool_executor(tool, arguments)
                call_status, error_type = "succeeded", None
            except Exception as exc:  # noqa: BLE001 - must audit tool failures
                output = {"error": str(exc)}
                call_status, error_type = "failed", type(exc).__name__
            await self._record_tool_call(
                run,
                tool,
                arguments,
                output,
                call_status,
                error_type,
                trace_id,
                latency_ms=int((time.perf_counter() - tool_started) * 1000),
            )
            steps.append({"tool": tool, "status": call_status, "output": output})
            history.append({"tool": tool, "status": call_status, "output": output})
            self.db.add(
                AgentMessage(
                    agent_session_id=agent.id,
                    role="tool",
                    content=tool,
                    structured_json=json.dumps(output, ensure_ascii=False, default=str)[:4000],
                )
            )
            observation = {**observation, "last_tool": tool, "last_output": output}
            if call_status == "failed":
                status = "tool_failed"
                break
        else:
            status = "step_limit_reached"

        run.status = status
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.estimated_cost_usd = self._estimated_cost(input_tokens, output_tokens)
        run.latency_ms = int((time.perf_counter() - started) * 1000)
        self.db.add(
            AgentMessage(
                agent_session_id=agent.id,
                role="system",
                content="controller_finished",
                structured_json=json.dumps({"status": status, "steps": steps}, ensure_ascii=False),
            )
        )
        await self.db.commit()
        return {"status": status, "steps": steps, "run_id": run.id}

    @staticmethod
    def _estimated_cost(input_tokens: int, output_tokens: int) -> float:
        settings = get_settings()
        return (
            input_tokens * settings.llm_input_cost_per_million_usd
            + output_tokens * settings.llm_output_cost_per_million_usd
        ) / 1_000_000

    async def _record_tool_call(
        self,
        run: AgentRun,
        tool: str,
        arguments: dict[str, Any],
        output: dict[str, Any],
        status: str,
        error_type: str | None,
        trace_id: str | None,
        latency_ms: int | None = None,
    ) -> None:
        self.db.add(
            AgentToolCall(
                agent_run_id=run.id,
                tool_name=tool,
                input_json=json.dumps(arguments, ensure_ascii=False, default=str),
                output_summary_json=json.dumps(output, ensure_ascii=False, default=str)[:4000],
                status=status,
                error_type=error_type,
                latency_ms=latency_ms,
                trace_id=trace_id,
            )
        )
