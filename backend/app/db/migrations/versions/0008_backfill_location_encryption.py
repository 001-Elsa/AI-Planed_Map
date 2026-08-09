"""Backfill legacy plaintext locations into the encrypted payload.

Revision ID: 0008
Revises: 0007
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

from backend.app.core.config import get_settings
from backend.app.core.privacy import decrypt_location, encrypt_location

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _require_key_when(rows: list[Any]) -> None:
    if rows and not get_settings().location_encryption_key:
        raise RuntimeError(
            "LOCATION_ENCRYPTION_KEY is required to migrate legacy plaintext locations"
        )


def upgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, longitude, latitude FROM location_snapshots "
                "WHERE encrypted_payload IS NULL "
                "AND longitude IS NOT NULL AND latitude IS NOT NULL"
            )
        ).mappings()
    )
    _require_key_when(rows)
    for row in rows:
        encrypted = encrypt_location(float(row["longitude"]), float(row["latitude"]))
        connection.execute(
            sa.text(
                "UPDATE location_snapshots SET encrypted_payload = :encrypted, "
                "longitude = NULL, latitude = NULL WHERE id = :id"
            ),
            {"encrypted": encrypted, "id": row["id"]},
        )
    connection.execute(
        sa.text(
            "UPDATE location_snapshots SET longitude = NULL, latitude = NULL "
            "WHERE encrypted_payload IS NOT NULL"
        )
    )
    connection.execute(
        sa.text(
            "DELETE FROM location_snapshots WHERE encrypted_payload IS NULL "
            "AND (longitude IS NULL OR latitude IS NULL)"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, encrypted_payload FROM location_snapshots "
                "WHERE encrypted_payload IS NOT NULL"
            )
        ).mappings()
    )
    _require_key_when(rows)
    for row in rows:
        longitude, latitude = decrypt_location(row["encrypted_payload"])
        connection.execute(
            sa.text(
                "UPDATE location_snapshots SET longitude = :longitude, latitude = :latitude "
                "WHERE id = :id"
            ),
            {"longitude": longitude, "latitude": latitude, "id": row["id"]},
        )
