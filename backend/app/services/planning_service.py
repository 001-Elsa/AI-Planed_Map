import asyncio
import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.clients.amap_client import MapProvider, haversine_meters
from backend.app.core.config import Settings
from backend.app.core.observability import metrics
from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore
from backend.app.schemas.agent_artifacts import AgentType, AgentWorkflowMode, ReviewReport
from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    AIPlanResult,
    CandidateReview,
    ClarificationQuestion,
    Coordinate,
    PlannedStop,
    PlanningIntent,
    PlanningState,
    PoiCandidate,
    TransportMode,
    UncertaintySummary,
)
from backend.app.services.agent_orchestrator import PlanningAgentOrchestrator
from backend.app.services.agent_shared_state import AgentSharedStateManager
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, DataScope, InvocationMode
from backend.app.services.agents.critic_agent import CriticAgent, RuleBasedCriticAgent
from backend.app.services.agents.intent_agent import IntentAgent
from backend.app.services.agents.safety_agent import SafetyAgent
from backend.app.services.agents.supervisor_agent import SupervisorAgent
from backend.app.services.clarification import select_clarification_questions
from backend.app.services.human_in_loop import select_human_confirmation_questions
from backend.app.services.intent_parser import IntentParser
from backend.app.services.route_optimizer import (
    CandidateNode,
    evaluate_joint_order,
    optimize_joint_route,
)
from backend.app.services.uncertainty import heuristic_envelope

SHANGHAI = ZoneInfo("Asia/Shanghai")
POI_RECOVERY_CACHE_LIMIT = 128
_POI_RECOVERY_CACHE: OrderedDict[str, list[PoiCandidate]] = OrderedDict()


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
    ) -> None:
        self.parser = parser
        self.map_provider = map_provider
        self.settings = settings
        self.orchestrator = PlanningAgentOrchestrator(
            settings=settings,
            supervisor_agent=SupervisorAgent(),
            intent_agent=IntentAgent(parser),
            safety_agent=SafetyAgent(),
            critic_agent=critic_agent or RuleBasedCriticAgent(),
            shared_state=shared_state
            or AgentSharedStateManager(InMemoryRuntimeStore(), settings),
        )

    async def _search_candidates(
        self,
        keywords: list[str],
        origin: Coordinate,
        city: str | None,
        avoid_hiking: bool = False,
    ) -> list[list[PoiCandidate]]:
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.search,
            capability="search_poi",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.map_search}),
        )
        recalled = await asyncio.gather(
            *(
                self._provider_search_with_timeout(keyword, origin, city)
                for keyword in keywords
            ),
            return_exceptions=True,
        )
        results: list[list[PoiCandidate] | None] = [None] * len(keywords)
        failures: list[tuple[int, Exception]] = []
        for index, item in enumerate(recalled):
            if isinstance(item, BaseException):
                if isinstance(item, Exception):
                    failures.append((index, item))
                else:
                    raise item
            else:
                results[index] = item
                self._cache_poi_results(keywords[index], origin, city, item)

        recovery_actions: list[dict[str, object]] = []
        max_attempts = max(1, min(10, self.settings.agent_search_max_attempts))
        # Retry only the failed recalls sequentially so one transient timeout
        # cannot abort an otherwise valid multi-stop plan. If the retry budget
        # is exhausted, Supervisor may authorize a provider-verified cache
        # fallback; otherwise the empty group becomes an explicit clarification.
        for index, initial_error in failures:
            current_error = initial_error
            for attempt in range(1, max_attempts + 1):
                fallback_available = self._cached_poi_results_available(
                    keywords[index], origin, city
                )
                decision = await self.orchestrator.recover(
                    stage="poi_search",
                    exc=current_error,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    timeout_seconds=self.settings.agent_stage_timeout_seconds,
                    fallback_available=fallback_available,
                    fallback_source="poi_recovery_cache",
                )
                recovery_actions.append(decision.model_dump(mode="json"))
                metrics.increment(
                    "mapgo_agent_recovery_total",
                    {"stage": decision.stage, "action": decision.action},
                )
                if decision.action == "retry":
                    await asyncio.sleep(0.15 * attempt)
                    try:
                        retry_result = await self._provider_search_with_timeout(
                            keywords[index], origin, city
                        )
                    except Exception as exc:
                        current_error = exc
                        continue
                    results[index] = retry_result
                    self._cache_poi_results(keywords[index], origin, city, retry_result)
                    break
                if decision.action == "fallback_cached":
                    results[index] = self._cached_poi_results(keywords[index], origin, city)
                    break
                break

        final_results = [item or [] for item in results]
        if avoid_hiking:
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
            final_results = [
                [
                    candidate
                    for candidate in group
                    if not any(
                        term in f"{candidate.name} {candidate.address}".casefold()
                        for term in hiking_terms
                    )
                ]
                for group in final_results
            ]
        await self.orchestrator.record_search(
            keywords=keywords,
            candidate_counts=[len(item) for item in final_results],
            provider_name=self.map_provider.name,
            recovered_failures=len(recovery_actions),
            candidates=final_results,
            recovery_actions=recovery_actions,
        )
        return final_results

    async def _provider_search_with_timeout(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]:
        return await asyncio.wait_for(
            self.map_provider.search_poi(keyword, origin, city),
            timeout=self.settings.agent_stage_timeout_seconds,
        )

    @staticmethod
    def _poi_cache_key(keyword: str, origin: Coordinate, city: str | None) -> str:
        normalized_city = (city or "").strip().casefold()
        return (
            f"{normalized_city}|{keyword.strip().casefold()}|"
            f"{origin.lng:.3f},{origin.lat:.3f}"
        )

    def _cache_poi_results(
        self,
        keyword: str,
        origin: Coordinate,
        city: str | None,
        candidates: list[PoiCandidate],
    ) -> None:
        if not candidates:
            return
        key = self._poi_cache_key(keyword, origin, city)
        _POI_RECOVERY_CACHE[key] = [candidate.model_copy(deep=True) for candidate in candidates]
        _POI_RECOVERY_CACHE.move_to_end(key)
        while len(_POI_RECOVERY_CACHE) > POI_RECOVERY_CACHE_LIMIT:
            _POI_RECOVERY_CACHE.popitem(last=False)

    def _cached_poi_results_available(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> bool:
        return self._poi_cache_key(keyword, origin, city) in _POI_RECOVERY_CACHE

    def _cached_poi_results(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]:
        key = self._poi_cache_key(keyword, origin, city)
        cached = _POI_RECOVERY_CACHE.get(key, [])
        if cached:
            _POI_RECOVERY_CACHE.move_to_end(key)
        return [
            candidate.model_copy(
                deep=True,
                update={
                    "source": f"cache:{candidate.source}",
                    "distance_meters": haversine_meters(origin, candidate.location),
                    "confidence": min(candidate.confidence, 0.55),
                },
            )
            for candidate in cached
        ]

    async def plan(self, request: AIPlanRequest) -> AIPlanResult:
        try:
            return await self._plan(request)
        finally:
            # finalize() has already copied the minimal audit summary into the
            # trace. Runtime planning memory is never retained as history.
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
                result.warnings.append("方案触发 Human-in-the-loop 人工确认闸门")
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
        intent = await self.orchestrator.begin_search(intent)
        origin = request.origin
        if origin is None:
            raise RuntimeError("澄清阶段结束后仍缺少起点")

        keywords = [self._recall_keyword(task, intent) for task in intent.tasks]
        search_results = await self._search_candidates(
            keywords,
            origin,
            request.city,
            avoid_hiking=intent.preferences.avoid_hiking,
        )
        if self.orchestrator.safety_required():
            await self.orchestrator.check_safety()
        search_results = await self.orchestrator.begin_planner(search_results)
        ambiguous = {
            index: found[:5]
            for index, found in enumerate(search_results)
            if found
            and len(found) >= 2
            and len({item.name.strip().casefold() for item in found[:3]}) == 1
            and str(index) not in request.task_poi_overrides
        }
        selected_missing: list[ClarificationQuestion] = []
        for raw_index, poi_id in request.task_poi_overrides.items():
            index = int(raw_index)
            if not 0 <= index < len(search_results):
                raise ValueError(f"task POI override index out of range: {index}")
            candidates_for_task = search_results[index]
            selected = next((item for item in candidates_for_task if item.id == poi_id), None)
            if selected is None:
                selected_missing.append(
                    ClarificationQuestion(
                        field=f"tasks.{index}.selected_poi_id",
                        reason="The selected POI is no longer returned by the map provider",
                        question="该地点已无法验证，请重新选择一个候选地点。",
                        candidates=candidates_for_task[:5],
                    )
                )
            else:
                # A resolved ambiguity is a hard user choice, not a ranking
                # hint.  Keep exactly that POI so the joint solver cannot
                # silently select another same-name result for a lower score.
                search_results[index] = [selected]
        missing = [
            ClarificationQuestion(
                field=f"tasks.{index}.location",
                reason="地图 Provider 未返回可验证的真实候选地点",
                question=f"没有找到“{keywords[index]}”，可以提供更具体的名称或区域吗？",
            )
            for index, found in enumerate(search_results)
            if not found
        ]
        if selected_missing:
            missing = selected_missing
        elif not missing and ambiguous:
            missing = select_clarification_questions(
                request=request,
                intent=intent,
                ambiguous_pois=ambiguous,
                text=request.text,
                max_questions=2,
            )
        if missing:
            result = AIPlanResult(
                status="need_clarification",
                planning_state=PlanningState.need_clarification,
                intent=intent,
                origin=request.origin,
                questions=missing,
            )
            await self.orchestrator.record_planner(
                status=result.status,
                algorithm=result.algorithm,
                candidate_count=result.candidate_count,
                stop_count=len(result.stops),
                conflict_count=len(result.conflicts),
                warning_count=len(result.warnings),
                result=result.model_dump(
                    mode="json", exclude={"critic_review", "agent_workflow"}
                ),
            )
            return result

        # Reserve one matrix point for the origin and share the remaining budget
        # fairly across tasks. This prevents a single request from exploding into
        # hundreds of upstream calls.
        per_task_limit = min(
            request.max_candidates_per_task,
            max(1, (self.settings.max_route_matrix_points - 1) // len(intent.tasks)),
        )
        candidates = [items[:per_task_limit] for items in search_results]
        flattened = [candidate for group in candidates for candidate in group]
        points = [origin, *(candidate.location for candidate in flattened)]
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.planner,
            capability="get_route_matrix",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.route_matrix}),
        )
        matrix = await self.map_provider.route_matrix(points, intent.transport_mode)

        candidate_groups: list[list[CandidateNode]] = []
        matrix_index = 1
        for task_index, group in enumerate(candidates):
            nodes = []
            for rank, candidate in enumerate(group):
                nodes.append(
                    CandidateNode(
                        task_index=task_index,
                        candidate_rank=rank,
                        matrix_index=matrix_index,
                        rating=candidate.rating,
                        confidence=candidate.confidence,
                        estimated_cost_yuan=candidate.estimated_cost_yuan,
                        open_now=candidate.open_now,
                        wheelchair_accessible=candidate.wheelchair_accessible,
                        district=candidate.district,
                    )
                )
                matrix_index += 1
            candidate_groups.append(nodes)

        departure = intent.departure_time or datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        if departure.tzinfo is None:
            departure = departure.replace(tzinfo=SHANGHAI)
        safety_buffer = max(
            (item.safety_buffer_minutes for item in intent.constraints.uncertain),
            default=0,
        )
        TOOL_REGISTRY.authorize(
            agent_type=AgentType.planner,
            capability="optimize_route",
            invocation_mode=InvocationMode.internal_stage,
            requested_scopes=frozenset({DataScope.route_optimization}),
        )
        evaluation, algorithm = await asyncio.to_thread(
            optimize_joint_route,
            departure,
            intent.tasks,
            candidate_groups,
            matrix,
            intent.preferences,
            intent.constraints.hard,
            intent.transport_mode,
            safety_buffer_minutes=safety_buffer,
        )

        # Public transit uses the same candidate/order solver, then verifies the
        # chosen sequence with real AMap transfer routes. This keeps upstream
        # calls linear in the number of stops instead of querying every pair.
        if intent.transport_mode == TransportMode.transit and evaluation.selected_nodes:
            selected_by_task_nodes = {node.task_index: node for node in evaluation.selected_nodes}
            sequence_points = [
                origin,
                *(
                    candidates[node.task_index][node.candidate_rank].location
                    for node in evaluation.selected_nodes
                ),
            ]
            if intent.constraints.hard.must_return_to_origin:
                sequence_points.append(origin)
            TOOL_REGISTRY.authorize(
                agent_type=AgentType.planner,
                capability="verify_transit_edges",
                invocation_mode=InvocationMode.internal_stage,
                requested_scopes=frozenset({DataScope.transit_routes}),
            )
            transit_edges = await self.map_provider.transit_route_edges(
                sequence_points, request.city
            )
            refined_matrix = matrix.model_copy(deep=True)
            previous_matrix_index = 0
            for node, edge in zip(
                evaluation.selected_nodes,
                transit_edges,
                strict=False,
            ):
                refined_matrix.edges[previous_matrix_index][node.matrix_index] = edge.model_copy(
                    update={
                        "origin_index": previous_matrix_index,
                        "destination_index": node.matrix_index,
                    }
                )
                previous_matrix_index = node.matrix_index
            if intent.constraints.hard.must_return_to_origin and len(transit_edges) > len(
                evaluation.selected_nodes
            ):
                refined_matrix.edges[previous_matrix_index][0] = transit_edges[-1].model_copy(
                    update={
                        "origin_index": previous_matrix_index,
                        "destination_index": 0,
                    }
                )
            matrix = refined_matrix
            TOOL_REGISTRY.authorize(
                agent_type=AgentType.planner,
                capability="optimize_route",
                invocation_mode=InvocationMode.internal_stage,
                requested_scopes=frozenset({DataScope.route_optimization}),
            )
            evaluation = await asyncio.to_thread(
                evaluate_joint_order,
                evaluation.order,
                selected_by_task_nodes,
                departure,
                intent.tasks,
                matrix,
                intent.preferences,
                intent.constraints.hard,
                intent.transport_mode,
                safety_buffer_minutes=safety_buffer,
            )
            algorithm += "+amap-transit-refinement"

        planned_stops: list[PlannedStop] = []
        previous = 0
        confidences: list[float] = []
        estimated_edges = 0
        for position, node in enumerate(evaluation.selected_nodes):
            task = intent.tasks[node.task_index]
            candidate = candidates[node.task_index][node.candidate_rank]
            edge = matrix.edges[previous][node.matrix_index]
            if edge.fallback_used:
                estimated_edges += 1
            confidences.append(min(edge.confidence, candidate.confidence))
            planned_stops.append(
                PlannedStop(
                    task_index=node.task_index,
                    candidate_rank=node.candidate_rank,
                    task=task,
                    poi=candidate,
                    arrival_time=evaluation.arrivals[position],
                    departure_time=evaluation.departures[position],
                    travel=edge,
                    constraint_satisfied=not (
                        task.deadline and evaluation.arrivals[position] > task.deadline
                    ),
                )
            )
            previous = node.matrix_index

        selected_by_task = {node.task_index: node for node in evaluation.selected_nodes}
        candidate_reviews = [
            CandidateReview(
                task_index=task_index,
                task_description=intent.tasks[task_index].description,
                considered_count=len(group),
                selected_poi_id=(
                    group[selected_by_task[task_index].candidate_rank].id
                    if task_index in selected_by_task
                    else None
                ),
                candidates=group,
            )
            for task_index, group in enumerate(candidates)
        ]

        confidence = sum(confidences) / len(confidences) if confidences else 0
        # ETA observations are collected for post-trip analysis but are not
        # yet queried as a statistically representative cohort at plan time.
        # Do not claim historical calibration until that data path exists.
        envelope = heuristic_envelope(
            expected_seconds=evaluation.total_travel_seconds,
            mean_confidence=confidence,
            fallback_used=bool(estimated_edges),
            safety_buffer_minutes=safety_buffer,
            has_deadline=any(task.deadline for task in intent.tasks),
        )
        uncertainty = UncertaintySummary(
            expected_duration_seconds=envelope.expected_seconds,
            lower_duration_seconds=envelope.lower_seconds,
            upper_duration_seconds=envelope.upper_seconds,
            on_time_probability=envelope.on_time_probability,
            method=envelope.method,
        )
        warnings = list(envelope.warnings)
        if estimated_edges:
            warnings.append(
                f"{estimated_edges} 段路线使用估算数据，时间仅供参考，前端应显示估算标记。"
            )
        if intent.constraints.uncertain:
            warnings.extend(item.reason for item in intent.constraints.uncertain)
        if evaluation.feasible and evaluation.conflicts:
            warnings.extend(evaluation.conflicts)

        if not evaluation.feasible:
            result = AIPlanResult(
                status="infeasible",
                planning_state=PlanningState.infeasible,
                intent=intent,
                origin=origin,
                departure_time=departure,
                stops=planned_stops,
                total_distance_meters=evaluation.total_distance,
                total_travel_seconds=evaluation.total_travel_seconds,
                algorithm=algorithm,
                explanation="联合求解器已尝试候选地点和访问顺序，但没有方案满足全部硬约束。",
                conflicts=evaluation.conflicts,
                warnings=warnings,
                score=evaluation.score,
                confidence=confidence,
                candidate_count=len(flattened),
                candidate_reviews=candidate_reviews,
                uncertainty=uncertainty,
            )
            await self.orchestrator.record_planner(
                status=result.status,
                algorithm=result.algorithm,
                candidate_count=result.candidate_count,
                stop_count=len(result.stops),
                conflict_count=len(result.conflicts),
                warning_count=len(result.warnings),
                result=result.model_dump(
                    mode="json", exclude={"critic_review", "agent_workflow"}
                ),
            )
            return result

        minutes = round(evaluation.total_travel_seconds / 60)
        result = AIPlanResult(
            status="success",
            planning_state=PlanningState.plan_ready,
            intent=intent,
            origin=origin,
            departure_time=departure,
            stops=planned_stops,
            total_distance_meters=evaluation.total_distance,
            total_travel_seconds=evaluation.total_travel_seconds,
            algorithm=algorithm,
            explanation=(
                f"已联合比较 {len(flattened)} 个候选地点、访问顺序与时间约束，"
                f"生成可验证方案，纯交通时间约 {minutes} 分钟。"
            ),
            warnings=warnings,
            score=evaluation.score,
            confidence=confidence,
            candidate_count=len(flattened),
            candidate_reviews=candidate_reviews,
            uncertainty=uncertainty,
        )
        await self.orchestrator.record_planner(
            status=result.status,
            algorithm=result.algorithm,
            candidate_count=result.candidate_count,
            stop_count=len(result.stops),
            conflict_count=len(result.conflicts),
            warning_count=len(result.warnings),
            result=result.model_dump(mode="json", exclude={"critic_review", "agent_workflow"}),
        )
        return result

    @staticmethod
    def _recall_keyword(task, intent) -> str:
        """Attach confirmed dining requirements to the actual POI recall query."""
        base = task.location_name or task.category or task.description
        dining_terms = ("餐", "饭", "吃", "咖啡", "茶", "food", "restaurant")
        task_text = f"{task.description} {task.location_name or ''} {task.category or ''}".lower()
        if intent.preferences.dietary_restrictions and any(
            term in task_text for term in dining_terms
        ):
            restrictions = " ".join(intent.preferences.dietary_restrictions)
            return f"{base} {restrictions}"
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
