"""Chaos / fault-injection scenarios that can run without external services."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore


async def redis_restart_simulation() -> dict:
    """Simulate queue persistence loss by swapping store instances mid-flight."""
    store_a = InMemoryRuntimeStore()
    await store_a.enqueue("mapgo:trip-events", {"event_id": 1})
    # "Restart" drops in-memory state.
    store_b = InMemoryRuntimeStore()
    recovered = await store_b.dequeue("mapgo:trip-events", timeout_seconds=0)
    return {
        "scenario": "runtime_store_restart_loses_inflight_memory_queue",
        "recovered_after_restart": recovered is not None,
        "expected": False,
        "passed": recovered is None,
    }


async def lock_contention() -> dict:
    store = InMemoryRuntimeStore()
    token = await store.acquire_lock("agent-run:trip:9", 2)
    contested = await store.acquire_lock("agent-run:trip:9", 2)
    await asyncio.sleep(0.05)
    released = await store.release_lock("agent-run:trip:9", token or "")
    return {
        "scenario": "distributed_lock_contention",
        "first_acquired": bool(token),
        "second_acquired": contested is not None,
        "released": released,
        "passed": bool(token) and contested is None and released,
    }


async def retry_exhaustion_to_dlq() -> dict:
    store = InMemoryRuntimeStore()
    dispositions = []
    payload = {"event_id": "x"}
    for attempt in range(1, 6):
        dispositions.append(
            await store.enqueue_retry(
                "mapgo:trip-events",
                payload,
                attempt=attempt,
                max_attempts=5,
                delay_seconds=0,
            )
        )
    dlq_item = await store.dequeue("mapgo:trip-events:dlq", timeout_seconds=1)
    return {
        "scenario": "retry_exhaustion_dead_letter",
        "dispositions": dispositions,
        "dlq_received": dlq_item is not None,
        "passed": dispositions[-1] == "dlq" and dlq_item is not None,
    }


async def provider_429_backoff_demo() -> dict:
    """Demonstrate client-side backoff timing without hitting a real provider."""
    delays = [0.01 * (2**attempt) for attempt in range(3)]
    started = time.perf_counter()
    for delay in delays:
        await asyncio.sleep(delay)
    elapsed = time.perf_counter() - started
    return {
        "scenario": "provider_429_exponential_backoff",
        "planned_delay_seconds": sum(delays),
        "elapsed_seconds": elapsed,
        "passed": elapsed >= sum(delays) * 0.8,
    }


async def main() -> None:
    results = [
        await redis_restart_simulation(),
        await lock_contention(),
        await retry_exhaustion_to_dlq(),
        await provider_429_backoff_demo(),
    ]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
        "passed": all(item["passed"] for item in results),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MapGo chaos / fault injection smoke checks")
    parser.parse_args()
    asyncio.run(main())
