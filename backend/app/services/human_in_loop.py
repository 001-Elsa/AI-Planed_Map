"""Human-in-the-loop confirmation gates for high-impact planning decisions."""

from __future__ import annotations

from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    AIPlanResult,
    ClarificationQuestion,
    TransportMode,
)

WALKING_CONFIRMATION_KEY = "walking_distance"
COST_CONFIRMATION_KEY = "estimated_cost"
GENERAL_WALKING_CONFIRMATION_METERS = 8_000
SENSITIVE_WALKING_CONFIRMATION_METERS = 6_000
HIGH_COST_CONFIRMATION_YUAN = 1_000


def select_human_confirmation_questions(
    *,
    request: AIPlanRequest,
    result: AIPlanResult,
    max_questions: int = 2,
) -> list[ClarificationQuestion]:
    """Return confirmation questions for feasible but user-impactful plans.

    The planner should only reach this after deterministic solving and Critic
    review.  These questions intentionally reuse the clarification protocol so
    a rejection can mutate the original request and re-enter the same audited
    planning workflow.
    """

    if result.status != "success":
        return []

    questions: list[ClarificationQuestion] = []
    if _needs_walking_confirmation(request, result):
        threshold = _walking_threshold_meters(result)
        walked_km = result.total_distance_meters / 1000
        questions.append(
            ClarificationQuestion(
                field=f"human_confirmation.{WALKING_CONFIRMATION_KEY}",
                kind="confirmation",
                reason=(
                    f"预计步行约 {walked_km:.1f} 公里，超过人工确认阈值 {threshold / 1000:.1f} 公里"
                ),
                question=(
                    f"当前路线预计步行约 {walked_km:.1f} 公里，是否继续？"
                    "选择不接受会改为少步行，并自动收紧步行上限后重新规划。"
                ),
            )
        )

    total_cost = _estimated_plan_cost_yuan(result)
    if (
        total_cost >= HIGH_COST_CONFIRMATION_YUAN
        and result.intent.constraints.hard.max_total_cost_yuan is None
        and request.human_confirmations.get(COST_CONFIRMATION_KEY) is not True
    ):
        questions.append(
            ClarificationQuestion(
                field=f"human_confirmation.{COST_CONFIRMATION_KEY}",
                kind="confirmation",
                reason=(
                    f"候选 POI 预计费用约 {total_cost:.0f} 元，超过人工确认阈值 "
                    f"{HIGH_COST_CONFIRMATION_YUAN} 元"
                ),
                question=(
                    f"当前方案预计门票/服务费用约 {total_cost:.0f} 元，是否继续？"
                    "选择不接受会改为省钱优先，并设置总预算上限后重新规划。"
                ),
            )
        )

    return questions[:max_questions]


def _needs_walking_confirmation(request: AIPlanRequest, result: AIPlanResult) -> bool:
    if result.intent.transport_mode != TransportMode.walking:
        return False
    if result.intent.constraints.hard.max_walking_meters is not None:
        return False
    if request.human_confirmations.get(WALKING_CONFIRMATION_KEY) is True:
        return False
    return result.total_distance_meters >= _walking_threshold_meters(result)


def _walking_threshold_meters(result: AIPlanResult) -> int:
    party = result.intent.constraints.hard.party
    preferences = result.intent.preferences
    sensitive = (
        party.elderly > 0
        or party.children > 0
        or party.wheelchair_users > 0
        or result.intent.constraints.hard.wheelchair_accessible
        or preferences.minimize_walking
        or preferences.travel_style == "relaxed"
    )
    return (
        SENSITIVE_WALKING_CONFIRMATION_METERS if sensitive else GENERAL_WALKING_CONFIRMATION_METERS
    )


def _estimated_plan_cost_yuan(result: AIPlanResult) -> float:
    return sum(stop.poi.estimated_cost_yuan or 0 for stop in result.stops)
