"""Session device management and revocation.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(
            sa.Column("device_name", sa.String(100), nullable=False, server_default="unknown")
        )
        batch.add_column(
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True)))


def downgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("revoked_at")
        batch.drop_column("last_seen_at")
        batch.drop_column("device_name")
