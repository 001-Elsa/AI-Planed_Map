"""Isolated multi-Agent workflows, typed artifacts and per-role run metadata.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_workflow_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "planning_conversation_id",
            sa.Integer(),
            sa.ForeignKey("planning_conversations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "planning_run_id", sa.Integer(), sa.ForeignKey("planning_runs.id", ondelete="SET NULL")
        ),
        sa.Column(
            "trip_session_id", sa.Integer(), sa.ForeignKey("trip_sessions.id", ondelete="CASCADE")
        ),
        sa.Column("trigger_type", sa.String(60), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("handoff_count", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    for column in (
        "user_id",
        "planning_conversation_id",
        "planning_run_id",
        "trip_session_id",
        "status",
        "trace_id",
    ):
        op.create_index(f"ix_agent_workflow_runs_{column}", "agent_workflow_runs", [column])

    with op.batch_alter_table("agent_runs") as batch:
        batch.alter_column("agent_session_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("workflow_run_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("parent_run_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("agent_type", sa.String(30), nullable=False, server_default="companion")
        )
        batch.add_column(
            sa.Column(
                "prompt_version", sa.String(50), nullable=False, server_default="companion-v1"
            )
        )
        batch.add_column(sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"))
        batch.add_column(
            sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("output_summary_json", sa.Text(), nullable=False, server_default="{}")
        )
        batch.create_foreign_key(
            "fk_agent_runs_workflow",
            "agent_workflow_runs",
            ["workflow_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_agent_runs_parent", "agent_runs", ["parent_run_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_agent_runs_workflow_run_id", ["workflow_run_id"])
        batch.create_index("ix_agent_runs_parent_run_id", ["parent_run_id"])
        batch.create_index("ix_agent_runs_agent_type", ["agent_type"])

    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="SET NULL")
        ),
        sa.Column("artifact_type", sa.String(60), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("producer_agent", sa.String(30), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    for column in (
        "workflow_run_id",
        "agent_run_id",
        "artifact_type",
        "producer_agent",
        "input_hash",
    ):
        op.create_index(f"ix_agent_artifacts_{column}", "agent_artifacts", [column])


def downgrade() -> None:
    op.drop_table("agent_artifacts")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_index("ix_agent_runs_agent_type")
        batch.drop_index("ix_agent_runs_parent_run_id")
        batch.drop_index("ix_agent_runs_workflow_run_id")
        batch.drop_constraint("fk_agent_runs_parent", type_="foreignkey")
        batch.drop_constraint("fk_agent_runs_workflow", type_="foreignkey")
        for column in (
            "output_summary_json",
            "fallback_used",
            "budget_json",
            "prompt_version",
            "agent_type",
            "parent_run_id",
            "workflow_run_id",
        ):
            batch.drop_column(column)
        batch.alter_column("agent_session_id", existing_type=sa.Integer(), nullable=False)
    op.drop_table("agent_workflow_runs")
