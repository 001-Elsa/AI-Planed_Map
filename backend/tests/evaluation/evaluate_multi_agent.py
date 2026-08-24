"""Static gates for MAPGO multi-Agent task-graph evaluation cases."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

REQUIRED_TAGS = {
    "clarification",
    "parallel_search",
    "time_window_conflict",
    "weather_change",
    "poi_closed",
    "off_route",
    "user_rejects_patch",
    "duplicate_event",
    "worker_crash_recovery",
    "tool_escalation",
    "hitl",
}


def _acyclic(nodes: list[dict]) -> bool:
    remaining = {node["id"]: set(node.get("depends_on") or []) for node in nodes}
    resolved: set[str] = set()
    while remaining:
        ready = [key for key, deps in remaining.items() if deps <= resolved]
        if not ready:
            return False
        for key in ready:
            resolved.add(key)
            remaining.pop(key)
    return True


def main() -> int:
    path = Path(__file__).with_name("multi_agent_cases.jsonl")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    tag_counts = Counter(tag for case in cases for tag in case["tags"])
    valid_graphs = sum(1 for case in cases if _acyclic(case["task_graph"]))
    valid_handoffs = sum(
        1
        for case in cases
        if all(
            set(task.get("depends_on") or []) <= {node["id"] for node in case["task_graph"]}
            for task in case["task_graph"]
        )
    )
    zero_tool_escalation = all(
        case.get("expected", {}).get("unauthorized_tool_executions", 0) == 0 for case in cases
    )
    terminated = sum(1 for case in cases if case.get("expected", {}).get("terminal_state"))
    metrics = {
        "cases": len(cases),
        "required_tag_coverage": sorted(REQUIRED_TAGS & set(tag_counts)),
        "missing_required_tags": sorted(REQUIRED_TAGS - set(tag_counts)),
        "average_subtasks": round(sum(len(case["task_graph"]) for case in cases) / len(cases), 2),
        "handoff_structure_valid_rate": valid_handoffs / len(cases),
        "acyclic_task_graph_rate": valid_graphs / len(cases),
        "workflow_terminal_rate": terminated / len(cases),
        "unauthorized_tool_execution_zero": zero_tool_escalation,
        "tracked_metrics": [
            "task_completion_rate",
            "average_subtask_count",
            "handoff_structure_valid_rate",
            "replanning_success_rate",
            "user_acceptance_rate",
            "model_call_count",
            "token_cost",
            "end_to_end_latency_ms",
        ],
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    passed = (
        len(cases) >= 60
        and not metrics["missing_required_tags"]
        and metrics["handoff_structure_valid_rate"] == 1
        and metrics["acyclic_task_graph_rate"] == 1
        and metrics["workflow_terminal_rate"] == 1
        and zero_tool_escalation
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
