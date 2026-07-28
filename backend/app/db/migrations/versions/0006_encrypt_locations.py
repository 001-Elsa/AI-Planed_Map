"""Encrypt precise location payloads at application field level.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("location_snapshots") as batch:
        batch.alter_column("latitude", existing_type=sa.Float(), nullable=True)
        batch.alter_column("longitude", existing_type=sa.Float(), nullable=True)
        batch.add_column(sa.Column("encrypted_payload", sa.Text(), nullable=True))


def downgrade():
    connection = op.get_bind()
    encrypted = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM location_snapshots "
            "WHERE encrypted_payload IS NOT NULL AND "
            "(latitude IS NULL OR longitude IS NULL)"
        )
    ).scalar()
    if encrypted:
        raise RuntimeError(
            "Cannot downgrade 0006 while encrypted-only location rows exist; "
            "decrypt them with the active key first."
        )
    with op.batch_alter_table("location_snapshots") as batch:
        batch.drop_column("encrypted_payload")
        batch.alter_column("longitude", existing_type=sa.Float(), nullable=False)
        batch.alter_column("latitude", existing_type=sa.Float(), nullable=False)
