"""Persist Agent tool authorization and argument-validation evidence.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.add_column(
            sa.Column("authorized", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(
            sa.Column("arguments_valid", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.drop_column("arguments_valid")
        batch.drop_column("authorized")
