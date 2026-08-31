"""Executable place-research Agent with isolated search capabilities."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from pydantic import Field

from backend.app.clients.amap_client import MapProvider, haversine_meters
from backend.app.core.config import Settings
from backend.app.core.observability import metrics
from backend.app.schemas.agent_artifacts import (
    AgentBudget,
    AgentRecoveryDecision,
    AgentSpec,
    AgentType,
    ArtifactEnvelope,
)
from backend.app.schemas.ai_intent import (
    ClarificationQuestion,
    Coordinate,
    PlanningIntent,
    PoiCandidate,
)
from backend.app.schemas.common import StrictModel
from backend.app.services.agent_planning_tools import SearchPoiTool
from backend.app.services.agent_tool_adapters import AgentToolRuntime
from backend.app.services.agent_tool_contracts import SearchPoiArgs, ToolResultEnvelope
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, InvocationMode
from backend.app.services.agents.base import AgentExecution, canonical_hash

POI_RECOVERY_CACHE_LIMIT = 128
_POI_RECOVERY_CACHE: OrderedDict[str, list[PoiCandidate]] = OrderedDict()

RecoveryHandler = Callable[..., Awaitable[AgentRecoveryDecision]]


class SearchAgentInput(StrictModel):
    """Minimal context granted to place research; raw conversation text is excluded."""

    intent: PlanningIntent
    origin: Coordinate
    city: str | None = Field(default=None, max_length=50)
    task_poi_overrides: dict[str, str] = Field(default_factory=dict)


class SearchArtifact(StrictModel):
    keywords: list[str]
    candidate_groups: list[list[PoiCandidate]]
    clarification_questions: list[ClarificationQuestion] = Field(default_factory=list)
    provider_name: str
    recovered_failures: int = Field(default=0, ge=0)
    recovery_actions: list[AgentRecoveryDecision] = Field(default_factory=list)
    tool_results: list[ToolResultEnvelope] = Field(default_factory=list)


SEARCH_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.search,
    prompt_version="search-stage-v2-executable",
    context_view="poi_research_minimal",
    allowed_tools=frozenset(),
    allowed_internal_capabilities=TOOL_REGISTRY.names_for(
        AgentType.search, InvocationMode.internal_stage
    ),
    input_artifact_types=frozenset({"intent_artifact"}),
    output_artifact_type="search_artifact",
    budget=AgentBudget(
        max_steps=1, max_input_tokens=2_000, max_output_tokens=1_000, max_cost_usd=0
    ),
)


class SearchAgent:
    """Owns POI recall, retry, verified-cache fallback, filtering and deduplication."""

    spec = SEARCH_AGENT_SPEC

    def __init__(
        self,
        provider: MapProvider,
        settings: Settings,
        external_tool_runtime: AgentToolRuntime | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.search_tool = SearchPoiTool(
            provider,
            timeout_seconds=settings.agent_stage_timeout_seconds,
            external_runtime=external_tool_runtime,
        )

    async def run(
        self,
        context: SearchAgentInput,
        *,
        recovery_handler: RecoveryHandler | None = None,
    ) -> AgentExecution[SearchArtifact]:
        started = time.perf_counter()
        keywords = [self._recall_keyword(task, context.intent) for task in context.intent.tasks]
        calls = await asyncio.gather(
            *(
                self.search_tool.execute(
                    SearchPoiArgs(keyword=keyword, origin=context.origin, city=context.city)
                )
                for keyword in keywords
            )
        )
        results: list[list[PoiCandidate] | None] = [None] * len(keywords)
        tool_results: list[ToolResultEnvelope] = []
        failures: list[tuple[int, str]] = []
        for index, call in enumerate(calls):
            tool_results.append(call.result)
            if call.data is None:
                failures.append((index, call.result.error_code or "UPSTREAM_ERROR"))
                continue
            normalized = self._deduplicate(call.data)
            results[index] = normalized
            self._cache_results(keywords[index], context.origin, context.city, normalized)

        recovery_actions: list[AgentRecoveryDecision] = []
        max_attempts = max(1, min(10, self.settings.agent_search_max_attempts))
        for index, initial_error_code in failures:
            error_code = initial_error_code
            for attempt in range(1, max_attempts + 1):
                fallback_available = self._cached_results_available(
                    keywords[index], context.origin, context.city
                )
                if recovery_handler is None:
                    decision = self._default_recovery_decision(
                        error_code=error_code,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        fallback_available=fallback_available,
                    )
                else:
                    decision = await recovery_handler(
                        stage="poi_search",
                        exc=SearchToolFailure(error_code),
                        attempt=attempt,
                        max_attempts=max_attempts,
                        timeout_seconds=self.settings.agent_stage_timeout_seconds,
                        fallback_available=fallback_available,
                        fallback_source="poi_recovery_cache",
                    )
                recovery_actions.append(decision)
                metrics.increment(
                    "mapgo_agent_recovery_total",
                    {"stage": decision.stage, "action": decision.action},
                )
                if decision.action == "retry":
                    await asyncio.sleep(0.15 * attempt)
                    retry = await self.search_tool.execute(
                        SearchPoiArgs(
                            keyword=keywords[index],
                            origin=context.origin,
                            city=context.city,
                        )
                    )
                    tool_results.append(retry.result)
                    if retry.data is None:
                        error_code = retry.result.error_code or "UPSTREAM_ERROR"
                        continue
                    normalized = self._deduplicate(retry.data)
                    results[index] = normalized
                    self._cache_results(keywords[index], context.origin, context.city, normalized)
                    break
                if decision.action == "fallback_cached":
                    results[index] = self._cached_results(
                        keywords[index], context.origin, context.city
                    )
                    break
                break

        candidate_groups = [self._deduplicate(item or []) for item in results]
        if context.intent.preferences.avoid_hiking:
            candidate_groups = [self._remove_hiking_candidates(group) for group in candidate_groups]
        questions = self._apply_user_choices_and_build_questions(
            candidate_groups=candidate_groups,
            keywords=keywords,
            overrides=context.task_poi_overrides,
            intent=context.intent,
        )
        output = SearchArtifact(
            keywords=keywords,
            candidate_groups=candidate_groups,
            clarification_questions=questions,
            provider_name=self.provider.name,
            recovered_failures=len(recovery_actions),
            recovery_actions=recovery_actions,
            tool_results=tool_results,
        )
        summary = {
            "workflow_state": "search_completed",
            "provider": self.provider.name,
            "keyword_count": len(keywords),
            "candidate_counts": [len(group) for group in candidate_groups],
            "total_candidates": sum(len(group) for group in candidate_groups),
            "question_count": len(questions),
            "recovered_failures": len(recovery_actions),
            "recovery_actions": [item.model_dump(mode="json") for item in recovery_actions],
        }
        confidence = min(
            (item.confidence for group in candidate_groups for item in group), default=0.5
        )
        artifact = ArtifactEnvelope(
            artifact_type=self.spec.output_artifact_type,
            producer_agent=AgentType.search,
            payload=summary,
            confidence=min(confidence, 0.7) if recovery_actions else confidence,
            evidence_refs=[
                result.artifact_ref
                for result in tool_results
                if result.success and result.artifact_ref is not None
            ],
            input_hash=canonical_hash(context.model_dump(mode="json")),
        )
        return AgentExecution(
            spec=self.spec,
            output=output,
            artifact=artifact,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=bool(recovery_actions),
            reason="search_retry_or_cache_recovery" if recovery_actions else None,
        )

    @staticmethod
    def _default_recovery_decision(
        *, error_code: str, attempt: int, max_attempts: int, fallback_available: bool
    ) -> AgentRecoveryDecision:
        if attempt < max_attempts:
            action = "retry"
            reason = "bounded retry remains"
        elif fallback_available:
            action = "fallback_cached"
            reason = "using provider-verified cached POIs"
        else:
            action = "fallback_unavailable"
            reason = "no verified cached POIs are available"
        return AgentRecoveryDecision(
            stage="poi_search",
            action=action,
            attempt=attempt,
            max_attempts=max_attempts,
            error_type=error_code,
            reason=reason,
            timeout_seconds=120,
            fallback_source="poi_recovery_cache" if fallback_available else None,
        )

    @staticmethod
    def _deduplicate(candidates: list[PoiCandidate]) -> list[PoiCandidate]:
        unique: dict[str, PoiCandidate] = {}
        for candidate in candidates:
            key = candidate.id or (
                f"{candidate.name.strip().casefold()}:"
                f"{candidate.location.lng:.6f}:{candidate.location.lat:.6f}"
            )
            unique.setdefault(key, candidate)
        return list(unique.values())

    @staticmethod
    def _remove_hiking_candidates(candidates: list[PoiCandidate]) -> list[PoiCandidate]:
        hiking_terms = (
            "爬山",
            "登山",
            "徒步",
            "山峰",
            "山岳",
            "登山步道",
            "hiking",
            "mountain trail",
        )
        return [
            candidate
            for candidate in candidates
            if not any(
                term in f"{candidate.name} {candidate.address}".casefold() for term in hiking_terms
            )
        ]

    @staticmethod
    def _apply_user_choices_and_build_questions(
        *,
        candidate_groups: list[list[PoiCandidate]],
        keywords: list[str],
        overrides: dict[str, str],
        intent: PlanningIntent,
    ) -> list[ClarificationQuestion]:
        selected_missing: list[ClarificationQuestion] = []
        for raw_index, poi_id in overrides.items():
            index = int(raw_index)
            if not 0 <= index < len(candidate_groups):
                raise ValueError(f"task POI override index out of range: {index}")
            group = candidate_groups[index]
            selected = next((item for item in group if item.id == poi_id), None)
            if selected is None:
                selected_missing.append(
                    ClarificationQuestion(
                        field=f"tasks.{index}.selected_poi_id",
                        reason="The selected POI is no longer returned by the map provider",
                        question="该地点已无法验证，请重新选择一个候选地点。",
                        candidates=group[:5],
                    )
                )
            else:
                candidate_groups[index] = [selected]
        if selected_missing:
            return selected_missing

        missing = [
            ClarificationQuestion(
                field=f"tasks.{index}.location",
                reason="地图 Provider 未返回可验证的真实候选地点",
                question=f"没有找到“{keywords[index]}”，可以提供更具体的名称或区域吗？",
            )
            for index, group in enumerate(candidate_groups)
            if not group
        ]
        if missing:
            return missing

        questions: list[ClarificationQuestion] = []
        for index, group in enumerate(candidate_groups):
            if (
                len(group) >= 2
                and len({item.name.strip().casefold() for item in group[:3]}) == 1
                and str(index) not in overrides
            ):
                questions.append(
                    ClarificationQuestion(
                        field=f"tasks.{index}.selected_poi_id",
                        reason="同名地点存在多个候选",
                        question=f"任务“{intent.tasks[index].description}”匹配到多个地点，请选择一个。",
                        candidates=group[:5],
                    )
                )
        return questions[:2]

    @staticmethod
    def _cache_key(keyword: str, origin: Coordinate, city: str | None) -> str:
        normalized_city = (city or "").strip().casefold()
        return f"{normalized_city}|{keyword.strip().casefold()}|{origin.lng:.3f},{origin.lat:.3f}"

    @classmethod
    def _cache_results(
        cls,
        keyword: str,
        origin: Coordinate,
        city: str | None,
        candidates: list[PoiCandidate],
    ) -> None:
        if not candidates:
            return
        key = cls._cache_key(keyword, origin, city)
        _POI_RECOVERY_CACHE[key] = [item.model_copy(deep=True) for item in candidates]
        _POI_RECOVERY_CACHE.move_to_end(key)
        while len(_POI_RECOVERY_CACHE) > POI_RECOVERY_CACHE_LIMIT:
            _POI_RECOVERY_CACHE.popitem(last=False)

    @classmethod
    def _cached_results_available(cls, keyword: str, origin: Coordinate, city: str | None) -> bool:
        return cls._cache_key(keyword, origin, city) in _POI_RECOVERY_CACHE

    @classmethod
    def _cached_results(
        cls, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]:
        key = cls._cache_key(keyword, origin, city)
        cached = _POI_RECOVERY_CACHE.get(key, [])
        if cached:
            _POI_RECOVERY_CACHE.move_to_end(key)
        return [
            item.model_copy(
                deep=True,
                update={
                    "source": f"cache:{item.source}",
                    "distance_meters": haversine_meters(origin, item.location),
                    "confidence": min(item.confidence, 0.55),
                },
            )
            for item in cached
        ]

    @staticmethod
    def _recall_keyword(task, intent: PlanningIntent) -> str:
        base = task.location_name or task.category or task.description
        dining_terms = ("餐", "饭", "吃", "咖啡", "茶", "food", "restaurant")
        task_text = f"{task.description} {task.location_name or ''} {task.category or ''}".lower()
        if intent.preferences.dietary_restrictions and any(
            term in task_text for term in dining_terms
        ):
            return f"{base} {' '.join(intent.preferences.dietary_restrictions)}"
        generic_terms = ("旅游", "旅行", "游玩", "逛逛", "景点", "行程", "travel", "trip")
        if any(term in task_text for term in generic_terms):
            hints = list(intent.preferences.preferred_categories[:2])
            environment_labels = {
                "quiet": "安静",
                "uncrowded": "小众",
                "indoor": "室内",
                "outdoor": "户外",
            }
            hints.extend(
                environment_labels[item]
                for item in intent.preferences.preferred_environment
                if item in environment_labels
            )
            if intent.preferences.avoid_queues and "小众" not in hints:
                hints.append("小众")
            if hints:
                return f"{base} {' '.join(hints[:3])}"
        return base


class SearchToolFailure(RuntimeError):
    """Stable failure handed to Supervisor without the upstream exception text."""
