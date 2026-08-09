from typing import Any

from backend.app.infrastructure.runtime_store import RuntimeStore

TRIP_STREAM_TTL_SECONDS = 86_400
# A cursor must not reset while a browser can still hold Last-Event-ID in memory.
TRIP_SEQUENCE_TTL_SECONDS = 10 * 365 * 24 * 60 * 60


async def publish_trip_stream(
    store: RuntimeStore,
    trip_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Publish one snapshot using a single monotonic sequence namespace per trip."""
    sequence = await store.increment(
        f"trip-sequence:{trip_id}",
        TRIP_SEQUENCE_TTL_SECONDS,
    )
    event = {**payload, "sequence": sequence, "trip_id": trip_id}
    await store.set_json(f"trip-stream:{trip_id}", event, TRIP_STREAM_TTL_SECONDS)
    await store.publish(f"trip:{trip_id}", event)
    return event
