"""Small, dependency-free geographic helpers (spherical-earth approximations)."""
from __future__ import annotations  # modern type hints on older Python

import math  # trigonometry
from typing import List, Sequence, Tuple  # type hints

EARTH_RADIUS_M = 6_371_000.0  # mean Earth radius in metres
LatLon = Tuple[float, float]  # a (latitude, longitude) pair


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:  # great-circle distance in metres
    p1, p2 = math.radians(lat1), math.radians(lat2)  # latitudes in radians
    dphi = p2 - p1  # latitude difference
    dlmb = math.radians(lon2 - lon1)  # longitude difference in radians
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2  # haversine term
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))  # convert to metres (min guards rounding above 1)


def offset_latlon(lat: float, lon: float, north_m: float, east_m: float) -> LatLon:  # shift a point by metres
    """Move a point by metres north/east (fine for distances of a few km)."""
    dlat = north_m / EARTH_RADIUS_M  # north shift as an angle
    dlon = east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat)))  # east shift as an angle (longitude lines converge)
    return lat + math.degrees(dlat), lon + math.degrees(dlon)  # new coordinates in degrees


def polyline_length_m(points: Sequence[LatLon]) -> float:  # total length of a path of points
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))  # add up every segment


def interpolate_polyline(points: Sequence[LatLon], fraction: float) -> LatLon:  # point part-way along a path
    """Point at ``fraction`` (0..1) of the way along a polyline, by distance."""
    f = min(1.0, max(0.0, fraction))  # clamp the fraction into 0..1
    if len(points) == 1 or f <= 0:  # single point or the very start
        return points[0]  # the first point
    if f >= 1:  # the very end
        return points[-1]  # the last point
    seg = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])]  # length of each segment
    total = sum(seg)  # whole length
    if total == 0:  # all points identical
        return points[0]  # nowhere to move
    target = f * total  # distance to walk from the start
    acc = 0.0  # distance covered so far
    for (a, b), s in zip(zip(points, points[1:]), seg):  # each segment with its length
        if acc + s >= target:  # the target lies inside this segment
            t = 0.0 if s == 0 else (target - acc) / s  # fraction inside the segment
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t  # linear blend between its two ends
        acc += s  # move on past this segment
    return points[-1]  # numerical fallback: the end


def distance_to_polyline_m(lat: float, lon: float, points: Sequence[LatLon]) -> float:  # how far a point is from a path
    """Approximate distance from a point to a polyline (local equirectangular projection)."""
    k = math.cos(math.radians(lat))  # shrink longitudes at this latitude

    def xy(p: LatLon) -> Tuple[float, float]:  # convert a point to local metres relative to (lat, lon)
        return ((p[1] - lon) * k * 111_320.0, (p[0] - lat) * 110_540.0)  # east and north in metres

    best = math.inf  # smallest distance found so far
    for a, b in zip(points, points[1:]):  # each segment of the path
        ax, ay = xy(a)  # segment start in metres
        bx, by = xy(b)  # segment end in metres
        dx, dy = bx - ax, by - ay  # segment direction vector
        denom = dx * dx + dy * dy  # squared segment length
        t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denom))  # closest point position on the segment, clamped
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))  # distance to that closest point
    return best  # smallest distance over all segments


def bounding_box(center: LatLon, radius_m: float) -> Tuple[float, float, float, float]:  # square box around a centre
    """(south, west, north, east)."""
    n, _ = offset_latlon(center[0], center[1], radius_m, 0)  # northern edge latitude
    s, _ = offset_latlon(center[0], center[1], -radius_m, 0)  # southern edge latitude
    _, e = offset_latlon(center[0], center[1], 0, radius_m)  # eastern edge longitude
    _, w = offset_latlon(center[0], center[1], 0, -radius_m)  # western edge longitude
    return s, w, n, e  # the four sides
