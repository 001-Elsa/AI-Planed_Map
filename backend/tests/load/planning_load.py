"""Dependency-free load runner that reports measured latency; never writes fake results."""

import argparse
import asyncio
import json
import statistics
import time
import uuid

import httpx


async def one(client: httpx.AsyncClient, url: str, token: str, index: int) -> tuple[int, float]:
    started = time.perf_counter()
    response = await client.post(
        f"{url.rstrip('/')}/api/ai/plans",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": f"load-{index}-{uuid.uuid4()}",
        },
        json={
            "text": "从学校出发，取快递，再买水果",
            "origin": {"lng": 116.397, "lat": 39.908},
            "transport_mode": "walking",
        },
    )
    return response.status_code, (time.perf_counter() - started) * 1000


async def main(args) -> None:
    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        semaphore = asyncio.Semaphore(args.concurrency)

        async def bounded(index: int):
            async with semaphore:
                return await one(client, args.url, args.token, index)

        wall_started = time.perf_counter()
        results = await asyncio.gather(*(bounded(index) for index in range(args.requests)))
        wall_seconds = time.perf_counter() - wall_started
    latencies = sorted(item[1] for item in results)

    def percentile(value: float) -> float:
        if not latencies:
            return 0.0
        return latencies[min(len(latencies) - 1, max(0, int(len(latencies) * value) - 1))]

    successes = sum(1 for status, _ in results if status < 400)
    print(
        json.dumps(
            {
                "requests": len(results),
                "concurrency": args.concurrency,
                "successes": successes,
                "errors": len(results) - successes,
                "throughput_rps": round(len(results) / wall_seconds, 2) if wall_seconds else 0,
                "latency_ms": {
                    "mean": statistics.fmean(latencies) if latencies else 0,
                    "p50": percentile(0.50),
                    "p95": percentile(0.95),
                    "p99": percentile(0.99),
                    "max": max(latencies) if latencies else 0,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=30)
    asyncio.run(main(parser.parse_args()))
