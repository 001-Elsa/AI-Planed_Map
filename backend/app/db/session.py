from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


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
