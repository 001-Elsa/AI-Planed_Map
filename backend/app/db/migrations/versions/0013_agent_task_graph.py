"""Explicit Agent task graph, handoffs and artifact versions.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_workflow_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_key", sa.String(80), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("dependency_keys_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_artifact_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("output_artifact_type", sa.String(80), nullable=False),
        sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workflow_run_id", "task_key"),
    )
    for column in ("workflow_run_id", "task_key", "role", "status"):
        op.create_index(f"ix_agent_workflow_tasks_{column}", "agent_workflow_tasks", [column])

    op.create_table(
        "agent_handoffs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("source_task_key", sa.String(80), nullable=True),
        sa.Column("target_task_key", sa.String(80), nullable=True),
        sa.Column("sender", sa.String(30), nullable=False),
        sa.Column("receiver", sa.String(30), nullable=False),
        sa.Column("artifact_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="delivered"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workflow_run_id", "message_id"),
    )
    for column in (
        "workflow_run_id",
        "message_id",
        "source_task_key",
        "target_task_key",
        "sender",
        "receiver",
        "artifact_type",
        "status",
    ):
        op.create_index(f"ix_agent_handoffs_{column}", "agent_handoffs", [column])

    with op.batch_alter_table("agent_artifacts") as batch:
        batch.add_column(sa.Column("artifact_key", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column("artifact_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("status", sa.String(20), nullable=False, server_default="active")
        )
        batch.add_column(sa.Column("plan_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_agent_artifacts_artifact_key", ["artifact_key"])
        batch.create_index("ix_agent_artifacts_status", ["status"])
        batch.create_index("ix_agent_artifacts_plan_version", ["plan_version"])


def downgrade() -> None:
    with op.batch_alter_table("agent_artifacts") as batch:
        batch.drop_index("ix_agent_artifacts_plan_version")
        batch.drop_index("ix_agent_artifacts_status")
        batch.drop_index("ix_agent_artifacts_artifact_key")
        for column in (
            "invalidated_at",
            "plan_version",
            "status",
            "artifact_version",
            "artifact_key",
        ):
            batch.drop_column(column)
    op.drop_table("agent_handoffs")
    op.drop_table("agent_workflow_tasks")
