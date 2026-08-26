"""Typed, versioned hand-offs used by the isolated Agent workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from backend.app.schemas.common import StrictModel

REDACTED_COORDINATE = {
    "redacted": "coordinate",
    "reason": "agent_artifact_minimized",
}
REDACTED_VALUE = "[REDACTED]"
REDACTED_TEXT = "[REDACTED_TEXT]"
MAX_AGENT_ARTIFACT_STRING_LENGTH = 800

SECRET_KEYS = frozenset(
    {
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "api_key",
        "amap_key",
        "amap_jscode",
        "jscode",
        "location_encryption_key",
    }
)
RAW_TEXT_KEYS = frozenset({"text", "input_text", "raw_text", "user_text"})


def _looks_like_coordinate(value: dict[str, Any]) -> bool:
    return {"lng", "lat"}.issubset(value) or {"longitude", "latitude"}.issubset(value)


def minimize_agent_payload(value: Any) -> Any:
    """Remove secrets and precise coordinates from persisted Agent audit payloads."""
    if isinstance(value, dict):
        if _looks_like_coordinate(value):
            return dict(REDACTED_COORDINATE)
        minimized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in SECRET_KEYS or normalized.endswith(("_token", "_secret", "_key")):
                minimized[key] = REDACTED_VALUE
            elif normalized in RAW_TEXT_KEYS:
                minimized[key] = REDACTED_TEXT
            else:
                minimized[key] = minimize_agent_payload(item)
        return minimized
    if isinstance(value, list):
        return [minimize_agent_payload(item) for item in value]
    if isinstance(value, str) and len(value) > MAX_AGENT_ARTIFACT_STRING_LENGTH:
        return f"{value[:MAX_AGENT_ARTIFACT_STRING_LENGTH]}...[truncated]"
    return value


class AgentType(str, Enum):
    supervisor = "supervisor"
    intent = "intent"
    search = "search"
    safety = "safety"
    planner = "planner"
    critic = "critic"
    companion = "companion"
    replanner = "replanner"


class AgentEndpoint(str, Enum):
    """Addressable protocol endpoints; services are explicit, never implicit roles."""

    user = "user"
    system = "system"
    supervisor = "supervisor"
    intent = "intent"
    search = "search"
    safety = "safety"
    planner = "planner"
    critic = "critic"
    companion = "companion"
    replanner = "replanner"
    tool_runtime = "tool_runtime"
    final_answer = "final_answer"


class AgentMessageType(str, Enum):
    command = "command"
    event = "event"
    artifact = "artifact"
    result = "result"
    error = "error"
    tool_request = "tool_request"
    tool_result = "tool_result"


class AgentMessage(StrictModel):
    """Runtime Agent communication envelope.

    `content` stays structured and is never persisted without minimization. The
    hashes and causal identifiers make delivery auditable and idempotent.
    """

    protocol_version: Literal["1.0"] = "1.0"
    message_id: UUID
    task_id: str = Field(min_length=8, max_length=128)
    sender: AgentEndpoint
    receiver: AgentEndpoint
    message_type: AgentMessageType
    artifact_type: str = Field(min_length=1, max_length=80)
    content: dict[str, Any]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=16, max_length=128)
    correlation_id: UUID
    causation_id: UUID | None = None
    attempt: int = Field(default=1, ge=1, le=5)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_envelope(self) -> AgentMessage:
        if self.sender == self.receiver:
            raise ValueError("sender and receiver must differ")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class AgentMessageAudit(StrictModel):
    """Minimized delivery record safe for API traces and durable audit logs."""

    protocol_version: Literal["1.0"] = "1.0"
    message_id: UUID
    task_id: str
    sender: AgentEndpoint
    receiver: AgentEndpoint
    message_type: AgentMessageType
    artifact_type: str
    content_summary: dict[str, Any]
    content_hash: str
    idempotency_key: str
    correlation_id: UUID
    causation_id: UUID | None = None
    attempt: int = 1
    delivery_status: Literal["delivered", "duplicate", "rejected"] = "delivered"
    created_at: datetime


class AgentWorkflowMode(str, Enum):
    off = "off"
    shadow = "shadow"
    enforce = "enforce"


class AgentRecoveryDecision(StrictModel):
    stage: str = Field(min_length=1, max_length=80)
    action: Literal["retry", "fallback_cached", "fallback_unavailable", "clarify", "fail"]
    attempt: int = Field(ge=1, le=10)
    max_attempts: int = Field(ge=1, le=10)
    error_type: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)
    timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    fallback_source: str | None = Field(default=None, max_length=80)


class AgentBudget(StrictModel):
    max_steps: int = Field(default=1, ge=1, le=20)
    max_input_tokens: int = Field(default=4_000, ge=0)
    max_output_tokens: int = Field(default=800, ge=0)
    max_cost_usd: float = Field(default=0.03, ge=0)
    timeout_seconds: float = Field(default=10, gt=0, le=120)


class AgentPlanStep(StrictModel):
    step_id: str = Field(min_length=1, max_length=80)
    agent_type: AgentType
    responsibility: str = Field(min_length=1, max_length=120)
    status: Literal["pending", "running", "succeeded", "failed", "skipped"] = "pending"
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    input_artifact_type: str = Field(min_length=1, max_length=80)
    output_artifact_type: str = Field(min_length=1, max_length=80)
    input_artifact_refs: list[str] = Field(default_factory=list, max_length=20)
    output_schema_ref: str | None = Field(default=None, max_length=120)
    budget: AgentBudget | None = None
    attempt_count: int = Field(default=0, ge=0, le=10)
    version: int = Field(default=1, ge=1)
    required: bool = True
    trigger_reason: str | None = Field(default=None, max_length=300)


class AgentExecutionPlan(StrictModel):
    plan_kind: Literal["standard_trip", "safety_sensitive_trip"]
    steps: list[AgentPlanStep] = Field(min_length=1, max_length=10)
    rationale: list[str] = Field(default_factory=list, max_length=10)
    skipped_optional_steps: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_task_graph(self) -> AgentExecutionPlan:
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"duplicate task graph step: {step.step_id}")
            missing = set(step.depends_on) - seen
            if missing:
                raise ValueError(
                    f"task {step.step_id} depends on unknown or later tasks: {sorted(missing)}"
                )
            seen.add(step.step_id)
        return self


class AgentSpec(StrictModel):
    agent_type: AgentType
    prompt_version: str = Field(min_length=1, max_length=50)
    context_view: str = Field(default="role_minimal", min_length=1, max_length=80)
    # Model-selectable tools and deterministic server-side capabilities are
    # deliberately separate.  Giving an Agent an internal capability must
    # never make that capability visible in an LLM tool schema.
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    allowed_internal_capabilities: frozenset[str] = Field(default_factory=frozenset)
    input_artifact_types: frozenset[str] = Field(default_factory=frozenset)
    output_artifact_type: str
    budget: AgentBudget


class ArtifactEnvelope(StrictModel):
    artifact_type: str
    schema_version: str = "1.0"
    producer_agent: AgentType
    payload: dict[str, Any]
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    input_hash: str = Field(min_length=16, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None


class CriticSoftAdjustments(StrictModel):
    """The critic can only tune soft objective weights, never hard constraints."""

    travel_time: float | None = Field(default=None, ge=0, le=10)
    walking_time: float | None = Field(default=None, ge=0, le=10)
    distance: float | None = Field(default=None, ge=0, le=10)
    low_rating: float | None = Field(default=None, ge=0, le=10)
    uncertainty: float | None = Field(default=None, ge=0, le=10)
    monetary_cost: float | None = Field(default=None, ge=0, le=10)

    def updates(self) -> dict[str, float]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class ReviewFinding(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    severity: Literal["info", "warning", "blocking"]
    message: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class SafetyCheckReport(StrictModel):
    verdict: Literal["passed", "passed_with_warnings", "needs_clarification"]
    summary: str = Field(min_length=1, max_length=800)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.85, ge=0, le=1)


class RouteEvaluationSummary(StrictModel):
    distance_score: float = Field(ge=0, le=100)
    time_score: float = Field(ge=0, le=100)
    preference_score: float = Field(ge=0, le=100)
    final_score: float = Field(ge=0, le=100)
    passed: bool
    hard_failures: list[str] = Field(default_factory=list, max_length=30)
    formula: Literal["distance*0.40 + time*0.30 + preference*0.30"] = (
        "distance*0.40 + time*0.30 + preference*0.30"
    )


class ReviewReport(StrictModel):
    verdict: Literal[
        "approved",
        "approved_with_warnings",
        "needs_clarification",
        "retry_with_soft_adjustments",
    ]
    summary: str = Field(min_length=1, max_length=800)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=30)
    suggested_adjustments: CriticSoftAdjustments | None = None
    route_evaluation: RouteEvaluationSummary | None = None
    confidence: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="after")
    def validate_retry_boundary(self) -> ReviewReport:
        if self.verdict == "retry_with_soft_adjustments" and (
            self.suggested_adjustments is None or not self.suggested_adjustments.updates()
        ):
            raise ValueError("retry verdict requires at least one soft adjustment")
        if self.verdict != "retry_with_soft_adjustments" and self.suggested_adjustments:
            raise ValueError("soft adjustments are only valid for retry verdicts")
        return self


class AgentStepTrace(StrictModel):
    agent_type: AgentType
    status: str
    prompt_version: str
    budget: AgentBudget
    input_artifact_type: str
    output_artifact: ArtifactEnvelope
    latency_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0, ge=0)
    fallback_used: bool = False
    reason: str | None = Field(default=None, max_length=500)
    input_message_id: UUID | None = None
    output_message_id: UUID | None = None


class AgentWorkflowTrace(StrictModel):
    mode: AgentWorkflowMode
    task_id: str = Field(default="unassigned", min_length=8, max_length=128)
    status: str = "running"
    steps: list[AgentStepTrace] = Field(default_factory=list, max_length=20)
    messages: list[AgentMessageAudit] = Field(default_factory=list, max_length=40)
    shared_state: dict[str, Any] | None = None
    handoff_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    total_cost_usd: float = Field(default=0, ge=0)
    workflow_id: int | None = None
