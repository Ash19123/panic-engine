"""Local GraphML cache for downloaded OpenStreetMap networks.

A cache entry is keyed by (rounded centre, radius, network type), so the same
request never hits the Overpass servers twice and previously cached areas work offline.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from config import settings

log = logging.getLogger(__name__)


def cache_key(lat: float, lon: float, radius_m: float, network_type: str) -> str:
    raw = f"{lat:.5f}|{lon:.5f}|{int(radius_m)}|{network_type}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def graphml_path(key: str) -> Path:
    return settings.CACHE_DIR / f"osm_{key}.graphml"


def meta_path(key: str) -> Path:
    return settings.CACHE_DIR / f"osm_{key}.json"


def is_cached(key: str) -> bool:
    return graphml_path(key).is_file() and meta_path(key).is_file()


def write_meta(key: str, lat: float, lon: float, radius_m: float, network_type: str, nodes: int, edges: int) -> None:
    settings.ensure_directories()
    meta_path(key).write_text(json.dumps({
        "key": key, "lat": lat, "lon": lon, "radius_m": radius_m, "network_type": network_type,
        "nodes": nodes, "edges": edges, "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "attribution": settings.OSM_ATTRIBUTION}, indent=2), encoding="utf-8")


def list_cached() -> List[dict]:
    out = []
    if settings.CACHE_DIR.is_dir():
        for p in sorted(settings.CACHE_DIR.glob("osm_*.json")):
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("Ignoring unreadable cache metadata %s", p)
                continue
            if graphml_path(meta["key"]).is_file():
                out.append(meta)
    return out


def find_cached(lat: float, lon: float, radius_m: float, network_type: str) -> Optional[str]:
    key = cache_key(lat, lon, radius_m, network_type)
    return key if is_cached(key) else None
