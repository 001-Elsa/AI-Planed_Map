"""Persist workflow execution mode and deterministic stage metadata.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_workflow_runs") as batch:
        batch.add_column(
            sa.Column("execution_mode", sa.String(length=20), nullable=False, server_default="sync")
        )

    with op.batch_alter_table("agent_workflow_tasks") as batch:
        batch.add_column(
            sa.Column(
                "execution_kind", sa.String(length=20), nullable=False, server_default="agent"
            )
        )
        batch.add_column(sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"))
        batch.create_index("ix_agent_workflow_tasks_execution_kind", ["execution_kind"])


def downgrade() -> None:
    with op.batch_alter_table("agent_workflow_tasks") as batch:
        batch.drop_index("ix_agent_workflow_tasks_execution_kind")
        batch.drop_column("summary_json")
        batch.drop_column("execution_kind")

    with op.batch_alter_table("agent_workflow_runs") as batch:
        batch.drop_column("execution_mode")
