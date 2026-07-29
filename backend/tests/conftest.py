import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest_asyncio
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager

from backend.app.main import app

DATABASE_FILE = None
if "DATABASE_URL" not in os.environ:
    DATABASE_FILE = Path(tempfile.gettempdir()) / f"mapgo-test-{uuid.uuid4().hex}.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DATABASE_FILE.as_posix()}"
os.environ["MOCK_MAP_PROVIDER"] = "true"
os.environ["ADMIN_INIT_TOKEN"] = ""
os.environ["ENVIRONMENT"] = "test"
os.environ["LOCATION_ENCRYPTION_KEY"] = "test-only-location-key-for-field-encryption"

alembic_config = Config("alembic.ini")
command.upgrade(alembic_config, "head")


def pytest_sessionfinish(session, exitstatus):
    if DATABASE_FILE:
        DATABASE_FILE.unlink(missing_ok=True)


@pytest_asyncio.fixture
async def async_client() -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
