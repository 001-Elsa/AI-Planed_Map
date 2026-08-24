"""Run the committed intent set and fail CI when quality gates regress."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.app.services.intent_parser import RuleBasedIntentParser

GATES = {
    "structured_output_valid_rate": 0.98,
    "task_count_accuracy": 0.70,
    "transport_mode_accuracy": 0.80,
    "deadline_accuracy": 0.70,
    "avoid_hiking_accuracy": 0.95,
    "travel_style_accuracy": 0.90,
}


async def main() -> None:
    path = Path(__file__).with_name("cases.jsonl")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    parser = RuleBasedIntentParser()
    valid = task_correct = mode_correct = deadline_correct = 0
    avoid_hiking_correct = travel_style_correct = 0
    avoid_hiking_cases = travel_style_cases = 0
    for case in cases:
        try:
            intent = await parser.parse(case["text"])
            valid += 1
        except Exception:
            continue
        task_correct += len(intent.tasks) == case["expected_task_count"]
        mode_correct += intent.transport_mode.value == case["expected_transport_mode"]
        expected_hour = case["expected_deadline_hour"]
        actual_hours = [task.deadline.hour for task in intent.tasks if task.deadline]
        deadline_correct += (
            expected_hour is None and not actual_hours or expected_hour in actual_hours
        )
        if "expected_avoid_hiking" in case:
            avoid_hiking_cases += 1
            avoid_hiking_correct += (
                intent.preferences.avoid_hiking is case["expected_avoid_hiking"]
            )
        if "expected_travel_style" in case:
            travel_style_cases += 1
            travel_style_correct += (
                intent.preferences.travel_style == case["expected_travel_style"]
            )
    total = len(cases)
    metrics = {
        "cases": total,
        "structured_output_valid_rate": valid / total,
        "task_count_accuracy": task_correct / total,
        "transport_mode_accuracy": mode_correct / total,
        "deadline_accuracy": deadline_correct / total,
        "avoid_hiking_accuracy": (
            avoid_hiking_correct / avoid_hiking_cases if avoid_hiking_cases else 1.0
        ),
        "travel_style_accuracy": (
            travel_style_correct / travel_style_cases if travel_style_cases else 1.0
        ),
        "preference_eval_cases": {
            "avoid_hiking": avoid_hiking_cases,
            "travel_style": travel_style_cases,
        },
        "parser": parser.name,
        "note": "RuleBased offline gates; live LLM eval is optional via LLM_API_KEY",
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    failures = [name for name, threshold in GATES.items() if metrics[name] < threshold]
    if failures:
        raise SystemExit(f"AI eval quality gates failed: {failures}")


if __name__ == "__main__":
    asyncio.run(main())
