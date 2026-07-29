"""Expand decision_audit_logs.policy_result from VARCHAR(30) to VARCHAR(80).

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("decision_audit_logs") as batch:
        batch.alter_column(
            "policy_result",
            existing_type=sa.String(length=30),
            type_=sa.String(length=80),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("decision_audit_logs") as batch:
        batch.alter_column(
            "policy_result",
            existing_type=sa.String(length=80),
            type_=sa.String(length=30),
            existing_nullable=False,
        )
