"""GPS corridor projection helpers for automatic off-route detection."""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0


def haversine_meters(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _to_xy(lng: float, lat: float, origin_lng: float, origin_lat: float) -> tuple[float, float]:
    x = math.radians(lng - origin_lng) * EARTH_RADIUS_M * math.cos(math.radians(origin_lat))
    y = math.radians(lat - origin_lat) * EARTH_RADIUS_M
    return x, y


@dataclass(frozen=True)
class OffRouteVerdict:
    off_route: bool
    distance_meters: float
    sustained_seconds: float
    reason: str


def distance_to_polyline(
    lng: float,
    lat: float,
    polyline: list[tuple[float, float]],
) -> float:
    """Return minimum distance from a point to a polyline of (lng, lat) vertices."""
    if not polyline:
        return float("inf")
    if len(polyline) == 1:
        return haversine_meters(lng, lat, polyline[0][0], polyline[0][1])

    best = float("inf")
    for (lng1, lat1), (lng2, lat2) in zip(polyline, polyline[1:], strict=False):
        ox, oy = _to_xy(lng, lat, lng1, lat1)
        ax, ay = 0.0, 0.0
        bx, by = _to_xy(lng2, lat2, lng1, lat1)
        abx, aby = bx - ax, by - ay
        ab2 = abx * abx + aby * aby
        if ab2 <= 1e-9:
            dist = math.hypot(ox - ax, oy - ay)
        else:
            t = max(0.0, min(1.0, ((ox - ax) * abx + (oy - ay) * aby) / ab2))
            px, py = ax + t * abx, ay + t * aby
            dist = math.hypot(ox - px, oy - py)
        best = min(best, dist)
    return best


def evaluate_off_route(
    *,
    lng: float,
    lat: float,
    polyline: list[tuple[float, float]],
    threshold_meters: float = 80.0,
    previous_off_route_seconds: float = 0.0,
    sample_interval_seconds: float = 5.0,
    sustain_seconds: float = 20.0,
) -> OffRouteVerdict:
    distance = distance_to_polyline(lng, lat, polyline)
    if distance <= threshold_meters:
        return OffRouteVerdict(
            off_route=False,
            distance_meters=distance,
            sustained_seconds=0.0,
            reason="within_corridor",
        )
    sustained = previous_off_route_seconds + sample_interval_seconds
    return OffRouteVerdict(
        off_route=sustained >= sustain_seconds,
        distance_meters=distance,
        sustained_seconds=sustained,
        reason="beyond_corridor" if sustained >= sustain_seconds else "transient_deviation",
    )


def polyline_from_plan_snapshot(snapshot: dict) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    origin = (snapshot.get("intent") or {}).get("origin_coordinate") or snapshot.get("origin")
    if isinstance(origin, dict) and "lng" in origin and "lat" in origin:
        points.append((float(origin["lng"]), float(origin["lat"])))
    for stop in snapshot.get("stops") or []:
        location = ((stop.get("poi") or {}).get("location")) or {}
        if "lng" in location and "lat" in location:
            points.append((float(location["lng"]), float(location["lat"])))
    return points
