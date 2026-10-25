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
from __future__ import annotations  # modern type hints on older Python

import logging  # log warnings
from typing import Callable, Optional, Tuple  # type hints

import networkx as nx  # graph library

from config import settings  # defaults and attribution
from geo import geographic_utils as geo  # distance helpers
from geo import network_cache as cache  # on-disk cache
from geo.synthetic_network import build_synthetic_network  # the fake grid fallback
from simulation.models import Edge, Node, RoadNetwork  # network building blocks

log = logging.getLogger(__name__)  # logger for this module
StatusCb = Optional[Callable[[str], None]]  # optional function that receives progress messages

# Assumed effective width factor per OSM "highway" value (persons per metre = 0.25 * factor).
WIDTH_FACTOR = {"footway": 0.6, "path": 0.5, "pedestrian": 1.5, "steps": 0.3, "cycleway": 0.6, "service": 0.8,  # narrow and special roads
                "residential": 1.0, "living_street": 1.0, "unclassified": 1.0, "tertiary": 1.3,  # ordinary roads
                "secondary": 1.6, "primary": 2.0, "trunk": 2.0, "track": 0.6}  # wide roads and tracks
PERSONS_PER_METER = 0.25  # how many people fit per metre of a standard road


class NetworkLoadError(RuntimeError):  # a map problem with a message the user can read
    """Raised with a user-readable message when real map data cannot be obtained."""


def osm_capacity(highway: str, length_m: float) -> int:  # capacity guess for a road
    return max(2, round(length_m * PERSONS_PER_METER * WIDTH_FACTOR.get(highway, 1.0)))  # length x density x width, at least 2 people


def osmnx_available() -> bool:  # is OSMnx installed and working?
    try:  # importing may fail
        import osmnx  # noqa: F401
        return True  # it works
    except Exception:  # ImportError or broken geo stack: report as unavailable, details are logged
        log.info("osmnx not importable", exc_info=True)  # log why
        return False  # not available


def _first(value):  # OSM sometimes gives a list where one value is expected
    return value[0] if isinstance(value, list) else value  # take the first item of a list


def graph_to_network(g: nx.MultiDiGraph, name: str, center: Tuple[float, float], radius_m: float) -> RoadNetwork:  # convert an OSMnx graph
    """Convert an OSMnx graph, retaining the largest weakly connected component.

    Strong-component filtering is too aggressive for pedestrian networks: it can
    discard valid cul-de-sacs and one-way foot links because not every node can
    reach every other node in both directions. Preserve the largest weak component
    and let the router handle remaining directional reachability constraints.
    """
    comps = list(nx.weakly_connected_components(g))  # groups of connected nodes
    if not comps:  # empty graph
        raise NetworkLoadError("The downloaded graph is empty.")  # nothing to convert
    keep = max(comps, key=len)  # the biggest group
    dropped = g.number_of_nodes() - len(keep)  # nodes outside it
    if dropped:  # some were dropped
        log.warning("Dropped %d nodes outside the largest weakly connected component", dropped)  # say so
    net = RoadNetwork(name, "OSM", center=center, radius_m=radius_m,  # create the network (part 1)
                      metadata={"dropped_disconnected_nodes": dropped, "component_policy": "largest weakly connected component", "attribution": settings.OSM_ATTRIBUTION,  # notes (1)
                                "capacity_note": "capacity is a heuristic from road class and length (assumption)"})  # notes (2)
    idmap = {}  # OSM node id -> our node id
    for i, osm_id in enumerate(sorted(keep), start=1):  # number the kept nodes from 1
        d = g.nodes[osm_id]  # the node's data
        try:  # coordinates may be missing
            net.add_node(Node(i, float(d["y"]), float(d["x"]), int(osm_id)))  # y = latitude, x = longitude
        except (KeyError, ValueError) as exc:  # bad data
            raise NetworkLoadError(f"Node {osm_id} lacks coordinates") from exc  # report it
        idmap[osm_id] = i  # remember the mapping
    eid = 1  # next road id
    for u, v, _k, data in g.edges(keys=True, data=True):  # every OSM road
        if u not in keep or v not in keep or u == v:  # outside the kept group, or a loop onto itself
            continue  # skip it
        a, b = net.nodes[idmap[u]], net.nodes[idmap[v]]  # its end nodes
        geom = None  # the road's shape
        if "geometry" in data and data["geometry"] is not None:  # it has a curved shape
            geom = [(lat, lon) for lon, lat in data["geometry"].coords]  # OSM stores (lon, lat); we use (lat, lon)
        length = float(data.get("length") or geo.haversine_m(a.lat, a.lon, b.lat, b.lon))  # stored length, or measured between the ends
        if length <= 0:  # degenerate road
            continue  # skip it
        hw = _first(data.get("highway", "residential"))  # road class
        way = _first(data.get("osmid"))  # OSM way id
        net.add_edge(Edge(eid, idmap[u], idmap[v], length, length / settings.WALKING_SPEED_MPS,  # add the road (part 1): id, ends, length, time
                          osm_capacity(str(hw), length), str(hw), True,  # (part 2): capacity, class, available
                          int(way) if isinstance(way, (int, float, str)) and str(way).isdigit() else None, geom))  # (part 3): way id, shape
        eid += 1  # next id
    return net  # the converted network


def download_osm_network(lat: float, lon: float, radius_m: float, network_type: str = settings.NETWORK_TYPE,
                         status: StatusCb = None) -> str:  # download and cache a network
    """Download via OSMnx and write the GraphML cache. Returns the cache key."""
    say = status or (lambda m: log.info(m))  # where progress messages go
    if not osmnx_available():  # library missing
        raise NetworkLoadError("OSMnx is not installed. Run: python -m pip install osmnx  (or use cached/synthetic data).")  # tell the user how to fix it
    import osmnx as ox  # imported late because it is optional
    settings.ensure_directories()  # make sure the cache folder exists
    ox.settings.use_cache = True  # reuse HTTP responses
    ox.settings.cache_folder = str(settings.CACHE_DIR / "osmnx_http")  # where OSMnx caches them
    ox.settings.requests_timeout = 90  # give slow servers 90 seconds
    try:  # the setting differs between versions
        ox.settings.http_headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}  # identify ourselves politely
    except Exception:  # attribute differs between osmnx versions; the default UA is acceptable
        log.debug("could not set custom User-Agent", exc_info=True)  # note it
    key = cache.cache_key(lat, lon, radius_m, network_type)  # cache key for this request
    say(f"Downloading OpenStreetMap '{network_type}' network ({int(radius_m)} m around {lat:.5f}, {lon:.5f})...")  # progress message
    try:  # the download can fail
        g = ox.graph_from_point((lat, lon), dist=radius_m, network_type=network_type, simplify=True)  # fetch the roads
    except Exception as exc:  # network or server problem
        raise NetworkLoadError(  # explain it
            f"Download failed ({type(exc).__name__}: {exc}). Check the internet connection / Overpass availability, "  # message (1)
            "try a smaller radius, or use cached/synthetic data.") from exc  # message (2)
    if g.number_of_nodes() == 0 or g.number_of_edges() == 0:  # nothing came back
        raise NetworkLoadError("OpenStreetMap returned no roads for this area; increase the radius or move the centre.")  # explain it
    say("Saving GraphML cache...")  # progress message
    ox.save_graphml(g, cache.graphml_path(key))  # write the graph file
    cache.write_meta(key, lat, lon, radius_m, network_type, g.number_of_nodes(), g.number_of_edges())  # write the description file
    return key  # the cache key


def load_cached_network(key: str) -> RoadNetwork:  # read a cached network
    """Read a GraphML cache entry (needs osmnx only for convenient type parsing)."""
    if not cache.is_cached(key):  # missing
        raise NetworkLoadError(f"No cache entry {key!r}.")  # report it
    import json  # imported here because it is only needed here
    meta = json.loads(cache.meta_path(key).read_text(encoding="utf-8"))  # the description file
    if osmnx_available():  # best reader
        import osmnx as ox  # the library
        g = ox.load_graphml(cache.graphml_path(key))  # parses types and geometry
    else:  # fallback reader
        g = _load_graphml_plain(cache.graphml_path(key))  # no curved shapes
    name = f"OpenStreetMap ({meta['network_type']}, r={int(meta['radius_m'])} m)"  # display name
    return graph_to_network(g, name, (meta["lat"], meta["lon"]), meta["radius_m"])  # convert to our network


def _load_graphml_plain(path) -> nx.MultiDiGraph:  # read GraphML without OSMnx
    """Fallback reader without osmnx (loses edge geometries; roads are drawn as node-to-node segments)."""
    g = nx.read_graphml(path)  # read the file
    g = nx.MultiDiGraph(g)  # make sure it is a directed multigraph
    for n, d in g.nodes(data=True):  # every node
        d["x"], d["y"] = float(d["x"]), float(d["y"])  # coordinates as numbers (GraphML stores text)
    for _u, _v, d in g.edges(data=True):  # every road
        d["length"] = float(d.get("length", 0) or 0)  # length as a number
        d.pop("geometry", None)  # the shape cannot be parsed without OSMnx
    return nx.relabel_nodes(g, {n: int(n) for n in g.nodes})  # node ids as integers


def get_network(lat: float = settings.CENTER_LAT, lon: float = settings.CENTER_LON,
                radius_m: float = settings.DEFAULT_RADIUS_M, network_type: str = settings.NETWORK_TYPE,
                allow_download: bool = False, status: StatusCb = None) -> Tuple[RoadNetwork, str]:  # best available network
    """Best available network and a message describing where it came from.

    Order: cached GraphML -> (optional, explicit) download -> synthetic fallback.
    """
    key = cache.find_cached(lat, lon, radius_m, network_type)  # is it cached?
    if key is None and allow_download:  # not cached, but a download is allowed
        try:  # downloads can fail
            key = download_osm_network(lat, lon, radius_m, network_type, status)  # fetch and cache
        except NetworkLoadError as exc:  # failed
            log.warning("OSM download unavailable: %s", exc)  # log it
            net = build_synthetic_network()  # use the fake grid instead
            return net, f"SYNTHETIC FALLBACK in use - {exc}"  # and say why
    if key is not None:  # a cache entry exists
        try:  # reading can fail
            return load_cached_network(key), f"Real OpenStreetMap data (cache {key}). {settings.OSM_ATTRIBUTION}"  # the real map and its credit
        except Exception as exc:  # unreadable cache
            log.exception("Cached network %s is unreadable", key)  # log it
            return build_synthetic_network(), f"SYNTHETIC FALLBACK in use - cache unreadable ({exc})"  # fall back
    return build_synthetic_network(), "SYNTHETIC FALLBACK in use - no cached OpenStreetMap data (download is optional)."  # nothing cached


def geocode_place(query: str) -> Tuple[float, float]:  # turn a place name into coordinates
    """One-off Nominatim lookup (user-triggered; respects the 1 request/s policy by being manual)."""
    if not osmnx_available():  # needs the library
        raise NetworkLoadError("OSMnx is required for geocoding.")  # report it
    import osmnx as ox  # the library
    try:  # lookups can fail
        return tuple(ox.geocode(query))  # (latitude, longitude)
    except Exception as exc:  # not found or network error
        raise NetworkLoadError(f"Geocoding failed for {query!r}: {exc}") from exc  # report it


# ------------------------------------------------------------ building footprints
def _buildings_path(lat: float, lon: float, radius_m: float):  # cache file for building shapes
    return settings.CACHE_DIR / f"buildings_{cache.cache_key(lat, lon, radius_m, 'bld')}.json"  # keyed like the road cache


def load_buildings(lat: float, lon: float, radius_m: float, download: bool = False) -> list:  # building footprints for the 3D view
    """OSM building footprints [{polygon:[[lon,lat],...], height}] from cache, optionally downloading once.

    Decoration for the 3-D view only. Returns [] when unavailable (never raises for network problems).
    """
    import json  # imported here because it is only needed here
    path = _buildings_path(lat, lon, radius_m)  # cache file
    if path.is_file():  # cached
        try:  # the file may be damaged
            return json.loads(path.read_text(encoding="utf-8"))  # use it
        except (OSError, ValueError):  # unreadable
            log.warning("unreadable building cache %s", path, exc_info=True)  # log it
    if not download or not osmnx_available():  # no download wanted or possible
        return []  # no buildings
    import osmnx as ox  # the library
    try:  # downloads can fail
        gdf = ox.features_from_point((lat, lon), tags={"building": True}, dist=radius_m)  # fetch building shapes
    except Exception as exc:  # network problem
        log.warning("building download failed: %s", exc)  # log it
        return []  # decoration only, so just skip it
    out = []  # the converted buildings
    for _, row in gdf.iterrows():  # each feature
        geom = row.geometry  # its shape
        parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom] if geom.geom_type == "Polygon" else []  # split multi-part shapes; ignore non-polygons
        try:  # height from the number of levels
            lv = float(str(row.get("building:levels", "")).split(";")[0])  # first level count
            h = max(4.0, lv * 3.2)  # about 3.2 m per level, at least 4 m
        except ValueError:  # no usable level count
            h = 5.0 + (abs(hash(str(row.name))) % 12)  # a varied default height between 5 and 16 m
        for p in parts:  # each polygon
            out.append({"polygon": [[x, y] for x, y in p.exterior.coords], "height": h})  # outline as [lon, lat] pairs with a height
    settings.ensure_directories()  # make sure the cache folder exists
    path.write_text(json.dumps(out), encoding="utf-8")  # save for next time
    return out  # the buildings
