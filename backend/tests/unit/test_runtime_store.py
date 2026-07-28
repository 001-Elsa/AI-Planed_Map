import asyncio

from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore


def test_runtime_store_counter_and_json_cache():
    async def scenario():
        store = InMemoryRuntimeStore()
        assert await store.increment("rate:user", 60) == 1
        assert await store.increment("rate:user", 60) == 2
        assert await store.increment("tokens:user", 60, 25) == 25
        assert await store.increment("tokens:user", 60, 0) == 25
        await store.set_json("trip:1", {"state": "ACTIVE_TRIP"}, 60)
        assert await store.get_json("trip:1") == {"state": "ACTIVE_TRIP"}
        await store.enqueue("events", {"event_id": 1})
        await store.close()

    asyncio.run(scenario())
