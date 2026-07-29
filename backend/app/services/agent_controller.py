"""Companion Agent Controller: Observation → Decision → Policy → Tool → Loop."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.models import AgentMessage, AgentRun, AgentSession, AgentToolCall, TripSession
from backend.app.schemas.companion import ConsentScope, TripState
from backend.app.services.agent_policy import TOOL_POLICIES, evaluate_tool_policy

SAFE_AUTO_TOOLS = {"get_trip_state", "get_current_location", "get_weather"}


class AgentController:
    """Rule-first controller with optional LLM proposal enrichment later.

    The loop is intentionally deterministic in v1 so CI and local demos do not
    depend on a live model. Each step is audited via AgentMessage / AgentRun.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def run_once(
        self,
        *,
        trip: TripSession,
        agent: AgentSession,
        observation: dict[str, Any],
        consents: set[ConsentScope],
        tool_executor,
        trace_id: str | None = None,
        max_steps: int = 3,
    ) -> dict[str, Any]:
        settings = get_settings()
        steps: list[dict[str, Any]] = []
        self.db.add(
            AgentMessage(
                agent_session_id=agent.id,
                role="observation",
                content="trip observation",
                structured_json=json.dumps(observation, ensure_ascii=False),
            )
        )

        run = AgentRun(
            agent_session_id=agent.id,
            trigger_type=str(observation.get("trigger") or "controller"),
            status="running",
            trace_id=trace_id,
        )
        self.db.add(run)
        await self.db.flush()

        state = TripState(trip.state)
        for step_index in range(max_steps):
            proposals = self._propose_actions(state, observation, step_index)
            self.db.add(
                AgentMessage(
                    agent_session_id=agent.id,
                    role="assistant",
                    content=f"step-{step_index}",
                    structured_json=json.dumps({"proposals": proposals}, ensure_ascii=False),
                )
            )
            if not proposals:
                break

            executed = False
            for proposal in proposals:
                tool = proposal["tool"]
                allowed, reason, confirmation_required = evaluate_tool_policy(tool, state, consents)
                call_count = await self.db.scalar(
                    select(func.count(AgentToolCall.id))
                    .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
                    .where(AgentRun.agent_session_id == agent.id)
                )
                if (call_count or 0) >= settings.max_agent_tool_calls:
                    steps.append(
                        {
                            "tool": tool,
                            "status": "budget_exceeded",
                            "reason": "max_agent_tool_calls",
                        }
                    )
                    run.status = "budget_exceeded"
                    await self.db.commit()
                    return {"status": run.status, "steps": steps, "run_id": run.id}

                if not allowed:
                    steps.append({"tool": tool, "status": "policy_denied", "reason": reason})
                    continue
                if confirmation_required or tool not in SAFE_AUTO_TOOLS:
                    steps.append(
                        {
                            "tool": tool,
                            "status": "proposal_only",
                            "reason": reason or "requires_confirmation",
                            "proposal": proposal,
                        }
                    )
                    continue

                started = datetime.now(timezone.utc)
                try:
                    output = await tool_executor(tool, proposal.get("arguments") or {})
                    status = "succeeded"
                    error_type = None
                except Exception as exc:  # noqa: BLE001 - audited failure path
                    output = {"error": str(exc)}
                    status = "failed"
                    error_type = type(exc).__name__
                latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                self.db.add(
                    AgentToolCall(
                        agent_run_id=run.id,
                        tool_name=tool,
                        input_json=json.dumps(proposal.get("arguments") or {}, ensure_ascii=False),
                        output_summary_json=json.dumps(output, ensure_ascii=False)[:4000],
                        status=status,
                        error_type=error_type,
                        latency_ms=latency_ms,
                        trace_id=trace_id,
                    )
                )
                steps.append({"tool": tool, "status": status, "output": output})
                observation = {**observation, "last_tool": tool, "last_output": output}
                executed = True
                if status == "failed":
                    break
            if not executed:
                break

        run.status = "succeeded"
        run.latency_ms = None
        self.db.add(
            AgentMessage(
                agent_session_id=agent.id,
                role="system",
                content="controller_finished",
                structured_json=json.dumps({"steps": steps}, ensure_ascii=False),
            )
        )
        await self.db.commit()
        return {"status": run.status, "steps": steps, "run_id": run.id}

    def _propose_actions(
        self,
        state: TripState,
        observation: dict[str, Any],
        step_index: int,
    ) -> list[dict[str, Any]]:
        event_type = str(observation.get("event_type") or "")
        if step_index == 0:
            proposals = [{"tool": "get_trip_state", "arguments": {}}]
            if state in {TripState.active_trip, TripState.off_route, TripState.at_risk}:
                proposals.append({"tool": "get_current_location", "arguments": {}})
            if event_type in {"WeatherAlert", "DeadlineRisk"}:
                proposals.append({"tool": "get_weather", "arguments": {}})
            return proposals
        if step_index == 1 and event_type in {
            "UserOffRoute",
            "ScheduleDelay",
            "TrafficChanged",
            "DeadlineRisk",
            "PoiStatusChanged",
            "WeatherAlert",
        }:
            return [
                {
                    "tool": "propose_replan",
                    "arguments": {
                        "reason": observation.get("reason") or event_type,
                        "requires_confirmation": True,
                    },
                }
            ]
        # Keep proposals within registered policy surface.
        return [
            {"tool": name, "arguments": {}}
            for name in TOOL_POLICIES
            if name == "generate_attraction_brief" and observation.get("poi_name")
        ][:1]
