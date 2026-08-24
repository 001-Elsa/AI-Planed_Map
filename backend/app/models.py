from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.session import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    # Migration 0001 creates both a unique constraint and a unique index;
    # declare the constraint explicitly so `alembic check` sees no drift.
    __table_args__ = (UniqueConstraint("username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(20))
    pass_hash: Mapped[str] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    device_name: Mapped[str] = mapped_column(String(100), default="unknown")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stops: Mapped[list["PlanStop"]] = relationship(cascade="all, delete-orphan")


class PlanStop(Base):
    __tablename__ = "plan_stops"
    __table_args__ = (UniqueConstraint("plan_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    task_description: Mapped[str] = mapped_column(String(300))
    poi_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    poi_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    service_duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    constraint_satisfied: Mapped[bool] = mapped_column(Boolean, default=True)


class PlanningRun(Base):
    __tablename__ = "planning_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    input_text: Mapped[str] = mapped_column(Text)
    intent_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    model_name: Mapped[str] = mapped_column(String(100), default="rule-based")
    prompt_version: Mapped[str] = mapped_column(String(50), default="intent-v1")
    map_provider: Mapped[str] = mapped_column(String(50), default="unknown")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanningConversation(Base):
    __tablename__ = "planning_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    request_json: Mapped[str] = mapped_column(Text)
    intent_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    questions_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_key", "idempotency_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="processing")
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("planning_run_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    planning_run_id: Mapped[int] = mapped_column(
        ForeignKey("planning_runs.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[str] = mapped_column(Text)
    change_reason: Mapped[str] = mapped_column(String(500), default="initial_plan")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanPatch(Base):
    __tablename__ = "plan_patches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    planning_run_id: Mapped[int] = mapped_column(
        ForeignKey("planning_runs.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    base_version: Mapped[int] = mapped_column(Integer)
    operations_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(500))
    impact_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionAuditLog(Base):
    __tablename__ = "decision_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    planning_run_id: Mapped[int] = mapped_column(
        ForeignKey("planning_runs.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(String(500))
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    policy_result: Mapped[str] = mapped_column(String(80))
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TripSession(Base):
    __tablename__ = "trip_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    planning_run_id: Mapped[int] = mapped_column(
        ForeignKey("planning_runs.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(30), default="PLAN_READY", index=True)
    current_plan_version: Mapped[int] = mapped_column(Integer, default=1)
    reminder_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=15)
    tracking_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notification_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TripEvent(Base):
    __tablename__ = "trip_events"
    __table_args__ = (UniqueConstraint("trip_session_id", "event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_session_id: Mapped[int] = mapped_column(
        ForeignKey("trip_sessions.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="received")
    impact_level: Mapped[str] = mapped_column(String(20), default="none")
    decision_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    # Migration 0003 creates both a unique constraint and a unique index;
    # declare the constraint explicitly so `alembic check` sees no drift.
    __table_args__ = (UniqueConstraint("trip_session_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_session_id: Mapped[int] = mapped_column(
        ForeignKey("trip_sessions.id", ondelete="CASCADE"), unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    model_name: Mapped[str] = mapped_column(String(100), default="policy-controller")
    prompt_version: Mapped[str] = mapped_column(String(50), default="companion-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("message_id"),
        UniqueConstraint("workflow_run_id", "idempotency_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"), index=True, nullable=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    structured_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    protocol_version: Mapped[str | None] = mapped_column(String(10), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    sender: Mapped[str | None] = mapped_column(String(30), index=True, nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(30), index=True, nullable=True)
    message_type: Mapped[str | None] = mapped_column(String(30), index=True, nullable=True)
    artifact_type: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    delivery_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"), index=True, nullable=True
    )
    parent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    agent_type: Mapped[str] = mapped_column(String(30), default="companion", index=True)
    prompt_version: Mapped[str] = mapped_column(String(50), default="companion-v1")
    budget_json: Mapped[str] = mapped_column(Text, default="{}")
    trigger_type: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(30))
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    output_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentWorkflowRun(Base):
    __tablename__ = "agent_workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    planning_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("planning_conversations.id", ondelete="SET NULL"), index=True, nullable=True
    )
    planning_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("planning_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    trip_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("trip_sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    trigger_type: Mapped[str] = mapped_column(String(60))
    mode: Mapped[str] = mapped_column(String(20), default="shadow")
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    handoff_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentWorkflowTask(Base):
    __tablename__ = "agent_workflow_tasks"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "task_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    task_key: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    dependency_keys_json: Mapped[str] = mapped_column(Text, default="[]")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    input_artifact_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    output_artifact_type: Mapped[str] = mapped_column(String(80))
    budget_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentHandoff(Base):
    __tablename__ = "agent_handoffs"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str] = mapped_column(String(36), index=True)
    source_task_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    target_task_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    sender: Mapped[str] = mapped_column(String(30), index=True)
    receiver: Mapped[str] = mapped_column(String(30), index=True)
    artifact_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="delivered", index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentArtifact(Base):
    __tablename__ = "agent_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    artifact_type: Mapped[str] = mapped_column(String(60), index=True)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    artifact_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    artifact_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    producer_agent: Mapped[str] = mapped_column(String(30), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1)
    evidence_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    input_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentSharedStateSnapshot(Base):
    __tablename__ = "agent_shared_state_snapshots"
    __table_args__ = (UniqueConstraint("workflow_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(30), index=True)
    state_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(100), index=True)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    output_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    upstream_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserConsent(Base):
    __tablename__ = "user_consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trip_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("trip_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(60), index=True)
    granted: Mapped[bool] = mapped_column(Boolean, default=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LocationSnapshot(Base):
    __tablename__ = "location_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_session_id: Mapped[int] = mapped_column(
        ForeignKey("trip_sessions.id", ondelete="CASCADE"), index=True
    )
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    encrypted_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    accuracy_meters: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60), default="explicit_user_confirmation")
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ExternalDataSnapshot(Base):
    __tablename__ = "external_data_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_session_id: Mapped[int] = mapped_column(
        ForeignKey("trip_sessions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(80))
    data_type: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (Index("ix_favorites_user_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(200), default="")
    lng: Mapped[float] = mapped_column(Float)
    lat: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(30), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (Index("ix_tracks_user_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(50))
    distance: Mapped[float] = mapped_column(Float)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_real: Mapped[bool] = mapped_column(Boolean, default=False)
    path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Checkin(Base):
    __tablename__ = "checkins"
    __table_args__ = (Index("ix_checkins_user_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    note: Mapped[str] = mapped_column(String(300), default="")
    emoji: Mapped[str] = mapped_column(String(8), default="📍")
    lng: Mapped[float] = mapped_column(Float)
    lat: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Share(Base):
    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(10))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class Friend(Base):
    __tablename__ = "friends"
    __table_args__ = (
        UniqueConstraint("user_id", "friend_id"),
        UniqueConstraint("pair_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    friend_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pair_key: Mapped[str] = mapped_column(String(50), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
