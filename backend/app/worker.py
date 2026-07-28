import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete

from backend.app.core.config import get_settings
from backend.app.db.session import SessionLocal, engine
from backend.app.models import LocationSnapshot

logger = logging.getLogger("mapgo.worker")


async def cleanup_expired_locations() -> int:
    async with SessionLocal() as db:
        result = await db.execute(
            delete(LocationSnapshot).where(
                LocationSnapshot.expires_at <= datetime.now(timezone.utc)
            )
        )
        await db.commit()
        return result.rowcount


async def run_worker() -> None:
    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError("Worker requires REDIS_URL")
    from redis.asyncio import from_url

    client = from_url(settings.redis_url, decode_responses=True)
    await client.ping()
    logger.info("MapGo worker started")
    try:
        while True:
            item = await client.brpop("mapgo:trip-events", timeout=30)
            if item:
                _, payload = item
                event = json.loads(payload)
                logger.info(
                    "trip_event_received trip_id=%s event_id=%s type=%s",
                    event.get("trip_id"),
                    event.get("event_id"),
                    event.get("event_type"),
                )
            removed = await cleanup_expired_locations()
            if removed:
                logger.info("expired_locations_removed count=%s", removed)
            await asyncio.sleep(0)
    finally:
        await client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_worker())
