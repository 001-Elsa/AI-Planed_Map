"""Companion Agent sessions, events, tools, consent and ephemeral location.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.Integer(), primary_key=True)


def _user() -> sa.Column:
    return sa.Column(
        "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


def _created() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def upgrade():
    op.create_table(
        "trip_sessions",
        _id(),
        _user(),
        sa.Column(
            "planning_run_id",
            sa.Integer(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("current_plan_version", sa.Integer(), nullable=False),
        sa.Column("reminder_cooldown_minutes", sa.Integer(), nullable=False),
        sa.Column("tracking_enabled", sa.Boolean(), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("last_notification_at", sa.DateTime(timezone=True)),
        _created(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name, columns in (
        ("ix_trip_sessions_user_id", ["user_id"]),
        ("ix_trip_sessions_planning_run_id", ["planning_run_id"]),
        ("ix_trip_sessions_state", ["state"]),
    ):
        op.create_index(name, "trip_sessions", columns)
    op.create_table(
        "trip_events",
        _id(),
        sa.Column(
            "trip_session_id",
            sa.Integer(),
            sa.ForeignKey("trip_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("impact_level", sa.String(20), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=False),
        _created(),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("trip_session_id", "event_id"),
    )
    op.create_index("ix_trip_events_trip_session_id", "trip_events", ["trip_session_id"])
    op.create_index("ix_trip_events_event_type", "trip_events", ["event_type"])
    op.create_table(
        "agent_sessions",
        _id(),
        sa.Column(
            "trip_session_id",
            sa.Integer(),
            sa.ForeignKey("trip_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _user(),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        _created(),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("trip_session_id"),
    )
    op.create_index(
        "ix_agent_sessions_trip_session_id", "agent_sessions", ["trip_session_id"], unique=True
    )
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"])
    op.create_table(
        "agent_messages",
        _id(),
        sa.Column(
            "agent_session_id",
            sa.Integer(),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_json", sa.Text()),
        _created(),
    )
    op.create_index("ix_agent_messages_agent_session_id", "agent_messages", ["agent_session_id"])
    op.create_table(
        "agent_runs",
        _id(),
        sa.Column(
            "agent_session_id",
            sa.Integer(),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger_type", sa.String(60), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("estimated_cost_usd", sa.Float()),
        sa.Column("latency_ms", sa.Integer()),
        _created(),
    )
    op.create_index("ix_agent_runs_agent_session_id", "agent_runs", ["agent_session_id"])
    op.create_index("ix_agent_runs_trace_id", "agent_runs", ["trace_id"])
    op.create_table(
        "agent_tool_calls",
        _id(),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_summary_json", sa.Text(), nullable=False),
        sa.Column("upstream_provider", sa.String(80)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error_type", sa.String(80)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("trace_id", sa.String(64)),
        _created(),
    )
    for name, columns in (
        ("ix_agent_tool_calls_agent_run_id", ["agent_run_id"]),
        ("ix_agent_tool_calls_tool_name", ["tool_name"]),
        ("ix_agent_tool_calls_trace_id", ["trace_id"]),
    ):
        op.create_index(name, "agent_tool_calls", columns)
    op.create_table(
        "user_consents",
        _id(),
        _user(),
        sa.Column(
            "trip_session_id",
            sa.Integer(),
            sa.ForeignKey("trip_sessions.id", ondelete="CASCADE"),
        ),
        sa.Column("scope", sa.String(60), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        _created(),
    )
    for name, columns in (
        ("ix_user_consents_user_id", ["user_id"]),
        ("ix_user_consents_trip_session_id", ["trip_session_id"]),
        ("ix_user_consents_scope", ["scope"]),
    ):
        op.create_index(name, "user_consents", columns)
    op.create_table(
        "location_snapshots",
        _id(),
        sa.Column(
            "trip_session_id",
            sa.Integer(),
            sa.ForeignKey("trip_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("accuracy_meters", sa.Float(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
    )
    op.create_index(
        "ix_location_snapshots_trip_session_id", "location_snapshots", ["trip_session_id"]
    )
    op.create_index("ix_location_snapshots_captured_at", "location_snapshots", ["captured_at"])
    op.create_index("ix_location_snapshots_expires_at", "location_snapshots", ["expires_at"])
    op.create_table(
        "user_preferences",
        _id(),
        _user(),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "key"),
    )
    op.create_index("ix_user_preferences_user_id", "user_preferences", ["user_id"])
    op.create_table(
        "external_data_snapshots",
        _id(),
        sa.Column(
            "trip_session_id",
            sa.Integer(),
            sa.ForeignKey("trip_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("data_type", sa.String(60), nullable=False),
        sa.Column("source_version", sa.String(80)),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        _created(),
    )
    op.create_index(
        "ix_external_data_snapshots_trip_session_id",
        "external_data_snapshots",
        ["trip_session_id"],
    )


def downgrade():
    for table in (
        "external_data_snapshots",
        "user_preferences",
        "location_snapshots",
        "user_consents",
        "agent_tool_calls",
        "agent_runs",
        "agent_messages",
        "agent_sessions",
        "trip_events",
        "trip_sessions",
    ):
        op.drop_table(table)
