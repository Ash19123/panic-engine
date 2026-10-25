"""Local GraphML cache for downloaded OpenStreetMap networks.

A cache entry is keyed by (rounded centre, radius, network type), so the same
request never hits the Overpass servers twice and previously cached areas work offline.
"""
from __future__ import annotations  # modern type hints on older Python

import hashlib  # short stable hash for cache keys
import json  # read and write the metadata files
import logging  # warnings about bad cache files
import time  # timestamp of the download
from pathlib import Path  # file paths
from typing import List, Optional  # type hints

from config import settings  # cache folder and attribution text

log = logging.getLogger(__name__)  # logger for this module


def cache_key(lat: float, lon: float, radius_m: float, network_type: str) -> str:  # stable id for one download request
    raw = f"{lat:.5f}|{lon:.5f}|{int(radius_m)}|{network_type}"  # centre rounded to 5 decimals, radius, type
    return hashlib.sha1(raw.encode()).hexdigest()[:12]  # first 12 characters of the SHA-1 hash


def graphml_path(key: str) -> Path:  # where the road graph file lives
    return settings.CACHE_DIR / f"osm_{key}.graphml"  # GraphML file inside the cache folder


def meta_path(key: str) -> Path:  # where the description file lives
    return settings.CACHE_DIR / f"osm_{key}.json"  # JSON file next to the graph


def is_cached(key: str) -> bool:  # is this request already downloaded?
    return graphml_path(key).is_file() and meta_path(key).is_file()  # both files must exist


def write_meta(key: str, lat: float, lon: float, radius_m: float, network_type: str, nodes: int, edges: int) -> None:  # save the description
    settings.ensure_directories()  # make sure the cache folder exists
    meta_path(key).write_text(json.dumps({  # write the JSON file
        "key": key, "lat": lat, "lon": lon, "radius_m": radius_m, "network_type": network_type,  # request details
        "nodes": nodes, "edges": edges, "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),  # size and download time
        "attribution": settings.OSM_ATTRIBUTION}, indent=2), encoding="utf-8")  # required OpenStreetMap credit


def list_cached() -> List[dict]:  # describe every cached area
    out = []  # collected descriptions
    if settings.CACHE_DIR.is_dir():  # only if the cache folder exists
        for p in sorted(settings.CACHE_DIR.glob("osm_*.json")):  # every metadata file in a fixed order
            try:  # the file may be damaged
                meta = json.loads(p.read_text(encoding="utf-8"))  # parse it
            except (OSError, json.JSONDecodeError):  # unreadable or invalid JSON
                log.warning("Ignoring unreadable cache metadata %s", p)  # tell the log
                continue  # skip this entry
            if graphml_path(meta["key"]).is_file():  # the graph file must be present too
                out.append(meta)  # keep this entry
    return out  # all valid entries


def find_cached(lat: float, lon: float, radius_m: float, network_type: str) -> Optional[str]:  # look up a request in the cache
    key = cache_key(lat, lon, radius_m, network_type)  # the request's key
    return key if is_cached(key) else None  # the key if cached, otherwise None
