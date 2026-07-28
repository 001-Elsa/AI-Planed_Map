"""Run the committed intent set and print measured (not invented) metrics."""
import asyncio
import json
from pathlib import Path

from backend.app.services.intent_parser import RuleBasedIntentParser


async def main() -> None:
    path = Path(__file__).with_name("cases.jsonl")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    parser = RuleBasedIntentParser()
    valid = task_correct = mode_correct = deadline_correct = 0
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
        deadline_correct += expected_hour is None and not actual_hours or expected_hour in actual_hours
    total = len(cases)
    print(json.dumps({
        "cases": total,
        "structured_output_valid_rate": valid / total,
        "task_count_accuracy": task_correct / total,
        "transport_mode_accuracy": mode_correct / total,
        "deadline_accuracy": deadline_correct / total,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

