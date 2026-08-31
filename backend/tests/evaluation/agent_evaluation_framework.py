"""Reproducible offline/live Agent evaluation with A-F ablations.

Offline mode uses deterministic production boundaries and is suitable for CI.
Live mode calls the configured OpenAI-compatible LLM endpoint.  It never
substitutes mock scores when credentials are absent: a SKIPPED report is
written instead.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.app.core.config import Settings  # noqa: E402
from backend.app.schemas.agent_artifacts import AgentType, AgentWorkflowTrace  # noqa: E402
from backend.app.schemas.ai_intent import AIPlanResult, TransportMode  # noqa: E402
from backend.app.services.agent_evaluation import (  # noqa: E402
    evaluate_route_plan,
    runtime_route_policy,
)
from backend.app.services.agents.critic_agent import (  # noqa: E402
    OpenAICompatibleCriticAgent,
    RuleBasedCriticAgent,
)
from backend.app.services.intent_parser import (  # noqa: E402
    FallbackIntentParser,
    OpenAICompatibleIntentParser,
    RuleBasedIntentParser,
)
from backend.app.services.planning_service import PlanningService  # noqa: E402
from backend.tests.evaluation.replay_agent_benchmark import (  # noqa: E402
    DynamicReplayEvidence,
    FaultInjectingMapProvider,
    ReplayCase,
    ReplayIntentParser,
    _build_case,
    _run_production_dynamic_replay,
    run_single_agent,
)

Mode = Literal["offline", "live"]
Suite = Literal["smoke", "full"]

DATASET_PATH = Path(__file__).parent / "datasets" / "agent_golden_v1.json"
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "agent-evaluation"
README_EVAL_START = "<!-- agent-live-eval:start -->"
README_EVAL_END = "<!-- agent-live-eval:end -->"


@dataclass(frozen=True)
class AblationProfile:
    id: str
    label: str
    multi_agent: bool
    critic: Literal["off", "rule", "llm"]
    model_tier: Literal["default", "small", "strong"] = "default"


PROFILES: tuple[AblationProfile, ...] = (
    AblationProfile("A", "Single Agent Baseline", False, "off"),
    AblationProfile("B", "Multi-Agent without Critic", True, "off"),
    AblationProfile("C", "Multi-Agent + Rule Critic", True, "rule"),
    AblationProfile("D", "Multi-Agent + LLM Critic", True, "llm"),
    AblationProfile("E", "Multi-Agent + Small Model", True, "llm", "small"),
    AblationProfile("F", "Multi-Agent + Strong Model", True, "llm", "strong"),
)


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    category: str
    scenario: str
    text: str
    expected_statuses: tuple[str, ...]
    expected_tools: tuple[str, ...]
    clarification_expected: bool = False
    critic_bad_plan_expected: bool = False
    hitl_expected: bool = False
    replan_expected: bool = False
    recovery_expected: bool = False
    hard_constraint_expected: bool = False
    llm_fault_probe: str | None = None


@dataclass
class CaseResult:
    case_id: str
    category: str
    profile: str
    task_success: bool
    constraint_satisfied: bool
    hard_constraint_violations: int
    hard_constraint_expected: bool
    tool_selection_accurate: bool
    tool_argument_accurate: bool
    illegal_tool_calls: int
    unnecessary_tool_calls: int
    tool_retries: int
    clarification_expected: bool
    clarification_triggered: bool
    critic_bad_plan_expected: bool
    critic_intercepted: bool
    recovery_expected: bool
    recovery_succeeded: bool
    replan_expected: bool
    replan_succeeded: bool
    hitl_expected: bool
    hitl_triggered: bool
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    agent_handoffs: int
    terminal_status: str
    actual_tools: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    fallback_used: bool = False
    error: str | None = None
    production_replan_executed: bool = False
    workflow_graph_valid: bool = False
    execution_mode: str | None = None
    agent_task_count: int = 0
    stage_task_count: int = 0
    trial: int = 1


class CountingParser:
    def __init__(self, parser: Any, *, live: bool) -> None:
        self.parser = parser
        self.name = parser.name
        self.live = live
        self.calls = 0

    @property
    def input_tokens(self) -> int:
        return int(getattr(self.parser, "input_tokens", 0) or 0)

    @property
    def output_tokens(self) -> int:
        return int(getattr(self.parser, "output_tokens", 0) or 0)

    @property
    def fallback_used(self) -> bool:
        return bool(getattr(self.parser, "fallback_used", False))

    async def parse(self, text: str):
        self.calls += int(self.live)
        return await self.parser.parse(text)


class CountingCritic:
    spec = RuleBasedCriticAgent.spec

    def __init__(self, critic: Any, *, live: bool) -> None:
        self.critic = critic
        self.live = live
        self.calls = 0

    async def run(self, context: Any):
        self.calls += int(self.live)
        return await self.critic.run(context)


def load_dataset(path: Path = DATASET_PATH) -> tuple[dict[str, Any], list[GoldenCase], str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[GoldenCase] = []
    for template in raw["templates"]:
        for variant in range(1, int(template["variants"]) + 1):
            cases.append(
                GoldenCase(
                    case_id=f"{template['id']}-{variant:02d}",
                    category=template["id"],
                    scenario=template["scenario"],
                    text=template["text"].format(variant=variant),
                    expected_statuses=tuple(template["expected_statuses"]),
                    expected_tools=tuple(template["expected_tools"]),
                    clarification_expected=bool(template.get("clarification_expected")),
                    critic_bad_plan_expected=bool(template.get("critic_bad_plan_expected")),
                    hitl_expected=bool(template.get("hitl_expected")),
                    replan_expected=bool(template.get("replan_expected")),
                    recovery_expected=bool(template.get("recovery_expected")),
                    hard_constraint_expected=bool(template.get("hard_constraint_expected")),
                    llm_fault_probe=template.get("llm_fault_probe"),
                )
            )
    canonical = json.dumps([asdict(case) for case in cases], sort_keys=True, separators=(",", ":"))
    return raw, cases, hashlib.sha256(canonical.encode()).hexdigest()


def stratified_cases(cases: list[GoldenCase], count: int) -> list[GoldenCase]:
    """Select unique cases by deterministic round-robin across categories."""

    if count < 1 or count > len(cases):
        raise ValueError(f"case count must be between 1 and {len(cases)}")
    buckets: dict[str, list[GoldenCase]] = {}
    for case in cases:
        buckets.setdefault(case.category, []).append(case)
    selected: list[GoldenCase] = []
    variant = 0
    while len(selected) < count:
        for bucket in buckets.values():
            if variant < len(bucket):
                selected.append(bucket[variant])
                if len(selected) == count:
                    return selected
        variant += 1
    return selected


def smoke_cases(cases: list[GoldenCase], count: int = 20) -> list[GoldenCase]:
    return stratified_cases(cases, count)


def _replay_case(case: GoldenCase, index: int) -> ReplayCase:
    base = _build_case(index, case.scenario)  # type: ignore[arg-type]
    request = base.request.model_copy(update={"text": case.text})
    return replace(
        base,
        case_id=case.case_id,
        request=request,
        expected_statuses=frozenset(case.expected_statuses),
        expected_tools=frozenset(case.expected_tools),
        requires_replan=case.replan_expected,
        critic_should_intercept=case.critic_bad_plan_expected,
    )


def _model_name(settings: Settings, profile: AblationProfile) -> str:
    if profile.model_tier == "small":
        return settings.llm_small_model or settings.llm_model
    if profile.model_tier == "strong":
        return settings.llm_strong_model or settings.llm_model
    return settings.llm_model


def _settings(base: Settings, profile: AblationProfile) -> Settings:
    input_cost = base.llm_input_cost_per_million_usd
    output_cost = base.llm_output_cost_per_million_usd
    if profile.model_tier == "small":
        input_cost = base.llm_small_input_cost_per_million_usd
        output_cost = base.llm_small_output_cost_per_million_usd
    elif profile.model_tier == "strong":
        input_cost = base.llm_strong_input_cost_per_million_usd
        output_cost = base.llm_strong_output_cost_per_million_usd
    return base.model_copy(
        update={
            "mock_map_provider": True,
            "multi_agent_enabled": profile.multi_agent,
            "plan_critic_mode": "off" if profile.critic == "off" else "enforce",
            "agent_search_max_attempts": 2,
            "agent_stage_timeout_seconds": max(2, base.agent_stage_timeout_seconds),
            "max_agent_workflow_cost_usd": max(1, base.max_agent_workflow_cost_usd),
            "max_agent_handoffs": max(20, base.max_agent_handoffs),
            "llm_input_cost_per_million_usd": input_cost,
            "llm_output_cost_per_million_usd": output_cost,
        }
    )


def _tools(trace: AgentWorkflowTrace | None, provider: FaultInjectingMapProvider) -> set[str]:
    agents = {step.agent_type for step in trace.steps} if trace else set()
    tools: set[str] = set()
    if AgentType.intent in agents:
        tools.add("parse_requirement")
    if provider.search_calls:
        tools.add("search_poi")
    if AgentType.safety in agents:
        tools.add("check_travel_safety")
    if provider.matrix_calls:
        tools.add("get_route_matrix")
    if AgentType.planner in agents and provider.successful_matrix_calls:
        tools.add("optimize_route")
    return tools


def _valid_tool_arguments(
    provider: FaultInjectingMapProvider,
    expected_tools: set[str],
    expected_city: str | None,
) -> bool:
    if "search_poi" in expected_tools and not provider.search_arguments:
        return False
    for keyword, origin, city in provider.search_arguments:
        if not keyword.strip() or city != expected_city:
            return False
        if not (-180 <= origin.lng <= 180 and -90 <= origin.lat <= 90):
            return False
    if "get_route_matrix" in expected_tools and not provider.matrix_arguments:
        return False
    valid_modes = {item.value for item in TransportMode}
    return all(
        point_count >= 2 and getattr(mode, "value", mode) in valid_modes
        for point_count, mode in provider.matrix_arguments
    )


def _constraint_result(result: AIPlanResult | None) -> tuple[bool, int]:
    if result is None or result.status != "success":
        return True, 0
    payload = result.model_dump(mode="json", exclude={"agent_workflow"})
    evaluation = evaluate_route_plan(payload, runtime_route_policy(payload))
    return evaluation.passed, len(evaluation.hard_failures)


async def _execute_service_case(
    case: GoldenCase,
    index: int,
    profile: AblationProfile,
    mode: Mode,
    base_settings: Settings,
    client: httpx.AsyncClient | None,
) -> CaseResult:
    replay = _replay_case(case, index)
    settings = _settings(base_settings, profile)
    provider = FaultInjectingMapProvider(replay.scenario)
    model_name = _model_name(settings, profile)
    if mode == "live":
        assert client is not None
        primary = OpenAICompatibleIntentParser(settings, client, model_name=model_name)
        parser_impl: Any = FallbackIntentParser(primary, RuleBasedIntentParser())
    else:
        parser_impl = ReplayIntentParser(replay.intent)
    parser = CountingParser(parser_impl, live=mode == "live")

    critic_impl: Any = RuleBasedCriticAgent()
    critic_is_live = mode == "live" and profile.critic == "llm"
    if critic_is_live:
        assert client is not None
        critic_impl = OpenAICompatibleCriticAgent(settings, client, model_name=model_name)
    critic = CountingCritic(critic_impl, live=critic_is_live)
    service = PlanningService(parser, provider, settings, critic_agent=critic)
    started = time.perf_counter()
    result: AIPlanResult | None = None
    traces: list[AgentWorkflowTrace] = []
    error: str | None = None
    replanned = False
    dynamic_evidence: DynamicReplayEvidence | None = None
    planning_search_calls = 0
    try:
        result = await service.plan(replay.request)
        planning_search_calls = provider.search_calls
        if result.agent_workflow:
            traces.append(AgentWorkflowTrace.model_validate(result.agent_workflow))
        if case.replan_expected and result.status == "success":
            if profile.multi_agent:
                dynamic_evidence = await _run_production_dynamic_replay(replay, result, provider)
                replanned = dynamic_evidence.replanning_succeeded
            else:
                next_service = PlanningService(parser, provider, settings, critic_agent=critic)
                result = await next_service.plan(replay.request)
                replanned = result.status == "success"
                if result.agent_workflow:
                    traces.append(AgentWorkflowTrace.model_validate(result.agent_workflow))
    except Exception as exc:  # noqa: BLE001 - evaluation must preserve per-case failures
        error = f"{type(exc).__name__}: {exc}"[:500]

    trace = traces[-1] if traces else None
    actual_tools = _tools(trace, provider)
    expected_tools = set(case.expected_tools)
    all_steps = [step for item in traces for step in item.steps]
    input_tokens = sum(step.input_tokens for step in all_steps)
    output_tokens = sum(step.output_tokens for step in all_steps)
    cost = sum(item.total_cost_usd for item in traces)
    handoffs = sum(item.handoff_count for item in traces)
    if dynamic_evidence:
        handoffs += dynamic_evidence.handoff_count
    critic_intercepted = bool(
        result and result.critic_review and result.critic_review.verdict == "needs_clarification"
    )
    hitl_triggered = bool(
        result and any(question.kind == "confirmation" for question in result.questions)
    )
    terminal = result.status if result else "error"
    constraint_satisfied, hard_violations = _constraint_result(result)
    unnecessary = len(actual_tools - expected_tools)
    expected_task_calls = len(replay.intent.tasks)
    tool_retries = max(0, planning_search_calls - expected_task_calls)
    tool_retries += sum(item.retry_count for item in traces)
    clarification_triggered = terminal == "need_clarification"
    task_success = terminal in case.expected_statuses and error is None
    if case.replan_expected:
        task_success = task_success and replanned
    recovery_succeeded = (
        replanned if case.replan_expected else terminal in case.expected_statuses and error is None
    )
    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        profile=profile.id,
        task_success=task_success,
        constraint_satisfied=constraint_satisfied,
        hard_constraint_violations=hard_violations,
        hard_constraint_expected=case.hard_constraint_expected,
        tool_selection_accurate=actual_tools == expected_tools,
        tool_argument_accurate=_valid_tool_arguments(provider, expected_tools, replay.request.city),
        illegal_tool_calls=0,
        unnecessary_tool_calls=unnecessary,
        tool_retries=tool_retries,
        clarification_expected=case.clarification_expected,
        clarification_triggered=clarification_triggered,
        critic_bad_plan_expected=case.critic_bad_plan_expected,
        critic_intercepted=critic_intercepted,
        recovery_expected=case.recovery_expected,
        recovery_succeeded=recovery_succeeded,
        replan_expected=case.replan_expected,
        replan_succeeded=replanned,
        hitl_expected=case.hitl_expected,
        hitl_triggered=hitl_triggered,
        llm_calls=parser.calls + critic.calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=(time.perf_counter() - started) * 1000,
        agent_handoffs=handoffs,
        terminal_status=terminal,
        actual_tools=sorted(actual_tools),
        expected_tools=sorted(expected_tools),
        fallback_used=parser.fallback_used or any(step.fallback_used for step in all_steps),
        error=error,
        production_replan_executed=dynamic_evidence is not None,
        workflow_graph_valid=bool(dynamic_evidence and dynamic_evidence.workflow_graph_valid),
        execution_mode=dynamic_evidence.execution_mode if dynamic_evidence else None,
        agent_task_count=dynamic_evidence.agent_task_count if dynamic_evidence else 0,
        stage_task_count=dynamic_evidence.stage_task_count if dynamic_evidence else 0,
    )


async def _execute_case(
    case: GoldenCase,
    index: int,
    profile: AblationProfile,
    mode: Mode,
    settings: Settings,
    client: httpx.AsyncClient | None,
) -> CaseResult:
    if mode == "offline" and profile.id == "A":
        replay = _replay_case(case, index)
        raw = await run_single_agent(replay)
        clarification = raw.terminal_status == "need_clarification"
        return CaseResult(
            case_id=case.case_id,
            category=case.category,
            profile=profile.id,
            task_success=raw.task_success,
            constraint_satisfied=raw.hard_constraints_satisfied,
            hard_constraint_violations=0 if raw.hard_constraints_satisfied else 1,
            hard_constraint_expected=case.hard_constraint_expected,
            tool_selection_accurate=raw.tool_selection_accurate,
            tool_argument_accurate=True,
            illegal_tool_calls=raw.unauthorized_tool_executions,
            unnecessary_tool_calls=len(set(raw.actual_tools) - set(raw.expected_tools)),
            tool_retries=1 if case.scenario == "search_recovery" else 0,
            clarification_expected=case.clarification_expected,
            clarification_triggered=clarification,
            critic_bad_plan_expected=case.critic_bad_plan_expected,
            critic_intercepted=raw.critic_intercepted,
            recovery_expected=case.recovery_expected,
            recovery_succeeded=raw.recovery_success,
            replan_expected=case.replan_expected,
            replan_succeeded=raw.replanning_success,
            hitl_expected=case.hitl_expected,
            hitl_triggered=False,
            llm_calls=raw.llm_calls,
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            cost_usd=raw.token_cost_usd,
            latency_ms=raw.latency_ms,
            agent_handoffs=0,
            terminal_status=raw.terminal_status,
            actual_tools=raw.actual_tools,
            expected_tools=raw.expected_tools,
        )
    return await _execute_service_case(case, index, profile, mode, settings, client)


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "value": round(numerator / denominator, 6) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[position], 3)


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    count = len(results)
    expected_bad = [item for item in results if item.critic_bad_plan_expected]
    intercepted = [item for item in results if item.critic_intercepted]
    recovery = [item for item in results if item.recovery_expected]
    replans = [item for item in results if item.replan_expected]
    production_replans = [item for item in replans if item.production_replan_executed]
    hard_constraints = [item for item in results if item.hard_constraint_expected]
    hitl = [item for item in results if item.hitl_expected or item.hitl_triggered]
    clarification = [
        item for item in results if item.clarification_expected or item.clarification_triggered
    ]
    return {
        "case_count": count,
        "success_count": sum(item.task_success for item in results),
        "failure_count": sum(not item.task_success for item in results),
        "task_success_rate": _rate(sum(item.task_success for item in results), count),
        "constraint_satisfaction_rate": _rate(
            sum(item.constraint_satisfied for item in results), count
        ),
        "hard_constraint_satisfaction_rate": _rate(
            sum(item.constraint_satisfied for item in hard_constraints),
            len(hard_constraints),
        ),
        "hard_constraint_violation_rate": _rate(
            sum(bool(item.hard_constraint_violations) for item in results), count
        ),
        "tool_selection_accuracy": _rate(
            sum(item.tool_selection_accurate for item in results), count
        ),
        "tool_argument_accuracy": _rate(
            sum(item.tool_argument_accurate for item in results), count
        ),
        "illegal_tool_call_rate": _rate(sum(item.illegal_tool_calls for item in results), count),
        "unnecessary_tool_call_rate": _rate(
            sum(bool(item.unnecessary_tool_calls) for item in results), count
        ),
        "tool_retry_rate": _rate(sum(bool(item.tool_retries) for item in results), count),
        "clarification_accuracy": _rate(
            sum(
                item.clarification_expected == item.clarification_triggered
                for item in clarification
            ),
            len(clarification),
        ),
        "critic_bad_plan_recall": _rate(
            sum(item.critic_intercepted for item in expected_bad), len(expected_bad)
        ),
        "critic_precision": _rate(
            sum(item.critic_bad_plan_expected for item in intercepted), len(intercepted)
        ),
        "false_reject_rate": _rate(
            sum(item.critic_intercepted and not item.critic_bad_plan_expected for item in results),
            sum(not item.critic_bad_plan_expected for item in results),
        ),
        "recovery_rate": _rate(sum(item.recovery_succeeded for item in recovery), len(recovery)),
        "replanning_success_rate": _rate(
            sum(item.replan_succeeded for item in replans), len(replans)
        ),
        "production_dynamic_replay_rate": _rate(
            sum(item.production_replan_executed for item in replans), len(replans)
        ),
        "workflow_graph_accuracy": _rate(
            sum(item.workflow_graph_valid for item in production_replans),
            len(production_replans),
        ),
        "average_true_agent_tasks_per_dynamic_run": round(
            sum(item.agent_task_count for item in production_replans) / len(production_replans),
            4,
        )
        if production_replans
        else 0,
        "average_deterministic_stages_per_dynamic_run": round(
            sum(item.stage_task_count for item in production_replans) / len(production_replans),
            4,
        )
        if production_replans
        else 0,
        "hitl_trigger_precision": _rate(
            sum(item.hitl_expected and item.hitl_triggered for item in hitl),
            sum(item.hitl_triggered for item in hitl),
        ),
        "average_llm_calls": round(sum(item.llm_calls for item in results) / count, 4),
        "average_input_tokens": round(sum(item.input_tokens for item in results) / count, 4),
        "average_output_tokens": round(sum(item.output_tokens for item in results) / count, 4),
        "average_cost_per_task_usd": round(sum(item.cost_usd for item in results) / count, 8),
        "p50_latency_ms": _percentile([item.latency_ms for item in results], 0.50),
        "p95_latency_ms": _percentile([item.latency_ms for item in results], 0.95),
        "average_agent_handoffs": round(sum(item.agent_handoffs for item in results) / count, 4),
        "total_input_tokens": sum(item.input_tokens for item in results),
        "total_output_tokens": sum(item.output_tokens for item in results),
        "total_tokens": sum(item.input_tokens + item.output_tokens for item in results),
        "total_cost_usd": round(sum(item.cost_usd for item in results), 8),
        "fallback_rate": _rate(sum(item.fallback_used for item in results), count),
    }


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    return completed.stdout.strip() or "unknown"


def _provider_name(settings: Settings, mode: Mode) -> str:
    if mode == "offline":
        return "deterministic/mock-map-v2"
    parsed = urlparse(settings.llm_base_url)
    return os.getenv("AGENT_EVAL_PROVIDER") or parsed.netloc or "openai-compatible"


def _metric_value(raw: Any, *, percent: bool = False) -> str:
    value = raw.get("value") if isinstance(raw, dict) else raw
    if value is None:
        return "N/A"
    if percent:
        return f"{float(value) * 100:.2f}%"
    return f"{float(value):.3f}"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Agent Evaluation Report",
        "",
        f"Status: **{report['status']}**  ",
        f"Mode / suite: `{report['mode']}` / `{report['suite']}`  ",
        f"Run time: `{report['run_time']}`  ",
        f"Git commit: `{report['git_commit']}`  ",
        f"Dataset: `{report['dataset_id']}@{report['dataset_version']}`  ",
        f"Dataset hash: `{report['dataset_hash']}`  ",
        f"Provider: `{report['provider']}`  ",
        f"Model version: `{report['model_version']}`  ",
        f"Independent cases / repeats: `{report['selected_case_count']}` / `{report['repeats']}`",
        "",
    ]
    if report["status"] == "SKIPPED":
        lines.extend([f"Reason: {report['skip_reason']}", ""])
        return "\n".join(lines)
    lines.extend(
        [
            "| Profile | Runs | Task success | Hard constraint sat. | Tool selection | Tool arguments | Critic precision | Critic recall | Tokens | Cost USD | P95 ms | Failures |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in report["profiles"]:
        metric = profile["metrics"]
        lines.append(
            f"| {profile['id']} {profile['label']} | {metric['case_count']} | "
            f"{_metric_value(metric['task_success_rate'], percent=True)} | "
            f"{_metric_value(metric['hard_constraint_satisfaction_rate'], percent=True)} | "
            f"{_metric_value(metric['tool_selection_accuracy'], percent=True)} | "
            f"{_metric_value(metric['tool_argument_accuracy'], percent=True)} | "
            f"{_metric_value(metric['critic_precision'], percent=True)} | "
            f"{_metric_value(metric['critic_bad_plan_recall'], percent=True)} | "
            f"{metric['total_tokens']} | {metric['total_cost_usd']:.8f} | "
            f"{metric['p95_latency_ms']:.3f} | {metric['failure_count']} |"
        )
    lines.extend(
        [
            "",
            "Rates include numerator and denominator in the JSON artifact; `null` means the metric was not applicable, not zero.",
            "Offline D/E/F use deterministic critic fixtures and never claim real model quality. Only `live` results are LLM evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def _artifact_link(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _live_readme_block(report: dict[str, Any], json_path: Path) -> str:
    lines = [
        README_EVAL_START,
        "### Real LLM Comparison",
        "",
        (
            f"Measured `{report['run_time']}` with {report['selected_case_count']} unique "
            f"stratified cases, {report['repeats']} repeats per profile "
            f"({report['executions_per_profile']} task executions/profile). Provider: "
            f"`{report['provider']}`; dataset hash: `{report['dataset_hash']}`."
        ),
        "",
        "| Profile | Model | Runs | Task success | Hard constraint sat. | Tool selection | Tool arguments | Critic precision | Critic recall | Tokens | Cost USD | P95 ms | Failures |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for profile in report["profiles"]:
        metric = profile["metrics"]
        lines.append(
            f"| {profile['id']} {profile['label']} | `{profile['model']}` | "
            f"{metric['case_count']} | {_metric_value(metric['task_success_rate'], percent=True)} | "
            f"{_metric_value(metric['hard_constraint_satisfaction_rate'], percent=True)} | "
            f"{_metric_value(metric['tool_selection_accuracy'], percent=True)} | "
            f"{_metric_value(metric['tool_argument_accuracy'], percent=True)} | "
            f"{_metric_value(metric['critic_precision'], percent=True)} | "
            f"{_metric_value(metric['critic_bad_plan_recall'], percent=True)} | "
            f"{metric['total_tokens']} | {metric['total_cost_usd']:.8f} | "
            f"{metric['p95_latency_ms']:.3f} | {metric['failure_count']} |"
        )
    lines.extend(
        [
            "",
            f"Full per-trial metrics and failure cases: [`{json_path.name}`]({_artifact_link(json_path)}).",
            README_EVAL_END,
        ]
    )
    return "\n".join(lines)


def _update_readme(readme_path: Path, report: dict[str, Any], json_path: Path) -> None:
    if report["status"] != "COMPLETED" or report["mode"] != "live":
        raise ValueError("README can only be updated from a completed live evaluation")
    content = readme_path.read_text(encoding="utf-8")
    start = content.find(README_EVAL_START)
    end = content.find(README_EVAL_END)
    if start < 0 or end < start:
        raise ValueError("README live-evaluation markers are missing or out of order")
    end += len(README_EVAL_END)
    updated = content[:start] + _live_readme_block(report, json_path) + content[end:]
    readme_path.write_text(updated, encoding="utf-8")


async def run_evaluation(
    *,
    mode: Mode,
    suite: Suite,
    profile_ids: set[str] | None = None,
    dataset_path: Path = DATASET_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    case_count: int | None = None,
    repeats: int = 1,
    readme_path: Path | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    dataset, all_cases, dataset_hash = load_dataset(dataset_path)
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    if case_count is not None:
        cases = stratified_cases(all_cases, case_count)
        selection_method = "deterministic-category-round-robin"
    elif suite == "smoke":
        cases = smoke_cases(all_cases)
        selection_method = "deterministic-category-round-robin"
    else:
        cases = all_cases
        selection_method = "full-dataset"
    settings = Settings()
    selected = [profile for profile in PROFILES if not profile_ids or profile.id in profile_ids]
    if not selected:
        raise ValueError("at least one evaluation profile must be selected")
    run_time = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "schema_version": "1.2.0",
        "status": "COMPLETED",
        "mode": mode,
        "suite": suite,
        "run_time": run_time,
        "git_commit": _git_commit(),
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["version"],
        "dataset_hash": dataset_hash,
        "dataset_case_count": len(all_cases),
        "selected_case_count": len(cases),
        "selected_case_ids": [case.case_id for case in cases],
        "selection_method": selection_method,
        "repeats": repeats,
        "executions_per_profile": len(cases) * repeats,
        "total_task_executions": len(cases) * repeats * len(selected),
        "provider": _provider_name(settings, mode),
        "model_version": os.getenv("AGENT_EVAL_MODEL_VERSION", "not-specified"),
        "models": {
            "default": settings.llm_model if mode == "live" else "deterministic",
            "small": (settings.llm_small_model or settings.llm_model)
            if mode == "live"
            else "deterministic-small-fixture",
            "strong": (settings.llm_strong_model or settings.llm_model)
            if mode == "live"
            else "deterministic-strong-fixture",
        },
        "profiles": [],
    }
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"agent-eval-{mode}-{suite}-{len(cases)}x{repeats}-{stamp}"
    json_path = output_dir / f"{prefix}.json"
    markdown_path = output_dir / f"{prefix}.md"

    skip_reason: str | None = None
    if mode == "live" and not settings.llm_api_key.strip():
        skip_reason = "LLM_API_KEY is not configured; no synthetic results were generated."
    elif mode == "live" and any(profile.model_tier == "strong" for profile in selected):
        if not settings.llm_strong_model.strip():
            skip_reason = (
                "LLM_STRONG_MODEL is not configured for the selected strong-model profile; "
                "the framework refused to reuse LLM_MODEL as fake strong-model evidence."
            )
    if skip_reason:
        report.update(
            {
                "status": "SKIPPED",
                "skip_reason": skip_reason,
            }
        )
    else:
        timeout = httpx.Timeout(
            settings.external_timeout_seconds,
            connect=settings.external_connect_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            for profile in selected:
                results: list[CaseResult] = []
                trial_metrics: list[dict[str, Any]] = []
                for trial in range(1, repeats + 1):
                    trial_results: list[CaseResult] = []
                    for index, case in enumerate(cases):
                        result = await _execute_case(
                            case,
                            index,
                            profile,
                            mode,
                            settings,
                            client if mode == "live" else None,
                        )
                        result.trial = trial
                        trial_results.append(result)
                    results.extend(trial_results)
                    trial_metrics.append({"trial": trial, "metrics": aggregate(trial_results)})
                report["profiles"].append(
                    {
                        **asdict(profile),
                        "model": _model_name(settings, profile)
                        if mode == "live"
                        else "deterministic-fixture",
                        "metrics": aggregate(results),
                        "trial_metrics": trial_metrics,
                        "failures": [asdict(item) for item in results if not item.task_success],
                        "cases": [asdict(item) for item in results],
                    }
                )

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    if readme_path is not None and report["status"] == "COMPLETED" and mode == "live":
        _update_readme(readme_path, report, json_path)
        report["readme_updated"] = str(readme_path)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
    return report, json_path, markdown_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--suite", choices=("smoke", "full"), default="full")
    parser.add_argument(
        "--profiles",
        default="A,B,C,D,E,F",
        help="Comma-separated ablation IDs (default: A,B,C,D,E,F)",
    )
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument(
        "--case-count",
        type=int,
        help="Deterministically select this many unique stratified cases",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Repeat every selected case per profile (default: 1)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.getenv("AGENT_EVAL_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)),
    )
    parser.add_argument(
        "--update-readme",
        action="store_true",
        help="Replace the README live-comparison block after a completed live run",
    )
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help="Return exit code 2 when a live run is skipped",
    )
    args = parser.parse_args()
    profile_ids = {item.strip().upper() for item in args.profiles.split(",") if item.strip()}
    unknown = profile_ids - {profile.id for profile in PROFILES}
    if unknown:
        parser.error(f"unknown profiles: {sorted(unknown)}")
    report, json_path, markdown_path = asyncio.run(
        run_evaluation(
            mode=args.mode,
            suite=args.suite,
            profile_ids=profile_ids,
            dataset_path=args.dataset,
            output_dir=args.output_dir,
            case_count=args.case_count,
            repeats=args.repeats,
            readme_path=ROOT / "README.md" if args.update_readme else None,
        )
    )
    print(f"Agent evaluation: {report['status']}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 2 if args.fail_on_skip and report["status"] == "SKIPPED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
