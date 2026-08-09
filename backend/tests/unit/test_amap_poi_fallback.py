import asyncio

import httpx

from backend.app.clients.amap_client import AMapProvider
from backend.app.core.config import Settings
from backend.app.schemas.ai_intent import Coordinate


def test_named_place_falls_back_from_inputtips_to_text_search():
    provider = AMapProvider(Settings(amap_web_key="test"), httpx.AsyncClient())
    calls = []

    async def fake_get(path, params):
        calls.append((path, params))
        if path == "/v3/assistant/inputtips":
            return {"tips": []}
        return {
            "pois": [
                {
                    "id": "poi-1",
                    "name": "花园酒店",
                    "address": "广州市越秀区",
                    "location": "113.2708,23.1353",
                    "adname": "越秀区",
                }
            ]
        }

    provider._get = fake_get
    try:
        results = asyncio.run(
            provider.search_poi("花园酒店", Coordinate(lng=113.26, lat=23.13), "广州")
        )
    finally:
        asyncio.run(provider.client.aclose())
    assert [path for path, _ in calls] == [
        "/v3/assistant/inputtips",
        "/v3/place/text",
    ]
    assert results[0].name == "花园酒店"
    assert results[0].source == "amap_place_text_v3"


def test_category_search_falls_back_from_nearby_to_text_index():
    provider = AMapProvider(Settings(amap_web_key="test"), httpx.AsyncClient())
    calls = []

    async def fake_get(path, params):
        calls.append(path)
        if path == "/v3/place/around":
            return {"pois": []}
        return {
            "pois": [
                {
                    "id": "pharmacy-1",
                    "name": "安心药店",
                    "location": "113.261,23.131",
                }
            ]
        }

    provider._get = fake_get
    try:
        results = asyncio.run(
            provider.search_poi("药店", Coordinate(lng=113.26, lat=23.13), "广州")
        )
    finally:
        asyncio.run(provider.client.aclose())
    assert calls == ["/v3/place/around", "/v3/place/text"]
    assert results[0].name == "安心药店"


def test_named_place_prefers_strong_name_match_over_nearer_loose_match():
    provider = AMapProvider(Settings(amap_web_key="test"), httpx.AsyncClient())

    async def fake_get(path, params):
        assert path == "/v3/assistant/inputtips"
        return {
            "tips": [
                {
                    "id": "loose-near",
                    "name": "广州博物馆(镇海楼展区)",
                    "address": "越秀公园内",
                    "location": "113.264,23.139",
                    "district": "广州市越秀区",
                },
                {
                    "id": "strong-far",
                    "name": "广东省博物馆(西门)",
                    "address": "珠江东路",
                    "location": "113.324,23.112",
                    "district": "广州市天河区",
                },
            ]
        }

    provider._get = fake_get
    try:
        results = asyncio.run(
            provider.search_poi("广东省博物馆", Coordinate(lng=113.26, lat=23.13), "广州")
        )
    finally:
        asyncio.run(provider.client.aclose())

    assert [item.id for item in results] == ["strong-far"]
