"""Unified Agent message protocol and workflow message audit.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_messages") as batch:
        batch.alter_column("agent_session_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("workflow_run_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("protocol_version", sa.String(10), nullable=True))
        batch.add_column(sa.Column("message_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("task_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("sender", sa.String(30), nullable=True))
        batch.add_column(sa.Column("receiver", sa.String(30), nullable=True))
        batch.add_column(sa.Column("message_type", sa.String(30), nullable=True))
        batch.add_column(sa.Column("artifact_type", sa.String(80), nullable=True))
        batch.add_column(sa.Column("content_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch.add_column(sa.Column("correlation_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("causation_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("delivery_status", sa.String(20), nullable=True))
        batch.create_foreign_key(
            "fk_agent_messages_workflow",
            "agent_workflow_runs",
            ["workflow_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_agent_messages_message_id", ["message_id"])
        batch.create_unique_constraint(
            "uq_agent_messages_workflow_idempotency",
            ["workflow_run_id", "idempotency_key"],
        )
        for column in (
            "workflow_run_id",
            "task_id",
            "sender",
            "receiver",
            "message_type",
            "artifact_type",
            "idempotency_key",
            "correlation_id",
        ):
            batch.create_index(f"ix_agent_messages_{column}", [column])


def downgrade() -> None:
    with op.batch_alter_table("agent_messages") as batch:
        for column in (
            "correlation_id",
            "idempotency_key",
            "artifact_type",
            "message_type",
            "receiver",
            "sender",
            "task_id",
            "workflow_run_id",
        ):
            batch.drop_index(f"ix_agent_messages_{column}")
        batch.drop_constraint("uq_agent_messages_workflow_idempotency", type_="unique")
        batch.drop_constraint("uq_agent_messages_message_id", type_="unique")
        batch.drop_constraint("fk_agent_messages_workflow", type_="foreignkey")
        for column in (
            "delivery_status",
            "attempt",
            "causation_id",
            "correlation_id",
            "idempotency_key",
            "content_hash",
            "artifact_type",
            "message_type",
            "receiver",
            "sender",
            "task_id",
            "message_id",
            "protocol_version",
            "workflow_run_id",
        ):
            batch.drop_column(column)
        batch.alter_column("agent_session_id", existing_type=sa.Integer(), nullable=False)
