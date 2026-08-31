"""Fence stale workers after a Redis lease is lost.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trip_events") as batch:
        batch.add_column(
            sa.Column(
                "worker_fencing_token",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("trip_events") as batch:
        batch.drop_column("worker_fencing_token")
