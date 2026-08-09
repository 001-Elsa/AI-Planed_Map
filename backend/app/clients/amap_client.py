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
    credential_mode: str

    async def search_poi(
        self, keyword: str, origin: Coordinate, city: str | None
    ) -> list[PoiCandidate]: ...

    async def route_matrix(self, points: list[Coordinate], mode: TransportMode) -> RouteMatrix: ...

    async def transit_route_edges(
        self, points: list[Coordinate], city: str | None
    ) -> list[RouteEdge]: ...


def haversine_meters(a: Coordinate, b: Coordinate) -> float:
    radius = 6_371_000
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


POI_TYPE_KEYWORDS = (
    ("充电", "011100"),
    ("加油", "010100"),
    ("火锅|烧烤|餐厅|饭店|美食|小吃|咖啡|奶茶|甜品|蛋糕|面包|面馆|快餐", "050000"),
    ("超市", "060400"),
    ("便利店", "060200"),
    ("商场|购物|书店|花店|水果|服装|数码", "060000"),
    ("快递|驿站|理发|洗衣|维修|家政", "070000"),
    ("电影院|健身|体育馆|游乐|剧院", "080000"),
    ("药店", "090601"),
    ("诊所", "090200"),
    ("医院|急救", "090100"),
    ("酒店|宾馆|民宿|旅馆|青旅", "100000"),
    ("公园|景点|景区|寺庙|动物园|植物园", "110000"),
    ("小区|住宅|写字楼|商务楼", "120000"),
    ("学校|大学|中学|小学|培训", "140000"),
    ("停车", "150900"),
    ("地铁|车站|火车站|机场|公交站", "150000"),
    ("银行|ATM", "160000"),
    ("公司|工厂|园区", "170000"),
    ("厕所|卫生间|公厕", "200300"),
)


POI_CATEGORY_FILLERS = (
    "请帮我",
    "帮我",
    "帮忙",
    "我想要",
    "我想",
    "我要",
    "想要",
    "想吃",
    "想喝",
    "哪里有",
    "哪有",
    "附近",
    "周边",
    "最近",
    "推荐",
    "搜索",
    "查找",
    "找一下",
    "搜一下",
    "看一下",
    "买药",
    "吃饭",
    "一下",
    "一家",
    "一个",
    "去",
    "找",
    "搜",
    "查",
)


def _matched_poi_type(keyword: str) -> tuple[str, str] | None:
    normalized = keyword.lower()
    for words, type_code in POI_TYPE_KEYWORDS:
        for word in words.split("|"):
            if word.lower() in normalized:
                return word, type_code
    return None


def infer_poi_type(keyword: str) -> str | None:
    match = _matched_poi_type(keyword)
    if not match:
        return None
    category, type_code = match
    reduced = keyword.lower()
    for filler in POI_CATEGORY_FILLERS:
        reduced = reduced.replace(filler, "")
    reduced = "".join(char for char in reduced if char.isalnum())
    return type_code if reduced == category.lower() else None


def normalize_poi_keyword(keyword: str) -> str:
    if infer_poi_type(keyword):
        match = _matched_poi_type(keyword)
        if match:
            return match[0]
    return keyword.strip()


def _compact_poi_text(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def _poi_name_match_bucket(keyword: str, candidate: PoiCandidate) -> tuple[int, float]:
    query = _compact_poi_text(keyword)
    name = _compact_poi_text(candidate.name)
    address = _compact_poi_text(candidate.address or "")
    if not query or not name:
        return (4, 0.0)
    if name == query:
        bucket = 0
    elif name.startswith(query):
        bucket = 1
    elif query in name:
        bucket = 2
    elif query in address:
        bucket = 3
    else:
        bucket = 4
    overlap = len(set(query) & set(name)) / max(1, len(set(query)))
    return (bucket, overlap)


class MockMapProvider:
    """Deterministic provider for tests. Every estimate is explicitly labelled."""

    name = "mock-map-v2"
    credential_mode = "mock"

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

    async def transit_route_edges(
        self, points: list[Coordinate], city: str | None
    ) -> list[RouteEdge]:
        del city
        matrix = await self.route_matrix(points, TransportMode.transit)
        return [
            matrix.edges[index][index + 1].model_copy(
                update={
                    "origin_index": index,
                    "destination_index": index + 1,
                    "source": "transit_network_estimate",
                }
            )
            for index in range(len(points) - 1)
        ]


class AMapProvider(MockMapProvider):
    name = "amap-v3"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.credential_mode = "web_service_key" if settings.amap_web_key else "js_api_proxy"
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
        if self.settings.amap_web_key:
            credential_params = {"key": self.settings.amap_web_key}
        else:
            credential_params = {
                "key": self.settings.amap_key,
                "jscode": self.settings.amap_jscode,
                "platform": "JS",
                "s": "rsv3",
                "sdkversion": "2.3.5.6",
            }
        params = {**params, **credential_params}
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
        type_code = infer_poi_type(keyword)
        search_keyword = normalize_poi_keyword(keyword)
        primary_error: UpstreamError | None = None
        if type_code:
            try:
                data = await self._get(
                    "/v3/place/around",
                    {
                        "keywords": search_keyword,
                        "types": type_code,
                        "location": f"{origin.lng},{origin.lat}",
                        "city": city or "",
                        "radius": 10_000,
                        "sortrule": "distance",
                        "offset": 10,
                        "extensions": "all",
                    },
                )
            except UpstreamError as exc:
                primary_error = exc
                data = {}
            rows = data.get("pois", [])
            source = "amap_place_around_v3"
            confidence = 0.9
        else:
            try:
                data = await self._get(
                    "/v3/assistant/inputtips",
                    {
                        "keywords": search_keyword,
                        "city": city or "",
                        "citylimit": "false",
                        "datatype": "poi",
                        "location": f"{origin.lng},{origin.lat}",
                    },
                )
            except UpstreamError as exc:
                primary_error = exc
                data = {}
            rows = data.get("tips", [])
            source = "amap_inputtips_v3"
            confidence = 0.82

        # Input tips and nearby search have different indexes. A valid place name
        # must not become "not found" merely because one index returned no rows.
        # Text search is city-aware but not city-limited, so it also supports
        # nationwide same-name choices.
        if not rows:
            try:
                fallback_data = await self._get(
                    "/v3/place/text",
                    {
                        "keywords": search_keyword,
                        "types": type_code or "",
                        "city": city or "全国",
                        "citylimit": "false",
                        "offset": 10,
                        "page": 1,
                        "extensions": "all",
                    },
                )
                rows = fallback_data.get("pois", [])
                source = "amap_place_text_v3"
                confidence = 0.86 if not type_code else 0.88
                metrics.increment("mapgo_poi_search_fallback_total", {"kind": "text"})
            except UpstreamError as fallback_error:
                if primary_error is not None:
                    raise primary_error from fallback_error
                raise
        now = datetime.now(timezone.utc)
        candidates: list[PoiCandidate] = []
        for poi in rows:
            try:
                lng, lat = map(float, poi["location"].split(","))
                rating = poi.get("biz_ext", {}).get("rating") if poi.get("biz_ext") else None
                point = Coordinate(lng=lng, lat=lat)
                distance_raw = poi.get("distance")
                distance = (
                    haversine_meters(origin, point)
                    if distance_raw in (None, "", [])
                    else float(str(distance_raw))
                )
                candidates.append(
                    PoiCandidate(
                        id=str(poi["id"]),
                        name=str(poi["name"]),
                        address=str(poi.get("address") or ""),
                        location=point,
                        rating=float(str(rating)) if rating not in (None, []) else None,
                        distance_meters=distance,
                        source=source,
                        data_updated_at=now,
                        confidence=confidence,
                        district=str(poi.get("adname") or poi.get("district") or "") or None,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        unique: dict[str, PoiCandidate] = {}
        for candidate in candidates:
            key = candidate.id or (
                f"{candidate.name}:{candidate.location.lng:.6f}:{candidate.location.lat:.6f}"
            )
            unique.setdefault(key, candidate)
        unique_candidates = list(unique.values())
        strong_named_matches = [
            item for item in unique_candidates if _poi_name_match_bucket(search_keyword, item)[0] <= 1
        ]
        if strong_named_matches and not type_code:
            unique_candidates = strong_named_matches
        return sorted(
            unique_candidates,
            key=lambda item: (
                _poi_name_match_bucket(search_keyword, item)[0],
                -_poi_name_match_bucket(search_keyword, item)[1],
                item.distance_meters or 0,
            ),
        )[:5]

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

        responses = await asyncio.gather(
            *(to_destination(point) for point in points),
            return_exceptions=True,
        )
        fallback = await super().route_matrix(points, mode)
        now = datetime.now(timezone.utc)
        edges = [[edge.model_copy(deep=True) for edge in row] for row in fallback.edges]
        for destination_index, response in enumerate(responses):
            if not isinstance(response, dict):
                metrics.increment(
                    "mapgo_route_matrix_partial_fallback_total",
                    {"mode": mode.value},
                )
                continue
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

    async def transit_route_edges(
        self, points: list[Coordinate], city: str | None
    ) -> list[RouteEdge]:
        """Verify only the selected public-transit legs with AMap.

        Candidate ordering still uses the same scalable joint optimizer. Querying
        all candidate pairs through the transit API would grow quadratically, so
        the selected sequence is refined with real transfer results in O(stops).
        """
        fallback_matrix = await super().route_matrix(points, TransportMode.transit)
        now = datetime.now(timezone.utc)

        async def verify(index: int) -> RouteEdge:
            fallback = fallback_matrix.edges[index][index + 1].model_copy(
                update={
                    "origin_index": index,
                    "destination_index": index + 1,
                    "source": "transit_network_estimate",
                }
            )
            source = points[index]
            destination = points[index + 1]
            try:
                data = await self._get(
                    "/v3/direction/transit/integrated",
                    {
                        "origin": f"{source.lng},{source.lat}",
                        "destination": f"{destination.lng},{destination.lat}",
                        "city": city or "全国",
                        "cityd": city or "",
                        "strategy": "0",
                        "extensions": "base",
                    },
                )
                route = data.get("route") or {}
                transit = (route.get("transits") or [None])[0]
                if not isinstance(transit, dict):
                    return fallback
                distance = float(transit.get("distance") or fallback.distance_meters)
                duration = float(transit.get("duration") or fallback.duration_seconds)
                return RouteEdge(
                    origin_index=index,
                    destination_index=index + 1,
                    distance_meters=distance,
                    duration_seconds=duration,
                    source="amap_transit_integrated_v3",
                    quality=DataQuality.provider,
                    traffic_timestamp=now,
                    confidence=0.9,
                    fallback_used=False,
                )
            except (UpstreamError, TypeError, ValueError):
                metrics.increment("mapgo_transit_leg_fallback_total")
                return fallback

        return list(await asyncio.gather(*(verify(index) for index in range(len(points) - 1))))


def build_map_provider(settings: Settings, client: httpx.AsyncClient) -> MapProvider:
    return MockMapProvider() if settings.use_mock_map else AMapProvider(settings, client)
