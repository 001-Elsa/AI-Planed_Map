import asyncio

import httpx
import pytest

from backend.app.clients.amap_client import AMapProvider
from backend.app.core.config import Settings
from backend.app.core.exceptions import UpstreamError
from backend.app.schemas.ai_intent import Coordinate, DataQuality, TransportMode


def test_partial_matrix_is_explicitly_marked_as_fallback():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "1", "results": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = AMapProvider(
                Settings(amap_web_key="contract-key", mock_map_provider=False),
                client,
            )
            matrix = await provider.route_matrix(
                [Coordinate(lng=116.3, lat=39.9), Coordinate(lng=116.31, lat=39.91)],
                TransportMode.driving,
            )
        edge = matrix.edges[0][1]
        assert edge.fallback_used is True
        assert edge.quality == DataQuality.estimated
        assert edge.source == "haversine_estimate"
        assert edge.confidence < 0.5

    asyncio.run(scenario())


def test_invalid_poi_rows_are_skipped_instead_of_hallucinated():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "pois": [
                        {"id": "bad", "name": "坏数据", "location": "invalid"},
                        {
                            "id": "good",
                            "name": "真实地点",
                            "location": "116.30,39.90",
                            "distance": "10",
                            "biz_ext": {"rating": "4.8"},
                        },
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = AMapProvider(
                Settings(amap_web_key="contract-key", mock_map_provider=False),
                client,
            )
            found = await provider.search_poi("地点", Coordinate(lng=116.3, lat=39.9), "北京")
        assert [item.id for item in found] == ["good"]
        assert found[0].source == "amap_place_around_v3"

    asyncio.run(scenario())


def test_repeated_failures_open_circuit_without_more_upstream_calls():
    async def scenario():
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        settings = Settings(
            amap_web_key="contract-key",
            mock_map_provider=False,
            upstream_max_retries=0,
            circuit_breaker_failure_threshold=2,
            circuit_breaker_recovery_seconds=60,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = AMapProvider(settings, client)
            for _ in range(2):
                with pytest.raises(UpstreamError):
                    await provider.search_poi("地点", Coordinate(lng=116.3, lat=39.9), "北京")
            with pytest.raises(UpstreamError, match="熔断器"):
                await provider.search_poi("地点", Coordinate(lng=116.3, lat=39.9), "北京")
        assert calls == 2

    asyncio.run(scenario())
