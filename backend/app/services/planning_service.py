"""Planning API application service.

Business execution belongs to isolated Agents. This service owns only request
lifecycle, workflow invocation, review policy and final response handling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from backend.app.clients.amap_client import MapProvider
from backend.app.core.config import Settings
from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore
from backend.app.schemas.agent_artifacts import AgentWorkflowMode, ReviewReport
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    AIPlanResult,
    ClarificationQuestion,
    PlanningIntent,
    PlanningState,
)
from backend.app.services.agent_orchestrator import PlanningAgentOrchestrator
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.agent_tool_adapters import AgentToolRuntime
from backend.app.services.agents.critic_agent import CriticAgent, RuleBasedCriticAgent
from backend.app.services.agents.intent_agent import IntentAgent
from backend.app.services.agents.planner_agent import PlannerAgent
from backend.app.services.agents.safety_agent import SafetyAgent
from backend.app.services.agents.search_agent import SearchAgent
from backend.app.services.agents.supervisor_agent import SupervisorAgent
from backend.app.services.human_in_loop import select_human_confirmation_questions
from backend.app.services.intent_parser import IntentParser


def request_fingerprint(
    owner: str,
    request: AIPlanRequest,
    model: str,
    prompt_version: str,
) -> str:
    normalized = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{owner}|{normalized}|{model}|{prompt_version}".encode()).hexdigest()


class PlanningService:
    def __init__(
        self,
        parser: IntentParser,
        map_provider: MapProvider,
        settings: Settings,
        critic_agent: CriticAgent | None = None,
        shared_state: AgentSharedStateManager | None = None,
        external_tool_runtime: AgentToolRuntime | None = None,
    ) -> None:
        self.parser = parser
        self.map_provider = map_provider
        self.settings = settings
        self.orchestrator = PlanningAgentOrchestrator(
            settings=settings,
            supervisor_agent=SupervisorAgent(),
            intent_agent=IntentAgent(parser),
            search_agent=SearchAgent(map_provider, settings, external_tool_runtime),
            safety_agent=SafetyAgent(),
            planner_agent=PlannerAgent(map_provider, settings, external_tool_runtime),
            critic_agent=critic_agent or RuleBasedCriticAgent(),
            shared_state=shared_state or AgentSharedStateManager(InMemoryRuntimeStore(), settings),
        )

    async def plan(self, request: AIPlanRequest) -> AIPlanResult:
        try:
            return await self._plan(request)
        finally:
            await asyncio.shield(self.orchestrator.clear_short_term_memory())

    async def _plan(self, request: AIPlanRequest) -> AIPlanResult:
        await self.orchestrator.start(request)
        intent, required_questions = await self.orchestrator.understand(request)
        if required_questions:
            result = AIPlanResult(
                status="need_clarification",
                planning_state=PlanningState.need_clarification,
                intent=intent,
                origin=request.origin,
                questions=required_questions,
            )
            await self.orchestrator.finalize(result.model_dump(mode="json"))
            result.agent_workflow = self.orchestrator.finish("needs_clarification")
            return result

        await self.orchestrator.plan_next(intent)
        result = await self._solve(request, intent)
        if result.status == "need_clarification":
            await self.orchestrator.finalize(result.model_dump(mode="json"))
            result.agent_workflow = self.orchestrator.finish("needs_clarification")
            return result

        review = await self.orchestrator.review(
            result.model_dump(mode="json", exclude={"critic_review", "agent_workflow"})
        )
        if review is None and self.orchestrator.mode == AgentWorkflowMode.enforce:
            result.status = "need_clarification"
            result.planning_state = PlanningState.need_clarification
            result.questions = [
                ClarificationQuestion(
                    field="critic_review",
                    reason="Critic Agent 未能在工作流预算内完成审阅",
                    question="方案审阅暂不可用，是否稍后重试规划？",
                )
            ]
        elif review is not None:
            result.critic_review = review
            if (
                review.verdict == "retry_with_soft_adjustments"
                and self.orchestrator.retry_allowed()
            ):
                adjusted_intent = await self.orchestrator.apply_soft_adjustments(intent, review)
                result = await self._solve(request, adjusted_intent)
                second_review = await self.orchestrator.review(
                    result.model_dump(mode="json", exclude={"critic_review", "agent_workflow"})
                )
                if second_review is not None:
                    review = second_review
                    result.critic_review = review
            if self.orchestrator.mode == AgentWorkflowMode.enforce:
                self._enforce_review(result, review)
        if result.status == "success":
            confirmation_questions = select_human_confirmation_questions(
                request=request,
                result=result,
            )
            if confirmation_questions:
                result.status = "need_clarification"
                result.planning_state = PlanningState.need_clarification
                result.questions = confirmation_questions
                result.warnings.append("方案触发 Human-in-the-loop 人工确认门槛")
        await self.orchestrator.finalize(result.model_dump(mode="json", exclude={"agent_workflow"}))
        result.agent_workflow = self.orchestrator.finish(result.status)
        return result

    @staticmethod
    def _enforce_review(result: AIPlanResult, review: ReviewReport) -> None:
        if review.verdict == "needs_clarification":
            result.status = "need_clarification"
            result.planning_state = PlanningState.need_clarification
            result.questions = [
                ClarificationQuestion(
                    field="critic_review",
                    reason=review.summary,
                    question="方案证据仍不完整，是否补充地点或偏好后重新规划？",
                )
            ]
        elif review.verdict == "approved_with_warnings":
            result.warnings.extend(
                finding.message for finding in review.findings if finding.severity == "warning"
            )
        elif review.verdict == "retry_with_soft_adjustments":
            result.warnings.append("已达到 Critic 软重算上限，保留通过硬约束校验的方案")

    async def _solve(self, request: AIPlanRequest, intent: PlanningIntent) -> AIPlanResult:
        """Delegate executable stages; retained as a narrow workflow seam for retries."""

        return await self.orchestrator.execute_planning_stages(request, intent)
