"""Small, dependency-free geographic helpers (spherical-earth approximations)."""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

EARTH_RADIUS_M = 6_371_000.0
LatLon = Tuple[float, float]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def offset_latlon(lat: float, lon: float, north_m: float, east_m: float) -> LatLon:
    """Move a point by metres north/east (fine for distances of a few km)."""
    dlat = north_m / EARTH_RADIUS_M
    dlon = east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def polyline_length_m(points: Sequence[LatLon]) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))


def interpolate_polyline(points: Sequence[LatLon], fraction: float) -> LatLon:
    """Point at ``fraction`` (0..1) of the way along a polyline, by distance."""
    f = min(1.0, max(0.0, fraction))
    if len(points) == 1 or f <= 0:
        return points[0]
    if f >= 1:
        return points[-1]
    seg = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])]
    total = sum(seg)
    if total == 0:
        return points[0]
    target = f * total
    acc = 0.0
    for (a, b), s in zip(zip(points, points[1:]), seg):
        if acc + s >= target:
            t = 0.0 if s == 0 else (target - acc) / s
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        acc += s
    return points[-1]


def distance_to_polyline_m(lat: float, lon: float, points: Sequence[LatLon]) -> float:
    """Approximate distance from a point to a polyline (local equirectangular projection)."""
    k = math.cos(math.radians(lat))

    def xy(p: LatLon) -> Tuple[float, float]:
        return ((p[1] - lon) * k * 111_320.0, (p[0] - lat) * 110_540.0)

    best = math.inf
    for a, b in zip(points, points[1:]):
        ax, ay = xy(a)
        bx, by = xy(b)
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denom))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def bounding_box(center: LatLon, radius_m: float) -> Tuple[float, float, float, float]:
    """(south, west, north, east)."""
    n, _ = offset_latlon(center[0], center[1], radius_m, 0)
    s, _ = offset_latlon(center[0], center[1], -radius_m, 0)
    _, e = offset_latlon(center[0], center[1], 0, radius_m)
    _, w = offset_latlon(center[0], center[1], 0, -radius_m)
    return s, w, n, e
