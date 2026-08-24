"""Privacy-preserving short/long-term memory helpers.

Long-term memory contains only explicitly confirmed, schema-checked soft
preferences. The API layer loads it and injects normalized defaults; no Agent
receives database access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.observability import metrics
from backend.app.models import UserPreference
from backend.app.schemas.ai_intent import AIPlanRequest

BOOLEAN_KEYS = frozenset(
    {
        "minimize_distance",
        "minimize_walking",
        "minimize_cost",
        "prefer_high_rating",
        "avoid_queues",
        "avoid_hiking",
    }
)
LIST_KEYS = frozenset({"dietary_restrictions", "preferred_categories"})
ENVIRONMENT_VALUES = frozenset({"quiet", "uncrowded", "indoor", "outdoor"})
SUPPORTED_LONG_TERM_KEYS = BOOLEAN_KEYS | LIST_KEYS | frozenset(
    {"optimization_goal", "preferred_environment", "travel_style"}
)
OPTIMIZATION_GOALS = frozenset({"balanced", "shortest_time", "shortest_distance"})
TRAVEL_STYLES = frozenset({"balanced", "relaxed", "intensive"})

RELATED_TEXT_CUES: dict[str, tuple[str, ...]] = {
    "minimize_distance": ("距离", "路程", "离得近", "distance"),
    "minimize_walking": ("走路", "步行", "少走", "多走", "walking", "walk"),
    "minimize_cost": ("省钱", "费用", "预算", "便宜", "cost", "budget"),
    "prefer_high_rating": ("评分", "高分", "口碑", "rating"),
    "optimization_goal": ("最快", "最短时间", "最短距离", "shortest", "fastest"),
    "dietary_restrictions": ("忌口", "素食", "清真", "过敏", "不吃", "diet", "allergy"),
    "avoid_queues": ("排队", "人少", "小众", "queue", "crowd"),
    "avoid_hiking": ("爬山", "登山", "徒步", "hiking", "mountain"),
    "travel_style": ("轻松", "悠闲", "紧凑", "特种兵", "慢节奏", "relaxed", "intensive"),
    "preferred_environment": ("安静", "热闹", "室内", "户外", "quiet", "indoor", "outdoor"),
    "preferred_categories": (
        "博物馆",
        "美术馆",
        "公园",
        "景区",
        "餐厅",
        "咖啡",
        "购物",
        "古镇",
        "建筑",
        "动物园",
        "museum",
        "park",
        "restaurant",
    ),
}
GENERIC_DISCOVERY_CUES = (
    "旅游",
    "旅行",
    "游玩",
    "逛逛",
    "景点推荐",
    "行程",
    "去哪",
    "travel",
    "trip",
    "things to do",
)


class MemoryPreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class MemoryApplication:
    enabled: bool
    source: str
    applied_keys: tuple[str, ...]
    skipped_explicit_keys: tuple[str, ...]
    ignored_invalid_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "source": self.source,
            "applied_keys": list(self.applied_keys),
            "skipped_explicit_keys": list(self.skipped_explicit_keys),
            "ignored_invalid_count": self.ignored_invalid_count,
            "values_included": False,
        }


def _normalized_string_list(value: Any, *, allowed: frozenset[str] | None = None) -> list[str]:
    if not isinstance(value, list):
        raise MemoryPreferenceError("preference value must be a list")
    if len(value) > 10:
        raise MemoryPreferenceError("preference list is too long")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MemoryPreferenceError("preference list items must be strings")
        text = item.strip().lower()
        if not text or len(text) > 50:
            raise MemoryPreferenceError("preference list item is empty or too long")
        if allowed is not None and text not in allowed:
            raise MemoryPreferenceError("preference list contains an unsupported value")
        if text not in normalized:
            normalized.append(text)
    return normalized


def normalize_long_term_preference(key: str, value: Any) -> Any:
    if key not in SUPPORTED_LONG_TERM_KEYS:
        raise MemoryPreferenceError("unsupported long-term preference key")
    if key in BOOLEAN_KEYS:
        if not isinstance(value, bool):
            raise MemoryPreferenceError("preference value must be boolean")
        return value
    if key in LIST_KEYS:
        return _normalized_string_list(value)
    if key == "preferred_environment":
        return _normalized_string_list(value, allowed=ENVIRONMENT_VALUES)
    if key == "optimization_goal":
        if not isinstance(value, str) or value not in OPTIMIZATION_GOALS:
            raise MemoryPreferenceError("unsupported optimization goal")
        return value
    if key == "travel_style":
        if not isinstance(value, str) or value not in TRAVEL_STYLES:
            raise MemoryPreferenceError("unsupported travel style")
        return value
    raise MemoryPreferenceError("unsupported long-term preference key")


async def load_confirmed_preferences(
    db: AsyncSession, user_id: int
) -> tuple[dict[str, Any], tuple[str, ...]]:
    rows = (
        await db.scalars(
            select(UserPreference)
            .where(UserPreference.user_id == user_id)
            .order_by(UserPreference.key)
        )
    ).all()
    values: dict[str, Any] = {}
    ignored: list[str] = []
    for row in rows:
        try:
            raw = json.loads(row.value_json)
            values[row.key] = normalize_long_term_preference(row.key, raw)
        except (json.JSONDecodeError, MemoryPreferenceError):
            ignored.append(row.key)
            metrics.increment(
                "mapgo_long_term_memory_ignored_total", {"reason": "invalid_or_unsupported"}
            )
    return values, tuple(ignored)


def apply_long_term_preferences(
    request: AIPlanRequest,
    preferences: dict[str, Any],
    *,
    ignored_invalid_keys: tuple[str, ...] = (),
) -> tuple[AIPlanRequest, MemoryApplication]:
    if not request.use_long_term_memory:
        return request, MemoryApplication(
            enabled=False,
            source="disabled_by_user",
            applied_keys=(),
            skipped_explicit_keys=(),
            ignored_invalid_count=len(ignored_invalid_keys),
        )
    answers = dict(request.preferences_answers)
    lowered_text = request.text.casefold()
    generic_discovery = any(cue in lowered_text for cue in GENERIC_DISCOVERY_CUES)
    applied: list[str] = []
    skipped: list[str] = []
    for key, value in preferences.items():
        has_related_text = any(cue in lowered_text for cue in RELATED_TEXT_CUES.get(key, ()))
        discovery_only = key in {"preferred_categories", "preferred_environment", "avoid_queues"}
        if key in answers or has_related_text or (discovery_only and not generic_discovery):
            skipped.append(key)
            continue
        answers[key] = value
        applied.append(key)
    metrics.increment(
        "mapgo_long_term_memory_applications_total",
        {"result": "applied" if applied else "no_defaults"},
    )
    effective = request.model_copy(update={"preferences_answers": answers})
    return effective, MemoryApplication(
        enabled=True,
        source="postgresql_explicit_confirmation",
        applied_keys=tuple(applied),
        skipped_explicit_keys=tuple(skipped),
        ignored_invalid_count=len(ignored_invalid_keys),
    )
