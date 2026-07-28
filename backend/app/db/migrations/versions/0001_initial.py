"""Initial FastAPI schema.

Revision ID: 0001
Revises:
"""

from typing import Any, cast

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(20), nullable=False),
        sa.Column("nickname", sa.String(20), nullable=False),
        sa.Column("pass_hash", sa.String(256), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_table(
        "sessions",
        sa.Column("token", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plans_user_id", "plans", ["user_id"])
    op.create_table(
        "plan_stops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("task_description", sa.String(300), nullable=False),
        sa.Column("poi_id", sa.String(80)),
        sa.Column("poi_name", sa.String(200)),
        sa.Column("longitude", sa.Float()),
        sa.Column("latitude", sa.Float()),
        sa.Column("eta", sa.DateTime(timezone=True)),
        sa.Column("service_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("constraint_satisfied", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("plan_id", "position"),
    )
    op.create_index("ix_plan_stops_plan_id", "plan_stops", ["plan_id"])
    op.create_table(
        "planning_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("intent_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_key", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("response_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_key", "idempotency_key"),
    )
    for table, columns in {
        "favorites": [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("address", sa.String(200), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("mode", sa.String(30), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
        "tracks": [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(10), nullable=False),
            sa.Column("name", sa.String(50), nullable=False),
            sa.Column("distance", sa.Float(), nullable=False),
            sa.Column("duration", sa.Float()),
            sa.Column("is_real", sa.Boolean(), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
        "checkins": [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("note", sa.String(300), nullable=False),
            sa.Column("emoji", sa.String(8), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
        "shares": [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("token", sa.String(16), nullable=False),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("type", sa.String(10), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
    }.items():
        op.create_table(table, *cast(list[Any], columns))
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    op.create_index("ix_shares_token", "shares", ["token"], unique=True)
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text()),
    )
    op.create_table(
        "friends",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "friend_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "friend_id"),
    )
    op.create_index("ix_friends_user_id", "friends", ["user_id"])
    op.create_index("ix_friends_friend_id", "friends", ["friend_id"])


def downgrade():
    for table in (
        "friends",
        "settings",
        "shares",
        "checkins",
        "tracks",
        "favorites",
        "idempotency_records",
        "planning_runs",
        "plan_stops",
        "plans",
        "sessions",
        "users",
    ):
        op.drop_table(table)
