"""3-D evacuation scene (pydeck / deck.gl): grey extruded buildings, pale roads,
green exit buildings for safe areas, red arenas for danger zones, walking agents.

Pure functions: network + data in, ``pydeck.Deck`` out. Buildings on the synthetic
network are generated blocks (decoration only); real footprints come from OSM when fetched.
"""
from __future__ import annotations

import base64
import math
import random
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pydeck as pdk

from geo import geographic_utils as geo
from simulation.models import DestinationSpec, RoadNetwork

BG = [16, 16, 18, 255]
ROAD = [158, 162, 168]
ROAD_BUSY = [217, 83, 79]
ROAD_CLOSED = [255, 255, 255]
SAFE_GREEN = [0, 150, 36, 255]
DANGER_RED = [214, 32, 32, 215]
AGENT_COLORS = {"WAITING": [120, 120, 120, 230], "MOVING": [30, 200, 80, 255], "QUEUED": [255, 170, 0, 255],
                "UNABLE_TO_EVACUATE": [214, 32, 32, 255]}


def _png_uri(draw_fn, size: int = 128) -> str:
    """Draw a sign with Pillow (4x supersampled) and return a PNG data URI (SVG icons are unreliable in deck.gl)."""
    import io
    from PIL import Image, ImageDraw
    k = 4
    img = Image.new("RGBA", (size * k, size * k), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    draw_fn(d, k)
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _draw_exit(d, k):
    """White sign, green disc and a running pedestrian (like the exit pictogram of the reference)."""
    g = (14, 138, 71, 255)
    d.rectangle([2 * k, 2 * k, 126 * k, 126 * k], outline=(207, 211, 216, 255), width=3 * k)
    d.ellipse([22 * k, 88 * k, 106 * k, 118 * k], fill=g)
    d.ellipse([64 * k, 14 * k, 86 * k, 36 * k], fill=g)
    for pts in ([(70, 40), (52, 62), (36, 58)], [(70, 40), (84, 62), (98, 56)], [(60, 52), (66, 78), (50, 100)],
                [(66, 78), (82, 92), (82, 104)]):
        d.line([(x * k, y * k) for x, y in pts], fill=g, width=10 * k, joint="curve")


def _draw_danger(d, k):
    r = (214, 32, 32, 255)
    d.rectangle([2 * k, 2 * k, 126 * k, 126 * k], outline=(207, 211, 216, 255), width=3 * k)
    d.polygon([(64 * k, 18 * k), (112 * k, 106 * k), (16 * k, 106 * k)], fill=r)
    d.rectangle([58 * k, 46 * k, 70 * k, 80 * k], fill="white")
    d.rectangle([58 * k, 88 * k, 70 * k, 100 * k], fill="white")


EXIT_ICON = {"url": _png_uri(_draw_exit), "width": 128, "height": 128, "anchorY": 128}
DANGER_ICON = {"url": _png_uri(_draw_danger), "width": 128, "height": 128, "anchorY": 128}


# ------------------------------------------------------------- geometry helpers
def square(lat: float, lon: float, half_m: float) -> List[List[float]]:
    pts = [geo.offset_latlon(lat, lon, n, e) for n, e in ((half_m, -half_m), (half_m, half_m),
                                                          (-half_m, half_m), (-half_m, -half_m))]
    return [[p[1], p[0]] for p in pts]


def circle(lat: float, lon: float, radius_m: float, steps: int = 48) -> List[List[float]]:
    out = []
    for i in range(steps):
        a = 2 * math.pi * i / steps
        p = geo.offset_latlon(lat, lon, radius_m * math.cos(a), radius_m * math.sin(a))
        out.append([p[1], p[0]])
    return out


def _bilerp(c, u: float, v: float) -> List[float]:
    """c = 4 corner (lon, lat): top-left, top-right, bottom-left, bottom-right."""
    top = [c[0][i] + (c[1][i] - c[0][i]) * u for i in (0, 1)]
    bot = [c[2][i] + (c[3][i] - c[2][i]) * u for i in (0, 1)]
    return [top[i] + (bot[i] - top[i]) * v for i in (0, 1)]


def synthetic_buildings(net: RoadNetwork, keep_clear: Sequence[Tuple[float, float, float]] = (),
                        seed: int = 3) -> List[dict]:
    """Decorative blocks inside the cells of the synthetic grid (never on roads)."""
    rows, cols = net.metadata.get("rows"), net.metadata.get("cols")
    if not rows or not cols:
        return []
    nodes = [net.nodes[i] for i in sorted(net.nodes)]
    rng = random.Random(seed)
    out = []
    for r in range(rows - 1):
        for c in range(cols - 1):
            corners = [(nodes[r * cols + c].lon, nodes[r * cols + c].lat),
                       (nodes[r * cols + c + 1].lon, nodes[r * cols + c + 1].lat),
                       (nodes[(r + 1) * cols + c].lon, nodes[(r + 1) * cols + c].lat),
                       (nodes[(r + 1) * cols + c + 1].lon, nodes[(r + 1) * cols + c + 1].lat)]
            for i in range(2):
                for j in range(2):
                    if rng.random() < 0.22:
                        continue
                    u0, u1 = 0.12 + 0.40 * i, 0.12 + 0.40 * i + 0.34
                    v0, v1 = 0.12 + 0.40 * j, 0.12 + 0.40 * j + 0.34
                    poly = [_bilerp(corners, u, v) for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]
                    clat = sum(p[1] for p in poly) / 4
                    clon = sum(p[0] for p in poly) / 4
                    if any(geo.haversine_m(clat, clon, la, lo) < rad for la, lo, rad in keep_clear):
                        continue
                    h = rng.choice([7, 9, 12, 15, 19, 24, 31])
                    shade = 150 - min(h, 30) * 2
                    out.append({"polygon": poly, "height": h, "color": [shade, shade + 4, shade + 10, 255]})
    return out


def osm_buildings(raw: Iterable[dict]) -> List[dict]:
    out = []
    for b in raw:
        h = b.get("height", 10)
        shade = 150 - min(h, 30) * 2
        out.append({"polygon": b["polygon"], "height": h, "color": [shade, shade + 4, shade + 10, 255]})
    return out


# ------------------------------------------------------------------- layers
def road_layer(net: RoadNetwork, closed: Iterable[int] = (), occupancy: Optional[Dict[int, Tuple[int, int]]] = None,
               capacity: Optional[Dict[int, int]] = None) -> pdk.Layer:
    closed, occupancy = set(closed), occupancy or {}
    rows, seen = [], set()
    for eid, e in net.edges.items():
        key = (min(e.source, e.target), max(e.source, e.target), eid in closed)
        if key in seen and eid not in closed:
            continue                                # one stroke per two-way road
        seen.add(key)
        occ, _ = occupancy.get(eid, (0, 0))
        ratio = occ / (capacity or {}).get(eid, e.capacity) if occ else 0.0
        if eid in closed:
            color, width = ROAD_CLOSED, 11
        else:
            color = [int(ROAD[i] + (ROAD_BUSY[i] - ROAD[i]) * min(1.0, ratio)) for i in range(3)]
            width = 16 if e.road_class in ("arterial", "primary", "secondary", "trunk") else 11
        rows.append({"path": [[lon, lat, 2.2] for lat, lon in net.edge_polyline(eid)], "color": color, "width": width})
    return pdk.Layer("PathLayer", rows, get_path="path", get_color="color", get_width="width", width_min_pixels=2, pickable=False)


def building_layer(buildings: Sequence[dict]) -> pdk.Layer:
    return pdk.Layer("PolygonLayer", list(buildings), get_polygon="polygon", extruded=True, get_elevation="height",
                     get_fill_color="color", get_line_color=[90, 94, 100], stroked=False, wireframe=False,
                     material={"ambient": 0.55, "diffuse": 0.6, "shininess": 20}, pickable=False)


def safe_area_layers(net: RoadNetwork, dests: Sequence[DestinationSpec]) -> List[pdk.Layer]:
    """Green extruded building + white pedestrian-exit sign floating above it (safe area)."""
    blocks, signs = [], []
    for d in dests:
        n = net.nodes[d.node_id]
        color = SAFE_GREEN if d.available else [120, 130, 120, 255]
        blocks.append({"polygon": square(n.lat, n.lon, 34), "height": 30, "color": color})
        blocks.append({"polygon": square(n.lat + 0.00022, n.lon + 0.0001, 22), "height": 44, "color": color})
        signs.append({"position": [n.lon, n.lat, 62], "icon": EXIT_ICON, "name": d.name,
                      "tip": f"SAFE AREA (hypothetical): {d.name} | capacity {d.capacity}"})
    return [
        pdk.Layer("PolygonLayer", blocks, get_polygon="polygon", extruded=True, get_elevation="height",
                  get_fill_color="color", stroked=False, material={"ambient": 0.6, "diffuse": 0.7}),
        pdk.Layer("IconLayer", signs, get_icon="icon", get_position="position", size_scale=1, get_size=34,
                  pickable=True),
    ]


def danger_layers(zones: Sequence[dict]) -> List[pdk.Layer]:
    """Red arena (low cylinder-like disc) per danger zone; each zone = {lat, lon, radius_m}."""
    arenas, rims, signs = [], [], []
    for z in zones:
        arenas.append({"polygon": circle(z["lat"], z["lon"], z["radius_m"]), "height": 6, "color": DANGER_RED})
        rims.append({"polygon": circle(z["lat"], z["lon"], z["radius_m"] * 1.04), "height": 12,
                     "color": [150, 10, 10, 255]})
        signs.append({"position": [z["lon"], z["lat"], 40], "icon": DANGER_ICON,
                      "tip": f"DANGER ZONE r={z['radius_m']:.0f} m (roads inside are closed)"})
    return [
        pdk.Layer("PolygonLayer", rims, get_polygon="polygon", extruded=True, get_elevation="height",
                  get_fill_color="color", stroked=False),
        pdk.Layer("PolygonLayer", arenas, get_polygon="polygon", extruded=True, get_elevation="height",
                  get_fill_color="color", stroked=False, opacity=0.9),
        pdk.Layer("IconLayer", signs, get_icon="icon", get_position="position", get_size=30,
                  pickable=True),
    ]


def agent_layer(rows: Sequence[dict], max_agents: int = 1500) -> pdk.Layer:
    stride = max(1, -(-len(rows) // max_agents))
    data = [{"position": [r["longitude"], r["latitude"], 5], "color": AGENT_COLORS[r["state"]],
             "tip": f"agent {r['agent_id']}: {r['state'].replace('_', ' ').lower()}"}
            for r in rows if r["state"] != "EVACUATED" and r["agent_id"] % stride == 0]
    return pdk.Layer("ScatterplotLayer", data, get_position="position", get_fill_color="color", get_radius=6,
                     radius_min_pixels=4, stroked=True, get_line_color=[255, 255, 255],
                     line_width_min_pixels=1, pickable=True)


def zoom_for(net: RoadNetwork) -> float:
    s, w, n, e = net.bounds()
    extent = max(geo.haversine_m(s, w, n, w), geo.haversine_m(s, w, s, e), 200.0)
    lat = (s + n) / 2
    return max(10.0, min(17.0, math.log2(156543.03 * math.cos(math.radians(lat)) * 900 / extent) - 1.7))


def make_deck(net: RoadNetwork, layers: List[pdk.Layer], pitch: float = 48, bearing: float = -20) -> pdk.Deck:
    s, w, n, e = net.bounds()
    view = pdk.ViewState(latitude=(s + n) / 2, longitude=(w + e) / 2, zoom=zoom_for(net), pitch=pitch,
                         bearing=bearing)
    # (Carto's free tile CDN now answers "API KEY REQUIRED", so no basemap is requested here; the live view
    # in live_view.py uses keyless OpenStreetMap tiles instead.)
    return pdk.Deck(layers=list(layers), initial_view_state=view, map_style=None, map_provider=None, views=[pdk.View("MapView", controller=True)],
                    parameters={"clearColor": [c / 255 for c in BG]},
                    tooltip={"text": "{tip}", "style": {"backgroundColor": "#000", "color": "#fff"}})
