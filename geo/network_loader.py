"""OpenStreetMap network ingestion (OSMnx) with caching and a synthetic fallback.

* OSMnx is an OPTIONAL dependency, imported lazily; without it (or without
  internet) the app uses cached GraphML files or the labelled synthetic network.
* Downloading happens only when explicitly requested; results are cached in
  ``data/cache`` as GraphML and never re-downloaded for the same request.
* Capacity is NOT in OpenStreetMap. It is a heuristic from road class and
  length (see ``osm_capacity``) and is labelled as an assumption everywhere.
* Campus gates, building exits and shelters are NOT assumed to exist in OSM:
  destinations must be placed by the user and are flagged hypothetical.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional, Tuple

import networkx as nx

from config import settings
from geo import geographic_utils as geo
from geo import network_cache as cache
from geo.synthetic_network import build_synthetic_network
from simulation.models import Edge, Node, RoadNetwork

log = logging.getLogger(__name__)
StatusCb = Optional[Callable[[str], None]]

# Assumed effective width factor per OSM "highway" value (persons per metre = 0.25 * factor).
WIDTH_FACTOR = {"footway": 0.6, "path": 0.5, "pedestrian": 1.5, "steps": 0.3, "cycleway": 0.6, "service": 0.8,
                "residential": 1.0, "living_street": 1.0, "unclassified": 1.0, "tertiary": 1.3,
                "secondary": 1.6, "primary": 2.0, "trunk": 2.0, "track": 0.6}
PERSONS_PER_METER = 0.25


class NetworkLoadError(RuntimeError):
    """Raised with a user-readable message when real map data cannot be obtained."""


def osm_capacity(highway: str, length_m: float) -> int:
    return max(2, round(length_m * PERSONS_PER_METER * WIDTH_FACTOR.get(highway, 1.0)))


def osmnx_available() -> bool:
    try:
        import osmnx  # noqa: F401
        return True
    except Exception:  # ImportError or broken geo stack: report as unavailable, details are logged
        log.info("osmnx not importable", exc_info=True)
        return False


def _first(value):
    return value[0] if isinstance(value, list) else value


def graph_to_network(g: nx.MultiDiGraph, name: str, center: Tuple[float, float], radius_m: float) -> RoadNetwork:
    """Convert an OSMnx graph, retaining the largest weakly connected component.

    Strong-component filtering is too aggressive for pedestrian networks: it can
    discard valid cul-de-sacs and one-way foot links because not every node can
    reach every other node in both directions. Preserve the largest weak component
    and let the router handle remaining directional reachability constraints.
    """
    comps = list(nx.weakly_connected_components(g))
    if not comps:
        raise NetworkLoadError("The downloaded graph is empty.")
    keep = max(comps, key=len)
    dropped = g.number_of_nodes() - len(keep)
    if dropped:
        log.warning("Dropped %d nodes outside the largest weakly connected component", dropped)
    net = RoadNetwork(name, "OSM", center=center, radius_m=radius_m,
                      metadata={"dropped_disconnected_nodes": dropped, "component_policy": "largest weakly connected component", "attribution": settings.OSM_ATTRIBUTION,
                                "capacity_note": "capacity is a heuristic from road class and length (assumption)"})
    idmap = {}
    for i, osm_id in enumerate(sorted(keep), start=1):
        d = g.nodes[osm_id]
        try:
            net.add_node(Node(i, float(d["y"]), float(d["x"]), int(osm_id)))
        except (KeyError, ValueError) as exc:
            raise NetworkLoadError(f"Node {osm_id} lacks coordinates") from exc
        idmap[osm_id] = i
    eid = 1
    for u, v, _k, data in g.edges(keys=True, data=True):
        if u not in keep or v not in keep or u == v:
            continue
        a, b = net.nodes[idmap[u]], net.nodes[idmap[v]]
        geom = None
        if "geometry" in data and data["geometry"] is not None:
            geom = [(lat, lon) for lon, lat in data["geometry"].coords]
        length = float(data.get("length") or geo.haversine_m(a.lat, a.lon, b.lat, b.lon))
        if length <= 0:
            continue
        hw = _first(data.get("highway", "residential"))
        way = _first(data.get("osmid"))
        net.add_edge(Edge(eid, idmap[u], idmap[v], length, length / settings.WALKING_SPEED_MPS,
                          osm_capacity(str(hw), length), str(hw), True,
                          int(way) if isinstance(way, (int, float, str)) and str(way).isdigit() else None, geom))
        eid += 1
    return net


def download_osm_network(lat: float, lon: float, radius_m: float, network_type: str = settings.NETWORK_TYPE,
                         status: StatusCb = None) -> str:
    """Download via OSMnx and write the GraphML cache. Returns the cache key."""
    say = status or (lambda m: log.info(m))
    if not osmnx_available():
        raise NetworkLoadError("OSMnx is not installed. Run: python -m pip install osmnx  (or use cached/synthetic data).")
    import osmnx as ox
    settings.ensure_directories()
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(settings.CACHE_DIR / "osmnx_http")
    ox.settings.requests_timeout = 90
    try:
        ox.settings.http_headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}
    except Exception:  # attribute differs between osmnx versions; the default UA is acceptable
        log.debug("could not set custom User-Agent", exc_info=True)
    key = cache.cache_key(lat, lon, radius_m, network_type)
    say(f"Downloading OpenStreetMap '{network_type}' network ({int(radius_m)} m around {lat:.5f}, {lon:.5f})...")
    try:
        g = ox.graph_from_point((lat, lon), dist=radius_m, network_type=network_type, simplify=True)
    except Exception as exc:
        raise NetworkLoadError(
            f"Download failed ({type(exc).__name__}: {exc}). Check the internet connection / Overpass availability, "
            "try a smaller radius, or use cached/synthetic data.") from exc
    if g.number_of_nodes() == 0 or g.number_of_edges() == 0:
        raise NetworkLoadError("OpenStreetMap returned no roads for this area; increase the radius or move the centre.")
    say("Saving GraphML cache...")
    ox.save_graphml(g, cache.graphml_path(key))
    cache.write_meta(key, lat, lon, radius_m, network_type, g.number_of_nodes(), g.number_of_edges())
    return key


def load_cached_network(key: str) -> RoadNetwork:
    """Read a GraphML cache entry (needs osmnx only for convenient type parsing)."""
    if not cache.is_cached(key):
        raise NetworkLoadError(f"No cache entry {key!r}.")
    import json
    meta = json.loads(cache.meta_path(key).read_text(encoding="utf-8"))
    if osmnx_available():
        import osmnx as ox
        g = ox.load_graphml(cache.graphml_path(key))
    else:
        g = _load_graphml_plain(cache.graphml_path(key))
    name = f"OpenStreetMap ({meta['network_type']}, r={int(meta['radius_m'])} m)"
    return graph_to_network(g, name, (meta["lat"], meta["lon"]), meta["radius_m"])


def _load_graphml_plain(path) -> nx.MultiDiGraph:
    """Fallback reader without osmnx (loses edge geometries; roads are drawn as node-to-node segments)."""
    g = nx.read_graphml(path)
    g = nx.MultiDiGraph(g)
    for n, d in g.nodes(data=True):
        d["x"], d["y"] = float(d["x"]), float(d["y"])
    for _u, _v, d in g.edges(data=True):
        d["length"] = float(d.get("length", 0) or 0)
        d.pop("geometry", None)
    return nx.relabel_nodes(g, {n: int(n) for n in g.nodes})


def get_network(lat: float = settings.CENTER_LAT, lon: float = settings.CENTER_LON,
                radius_m: float = settings.DEFAULT_RADIUS_M, network_type: str = settings.NETWORK_TYPE,
                allow_download: bool = False, status: StatusCb = None) -> Tuple[RoadNetwork, str]:
    """Best available network and a message describing where it came from.

    Order: cached GraphML -> (optional, explicit) download -> synthetic fallback.
    """
    key = cache.find_cached(lat, lon, radius_m, network_type)
    if key is None and allow_download:
        try:
            key = download_osm_network(lat, lon, radius_m, network_type, status)
        except NetworkLoadError as exc:
            log.warning("OSM download unavailable: %s", exc)
            net = build_synthetic_network()
            return net, f"SYNTHETIC FALLBACK in use - {exc}"
    if key is not None:
        try:
            return load_cached_network(key), f"Real OpenStreetMap data (cache {key}). {settings.OSM_ATTRIBUTION}"
        except Exception as exc:
            log.exception("Cached network %s is unreadable", key)
            return build_synthetic_network(), f"SYNTHETIC FALLBACK in use - cache unreadable ({exc})"
    return build_synthetic_network(), "SYNTHETIC FALLBACK in use - no cached OpenStreetMap data (download is optional)."


def geocode_place(query: str) -> Tuple[float, float]:
    """One-off Nominatim lookup (user-triggered; respects the 1 request/s policy by being manual)."""
    if not osmnx_available():
        raise NetworkLoadError("OSMnx is required for geocoding.")
    import osmnx as ox
    try:
        return tuple(ox.geocode(query))
    except Exception as exc:
        raise NetworkLoadError(f"Geocoding failed for {query!r}: {exc}") from exc


# ------------------------------------------------------------ building footprints
def _buildings_path(lat: float, lon: float, radius_m: float):
    return settings.CACHE_DIR / f"buildings_{cache.cache_key(lat, lon, radius_m, 'bld')}.json"


def load_buildings(lat: float, lon: float, radius_m: float, download: bool = False) -> list:
    """OSM building footprints [{polygon:[[lon,lat],...], height}] from cache, optionally downloading once.

    Decoration for the 3-D view only. Returns [] when unavailable (never raises for network problems).
    """
    import json
    path = _buildings_path(lat, lon, radius_m)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("unreadable building cache %s", path, exc_info=True)
    if not download or not osmnx_available():
        return []
    import osmnx as ox
    try:
        gdf = ox.features_from_point((lat, lon), tags={"building": True}, dist=radius_m)
    except Exception as exc:
        log.warning("building download failed: %s", exc)
        return []
    out = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom] if geom.geom_type == "Polygon" else []
        try:
            lv = float(str(row.get("building:levels", "")).split(";")[0])
            h = max(4.0, lv * 3.2)
        except ValueError:
            h = 5.0 + (abs(hash(str(row.name))) % 12)
        for p in parts:
            out.append({"polygon": [[x, y] for x, y in p.exterior.coords], "height": h})
    settings.ensure_directories()
    path.write_text(json.dumps(out), encoding="utf-8")
    return out
