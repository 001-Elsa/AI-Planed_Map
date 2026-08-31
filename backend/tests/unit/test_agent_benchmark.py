import asyncio

from backend.tests.evaluation.replay_agent_benchmark import benchmark, build_cases


def test_replay_dataset_is_deterministic_and_covers_one_hundred_shared_cases():
    first = build_cases(100)
    second = build_cases(100)

    assert len(first) == 100
    assert [item.case_id for item in first] == [item.case_id for item in second]
    assert {item.scenario for item in first} == {
        "standard",
        "safety",
        "clarification",
        "search_recovery",
        "matrix_failure",
        "infeasible",
        "critic_bad_evidence",
        "weather_change",
        "poi_closed",
        "off_route",
        "duplicate_event",
        "worker_crash_recovery",
        "tool_escalation",
    }


def test_replay_executes_every_scenario_and_computes_runtime_metrics():
    report = asyncio.run(benchmark(13))

    assert report["case_count"] == 13
    assert report["single_agent"]["case_count"] == 13
    assert report["multi_agent"]["case_count"] == 13
    assert report["multi_agent"]["hard_constraint_satisfaction_rate"] == 1
    assert report["multi_agent"]["illegal_tool_execution_rate"] == 0
    assert report["multi_agent"]["agent_handoff_success_rate"] == 1
    assert report["multi_agent"]["critic_bad_plan_recall"] == 1
    assert report["multi_agent"]["production_dynamic_replay_rate"] == 1
    assert report["multi_agent"]["workflow_graph_accuracy"] == 1
    assert report["multi_agent"]["average_true_agent_tasks_per_dynamic_run"] == 1
    assert report["multi_agent"]["average_deterministic_stages_per_dynamic_run"] == 5
    assert report["multi_agent"]["latency_p95_ms"] >= report["multi_agent"]["latency_p50_ms"]
