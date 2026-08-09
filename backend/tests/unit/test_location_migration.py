import importlib

import sqlalchemy as sa

from backend.app.core.privacy import decrypt_location


def test_location_backfill_encrypts_plaintext_and_removes_incomplete_rows(monkeypatch):
    migration = importlib.import_module(
        "backend.app.db.migrations.versions.0008_backfill_location_encryption"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE location_snapshots ("
                "id INTEGER PRIMARY KEY, longitude FLOAT, latitude FLOAT, "
                "encrypted_payload TEXT)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO location_snapshots "
                "(id, longitude, latitude, encrypted_payload) VALUES "
                "(1, 116.397, 39.908, NULL), (2, NULL, 39.9, NULL)"
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()

        rows = connection.execute(
            sa.text(
                "SELECT id, longitude, latitude, encrypted_payload "
                "FROM location_snapshots ORDER BY id"
            )
        ).mappings().all()
        assert len(rows) == 1
        assert rows[0]["longitude"] is None
        assert rows[0]["latitude"] is None
        assert decrypt_location(rows[0]["encrypted_payload"]) == (116.397, 39.908)

        migration.downgrade()
        restored = connection.execute(
            sa.text(
                "SELECT longitude, latitude FROM location_snapshots WHERE id = 1"
            )
        ).mappings().one()
        assert restored == {"longitude": 116.397, "latitude": 39.908}
