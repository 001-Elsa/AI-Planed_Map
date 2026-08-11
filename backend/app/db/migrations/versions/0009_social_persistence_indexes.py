"""Strengthen social uniqueness and persisted activity queries.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("friends") as batch:
        batch.add_column(sa.Column("pair_key", sa.String(length=50), nullable=True))

    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, user_id, friend_id, status FROM friends "
                "ORDER BY CASE WHEN status = 'accepted' THEN 0 ELSE 1 END, id"
            )
        ).mappings()
    )
    kept_by_pair: dict[str, int] = {}
    for row in rows:
        pair_key = (
            f"{min(row['user_id'], row['friend_id'])}:{max(row['user_id'], row['friend_id'])}"
        )
        if pair_key in kept_by_pair:
            connection.execute(
                sa.text("DELETE FROM friends WHERE id = :id"),
                {"id": row["id"]},
            )
            continue
        kept_by_pair[pair_key] = row["id"]
        connection.execute(
            sa.text("UPDATE friends SET pair_key = :pair_key WHERE id = :id"),
            {"pair_key": pair_key, "id": row["id"]},
        )

    with op.batch_alter_table("friends") as batch:
        batch.alter_column("pair_key", existing_type=sa.String(length=50), nullable=False)
        batch.create_unique_constraint("uq_friends_pair_key", ["pair_key"])

    op.create_index("ix_tracks_user_created_at", "tracks", ["user_id", "created_at"])
    op.create_index("ix_checkins_user_created_at", "checkins", ["user_id", "created_at"])
    op.create_index("ix_favorites_user_created_at", "favorites", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_favorites_user_created_at", table_name="favorites")
    op.drop_index("ix_checkins_user_created_at", table_name="checkins")
    op.drop_index("ix_tracks_user_created_at", table_name="tracks")
    with op.batch_alter_table("friends") as batch:
        batch.drop_constraint("uq_friends_pair_key", type_="unique")
        batch.drop_column("pair_key")
