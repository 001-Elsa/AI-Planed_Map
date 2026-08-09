from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine_options: dict = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"timeout": settings.sqlite_busy_timeout_seconds}
else:
    engine_options.update(
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_recycle=1800,
    )
engine = create_async_engine(settings.database_url, **engine_options)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_seconds * 1000)}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def check_database() -> None:
    """Fail fast when migrations were not executed; the app never mutates schema."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        try:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        except Exception as exc:
            raise RuntimeError("数据库尚未迁移，请先执行 `alembic upgrade head`") from exc
    expected = get_settings().required_schema_revision
    if revision != expected:
        raise RuntimeError(f"数据库版本为 {revision!r}，应用要求 Alembic revision {expected!r}")
