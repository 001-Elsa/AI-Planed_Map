"""AI-Planned constraints, provenance, plan versions and decision audit.

Revision ID: 0002
Revises: 0001
"""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("planning_runs") as batch:
        batch.add_column(
            sa.Column("prompt_version", sa.String(50), nullable=False, server_default="intent-v1")
        )
        batch.add_column(
            sa.Column("map_provider", sa.String(50), nullable=False, server_default="unknown")
        )
        batch.add_column(sa.Column("trace_id", sa.String(64)))
        batch.add_column(sa.Column("latency_ms", sa.Integer()))
        batch.add_column(sa.Column("input_tokens", sa.Integer()))
        batch.add_column(sa.Column("output_tokens", sa.Integer()))
        batch.add_column(sa.Column("estimated_cost_usd", sa.Float()))
        batch.create_index("ix_planning_runs_trace_id", ["trace_id"])
    expires_default = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with op.batch_alter_table("idempotency_records") as batch:
        batch.add_column(sa.Column("error_code", sa.String(80)))
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch.add_column(
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=expires_default,
            )
        )
        batch.create_index("ix_idempotency_records_expires_at", ["expires_at"])
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "planning_run_id",
            sa.Integer(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("change_reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("planning_run_id", "version"),
    )
    op.create_index("ix_plan_versions_planning_run_id", "plan_versions", ["planning_run_id"])
    op.create_index("ix_plan_versions_user_id", "plan_versions", ["user_id"])
    op.create_table(
        "plan_patches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "planning_run_id",
            sa.Integer(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("operations_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("impact_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_patches_planning_run_id", "plan_patches", ["planning_run_id"])
    op.create_index("ix_plan_patches_user_id", "plan_patches", ["user_id"])
    op.create_table(
        "decision_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "planning_run_id",
            sa.Integer(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("policy_result", sa.String(30), nullable=False),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_decision_audit_logs_planning_run_id", "decision_audit_logs", ["planning_run_id"]
    )
    op.create_index("ix_decision_audit_logs_user_id", "decision_audit_logs", ["user_id"])


def downgrade():
    op.drop_table("decision_audit_logs")
    op.drop_table("plan_patches")
    op.drop_table("plan_versions")
    with op.batch_alter_table("idempotency_records") as batch:
        batch.drop_index("ix_idempotency_records_expires_at")
        batch.drop_column("expires_at")
        batch.drop_column("updated_at")
        batch.drop_column("error_code")
    with op.batch_alter_table("planning_runs") as batch:
        batch.drop_index("ix_planning_runs_trace_id")
        for column in (
            "estimated_cost_usd",
            "output_tokens",
            "input_tokens",
            "latency_ms",
            "trace_id",
            "map_provider",
            "prompt_version",
        ):
            batch.drop_column(column)
