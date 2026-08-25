"""Strongly typed artifacts for the in-trip event-driven replanning loop."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from backend.app.schemas.ai_intent import Coordinate, PlanPatchOperation
from backend.app.schemas.common import StrictModel


class TripEventArtifact(StrictModel):
    trip_id: int = Field(gt=0)
    event_id: int | None = Field(default=None, gt=0)
    event_type: str = Field(min_length=1, max_length=60)
    occurred_at: datetime
    impact_level: Literal["none", "low", "medium", "high", "critical"]
    reason: str = Field(min_length=1, max_length=500)
    payload_summary: dict[str, Any] = Field(default_factory=dict)
    base_plan_version: int = Field(ge=1)


class ReplanDirective(StrictModel):
    base_plan_version: int = Field(ge=1)
    current_location: Coordinate
    current_time: datetime
    completed_stop_ids: list[str] = Field(default_factory=list, max_length=20)
    event_type: str = Field(min_length=1, max_length=60)
    reason: str = Field(min_length=1, max_length=500)
    event_payload: dict[str, Any] = Field(default_factory=dict)
    weather: dict[str, Any] | None = None
    strategy: Literal[
        "reorder_remaining",
        "replace_closed_poi",
        "weather_indoor_fallback",
        "fastest_feasible_route",
        "off_route_recovery",
    ]


class DynamicPatchReview(StrictModel):
    verdict: Literal["approved", "approved_with_warnings", "blocked"]
    risk_level: Literal["low", "medium", "high", "critical"]
    requires_confirmation: bool
    findings: list[str] = Field(default_factory=list, max_length=30)
    checked_base_version: int = Field(ge=1)
    operation_count: int = Field(ge=0, le=20)
    confidence: float = Field(default=1, ge=0, le=1)


class PlanPatchArtifact(StrictModel):
    patch_id: int | None = Field(default=None, gt=0)
    base_version: int = Field(ge=1)
    operations: list[PlanPatchOperation] = Field(default_factory=list, max_length=20)
    impact: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(min_length=1, max_length=40)
    review: DynamicPatchReview | None = None

    @model_validator(mode="after")
    def validate_patch_identity(self) -> PlanPatchArtifact:
        if self.status == "patch_pending_confirmation" and self.patch_id is None:
            raise ValueError("a pending patch requires a persisted patch id")
        return self
