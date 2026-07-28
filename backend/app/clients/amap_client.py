import asyncio
import math
from typing import Protocol

import httpx

from backend.app.core.config import Settings
from backend.app.core.exceptions import UpstreamError
from backend.app.schemas.ai_intent import Coordinate, PoiCandidate, TransportMode


class MapProvider(Protocol):
    async def search_poi(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]: ...

    async def route_matrix(
        self, points: list[Coordinate], mode: TransportMode
    ) -> tuple[list[list[float]], list[list[float]]]: ...


def haversine_meters(a: Coordinate, b: Coordinate) -> float:
    radius = 6_371_000
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


class MockMapProvider:
    """Stable provider for CI; generated POIs remain near the requested origin."""

    async def search_poi(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]:
        seed = sum(ord(char) for char in keyword)
        lng = origin.lng + ((seed % 19) - 9) * 0.001
        lat = origin.lat + (((seed // 19) % 19) - 9) * 0.001
        point = Coordinate(lng=lng, lat=lat)
        return [
            PoiCandidate(
                id=f"mock-{seed}",
                name=keyword,
                address=f"{city or '当前城市'} · Mock POI",
                location=point,
                rating=4.5,
                distance_meters=haversine_meters(origin, point),
            )
        ]

    async def route_matrix(
        self, points: list[Coordinate], mode: TransportMode
    ) -> tuple[list[list[float]], list[list[float]]]:
        speed = {
            TransportMode.walking: 1.2,
            TransportMode.cycling: 4.0,
            TransportMode.driving: 8.3,
            TransportMode.transit: 6.0,
        }[mode]
        distances = [[0.0 for _ in points] for _ in points]
        durations = [[0.0 for _ in points] for _ in points]
        for i, source in enumerate(points):
            for j, target in enumerate(points):
                if i == j:
                    continue
                factor = 1.22 if mode == TransportMode.walking else 1.35
                distances[i][j] = haversine_meters(source, target) * factor
                durations[i][j] = distances[i][j] / speed
        return distances, durations


class AMapProvider(MockMapProvider):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _get(self, path: str, params: dict) -> dict:
        params = {**params, "key": self.settings.amap_web_key}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.settings.external_timeout_seconds) as client:
                    response = await client.get(f"https://restapi.amap.com{path}", params=params)
                    response.raise_for_status()
                    data = response.json()
                if data.get("status") != "1":
                    raise UpstreamError("高德地图返回业务错误", {"info": data.get("info")})
                return data
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status and status not in (429, 500, 502, 503, 504):
                    break
                if attempt < 2:
                    await asyncio.sleep(0.15 * (2**attempt))
        raise UpstreamError("高德地图服务暂时不可用", {"reason": str(last_error)})

    async def search_poi(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]:
        data = await self._get(
            "/v3/place/around",
            {
                "keywords": keyword,
                "location": f"{origin.lng},{origin.lat}",
                "city": city or "",
                "sortrule": "distance",
                "offset": 5,
                "extensions": "all",
            },
        )
        candidates: list[PoiCandidate] = []
        for poi in data.get("pois", []):
            try:
                lng, lat = map(float, poi["location"].split(","))
                rating = poi.get("biz_ext", {}).get("rating")
                candidates.append(
                    PoiCandidate(
                        id=str(poi["id"]),
                        name=str(poi["name"]),
                        address=str(poi.get("address") or ""),
                        location=Coordinate(lng=lng, lat=lat),
                        rating=float(rating) if rating not in (None, []) else None,
                        distance_meters=float(poi.get("distance") or 0),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return candidates

    async def route_matrix(
        self, points: list[Coordinate], mode: TransportMode
    ) -> tuple[list[list[float]], list[list[float]]]:
        distance_type = {
            TransportMode.driving: "1",
            TransportMode.walking: "3",
            TransportMode.cycling: "0",
            TransportMode.transit: "0",
        }[mode]

        async def to_destination(destination: Coordinate):
            origins = "|".join(f"{point.lng},{point.lat}" for point in points)
            return await self._get(
                "/v3/distance",
                {
                    "origins": origins,
                    "destination": f"{destination.lng},{destination.lat}",
                    "type": distance_type,
                },
            )

        responses = await asyncio.gather(*(to_destination(point) for point in points))
        distances = [[0.0 for _ in points] for _ in points]
        durations = [[0.0 for _ in points] for _ in points]
        fallback_distances, fallback_durations = await super().route_matrix(points, mode)
        for destination_index, response in enumerate(responses):
            for result in response.get("results", []):
                try:
                    origin_index = int(result["origin_id"]) - 1
                    distances[origin_index][destination_index] = float(result["distance"])
                    durations[origin_index][destination_index] = float(
                        result.get("duration") or fallback_durations[origin_index][destination_index]
                    )
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
        for i in range(len(points)):
            for j in range(len(points)):
                if i != j and not distances[i][j]:
                    distances[i][j] = fallback_distances[i][j]
                    durations[i][j] = fallback_durations[i][j]
        return distances, durations


def build_map_provider(settings: Settings) -> MapProvider:
    return MockMapProvider() if settings.use_mock_map else AMapProvider(settings)
