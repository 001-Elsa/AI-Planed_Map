import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Protocol

import httpx

from backend.app.core.config import Settings
from backend.app.core.exceptions import UpstreamError
from backend.app.core.observability import metrics
from backend.app.schemas.ai_intent import (
    Coordinate,
    DataQuality,
    PoiCandidate,
    RouteEdge,
    RouteMatrix,
    TransportMode,
)


class MapProvider(Protocol):
    name: str

    async def search_poi(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]: ...

    async def route_matrix(self, points: list[Coordinate], mode: TransportMode) -> RouteMatrix: ...


def haversine_meters(a: Coordinate, b: Coordinate) -> float:
    radius = 6_371_000
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


class MockMapProvider:
    """Deterministic provider for tests. Every estimate is explicitly labelled."""

    name = "mock-map-v2"

    async def search_poi(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]:
        now = datetime.now(timezone.utc)
        seed = sum(ord(char) for char in keyword)
        results = []
        for rank in range(3):
            lng = origin.lng + (((seed + rank * 7) % 23) - 11) * 0.001
            lat = origin.lat + ((((seed // 19) + rank * 5) % 23) - 11) * 0.001
            point = Coordinate(lng=lng, lat=lat)
            results.append(
                PoiCandidate(
                    id=f"mock-{seed}-{rank}",
                    name=f"{keyword}{' ' + chr(65 + rank) if rank else ''}",
                    address=f"{city or '当前城市'} · Mock POI",
                    location=point,
                    rating=round(4.7 - rank * 0.15, 1),
                    distance_meters=haversine_meters(origin, point),
                    source=self.name,
                    data_updated_at=now,
                    confidence=0.65,
                    district=city,
                    open_now=True,
                )
            )
        return sorted(results, key=lambda item: item.distance_meters or 0)

    async def route_matrix(self, points: list[Coordinate], mode: TransportMode) -> RouteMatrix:
        speed = {
            TransportMode.walking: 1.2,
            TransportMode.cycling: 4.0,
            TransportMode.driving: 8.3,
            TransportMode.transit: 6.0,
        }[mode]
        now = datetime.now(timezone.utc)
        edges: list[list[RouteEdge]] = []
        for i, source in enumerate(points):
            row = []
            for j, target in enumerate(points):
                factor = 0 if i == j else (1.22 if mode == TransportMode.walking else 1.35)
                distance = haversine_meters(source, target) * factor
                row.append(
                    RouteEdge(
                        origin_index=i,
                        destination_index=j,
                        distance_meters=distance,
                        duration_seconds=distance / speed,
                        source="haversine_estimate",
                        quality=DataQuality.estimated,
                        traffic_timestamp=None,
                        confidence=1.0 if i == j else 0.45,
                        fallback_used=i != j,
                    )
                )
            edges.append(row)
        return RouteMatrix(edges=edges, provider=self.name, generated_at=now)


class AMapProvider(MockMapProvider):
    name = "amap-v3"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self._semaphore = asyncio.Semaphore(settings.map_max_concurrency)
        self._failure_count = 0
        self._circuit_opened_at: float | None = None
        self._circuit_lock = asyncio.Lock()

    async def _before_request(self) -> None:
        async with self._circuit_lock:
            if self._circuit_opened_at is None:
                return
            elapsed = time.monotonic() - self._circuit_opened_at
            if elapsed >= self.settings.circuit_breaker_recovery_seconds:
                self._circuit_opened_at = None
                self._failure_count = 0
                return
            metrics.increment("mapgo_map_circuit_rejections_total")
            raise UpstreamError(
                "高德地图熔断器已打开",
                {
                    "retry_after_seconds": round(
                        self.settings.circuit_breaker_recovery_seconds - elapsed,
                        2,
                    )
                },
            )

    async def _record_success(self) -> None:
        async with self._circuit_lock:
            self._failure_count = 0
            self._circuit_opened_at = None

    async def _record_failure(self) -> None:
        async with self._circuit_lock:
            self._failure_count += 1
            if self._failure_count >= self.settings.circuit_breaker_failure_threshold:
                self._circuit_opened_at = time.monotonic()
                metrics.increment("mapgo_map_circuit_open_total")

    async def _get(self, path: str, params: dict) -> dict:
        await self._before_request()
        params = {**params, "key": self.settings.amap_web_key}
        last_error: Exception | None = None
        async with self._semaphore:
            for attempt in range(self.settings.upstream_max_retries + 1):
                try:
                    started = time.perf_counter()
                    response = await self.client.get(
                        f"https://restapi.amap.com{path}", params=params
                    )
                    metrics.observe(
                        "mapgo_map_api_latency_seconds",
                        time.perf_counter() - started,
                        {"path": path},
                    )
                    response.raise_for_status()
                    data = response.json()
                    if data.get("status") != "1":
                        raise UpstreamError("高德地图返回业务错误", {"info": data.get("info")})
                    await self._record_success()
                    return data
                except UpstreamError:
                    await self._record_failure()
                    metrics.increment(
                        "mapgo_map_api_errors_total",
                        {"path": path, "type": "business"},
                    )
                    raise
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    metrics.increment(
                        "mapgo_map_api_errors_total",
                        {"path": path, "type": type(exc).__name__},
                    )
                    last_error = exc
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status and status not in (429, 500, 502, 503, 504):
                        break
                    if attempt < self.settings.upstream_max_retries:
                        await asyncio.sleep(0.15 * (2**attempt))
            await self._record_failure()
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
        now = datetime.now(timezone.utc)
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
                        source="amap_place_around_v3",
                        data_updated_at=now,
                        confidence=0.9,
                        district=str(poi.get("adname") or "") or None,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return candidates

    async def route_matrix(self, points: list[Coordinate], mode: TransportMode) -> RouteMatrix:
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
        fallback = await super().route_matrix(points, mode)
        now = datetime.now(timezone.utc)
        edges = [[edge.model_copy(deep=True) for edge in row] for row in fallback.edges]
        for destination_index, response in enumerate(responses):
            for result in response.get("results", []):
                try:
                    origin_index = int(result["origin_id"]) - 1
                    distance = float(result["distance"])
                    duration_raw = result.get("duration")
                    duration = (
                        float(duration_raw)
                        if duration_raw
                        else edges[origin_index][destination_index].duration_seconds
                    )
                    estimated_duration = not bool(duration_raw)
                    edges[origin_index][destination_index] = RouteEdge(
                        origin_index=origin_index,
                        destination_index=destination_index,
                        distance_meters=distance,
                        duration_seconds=duration,
                        source="amap_distance_v3",
                        quality=DataQuality.estimated
                        if estimated_duration
                        else DataQuality.provider,
                        traffic_timestamp=now if mode == TransportMode.driving else None,
                        confidence=0.7 if estimated_duration else 0.9,
                        fallback_used=estimated_duration,
                    )
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
        return RouteMatrix(edges=edges, provider=self.name, generated_at=now)


def build_map_provider(settings: Settings, client: httpx.AsyncClient) -> MapProvider:
    return MockMapProvider() if settings.use_mock_map else AMapProvider(settings, client)
