import asyncio

import httpx
import pytest

from backend.app.clients.amap_client import (
    AMapProvider,
    build_map_provider,
    infer_poi_type,
    normalize_poi_keyword,
)
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
            found = await provider.search_poi(
                "去附近药店买药", Coordinate(lng=116.3, lat=39.9), "北京"
            )
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


def test_js_api_key_and_jscode_enable_real_backend_provider():
    async def scenario():
        seen_query: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_query.update(dict(request.url.params))
            return httpx.Response(200, json={"status": "1", "pois": []})

        settings = Settings(
            amap_web_key="",
            amap_key="js-key",
            amap_jscode="js-code",
            mock_map_provider=False,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = build_map_provider(settings, client)
            assert isinstance(provider, AMapProvider)
            await provider.search_poi("附近药店", Coordinate(lng=116.3, lat=39.9), "北京")

        assert seen_query["key"] == "js-key"
        assert seen_query["jscode"] == "js-code"
        assert seen_query["platform"] == "JS"
        assert seen_query["types"] == "090601"
        assert seen_query["keywords"] == "药店"

    asyncio.run(scenario())


def test_named_place_uses_inputtips_fallback_with_distance():
    async def scenario():
        seen_path = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_path
            seen_path = request.url.path
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "tips": [
                        {
                            "id": "named-1",
                            "name": "天安门",
                            "location": "116.397499,39.908722",
                            "district": "北京市东城区",
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = AMapProvider(
                Settings(amap_web_key="contract-key", mock_map_provider=False), client
            )
            found = await provider.search_poi("天安门", Coordinate(lng=116.397, lat=39.908), "北京")

        assert seen_path.endswith("/v3/assistant/inputtips")
        assert found[0].source == "amap_inputtips_v3"
        assert found[0].distance_meters is not None

    asyncio.run(scenario())


def test_named_hotel_is_not_mistaken_for_a_generic_hotel_category():
    assert infer_poi_type("花园酒店") is None
    assert normalize_poi_keyword("花园酒店") == "花园酒店"
    assert infer_poi_type("去附近药店买药") == "090601"
    assert normalize_poi_keyword("去附近药店买药") == "药店"


def test_selected_public_transit_legs_are_verified_with_real_transfer_data():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/v3/direction/transit/integrated")
            assert request.url.params["city"] == "广州市"
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "route": {
                        "transits": [
                            {
                                "distance": "12345",
                                "duration": "2345",
                                "cost": "5",
                            }
                        ]
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = AMapProvider(
                Settings(amap_web_key="contract-key", mock_map_provider=False), client
            )
            edges = await provider.transit_route_edges(
                [
                    Coordinate(lng=113.26, lat=23.13),
                    Coordinate(lng=113.32, lat=23.11),
                ],
                "广州市",
            )
        assert len(edges) == 1
        assert edges[0].source == "amap_transit_integrated_v3"
        assert edges[0].quality == DataQuality.provider
        assert edges[0].distance_meters == 12345
        assert edges[0].duration_seconds == 2345
        assert edges[0].fallback_used is False

    asyncio.run(scenario())
