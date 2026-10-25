"""3-D evacuation scene (pydeck / deck.gl): grey extruded buildings, pale roads,
green exit buildings for safe areas, red arenas for danger zones, walking agents.

Pure functions: network + data in, ``pydeck.Deck`` out. Buildings on the synthetic
network are generated blocks (decoration only); real footprints come from OSM when fetched.
"""
from __future__ import annotations  # modern type hints on older Python

import base64  # encode images as text
import math  # trigonometry
import random  # seeded decoration
from typing import Dict, Iterable, List, Optional, Sequence, Tuple  # type hints

import pydeck as pdk  # Python wrapper for deck.gl

from geo import geographic_utils as geo  # distance helpers
from simulation.models import DestinationSpec, RoadNetwork  # scenario building blocks

BG = [16, 16, 18, 255]  # background colour (red, green, blue, alpha)
ROAD = [158, 162, 168]  # empty road colour
ROAD_BUSY = [217, 83, 79]  # crowded road colour
ROAD_CLOSED = [255, 255, 255]  # closed road colour
SAFE_GREEN = [0, 150, 36, 255]  # safe stop colour
DANGER_RED = [214, 32, 32, 215]  # danger zone colour
AGENT_COLORS = {"WAITING": [120, 120, 120, 230], "MOVING": [30, 200, 80, 255], "QUEUED": [255, 170, 0, 255],  # colour per agent state (1)
                "UNABLE_TO_EVACUATE": [214, 32, 32, 255]}  # (2)


def _png_uri(draw_fn, size: int = 128) -> str:  # draw a picture and return it as a data address
    """Draw a sign with Pillow (4x supersampled) and return a PNG data URI (SVG icons are unreliable in deck.gl)."""
    import io  # in-memory bytes
    from PIL import Image, ImageDraw  # image drawing
    k = 4  # supersampling factor, for smooth edges
    img = Image.new("RGBA", (size * k, size * k), (255, 255, 255, 255))  # white canvas at 4x size
    d = ImageDraw.Draw(img)  # drawing tool
    draw_fn(d, k)  # let the caller draw the sign
    img = img.resize((size, size), Image.LANCZOS)  # shrink for anti-aliasing
    buf = io.BytesIO()  # memory buffer
    img.save(buf, format="PNG")  # encode as PNG
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()  # as a data URI


def _draw_exit(d, k):  # the green running-person exit sign
    """White sign, green disc and a running pedestrian (like the exit pictogram of the reference)."""
    g = (14, 138, 71, 255)  # green
    d.rectangle([2 * k, 2 * k, 126 * k, 126 * k], outline=(207, 211, 216, 255), width=3 * k)  # grey border
    d.ellipse([22 * k, 88 * k, 106 * k, 118 * k], fill=g)  # green ground ellipse
    d.ellipse([64 * k, 14 * k, 86 * k, 36 * k], fill=g)  # head
    for pts in ([(70, 40), (52, 62), (36, 58)], [(70, 40), (84, 62), (98, 56)], [(60, 52), (66, 78), (50, 100)],  # limbs (1): arms and a leg
                [(66, 78), (82, 92), (82, 104)]):  # limbs (2): the other leg
        d.line([(x * k, y * k) for x, y in pts], fill=g, width=10 * k, joint="curve")  # draw each limb as a thick line


def _draw_danger(d, k):  # the red warning triangle
    r = (214, 32, 32, 255)  # red
    d.rectangle([2 * k, 2 * k, 126 * k, 126 * k], outline=(207, 211, 216, 255), width=3 * k)  # grey border
    d.polygon([(64 * k, 18 * k), (112 * k, 106 * k), (16 * k, 106 * k)], fill=r)  # triangle
    d.rectangle([58 * k, 46 * k, 70 * k, 80 * k], fill="white")  # exclamation bar
    d.rectangle([58 * k, 88 * k, 70 * k, 100 * k], fill="white")  # exclamation dot


EXIT_ICON = {"url": _png_uri(_draw_exit), "width": 128, "height": 128, "anchorY": 128}  # exit sign for deck.gl
DANGER_ICON = {"url": _png_uri(_draw_danger), "width": 128, "height": 128, "anchorY": 128}  # danger sign for deck.gl


# ------------------------------------------------------------- geometry helpers
def square(lat: float, lon: float, half_m: float) -> List[List[float]]:  # a square around a point
    pts = [geo.offset_latlon(lat, lon, n, e) for n, e in ((half_m, -half_m), (half_m, half_m),  # corners in metres (1)
                                                          (-half_m, half_m), (-half_m, -half_m))]  # (2)
    return [[p[1], p[0]] for p in pts]  # as [longitude, latitude] pairs


def circle(lat: float, lon: float, radius_m: float, steps: int = 48) -> List[List[float]]:  # a circle around a point
    out = []  # outline points
    for i in range(steps):  # equally spaced angles
        a = 2 * math.pi * i / steps  # this angle
        p = geo.offset_latlon(lat, lon, radius_m * math.cos(a), radius_m * math.sin(a))  # point on the circle
        out.append([p[1], p[0]])  # as [longitude, latitude]
    return out  # the outline


def _bilerp(c, u: float, v: float) -> List[float]:  # a point inside a four-cornered cell
    """c = 4 corner (lon, lat): top-left, top-right, bottom-left, bottom-right."""
    top = [c[0][i] + (c[1][i] - c[0][i]) * u for i in (0, 1)]  # point along the top edge
    bot = [c[2][i] + (c[3][i] - c[2][i]) * u for i in (0, 1)]  # point along the bottom edge
    return [top[i] + (bot[i] - top[i]) * v for i in (0, 1)]  # blend between them


def synthetic_buildings(net: RoadNetwork, keep_clear: Sequence[Tuple[float, float, float]] = (),
                        seed: int = 3) -> List[dict]:  # decorative blocks for the fake grid
    """Decorative blocks inside the cells of the synthetic grid (never on roads)."""
    rows, cols = net.metadata.get("rows"), net.metadata.get("cols")  # grid size
    if not rows or not cols:  # not a grid network
        return []  # nothing to decorate
    nodes = [net.nodes[i] for i in sorted(net.nodes)]  # nodes in grid order
    rng = random.Random(seed)  # seeded: the same decoration every time
    out = []  # buildings
    for r in range(rows - 1):  # each grid cell row
        for c in range(cols - 1):  # each grid cell column
            corners = [(nodes[r * cols + c].lon, nodes[r * cols + c].lat),  # cell corner: top-left
                       (nodes[r * cols + c + 1].lon, nodes[r * cols + c + 1].lat),  # top-right
                       (nodes[(r + 1) * cols + c].lon, nodes[(r + 1) * cols + c].lat),  # bottom-left
                       (nodes[(r + 1) * cols + c + 1].lon, nodes[(r + 1) * cols + c + 1].lat)]  # bottom-right
            for i in range(2):  # two blocks across
                for j in range(2):  # two blocks down
                    if rng.random() < 0.22:  # leave some gaps
                        continue  # skip it
                    u0, u1 = 0.12 + 0.40 * i, 0.12 + 0.40 * i + 0.34  # block extent across the cell
                    v0, v1 = 0.12 + 0.40 * j, 0.12 + 0.40 * j + 0.34  # block extent down the cell
                    poly = [_bilerp(corners, u, v) for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]  # the four corners of the block
                    clat = sum(p[1] for p in poly) / 4  # centre latitude
                    clon = sum(p[0] for p in poly) / 4  # centre longitude
                    if any(geo.haversine_m(clat, clon, la, lo) < rad for la, lo, rad in keep_clear):  # inside a protected circle
                        continue  # skip it
                    h = rng.choice([7, 9, 12, 15, 19, 24, 31])  # random height in metres
                    shade = 150 - min(h, 30) * 2  # taller buildings are darker
                    out.append({"polygon": poly, "height": h, "color": [shade, shade + 4, shade + 10, 255]})  # add the building
    return out  # all buildings


def osm_buildings(raw: Iterable[dict]) -> List[dict]:  # convert OpenStreetMap footprints to drawable buildings
    out = []  # buildings
    for b in raw:  # each footprint
        h = b.get("height", 10)  # height, default 10 m
        shade = 150 - min(h, 30) * 2  # taller buildings are darker
        out.append({"polygon": b["polygon"], "height": h, "color": [shade, shade + 4, shade + 10, 255]})  # add it
    return out  # all buildings


# ------------------------------------------------------------------- layers
def road_layer(net: RoadNetwork, closed: Iterable[int] = (), occupancy: Optional[Dict[int, Tuple[int, int]]] = None,
               capacity: Optional[Dict[int, int]] = None) -> pdk.Layer:  # the roads as a deck.gl layer
    closed, occupancy = set(closed), occupancy or {}  # sets and dictionaries for fast lookup
    rows, seen = [], set()  # layer rows and strokes already drawn
    for eid, e in net.edges.items():  # every road
        key = (min(e.source, e.target), max(e.source, e.target), eid in closed)  # unordered pair plus closed flag
        if key in seen and eid not in closed:  # the other direction of a road already drawn
            continue                                # one stroke per two-way road
        seen.add(key)  # remember it
        occ, _ = occupancy.get(eid, (0, 0))  # people on it
        ratio = occ / (capacity or {}).get(eid, e.capacity) if occ else 0.0  # how full it is
        if eid in closed:  # closed road
            color, width = ROAD_CLOSED, 11  # white and thick
        else:  # open road
            color = [int(ROAD[i] + (ROAD_BUSY[i] - ROAD[i]) * min(1.0, ratio)) for i in range(3)]  # blend from grey to red with fullness
            width = 16 if e.road_class in ("arterial", "primary", "secondary", "trunk") else 11  # wider for main roads
        rows.append({"path": [[lon, lat, 2.2] for lat, lon in net.edge_polyline(eid)], "color": color, "width": width})  # the stroke slightly above the ground
    return pdk.Layer("PathLayer", rows, get_path="path", get_color="color", get_width="width", width_min_pixels=2, pickable=False)  # build the layer


def building_layer(buildings: Sequence[dict]) -> pdk.Layer:  # extruded buildings as a layer
    return pdk.Layer("PolygonLayer", list(buildings), get_polygon="polygon", extruded=True, get_elevation="height",  # raised shapes (part 1)
                     get_fill_color="color", get_line_color=[90, 94, 100], stroked=False, wireframe=False,  # colours (part 2)
                     material={"ambient": 0.55, "diffuse": 0.6, "shininess": 20}, pickable=False)  # lighting (part 3)


def safe_area_layers(net: RoadNetwork, dests: Sequence[DestinationSpec]) -> List[pdk.Layer]:  # green buildings and exit signs
    """Green extruded building + white pedestrian-exit sign floating above it (safe area)."""
    blocks, signs = [], []  # building shapes and signs
    for d in dests:  # each safe stop
        n = net.nodes[d.node_id]  # its node
        color = SAFE_GREEN if d.available else [120, 130, 120, 255]  # grey-green if unavailable
        blocks.append({"polygon": square(n.lat, n.lon, 34), "height": 30, "color": color})  # a low wide block
        blocks.append({"polygon": square(n.lat + 0.00022, n.lon + 0.0001, 22), "height": 44, "color": color})  # a taller smaller block
        signs.append({"position": [n.lon, n.lat, 62], "icon": EXIT_ICON, "name": d.name,  # a sign floating above it (part 1)
                      "tip": f"SAFE AREA (hypothetical): {d.name} | capacity {d.capacity}"})  # hover text (part 2)
    return [  # two layers
        pdk.Layer("PolygonLayer", blocks, get_polygon="polygon", extruded=True, get_elevation="height",  # the buildings (part 1)
                  get_fill_color="color", stroked=False, material={"ambient": 0.6, "diffuse": 0.7}),  # (part 2)
        pdk.Layer("IconLayer", signs, get_icon="icon", get_position="position", size_scale=1, get_size=34,  # the signs (part 1)
                  pickable=True),  # (part 2)
    ]


def danger_layers(zones: Sequence[dict]) -> List[pdk.Layer]:  # red arenas for danger zones
    """Red arena (low cylinder-like disc) per danger zone; each zone = {lat, lon, radius_m}."""
    arenas, rims, signs = [], [], []  # discs, rims and signs
    for z in zones:  # each zone
        arenas.append({"polygon": circle(z["lat"], z["lon"], z["radius_m"]), "height": 6, "color": DANGER_RED})  # the red disc
        rims.append({"polygon": circle(z["lat"], z["lon"], z["radius_m"] * 1.04), "height": 12,  # a slightly larger, taller rim (part 1)
                     "color": [150, 10, 10, 255]})  # dark red (part 2)
        signs.append({"position": [z["lon"], z["lat"], 40], "icon": DANGER_ICON,  # a warning sign (part 1)
                      "tip": f"DANGER ZONE r={z['radius_m']:.0f} m (roads inside are closed)"})  # hover text (part 2)
    return [  # three layers
        pdk.Layer("PolygonLayer", rims, get_polygon="polygon", extruded=True, get_elevation="height",  # the rims (part 1)
                  get_fill_color="color", stroked=False),  # (part 2)
        pdk.Layer("PolygonLayer", arenas, get_polygon="polygon", extruded=True, get_elevation="height",  # the discs (part 1)
                  get_fill_color="color", stroked=False, opacity=0.9),  # (part 2)
        pdk.Layer("IconLayer", signs, get_icon="icon", get_position="position", get_size=30,  # the signs (part 1)
                  pickable=True),  # (part 2)
    ]


def agent_layer(rows: Sequence[dict], max_agents: int = 1500) -> pdk.Layer:  # people as dots
    stride = max(1, -(-len(rows) // max_agents))  # draw every n-th agent to stay under the limit
    data = [{"position": [r["longitude"], r["latitude"], 5], "color": AGENT_COLORS[r["state"]],  # position just above the ground, colour by state
             "tip": f"agent {r['agent_id']}: {r['state'].replace('_', ' ').lower()}"}  # hover text
            for r in rows if r["state"] != "EVACUATED" and r["agent_id"] % stride == 0]  # skip evacuated people and unsampled agents
    return pdk.Layer("ScatterplotLayer", data, get_position="position", get_fill_color="color", get_radius=6,  # dots (part 1)
                     radius_min_pixels=4, stroked=True, get_line_color=[255, 255, 255],  # size and white outline (part 2)
                     line_width_min_pixels=1, pickable=True)  # (part 3)


def zoom_for(net: RoadNetwork) -> float:  # map zoom that fits the network
    s, w, n, e = net.bounds()  # extent of the network
    extent = max(geo.haversine_m(s, w, n, w), geo.haversine_m(s, w, s, e), 200.0)  # larger side in metres, at least 200
    lat = (s + n) / 2  # middle latitude
    return max(10.0, min(17.0, math.log2(156543.03 * math.cos(math.radians(lat)) * 900 / extent) - 1.7))  # zoom formula, kept between 10 and 17


def make_deck(net: RoadNetwork, layers: List[pdk.Layer], pitch: float = 48, bearing: float = -20) -> pdk.Deck:  # assemble the scene
    s, w, n, e = net.bounds()  # extent of the network
    view = pdk.ViewState(latitude=(s + n) / 2, longitude=(w + e) / 2, zoom=zoom_for(net), pitch=pitch,  # camera centred on the network (part 1)
                         bearing=bearing)  # (part 2)
    # (Carto's free tile CDN now answers "API KEY REQUIRED", so no basemap is requested here; the live view
    # in live_view.py uses keyless OpenStreetMap tiles instead.)
    return pdk.Deck(layers=list(layers), initial_view_state=view, map_style=None, map_provider=None, views=[pdk.View("MapView", controller=True)],  # the scene without a basemap
                    parameters={"clearColor": [c / 255 for c in BG]},  # background colour
                    tooltip={"text": "{tip}", "style": {"backgroundColor": "#000", "color": "#fff"}})  # hover tooltip style
