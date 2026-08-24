"""Deterministic Agent route evaluation shared by CI and the runtime Critic."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from backend.app.schemas.common import StrictModel

HIKING_TERMS = (
    "爬山",
    "登山",
    "徒步",
    "山峰",
    "山岳",
    "登山步道",
    "hiking",
    "mountain trail",
)


class ExpectedRoutePreferences(StrictModel):
    avoid_hiking: bool | None = None
    travel_style: Literal["balanced", "relaxed", "intensive"] | None = None
    minimize_walking: bool | None = None
    prefer_high_rating: bool | None = None


class RouteEvaluationPolicy(StrictModel):
    max_reasonable_distance_meters: float | None = Field(default=None, gt=0)
    max_reasonable_travel_seconds: float | None = Field(default=None, gt=0)
    hard_time_limit_seconds: float | None = Field(default=None, gt=0)
    relaxed_max_stops: int = Field(default=4, ge=1, le=20)
    relaxed_max_walking_meters: float = Field(default=6_000, gt=0)
    relaxed_max_travel_seconds: float = Field(default=14_400, gt=0)
    min_preferred_rating: float = Field(default=4.2, ge=0, le=5)
    pass_score: float = Field(default=75, ge=0, le=100)
    expected_preferences: ExpectedRoutePreferences = Field(
        default_factory=ExpectedRoutePreferences
    )


class RouteEvaluationFinding(StrictModel):
    code: str
    severity: Literal["info", "warning", "blocking"]
    message: str


class AgentRouteEvaluation(StrictModel):
    distance_score: float = Field(ge=0, le=100)
    time_score: float = Field(ge=0, le=100)
    preference_score: float = Field(ge=0, le=100)
    final_score: float = Field(ge=0, le=100)
    passed: bool
    hard_failures: list[str] = Field(default_factory=list)
    findings: list[RouteEvaluationFinding] = Field(default_factory=list)
    weights: dict[str, float] = Field(
        default_factory=lambda: {"distance": 0.4, "time": 0.3, "preference": 0.3}
    )

    @model_validator(mode="after")
    def validate_weight_contract(self) -> AgentRouteEvaluation:
        if abs(sum(self.weights.values()) - 1) > 1e-9:
            raise ValueError("route evaluation weights must sum to 1")
        if self.hard_failures and self.final_score != 0:
            raise ValueError("hard failures must zero the final score")
        return self


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _after(left: datetime, right: datetime) -> bool | None:
    try:
        return left > right
    except TypeError:
        return None


def _bounded_score(actual: float, limit: float | None) -> float:
    if limit is None or actual <= limit:
        return 100.0
    if actual >= limit * 2:
        return 0.0
    return max(0.0, 100.0 * (2 - actual / limit))


def _route_contains_hiking(stops: list[dict[str, Any]]) -> bool:
    for stop in stops:
        poi = stop.get("poi") or {}
        task = stop.get("task") or {}
        text = " ".join(
            str(value or "")
            for value in (
                poi.get("name"),
                poi.get("address"),
                task.get("description"),
                task.get("location_name"),
                task.get("category"),
            )
        ).casefold()
        if any(term in text for term in HIKING_TERMS):
            return True
    return False


def _expected_preferences_from_plan(plan: dict[str, Any]) -> ExpectedRoutePreferences:
    preferences = ((plan.get("intent") or {}).get("preferences") or {})
    return ExpectedRoutePreferences(
        avoid_hiking=True if preferences.get("avoid_hiking") else None,
        travel_style=(
            preferences.get("travel_style")
            if preferences.get("travel_style") in {"relaxed", "intensive"}
            else None
        ),
        minimize_walking=True if preferences.get("minimize_walking") else None,
        prefer_high_rating=True if preferences.get("prefer_high_rating") else None,
    )


def runtime_route_policy(plan: dict[str, Any]) -> RouteEvaluationPolicy:
    return RouteEvaluationPolicy(expected_preferences=_expected_preferences_from_plan(plan))


def evaluate_route_plan(
    plan: dict[str, Any], policy: RouteEvaluationPolicy | None = None
) -> AgentRouteEvaluation:
    policy = policy or runtime_route_policy(plan)
    stops = [item for item in plan.get("stops") or [] if isinstance(item, dict)]
    intent = plan.get("intent") or {}
    preferences = intent.get("preferences") or {}
    hard = (intent.get("constraints") or {}).get("hard") or {}
    total_distance = _number(plan.get("total_distance_meters"))
    total_seconds = _number(plan.get("total_travel_seconds"))
    findings: list[RouteEvaluationFinding] = []
    hard_failures: list[str] = []

    def finding(code: str, severity: Literal["info", "warning", "blocking"], message: str) -> None:
        findings.append(RouteEvaluationFinding(code=code, severity=severity, message=message))
        if severity == "blocking" and code not in hard_failures:
            hard_failures.append(code)

    if plan.get("status") != "success":
        finding("plan_not_successful", "blocking", "方案不是可执行的成功状态")
    if not stops:
        finding("empty_route", "blocking", "路线没有可验证站点")

    poi_ids = [str((stop.get("poi") or {}).get("id") or "") for stop in stops]
    if any(not poi_id for poi_id in poi_ids):
        finding("poi_id_missing", "blocking", "路线包含缺少 Provider ID 的地点")
    present_ids = [poi_id for poi_id in poi_ids if poi_id]
    if len(present_ids) != len(set(present_ids)):
        finding("duplicate_poi", "blocking", "路线包含重复景点")

    for index, stop in enumerate(stops):
        if stop.get("constraint_satisfied") is False:
            finding(
                "stop_constraint_violated",
                "blocking",
                f"第 {index + 1} 站标记为违反约束",
            )
        arrival = _datetime(stop.get("arrival_time"))
        deadline = _datetime((stop.get("task") or {}).get("deadline"))
        if arrival is not None and deadline is not None:
            comparison = _after(arrival, deadline)
            if comparison is None:
                finding("time_evidence_invalid", "blocking", "路线时间证据的时区不一致")
            elif comparison:
                finding(
                    "task_deadline_exceeded", "blocking", f"第 {index + 1} 站超过截止时间"
                )

    latest_return = _datetime(hard.get("latest_return_time"))
    last_departure = _datetime(stops[-1].get("departure_time")) if stops else None
    if latest_return is not None and last_departure is not None:
        comparison = _after(last_departure, latest_return)
        if comparison is None:
            finding("time_evidence_invalid", "blocking", "最晚返回时间的时区证据不一致")
        elif comparison:
            finding("latest_return_exceeded", "blocking", "路线超过最晚返回时间")
    max_duration = hard.get("max_total_duration_minutes")
    if max_duration is not None and total_seconds > _number(max_duration) * 60:
        finding("duration_constraint_exceeded", "blocking", "路线超过总时长硬限制")
    if policy.hard_time_limit_seconds is not None and total_seconds > policy.hard_time_limit_seconds:
        finding("time_limit_exceeded", "blocking", "路线超过评测时间上限")
    max_cost = hard.get("max_total_cost_yuan")
    if max_cost is not None and _number(plan.get("estimated_cost_yuan")) > _number(max_cost):
        finding("cost_constraint_exceeded", "blocking", "路线超过总费用硬限制")

    mode = intent.get("transport_mode")
    walking_meters = sum(
        _number((stop.get("travel") or {}).get("distance_meters"))
        for stop in stops
        if mode == "walking" or (stop.get("travel") or {}).get("mode") == "walking"
    )
    max_walking = hard.get("max_walking_meters")
    if max_walking is not None and walking_meters > _number(max_walking):
        finding("walking_constraint_exceeded", "blocking", "路线超过步行距离硬限制")

    distance_score = _bounded_score(total_distance, policy.max_reasonable_distance_meters)
    time_score = _bounded_score(total_seconds, policy.max_reasonable_travel_seconds)
    if (
        policy.max_reasonable_distance_meters is not None
        and total_distance >= policy.max_reasonable_distance_meters * 2
    ):
        finding("distance_excessive", "blocking", "路线距离达到合理上限的两倍")
    elif distance_score < 100:
        finding("distance_above_target", "warning", "路线距离超过评测目标")

    expected = policy.expected_preferences
    preference_checks: list[bool] = []
    if expected.avoid_hiking is not None:
        preference_checks.append(
            bool(preferences.get("avoid_hiking")) == expected.avoid_hiking
            and (not expected.avoid_hiking or not _route_contains_hiking(stops))
        )
    if expected.travel_style is not None:
        style_ok = preferences.get("travel_style") == expected.travel_style
        if expected.travel_style == "relaxed":
            style_ok = (
                style_ok
                and len(stops) <= policy.relaxed_max_stops
                and walking_meters <= policy.relaxed_max_walking_meters
                and total_seconds <= policy.relaxed_max_travel_seconds
            )
        preference_checks.append(style_ok)
    if expected.minimize_walking is not None:
        preference_checks.append(
            bool(preferences.get("minimize_walking")) == expected.minimize_walking
            and (
                not expected.minimize_walking
                or walking_meters <= policy.relaxed_max_walking_meters
            )
        )
    if expected.prefer_high_rating is not None:
        ratings = [
            _number((stop.get("poi") or {}).get("rating"))
            for stop in stops
            if (stop.get("poi") or {}).get("rating") is not None
        ]
        preference_checks.append(
            bool(preferences.get("prefer_high_rating")) == expected.prefer_high_rating
            and (
                not expected.prefer_high_rating
                or bool(ratings)
                and sum(ratings) / len(ratings) >= policy.min_preferred_rating
            )
        )
    # A route claiming several user preferences must satisfy all of them; one
    # missed explicit preference cannot be averaged away by easier matches.
    preference_score = 100.0 if not preference_checks or all(preference_checks) else 0.0
    if preference_score < 100:
        finding("preference_mismatch", "warning", "路线行为未完全符合用户软偏好")

    weighted_score = distance_score * 0.4 + time_score * 0.3 + preference_score * 0.3
    final_score = 0.0 if hard_failures else round(weighted_score, 2)
    return AgentRouteEvaluation(
        distance_score=round(distance_score, 2),
        time_score=round(time_score, 2),
        preference_score=round(preference_score, 2),
        final_score=final_score,
        passed=not hard_failures and final_score >= policy.pass_score,
        hard_failures=hard_failures,
        findings=findings,
    )
