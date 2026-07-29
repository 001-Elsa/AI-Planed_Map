"""In-app notification service with dedupe, retry metadata and delivery status."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from backend.app.infrastructure.runtime_store import RuntimeStore

NOTIFICATION_QUEUE = "mapgo:notifications"


def notification_fingerprint(
    trip_id: int,
    channel: str,
    event_type: str,
    template_key: str,
) -> str:
    raw = f"{trip_id}:{channel}:{event_type}:{template_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class NotificationService:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    async def enqueue(
        self,
        *,
        trip_id: int,
        user_id: int,
        channel: str,
        event_type: str,
        title: str,
        body: str,
        payload: dict[str, Any] | None = None,
        template_key: str | None = None,
    ) -> dict[str, Any]:
        key = template_key or event_type
        fingerprint = notification_fingerprint(trip_id, channel, event_type, key)
        dedupe_key = f"notify:dedupe:{fingerprint}"
        existing = await self.store.get_json(dedupe_key)
        if existing:
            return {"deduplicated": True, "notification": existing}

        notification = {
            "id": fingerprint,
            "trip_id": trip_id,
            "user_id": user_id,
            "channel": channel,
            "event_type": event_type,
            "title": title,
            "body": body,
            "payload": payload or {},
            "status": "queued",
            "attempts": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.store.set_json(dedupe_key, notification, 3_600)
        await self.store.set_json(
            f"notify:status:{fingerprint}",
            notification,
            86_400,
        )
        await self.store.enqueue(NOTIFICATION_QUEUE, notification)
        # Mirror into trip stream for SSE / in-app cards.
        await self.store.publish(
            f"trip-stream:{trip_id}",
            {
                "sequence": int(datetime.now(timezone.utc).timestamp() * 1000),
                "type": "NotificationQueued",
                "event_id": fingerprint,
                "trip_id": trip_id,
                "notification": notification,
            },
        )
        return {"deduplicated": False, "notification": notification}

    async def mark_delivered(self, notification_id: str, channel_result: dict[str, Any]) -> None:
        current = await self.store.get_json(f"notify:status:{notification_id}") or {}
        current.update(
            {
                "status": "delivered",
                "delivered_at": datetime.now(timezone.utc).isoformat(),
                "channel_result": channel_result,
            }
        )
        await self.store.set_json(f"notify:status:{notification_id}", current, 86_400)

    async def mark_failed(self, notification: dict[str, Any], error: str) -> str:
        attempt = int(notification.get("attempts") or 0) + 1
        notification = dict(notification)
        notification["attempts"] = attempt
        notification["status"] = "failed"
        notification["last_error"] = error
        await self.store.set_json(
            f"notify:status:{notification['id']}",
            notification,
            86_400,
        )
        return await self.store.enqueue_retry(
            NOTIFICATION_QUEUE,
            notification,
            attempt=attempt,
            max_attempts=5,
        )


def render_event_notification(event_type: str, decision: dict[str, Any]) -> tuple[str, str]:
    reason = str(decision.get("reason") or "行程状态发生变化")
    titles = {
        "DeadlineRisk": "截止时间风险提醒",
        "ScheduleDelay": "行程延误提醒",
        "TrafficChanged": "路况变化提醒",
        "WeatherAlert": "天气变化提醒",
        "UserOffRoute": "偏航提醒",
        "PoiStatusChanged": "地点状态变化",
    }
    return titles.get(event_type, "行程提醒"), reason
