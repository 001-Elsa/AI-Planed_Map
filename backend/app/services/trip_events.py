from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.app.schemas.companion import TripEventType, TripState


@dataclass(frozen=True)
class EventDecision:
    next_state: TripState
    impact_level: str
    should_notify: bool
    reason: str
    proposals: list[dict[str, Any]]


def evaluate_trip_event(
    current: TripState,
    event_type: TripEventType,
    payload: dict[str, Any],
    last_notification_at: datetime | None,
    cooldown_minutes: int,
    now: datetime | None = None,
) -> EventDecision:
    clock = now or datetime.now(timezone.utc)
    cooldown_active = bool(
        last_notification_at
        and clock
        - (
            last_notification_at
            if last_notification_at.tzinfo
            else last_notification_at.replace(tzinfo=timezone.utc)
        )
        < timedelta(minutes=cooldown_minutes)
    )
    impact = "none"
    next_state = current
    reason = "事件不影响当前硬约束"
    proposals: list[dict[str, Any]] = []

    if event_type == TripEventType.user_off_route:
        impact, next_state, reason = "high", TripState.off_route, "检测到用户偏离当前路线"
    elif event_type in {TripEventType.deadline_risk, TripEventType.poi_status_changed}:
        impact, next_state, reason = "critical", TripState.at_risk, "事件可能破坏硬约束"
    elif event_type == TripEventType.schedule_delay:
        delay = max(0, int(payload.get("delay_minutes") or 0))
        if delay >= 10:
            impact, next_state = ("critical" if delay >= 30 else "high"), TripState.at_risk
            reason = f"当前行程延误 {delay} 分钟，需要重新验证剩余计划"
    elif event_type == TripEventType.traffic_changed:
        delay = max(0, int(payload.get("extra_minutes") or 0))
        if delay >= 10:
            impact, next_state, reason = "high", TripState.at_risk, f"路况增加约 {delay} 分钟"
    elif event_type == TripEventType.weather_alert:
        severity = str(payload.get("severity") or "low").lower()
        if severity in {"high", "severe", "extreme"}:
            impact, next_state, reason = "high", TripState.at_risk, "天气预警影响户外行程"
    elif event_type == TripEventType.user_paused:
        next_state, reason = TripState.paused, "用户暂停行程"
    elif event_type == TripEventType.user_resumed:
        next_state, reason = TripState.active_trip, "用户恢复行程"
    elif event_type == TripEventType.trip_completed:
        next_state, reason = TripState.completed, "行程已完成"

    if impact in {"high", "critical"}:
        proposals = [
            {
                "action": "recalculate_remaining_plan",
                "reason": reason,
                "requires_confirmation": False,
            },
            {
                "action": "create_plan_patch",
                "reason": "如果重算发现不可行，只生成待确认补丁，不覆盖正式计划",
                "requires_confirmation": True,
            },
        ]
    should_notify = impact == "critical" or (impact == "high" and not cooldown_active)
    return EventDecision(next_state, impact, should_notify, reason, proposals)
