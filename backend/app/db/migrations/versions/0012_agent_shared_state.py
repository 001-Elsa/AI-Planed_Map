"""Durable minimized snapshots for Agent Shared State.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_shared_state_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workflow_run_id"),
    )
    for column in ("workflow_run_id", "task_id", "phase", "state_hash"):
        op.create_index(
            f"ix_agent_shared_state_snapshots_{column}",
            "agent_shared_state_snapshots",
            [column],
        )


def downgrade() -> None:
    op.drop_table("agent_shared_state_snapshots")
