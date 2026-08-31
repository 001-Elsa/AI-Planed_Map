from pathlib import Path

import pytest

from backend.app.core.config import Settings
from backend.app.schemas.ai_intent import Coordinate
from backend.tests.evaluation.agent_evaluation_framework import (
    PROFILES,
    README_EVAL_END,
    README_EVAL_START,
    CaseResult,
    _settings,
    _update_readme,
    _valid_tool_arguments,
    aggregate,
    load_dataset,
    run_evaluation,
    smoke_cases,
    stratified_cases,
)
from backend.tests.evaluation.replay_agent_benchmark import FaultInjectingMapProvider


def _result(**updates) -> CaseResult:
    values = {
        "case_id": "case-1",
        "category": "standard",
        "profile": "C",
        "task_success": True,
        "constraint_satisfied": True,
        "hard_constraint_violations": 0,
        "hard_constraint_expected": False,
        "tool_selection_accurate": True,
        "tool_argument_accurate": True,
        "illegal_tool_calls": 0,
        "unnecessary_tool_calls": 0,
        "tool_retries": 0,
        "clarification_expected": False,
        "clarification_triggered": False,
        "critic_bad_plan_expected": False,
        "critic_intercepted": False,
        "recovery_expected": False,
        "recovery_succeeded": False,
        "replan_expected": False,
        "replan_succeeded": False,
        "hitl_expected": False,
        "hitl_triggered": False,
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0,
        "latency_ms": 10,
        "agent_handoffs": 4,
        "terminal_status": "success",
    }
    values.update(updates)
    return CaseResult(**values)


def test_versioned_golden_dataset_expands_to_180_stable_cases():
    metadata, cases, dataset_hash = load_dataset()

    assert metadata["version"] == "1.0.0"
    assert len(cases) == 180
    assert len({case.case_id for case in cases}) == 180
    assert len(dataset_hash) == 64
    assert len(smoke_cases(cases)) == 20
    assert len({case.category for case in smoke_cases(cases)}) == 20


def test_sixty_case_protocol_is_unique_and_balanced_across_categories():
    _, cases, _ = load_dataset()

    selected = stratified_cases(cases, 60)
    category_counts: dict[str, int] = {}
    for case in selected:
        category_counts[case.category] = category_counts.get(case.category, 0) + 1

    assert len(selected) == 60
    assert len({case.case_id for case in selected}) == 60
    assert set(category_counts) == {case.category for case in cases}
    assert max(category_counts.values()) - min(category_counts.values()) <= 1


def test_strong_profile_uses_strong_tier_prices():
    base = Settings(
        llm_input_cost_per_million_usd=0.1,
        llm_output_cost_per_million_usd=0.2,
        llm_strong_input_cost_per_million_usd=3.0,
        llm_strong_output_cost_per_million_usd=9.0,
    )
    strong = next(profile for profile in PROFILES if profile.id == "F")

    settings = _settings(base, strong)

    assert settings.llm_input_cost_per_million_usd == 3.0
    assert settings.llm_output_cost_per_million_usd == 9.0


def test_tool_argument_accuracy_can_reject_missing_and_invalid_arguments():
    provider = FaultInjectingMapProvider("standard")

    assert not _valid_tool_arguments(provider, {"search_poi"}, "Hangzhou")
    provider.search_arguments.append(("", Coordinate(lng=120, lat=30), "Hangzhou"))
    assert not _valid_tool_arguments(provider, {"search_poi"}, "Hangzhou")
    provider.search_arguments[0] = (
        "museum",
        Coordinate(lng=120, lat=30),
        "Hangzhou",
    )
    assert not _valid_tool_arguments(provider, {"search_poi", "get_route_matrix"}, "Hangzhou")


def test_aggregate_preserves_metric_denominators_and_null_not_applicable():
    metrics = aggregate(
        [
            _result(),
            _result(
                case_id="case-2",
                category="critic_bad_evidence",
                hard_constraint_expected=True,
                critic_bad_plan_expected=True,
                critic_intercepted=True,
                recovery_expected=True,
                recovery_succeeded=True,
                replan_expected=True,
                replan_succeeded=True,
                llm_calls=2,
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.01,
                latency_ms=30,
            ),
        ]
    )

    assert metrics["task_success_rate"] == {"value": 1.0, "numerator": 2, "denominator": 2}
    assert metrics["critic_bad_plan_recall"]["denominator"] == 1
    assert metrics["hard_constraint_satisfaction_rate"]["denominator"] == 1
    assert metrics["replanning_success_rate"]["value"] == 1.0
    assert metrics["hitl_trigger_precision"]["value"] is None
    assert metrics["average_llm_calls"] == 1
    assert metrics["p95_latency_ms"] == 30


@pytest.mark.asyncio
async def test_live_without_api_key_writes_explicit_skipped_report(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")

    report, json_path, markdown_path = await run_evaluation(
        mode="live",
        suite="smoke",
        profile_ids={"A"},
        output_dir=tmp_path,
    )

    assert report["status"] == "SKIPPED"
    assert report["profiles"] == []
    assert "no synthetic results" in report["skip_reason"]
    assert json_path.exists()
    assert "SKIPPED" in markdown_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_live_strong_profile_refuses_implicit_default_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_STRONG_MODEL", "")

    report, _, _ = await run_evaluation(
        mode="live",
        suite="full",
        profile_ids={"F"},
        case_count=1,
        output_dir=tmp_path,
    )

    assert report["status"] == "SKIPPED"
    assert "refused to reuse LLM_MODEL" in report["skip_reason"]


@pytest.mark.asyncio
async def test_repeats_preserve_trial_metrics_failures_and_readme_rendering(tmp_path: Path):
    report, json_path, _ = await run_evaluation(
        mode="offline",
        suite="full",
        profile_ids={"A"},
        case_count=3,
        repeats=2,
        output_dir=tmp_path,
    )
    profile = report["profiles"][0]

    assert report["selected_case_count"] == 3
    assert report["executions_per_profile"] == 6
    assert profile["metrics"]["case_count"] == 6
    assert [trial["trial"] for trial in profile["trial_metrics"]] == [1, 2]
    assert {item["trial"] for item in profile["cases"]} == {1, 2}
    assert profile["failures"] == [item for item in profile["cases"] if not item["task_success"]]

    readme = tmp_path / "README.md"
    readme.write_text(
        f"before\n{README_EVAL_START}\npending\n{README_EVAL_END}\nafter\n",
        encoding="utf-8",
    )
    report["mode"] = "live"
    profile["model"] = "test-model"
    _update_readme(readme, report, json_path)
    rendered = readme.read_text(encoding="utf-8")

    assert "3 unique stratified cases" in rendered
    assert "test-model" in rendered
    assert rendered.startswith("before\n")
    assert rendered.endswith("after\n")


@pytest.mark.asyncio
async def test_offline_multi_agent_smoke_executes_production_dynamic_replans(
    tmp_path: Path,
):
    report, json_path, markdown_path = await run_evaluation(
        mode="offline",
        suite="smoke",
        profile_ids={"C"},
        output_dir=tmp_path,
    )

    metrics = report["profiles"][0]["metrics"]
    assert report["status"] == "COMPLETED"
    assert metrics["production_dynamic_replay_rate"]["value"] == 1
    assert metrics["workflow_graph_accuracy"]["value"] == 1
    assert metrics["average_true_agent_tasks_per_dynamic_run"] == 1
    assert metrics["average_deterministic_stages_per_dynamic_run"] == 5
    assert json_path.exists()
    assert markdown_path.exists()
