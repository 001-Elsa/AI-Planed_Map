"""Dynamic clarification question selection based on plan gaps."""

from __future__ import annotations

from typing import Any

from backend.app.schemas.ai_intent import (
    AIPlanRequest,
    ClarificationQuestion,
    PartyProfile,
    PlanningIntent,
    PoiCandidate,
)

SOFT_PREFERENCE_HINTS = (
    ("别太累", "constraints.hard.max_walking_meters", "你希望把步行上限控制在多少米？"),
    ("离得近", "preferences.minimize_distance", "是否优先选择路程更短的方案？"),
    ("少走路", "preferences.minimize_walking", "是否优先减少步行？"),
    ("省钱", "preferences.minimize_cost", "是否优先控制费用？"),
)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "y", "accept", "accepted", "continue", "ok"}:
            return True
        if normalized in {"false", "0", "no", "n", "reject", "rejected", "stop"}:
            return False
    raise ValueError("expected boolean confirmation answer")


def _ensure_hard_constraints(request_data: dict[str, Any]) -> dict[str, Any]:
    constraints = request_data.get("constraints")
    if not isinstance(constraints, dict):
        constraints = {"hard": {}, "uncertain": []}
        request_data["constraints"] = constraints
    hard = constraints.setdefault("hard", {})
    if not isinstance(hard, dict):
        hard = {}
        constraints["hard"] = hard
    constraints.setdefault("uncertain", [])
    return hard


def select_clarification_questions(
    *,
    request: AIPlanRequest,
    intent: PlanningIntent,
    ambiguous_pois: dict[int, list[PoiCandidate]] | None = None,
    text: str = "",
    max_questions: int = 3,
) -> list[ClarificationQuestion]:
    questions: list[ClarificationQuestion] = []
    hard = intent.constraints.hard

    if not intent.origin and request.origin is None:
        questions.append(
            ClarificationQuestion(
                field="origin",
                reason="缺少起点",
                question="请提供出发地点或当前位置坐标。",
            )
        )
    if intent.departure_time is None and request.departure_time is None:
        questions.append(
            ClarificationQuestion(
                field="departure_time",
                reason="缺少出发时间",
                question="你计划什么时候出发？",
                required=False,
            )
        )

    if ambiguous_pois:
        for task_index, candidates in sorted(ambiguous_pois.items()):
            if len(candidates) < 2:
                continue
            questions.append(
                ClarificationQuestion(
                    field=f"tasks.{task_index}.selected_poi_id",
                    reason="同名或近似地点存在多个候选",
                    question=f"任务「{intent.tasks[task_index].description}」匹配到多个地点，请选择一个。",
                    candidates=candidates[:5],
                )
            )

    party = hard.party or PartyProfile()
    if any(token in text for token in ("老人", "带娃", "儿童", "轮椅", "无障碍")):
        if party.elderly == 0 and "老人" in text:
            questions.append(
                ClarificationQuestion(
                    field="constraints.hard.party.elderly",
                    reason="检测到同行老人语义",
                    question="同行老人有几位？这将影响步行和节奏安排。",
                    required=False,
                )
            )
        if party.children == 0 and any(token in text for token in ("带娃", "儿童", "小孩")):
            questions.append(
                ClarificationQuestion(
                    field="constraints.hard.party.children",
                    reason="检测到同行儿童语义",
                    question="同行儿童有几位？",
                    required=False,
                )
            )
        if not hard.wheelchair_accessible and ("轮椅" in text or "无障碍" in text):
            questions.append(
                ClarificationQuestion(
                    field="constraints.hard.wheelchair_accessible",
                    reason="检测到无障碍需求",
                    question="是否要求全程轮椅可达？",
                )
            )

    if any(token in text for token in ("忌口", "过敏", "不吃", "素食", "清真")):
        questions.append(
            ClarificationQuestion(
                field="preferences.dietary_restrictions",
                reason="检测到餐饮忌口",
                question="请说明餐饮忌口或饮食限制（可多选描述）。",
                required=False,
            )
        )

    if any(token in text for token in ("预约", "订票", "门票")) and not any(
        task.appointment_time for task in intent.tasks
    ):
        questions.append(
            ClarificationQuestion(
                field="tasks.0.appointment_time",
                reason="检测到预约需求但缺少具体时间",
                question="需要预约的地点具体是几点？",
                required=False,
            )
        )

    if any(token in text for token in ("必经", "路过", "避开", "不要经过")):
        if not hard.must_pass_areas and ("必经" in text or "路过" in text):
            questions.append(
                ClarificationQuestion(
                    field="constraints.hard.must_pass_areas",
                    reason="检测到必经区域语义",
                    question="请写出必须经过的区域名称（可多个）。",
                    required=False,
                )
            )
        if not hard.avoid_areas and ("避开" in text or "不要经过" in text):
            questions.append(
                ClarificationQuestion(
                    field="constraints.hard.avoid_areas",
                    reason="检测到避开区域语义",
                    question="请写出希望避开的区域名称（可多个）。",
                    required=False,
                )
            )

    for hint, field, question in SOFT_PREFERENCE_HINTS:
        if hint in text:
            questions.append(
                ClarificationQuestion(
                    field=field,
                    reason=f"检测到模糊偏好「{hint}」",
                    question=question,
                    required=False,
                )
            )

    if hard.max_walking_meters is None and (
        intent.transport_mode.value == "walking" or intent.preferences.minimize_walking
    ):
        questions.append(
            ClarificationQuestion(
                field="constraints.hard.max_walking_meters",
                reason="步行上限缺失，无法验证少走路/步行硬约束",
                question="最长可以接受步行多少米？",
                required=bool(intent.preferences.minimize_walking),
            )
        )
    if hard.max_total_cost_yuan is None and intent.preferences.minimize_cost:
        questions.append(
            ClarificationQuestion(
                field="constraints.hard.max_total_cost_yuan",
                reason="省钱偏好缺少预算上限",
                question="本次行程总预算上限是多少元？",
                required=False,
            )
        )

    # Prefer required gaps first, then optional high-value questions.
    questions.sort(key=lambda item: (not item.required, item.field))
    deduped: list[ClarificationQuestion] = []
    seen: set[str] = set()
    for item in questions:
        if item.field in seen:
            continue
        seen.add(item.field)
        deduped.append(item)
        if len(deduped) >= max_questions:
            break
    return deduped


def apply_clarification_answer(request_data: dict[str, Any], field: str, value: Any) -> None:
    """Mutate a persisted conversation request_json with a structured answer."""
    if field.startswith("human_confirmation."):
        confirmation_key = field.split(".", 1)[1]
        if confirmation_key not in {"walking_distance", "estimated_cost"}:
            raise KeyError(field)
        accepted = _coerce_bool(value)
        confirmations = request_data.setdefault("human_confirmations", {})
        confirmations[confirmation_key] = accepted
        if accepted:
            return
        preferences = request_data.setdefault("preferences_answers", {})
        hard = _ensure_hard_constraints(request_data)
        if confirmation_key == "walking_distance":
            preferences["minimize_walking"] = True
            preferences["travel_style"] = "relaxed"
            hard.setdefault("max_walking_meters", 6000)
            request_data["text"] = f"{request_data.get('text', '')}；用户不接受长距离步行，请少走路"
            return
        if confirmation_key == "estimated_cost":
            preferences["minimize_cost"] = True
            hard.setdefault("max_total_cost_yuan", 800)
            request_data["text"] = f"{request_data.get('text', '')}；用户不接受高费用，请省钱优先"
            return
    if field in {"origin", "departure_time", "transport_mode"}:
        request_data[field] = value
        return
    if field.startswith("tasks.") and field.endswith(".location"):
        parts = field.split(".")
        task_index = int(parts[1])
        overrides = request_data.setdefault("task_location_overrides", {})
        overrides[str(task_index)] = value
        # Keep the original natural-language request stable.  Appending an
        # answer to it can make the parser invent an extra task and shift the
        # task index that this override is meant to target.
        return
    if field.startswith("tasks.") and field.endswith(".selected_poi_id"):
        parts = field.split(".")
        task_index = int(parts[1])
        overrides = request_data.setdefault("task_poi_overrides", {})
        overrides[str(task_index)] = value
        return
    if field.startswith("tasks.") and ".appointment_time" in field:
        parts = field.split(".")
        task_index = int(parts[1])
        task_overrides = request_data.setdefault("task_field_overrides", {})
        bucket = task_overrides.setdefault(str(task_index), {})
        bucket["appointment_time"] = value
        return
    if field.startswith("preferences."):
        pref_field = field.split(".", 1)[1]
        preferences = request_data.setdefault("preferences_answers", {})
        preferences[pref_field] = value
        if pref_field == "minimize_distance" and value:
            request_data["text"] = f"{request_data.get('text', '')}；尽量路程短一点"
        if pref_field == "minimize_walking" and value:
            request_data["text"] = f"{request_data.get('text', '')}；尽量少走路"
        if pref_field == "minimize_cost" and value:
            request_data["text"] = f"{request_data.get('text', '')}；尽量省钱"
        if pref_field == "dietary_restrictions":
            request_data["text"] = f"{request_data.get('text', '')}；餐饮忌口:{value}"
        return
    if field.startswith("constraints.hard.party."):
        party_field = field.rsplit(".", 1)[-1]
        hard = _ensure_hard_constraints(request_data)
        party = hard.setdefault("party", {})
        if not isinstance(party, dict):
            party = {}
            hard["party"] = party
        party[party_field] = value
        return
    if field.startswith("constraints.hard."):
        hard_field = field.partition("constraints.hard.")[2]
        hard = _ensure_hard_constraints(request_data)
        hard[hard_field] = value
        return
    raise KeyError(field)
