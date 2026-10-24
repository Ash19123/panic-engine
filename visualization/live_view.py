"""Client-side LIVE 3-D replay with a simulated phone, tracked people and manual control.

Streamlit re-runs the whole script on every interaction, so stepping a map one rerun at a time cannot be
smooth. Here the stored simulation snapshots are embedded once into a self-contained deck.gl page that
animates in the browser (requestAnimationFrame):

* positions are linearly interpolated between the genuine engine snapshots (no random motion),
* moving people are arrows pointing along their direction of travel, queued/waiting people are dots,
* Play / Pause / Reset / Step / speed / time slider / basemap switch live inside the page, so using
  them never triggers a Streamlit rerun and never reloads the road network,
* a simulated PHONE shows each tracked person the emergency alert (siren + flashing screen), the nearest safe
  stop, walking distance/time and a live mini-map with their route and movement,
* tracked people can be renamed, followed, re-targeted to another safe stop, or TAKEN OVER and walked by hand
  (arrow keys / WASD / D-pad); the phone and the 3-D map follow the same person at the same time.

deck.gl is inlined from the copy that ships with pydeck (works offline). Basemap tiles (OpenStreetMap, keyless)
need internet and are only offered for REAL networks; synthetic networks never get a basemap.
The page template lives in ``live_template.html``.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from geo import geographic_utils as geo
from simulation.models import DestinationSpec, OriginSpec, RoadNetwork
from visualization import scene3d as sc

TEMPLATE = Path(__file__).with_name("live_template.html")
CORE = Path(__file__).with_name("live_core.js")
PHONE_TEMPLATE = Path(__file__).with_name("live_phone.html")
SAFE_TEMPLATE = Path(__file__).with_name("safe_page.html")
PEOPLE_NAMES = ["Aarav", "Diya", "Kavya", "Rohan", "Meera", "Arjun", "Sana", "Vikram", "Ishita", "Kabir",
                "Anaya", "Dev", "Neha", "Rahul", "Tara", "Imran"]     # fictional demo names, editable in the page
STATE_CODE = {"WAITING": 0, "MOVING": 1, "QUEUED": 2, "EVACUATED": 3, "UNABLE_TO_EVACUATE": 4}


@lru_cache(maxsize=1)
def _deck_bundle() -> str:
    import pydeck
    path = Path(pydeck.__file__).parent / "nbextension" / "static" / "index.js"
    return path.read_text(encoding="utf-8", errors="ignore").replace("</script", "<\\/script")


def _r(x: float) -> float:
    return round(x, 6)


def reaching_nodes(net: RoadNetwork, targets: Sequence[int]) -> set:
    """Nodes from which at least one target is reachable on foot (directed graph; reverse BFS from the targets)."""
    import networkx as nx
    if not targets:
        return set()
    return set(nx.multi_source_dijkstra_path_length(net.reverse_graph(), list(targets), weight=None))


def view_for(net: RoadNetwork, points: Sequence[tuple]) -> dict:
    """Camera that frames the interesting points (origins, safe areas, hazards), not the whole network."""
    pts = list(points)
    if not pts:
        s, w, n, e = net.bounds()
        pts = [(s, w), (n, e)]
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    clat, clon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    extent = max(geo.haversine_m(min(lats), clon, max(lats), clon), geo.haversine_m(clat, min(lons), clat, max(lons)))
    extent = max(450.0, extent * 1.5)
    zoom = math.log2(156543.03 * math.cos(math.radians(clat)) * 800 / extent) - 1.0
    return {"lat": clat, "lon": clon, "zoom": max(11.0, min(17.5, zoom)), "pitch": 52, "bearing": -18}


def build_live_html(net: RoadNetwork, bundle: dict, destinations: Sequence[DestinationSpec],
                    origins: Sequence[OriginSpec], zones: Sequence[dict], closures: Sequence, buildings: Sequence[dict],
                    capacity_overrides: Optional[Dict[int, int]] = None, focus: Optional[dict] = None,
                    height: int = 880, max_agents: int = 1500, hazards: Sequence[dict] = (),
                    tracked: int = 10, payload_only: bool = False,
                    sync_url: Optional[str] = None, run_id: Optional[int] = None):
    times = bundle["times"]
    n_agents = len(bundle["agent_ids"])
    stride = max(1, -(-n_agents // max_agents))
    keep = list(range(0, n_agents, stride))
    alerts = bundle.get("alert_times") or [None] * n_agents
    deps = bundle.get("departures") or [None] * n_agents
    chans = bundle.get("alert_channels") or [None] * n_agents

    # one road stroke per two-way pair; remember which stroke each directed edge belongs to
    roads, road_of, seen = [], {}, {}
    for eid, e in net.edges.items():
        key = (min(e.source, e.target), max(e.source, e.target))
        if key not in seen:
            seen[key] = len(roads)
            wide = e.road_class in ("arterial", "primary", "secondary", "trunk")
            roads.append({"p": [[_r(lon), _r(lat)] for lat, lon in net.edge_polyline(eid)], "w": 15 if wide else 10})
        road_of[eid] = seen[key]
    caps = {eid: max(1, (capacity_overrides or {}).get(eid, e.capacity)) for eid, e in net.edges.items()}
    occ = []
    for frame in bundle["occupancy"]:
        m: Dict[int, List[float]] = {}
        for eid, (o, q) in frame.items():
            if eid in road_of:
                ratio, idx = o / caps[eid], road_of[eid]
                if idx not in m or ratio > m[idx][0]:
                    m[idx] = [round(ratio, 3), q]
        occ.append([[idx, v[0]] for idx, v in m.items() if v[0] > 0.02])

    # walking graph for the phone's route finder / manual control (node indices, edge lengths in metres)
    node_ids = sorted(net.nodes)
    nidx = {nid: i for i, nid in enumerate(node_ids)}
    graph = {"lon": [_r(net.nodes[i].lon) for i in node_ids], "lat": [_r(net.nodes[i].lat) for i in node_ids],
             "eu": [nidx[e.source] for e in net.edges.values()], "ev": [nidx[e.target] for e in net.edges.values()],
             "el": [round(e.length_m, 1) for e in net.edges.values()], "er": [road_of[eid] for eid in net.edges]}

    # blast hazards: alert ring, blast disc, and the roads the blast closes at strike time
    bombs, blast_closures = [], []
    for h in hazards:
        bombs.append({"name": h.get("name", "Bomb"), "lon": h["lon"], "lat": h["lat"], "blast": h["blast_radius_m"],
                      "alertR": h["alert_radius_m"], "strike": h["strike_time_s"], "alert": h["alert_time_s"],
                      "p": [[_r(x), _r(y)] for x, y in sc.circle(h["lat"], h["lon"], h["blast_radius_m"])],
                      "pr": [[_r(x), _r(y)] for x, y in sc.circle(h["lat"], h["lon"], h["blast_radius_m"] * 1.04)]})
        blast_closures += [[road_of[eid], h["strike_time_s"]]
                           for eid in net.edges_within(h["lat"], h["lon"], h["blast_radius_m"])]

    def lost_at(lat: float, lon: float):
        ts = [h["strike_time_s"] for h in hazards if geo.haversine_m(lat, lon, h["lat"], h["lon"]) <= h["blast_radius_m"]]
        return min(ts) if ts else None

    safe = []
    for i, d in enumerate(destinations):
        nd = net.nodes[d.node_id]
        label = d.name.replace("Safe area ", "Safe stop ")
        if "Node " in label or "(" in label:          # real-network nodes have no friendly name: use letters
            label = f"Safe stop {chr(65 + i % 26)}"
        safe.append({"node": nidx[d.node_id], "name": label, "cap": d.capacity,
                     "lon": nd.lon, "lat": nd.lat, "lost": lost_at(nd.lat, nd.lon) if d.available else 0,
                     "p": sc.square(nd.lat, nd.lon, 34), "p2": sc.square(nd.lat + 0.00022, nd.lon + 0.0001, 22),
                     "tip": f"SAFE STOP (hypothetical): {d.name} | capacity {d.capacity} | admits {d.throughput_per_timestep}/step"})

    # tracked people: evenly spread through the crowd, preferring people who receive an alert AND can actually
    # walk to a safe stop on the directed road graph (so the phone demo always has a route to show)
    reach = reaching_nodes(net, [d.node_id for d in destinations if d.available])
    pool = [j for j, k in enumerate(keep) if chans[k] is not None] or list(range(len(keep)))      # everyone inside an alert area
    sample = [pool[i * len(pool) // min(len(pool), 120)] for i in range(min(len(pool), 120))]
    safe_ids = {d.node_id for d in destinations}
    near = {j: net.nearest_node(bundle["lat"][0][keep[j]], bundle["lon"][0][keep[j]]) for j in sample}
    ok = ([j for j in sample if near[j] in reach and near[j] not in safe_ids]       # needs a real walk to show
          or [j for j in sample if near[j] in reach] or sample)
    count = min(tracked, len(ok))
    pick = [ok[i * len(ok) // max(1, count)] for i in range(count)]
    people = [{"k": j, "name": PEOPLE_NAMES[i % len(PEOPLE_NAMES)] + ("" if i < len(PEOPLE_NAMES) else f" {i}"),
               "alert": alerts[keep[j]], "dep": deps[keep[j]], "ch": chans[keep[j]]} for i, j in enumerate(pick)]

    view_pts = ([(net.nodes[o.node_id].lat, net.nodes[o.node_id].lon) for o in origins]
                + [(net.nodes[d.node_id].lat, net.nodes[d.node_id].lon) for d in destinations]
                + [(z["lat"], z["lon"]) for z in zones] + [(h["lat"], h["lon"]) for h in hazards])
    view = view_for(net, view_pts)
    cx, cy = view["lon"], view["lat"]
    near = sorted(buildings, key=lambda b: (b["polygon"][0][0] - cx) ** 2 + (b["polygon"][0][1] - cy) ** 2)[:9000]

    data = {
        "times": times, "n": len(keep), "total": sum(bundle["stats"][0]) if bundle["stats"] else n_agents,
        "states": [[STATE_CODE[f[k]] for k in keep] for f in bundle["states"]],
        "lat": [[_r(f[k]) for k in keep] for f in bundle["lat"]],
        "lon": [[_r(f[k]) for k in keep] for f in bundle["lon"]],
        "stats": bundle["stats"], "occ": occ, "roads": roads, "graph": graph, "bombs": bombs, "people": people,
        "closures": ([[road_of[c.edge_id], c.close_time_seconds] for c in closures if c.edge_id in road_of]
                     + blast_closures),
        "buildings": [{"p": [[_r(x), _r(y)] for x, y in b["polygon"]], "h": b["height"], "c": b["color"][:3]} for b in near],
        "zones": [{"p": [[_r(x), _r(y)] for x, y in sc.circle(z["lat"], z["lon"], z["radius_m"])],
                   "pr": [[_r(x), _r(y)] for x, y in sc.circle(z["lat"], z["lon"], z["radius_m"] * 1.04)],
                   "lon": z["lon"], "lat": z["lat"], "r": z["radius_m"]} for z in zones],
        "safe": safe,
        "origins": [{"lon": net.nodes[o.node_id].lon, "lat": net.nodes[o.node_id].lat,
                     "tip": f"START POINT: {o.label or o.node_id}"} for o in origins],
        "focus": focus, "view": view, "synthetic": net.is_synthetic,
        "icons": {"exit": sc.EXIT_ICON["url"], "danger": sc.DANGER_ICON["url"]},
        "attribution": "Map data © OpenStreetMap contributors (ODbL)",
        "sync": sync_url, "run": run_id,
    }
    if payload_only:                      # used by the Node unit tests of live_core.js
        return data
    # the big library goes in LAST so its text can never be mistaken for a placeholder
    return (TEMPLATE.read_text(encoding="utf-8").replace("__HEIGHT__", str(height))
            .replace("__CORE__", CORE.read_text(encoding="utf-8"))
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))).replace("__DECK__", _deck_bundle()))


def build_phone_html(net: RoadNetwork, bundle: dict, destinations: Sequence[DestinationSpec], hazards: Sequence[dict],
                     closures: Sequence, run_id: int, sync_url: str = "/state") -> str:
    """Stand-alone mobile page: the phone UI only (alert, safe-stop route, mini-map, D-pad), no 3-D engine.

    It carries the walking graph and the frames of the tracked people only, so it is small enough for a phone.
    It talks to the phone server (``/state``) so that its clock, selected person and manual avatar stay in sync
    with the dashboard.
    """
    full = build_live_html(net, bundle, destinations, [], [], closures, [], hazards=hazards, payload_only=True,
                           run_id=run_id)
    people = []
    for p in full["people"]:
        k = p["k"]
        people.append({"id": k, "name": p["name"], "alert": p["alert"], "dep": p["dep"], "ch": p.get("ch"),
                       "lon": [f[k] for f in full["lon"]], "lat": [f[k] for f in full["lat"]],
                       "st": [f[k] for f in full["states"]]})
    data = {"times": full["times"], "people": people, "graph": full["graph"], "bombs": full["bombs"],
            "safe": [{k: v for k, v in s.items() if k not in ("p", "p2", "tip")} for s in full["safe"]],
            "closures": full["closures"], "view": full["view"], "synthetic": full["synthetic"], "run": run_id,
            "sync": sync_url}
    return (PHONE_TEMPLATE.read_text(encoding="utf-8").replace("__CORE__", CORE.read_text(encoding="utf-8"))
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def build_safe_html(net: RoadNetwork, destinations: Sequence[DestinationSpec], scenario_id: Optional[int] = None) -> str:
    """"Know your nearest safe stop" page: works BEFORE any alert (no run, no bomb).

    The visitor picks where they live (tap the map, type coordinates or use device location) and gets the nearest
    safe stop, walking distance/time, plain-language directions, a backup stop and a printable card with a QR code.
    """
    node_ids = sorted(net.nodes)
    nidx = {nid: i for i, nid in enumerate(node_ids)}
    seen, road_of = {}, {}
    for eid, e in net.edges.items():
        road_of[eid] = seen.setdefault((min(e.source, e.target), max(e.source, e.target)), len(seen))
    graph = {"lon": [_r(net.nodes[i].lon) for i in node_ids], "lat": [_r(net.nodes[i].lat) for i in node_ids],
             "eu": [nidx[e.source] for e in net.edges.values()], "ev": [nidx[e.target] for e in net.edges.values()],
             "el": [round(e.length_m, 1) for e in net.edges.values()], "er": [road_of[eid] for eid in net.edges]}
    stops = [d for d in destinations if d.available]
    safe = []
    for i, d in enumerate(stops):
        nd = net.nodes[d.node_id]
        label = d.name.replace("Safe area ", "Safe stop ")
        if "Node " in label or "(" in label:
            label = f"Safe stop {chr(65 + i % 26)}"
        safe.append({"node": nidx[d.node_id], "name": label, "cap": d.capacity, "lon": nd.lon, "lat": nd.lat, "lost": None})
    view = view_for(net, [(net.nodes[d.node_id].lat, net.nodes[d.node_id].lon) for d in stops])
    data = {"graph": graph, "safe": safe, "closures": [], "bombs": [], "view": view, "synthetic": net.is_synthetic,
            "scenario": scenario_id}
    return (SAFE_TEMPLATE.read_text(encoding="utf-8").replace("__CORE__", CORE.read_text(encoding="utf-8"))
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))
