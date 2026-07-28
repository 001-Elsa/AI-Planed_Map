from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.app.core.config import get_settings
from backend.app.db.session import Base
from backend.app import models  # noqa: F401


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
sync_url = get_settings().database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")
config.set_main_option("sqlalchemy.url", sync_url)
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=sync_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
