"""Score committed route cases and fail CI on evaluator/quality regressions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.app.services.agent_evaluation import (  # noqa: E402
    RouteEvaluationPolicy,
    evaluate_route_plan,
)


def main() -> int:
    path = Path(__file__).with_name("route_cases.jsonl")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    correct = 0
    hard_expected = 0
    hard_detected = 0
    good_scores: list[float] = []
    results = []
    for case in cases:
        report = evaluate_route_plan(
            case["plan"], RouteEvaluationPolicy.model_validate(case["policy"])
        )
        expected_hard = set(case["expected_hard_failures"])
        detected = expected_hard.issubset(report.hard_failures)
        score_ok = report.final_score >= float(
            case.get("min_score", 0)
        ) and report.final_score <= float(case.get("max_score", 100))
        passed = report.passed is case["expected_pass"] and detected and score_ok
        correct += int(passed)
        if expected_hard:
            hard_expected += 1
            hard_detected += int(detected)
        if case["expected_pass"]:
            good_scores.append(report.final_score)
        results.append(
            {
                "id": case["id"],
                "expected_pass": case["expected_pass"],
                "actual_pass": report.passed,
                "score": report.final_score,
                "components": {
                    "distance": report.distance_score,
                    "time": report.time_score,
                    "preference": report.preference_score,
                },
                "hard_failures": report.hard_failures,
                "case_passed": passed,
            }
        )
    metrics = {
        "cases": len(cases),
        "expectation_accuracy": correct / len(cases),
        "hard_failure_detection_rate": hard_detected / hard_expected if hard_expected else 1.0,
        "good_route_min_score": min(good_scores) if good_scores else 0,
        "score_formula": "distance*0.40 + time*0.30 + preference*0.30",
        "hard_failure_score": 0,
        "results": results,
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if (
        metrics["expectation_accuracy"] < 1
        or metrics["hard_failure_detection_rate"] < 1
        or metrics["good_route_min_score"] < 85
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
