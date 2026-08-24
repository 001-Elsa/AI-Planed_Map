"""Versioned shared task state used across isolated Agent roles."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from backend.app.schemas.agent_artifacts import (
    AgentType,
    CriticSoftAdjustments,
    ReviewReport,
)
from backend.app.schemas.ai_intent import PlanningIntent, PoiCandidate
from backend.app.schemas.common import StrictModel


class AgentSharedStatePhase(str, Enum):
    initialized = "initialized"
    intent_ready = "intent_ready"
    search_ready = "search_ready"
    safety_ready = "safety_ready"
    plan_ready = "plan_ready"
    reviewed = "reviewed"
    retrying = "retrying"
    finalized = "finalized"
    in_trip = "in_trip"
    completed = "completed"
    failed = "failed"


class AgentSharedStateEvent(StrictModel):
    revision: int = Field(ge=0)
    actor: AgentType
    action: str = Field(min_length=1, max_length=80)
    changed_fields: list[str] = Field(default_factory=list, max_length=20)
    message_id: UUID | None = None
    change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentSharedState(StrictModel):
    schema_version: str = "1.0"
    task_id: str = Field(min_length=8, max_length=128)
    revision: int = Field(default=0, ge=0)
    state_hash: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    phase: AgentSharedStatePhase = AgentSharedStatePhase.initialized
    user_requirement: PlanningIntent | None = None
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    poi_candidates: list[list[PoiCandidate]] = Field(default_factory=list, max_length=24)
    route_plan: dict[str, Any] | None = None
    evaluation_result: ReviewReport | None = None
    soft_adjustments: CriticSoftAdjustments | None = None
    execution_context: dict[str, Any] = Field(default_factory=dict)
    execution_history: list[AgentSharedStateEvent] = Field(default_factory=list, max_length=200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    @model_validator(mode="after")
    def validate_candidate_groups(self) -> AgentSharedState:
        if any(len(group) > 5 for group in self.poi_candidates):
            raise ValueError("shared-state candidate groups are limited to five POIs")
        return self


class AgentSharedStateView(StrictModel):
    task_id: str
    revision: int
    state_hash: str
    phase: AgentSharedStatePhase
    visible_fields: list[str]
    user_requirement: PlanningIntent | None = None
    clarification_questions: list[dict[str, Any]] | None = None
    poi_candidates: list[list[PoiCandidate]] | None = None
    route_plan: dict[str, Any] | None = None
    evaluation_result: ReviewReport | None = None
    soft_adjustments: CriticSoftAdjustments | None = None
    execution_context: dict[str, Any] | None = None
    execution_history: list[AgentSharedStateEvent] | None = None


class AgentSharedStateAudit(StrictModel):
    task_id: str
    revision: int
    phase: AgentSharedStatePhase
    preference_flags: list[str] = Field(default_factory=list)
    candidate_group_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    route_status: str | None = None
    stop_count: int = Field(default=0, ge=0)
    evaluation_verdict: str | None = None
    history_count: int = Field(default=0, ge=0)
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
