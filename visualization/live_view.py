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
from __future__ import annotations  # modern type hints on older Python

import json  # embed data as JSON
import math  # logarithm for the zoom level
from functools import lru_cache  # remember a result so it is computed once
from pathlib import Path  # file paths
from typing import Dict, List, Optional, Sequence  # type hints

from geo import geographic_utils as geo  # distance helpers
from simulation.models import DestinationSpec, OriginSpec, RoadNetwork  # scenario building blocks
from visualization import scene3d as sc  # circle and square helpers and icons

TEMPLATE = Path(__file__).with_name("live_template.html")  # the dashboard live page template
CORE = Path(__file__).with_name("live_core.js")  # the shared browser logic (route finder and so on)
PHONE_TEMPLATE = Path(__file__).with_name("live_phone.html")  # the mobile phone page template
PEOPLE_NAMES = ["Aarav", "Diya", "Kavya", "Rohan", "Meera", "Arjun", "Sana", "Vikram", "Ishita", "Kabir",  # names (1)
                "Anaya", "Dev", "Neha", "Rahul", "Tara", "Imran"]     # fictional demo names, editable in the page
STATE_CODE = {"WAITING": 0, "MOVING": 1, "QUEUED": 2, "EVACUATED": 3, "UNABLE_TO_EVACUATE": 4}  # state -> small number for compact JSON


@lru_cache(maxsize=1)
def _deck_bundle() -> str:  # the deck.gl library as text
    import pydeck  # ships a copy of deck.gl
    path = Path(pydeck.__file__).parent / "nbextension" / "static" / "index.js"  # where it lives
    return path.read_text(encoding="utf-8", errors="ignore").replace("</script", "<\\/script")  # escape so it cannot close our script tag


def _r(x: float) -> float:  # round a coordinate to 6 decimals
    return round(x, 6)  # about 0.1 m, keeps the JSON small


def reaching_nodes(net: RoadNetwork, targets: Sequence[int]) -> set:  # nodes that can walk to a safe stop
    """Nodes from which at least one target is reachable on foot (directed graph; reverse BFS from the targets)."""
    import networkx as nx  # graph library
    if not targets:  # no safe stops
        return set()  # nobody can reach one
    return set(nx.multi_source_dijkstra_path_length(net.reverse_graph(), list(targets), weight=None))  # search backwards from all stops at once


def view_for(net: RoadNetwork, points: Sequence[tuple]) -> dict:  # camera position
    """Camera that frames the interesting points (origins, safe areas, hazards), not the whole network."""
    pts = list(points)  # the points to frame
    if not pts:  # none given
        s, w, n, e = net.bounds()  # use the whole network
        pts = [(s, w), (n, e)]  # its two corners
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]  # separate the coordinates
    clat, clon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2  # centre of the points
    extent = max(geo.haversine_m(min(lats), clon, max(lats), clon), geo.haversine_m(clat, min(lons), clat, max(lons)))  # larger of height and width in metres
    extent = max(450.0, extent * 1.5)  # add margin, at least 450 m
    zoom = math.log2(156543.03 * math.cos(math.radians(clat)) * 800 / extent) - 1.0  # map zoom that fits that extent
    return {"lat": clat, "lon": clon, "zoom": max(11.0, min(17.5, zoom)), "pitch": 52, "bearing": -18}  # tilted view, zoom kept in range


def build_live_html(net: RoadNetwork, bundle: dict, destinations: Sequence[DestinationSpec],
                    origins: Sequence[OriginSpec], zones: Sequence[dict], closures: Sequence, buildings: Sequence[dict],
                    capacity_overrides: Optional[Dict[int, int]] = None, focus: Optional[dict] = None,
                    height: int = 880, max_agents: int = 1500, hazards: Sequence[dict] = (),
                    tracked: int = 10, payload_only: bool = False,
                    sync_url: Optional[str] = None, run_id: Optional[int] = None):  # build the live page
    times = bundle["times"]  # frame times
    n_agents = len(bundle["agent_ids"])  # number of stored agents
    stride = max(1, -(-n_agents // max_agents))  # sample every n-th agent to cap the size
    keep = list(range(0, n_agents, stride))  # indices of the kept agents
    alerts = bundle.get("alert_times") or [None] * n_agents  # alert time per agent
    deps = bundle.get("departures") or [None] * n_agents  # departure time per agent
    chans = bundle.get("alert_channels") or [None] * n_agents  # alert channel per agent

    # one road stroke per two-way pair; remember which stroke each directed edge belongs to
    roads, road_of, seen = [], {}, {}  # strokes, edge -> stroke index, pair -> stroke index
    for eid, e in net.edges.items():  # every road
        key = (min(e.source, e.target), max(e.source, e.target))  # the unordered pair of end nodes
        if key not in seen:  # first road of this pair
            seen[key] = len(roads)  # new stroke index
            wide = e.road_class in ("arterial", "primary", "secondary", "trunk")  # a main road?
            roads.append({"p": [[_r(lon), _r(lat)] for lat, lon in net.edge_polyline(eid)], "w": 15 if wide else 10})  # its shape and width
        road_of[eid] = seen[key]  # remember its stroke
    caps = {eid: max(1, (capacity_overrides or {}).get(eid, e.capacity)) for eid, e in net.edges.items()}  # capacity per road
    occ = []  # per frame: [stroke, fullness] pairs
    for frame in bundle["occupancy"]:  # each frame
        m: Dict[int, List[float]] = {}  # stroke -> [ratio, queue]
        for eid, (o, q) in frame.items():  # each busy road
            if eid in road_of:  # it has a stroke
                ratio, idx = o / caps[eid], road_of[eid]  # fullness and stroke index
                if idx not in m or ratio > m[idx][0]:  # keep the fuller direction
                    m[idx] = [round(ratio, 3), q]  # store it
        occ.append([[idx, v[0]] for idx, v in m.items() if v[0] > 0.02])  # drop nearly empty roads

    # walking graph for the phone's route finder / manual control (node indices, edge lengths in metres)
    node_ids = sorted(net.nodes)  # nodes in a fixed order
    nidx = {nid: i for i, nid in enumerate(node_ids)}  # node id -> index
    graph = {"lon": [_r(net.nodes[i].lon) for i in node_ids], "lat": [_r(net.nodes[i].lat) for i in node_ids],  # node coordinates
             "eu": [nidx[e.source] for e in net.edges.values()], "ev": [nidx[e.target] for e in net.edges.values()],  # edge end indices
             "el": [round(e.length_m, 1) for e in net.edges.values()], "er": [road_of[eid] for eid in net.edges]}  # edge lengths and strokes

    # blast hazards: alert ring, blast disc, and the roads the blast closes at strike time
    bombs, blast_closures = [], []  # drawn bombs and the roads they close
    for h in hazards:  # each bomb
        bombs.append({"name": h.get("name", "Bomb"), "lon": h["lon"], "lat": h["lat"], "blast": h["blast_radius_m"],  # identity and blast radius
                      "alertR": h["alert_radius_m"], "strike": h["strike_time_s"], "alert": h["alert_time_s"],  # alert radius and times
                      "p": [[_r(x), _r(y)] for x, y in sc.circle(h["lat"], h["lon"], h["blast_radius_m"])],  # blast disc outline
                      "pr": [[_r(x), _r(y)] for x, y in sc.circle(h["lat"], h["lon"], h["blast_radius_m"] * 1.04)]})  # slightly larger rim
        blast_closures += [[road_of[eid], h["strike_time_s"]]  # closure: (stroke, time) for every road inside the blast (part 1)
                           for eid in net.edges_within(h["lat"], h["lon"], h["blast_radius_m"])]  # (part 2)

    def lost_at(lat: float, lon: float):  # when would a place be destroyed?
        ts = [h["strike_time_s"] for h in hazards if geo.haversine_m(lat, lon, h["lat"], h["lon"]) <= h["blast_radius_m"]]  # strike times of bombs covering it
        return min(ts) if ts else None  # the earliest, or None if safe

    safe = []  # drawn safe stops
    for i, d in enumerate(destinations):  # each safe stop
        nd = net.nodes[d.node_id]  # its node
        label = d.name.replace("Safe area ", "Safe stop ")  # friendlier name
        if "Node " in label or "(" in label:          # real-network nodes have no friendly name: use letters
            label = f"Safe stop {chr(65 + i % 26)}"  # A, B, C ...
        safe.append({"node": nidx[d.node_id], "name": label, "cap": d.capacity,  # identity and capacity
                     "lon": nd.lon, "lat": nd.lat, "lost": lost_at(nd.lat, nd.lon) if d.available else 0,  # position and time of loss (0 = never usable)
                     "p": sc.square(nd.lat, nd.lon, 34), "p2": sc.square(nd.lat + 0.00022, nd.lon + 0.0001, 22),  # two building footprints
                     "tip": f"SAFE STOP (hypothetical): {d.name} | capacity {d.capacity} | admits {d.throughput_per_timestep}/step"})  # hover text

    # tracked people: evenly spread through the crowd, preferring people who receive an alert AND can actually
    # walk to a safe stop on the directed road graph (so the phone demo always has a route to show)
    reach = reaching_nodes(net, [d.node_id for d in destinations if d.available])  # nodes that can reach a safe stop
    pool = [j for j, k in enumerate(keep) if chans[k] is not None] or list(range(len(keep)))      # everyone inside an alert area
    sample = [pool[i * len(pool) // min(len(pool), 120)] for i in range(min(len(pool), 120))]  # up to 120 people spread evenly
    safe_ids = {d.node_id for d in destinations}  # nodes that are safe stops
    near = {j: net.nearest_node(bundle["lat"][0][keep[j]], bundle["lon"][0][keep[j]]) for j in sample}  # each candidate's nearest node
    ok = ([j for j in sample if near[j] in reach and near[j] not in safe_ids]       # needs a real walk to show
          or [j for j in sample if near[j] in reach] or sample)  # fall back to weaker conditions
    count = min(tracked, len(ok))  # how many to track
    pick = [ok[i * len(ok) // max(1, count)] for i in range(count)]  # evenly spaced choices
    people = [{"k": j, "name": PEOPLE_NAMES[i % len(PEOPLE_NAMES)] + ("" if i < len(PEOPLE_NAMES) else f" {i}"),  # tracked person: index and name
               "alert": alerts[keep[j]], "dep": deps[keep[j]], "ch": chans[keep[j]]} for i, j in enumerate(pick)]  # alert, departure, channel

    view_pts = ([(net.nodes[o.node_id].lat, net.nodes[o.node_id].lon) for o in origins]  # camera points: origins
                + [(net.nodes[d.node_id].lat, net.nodes[d.node_id].lon) for d in destinations]  # safe stops
                + [(z["lat"], z["lon"]) for z in zones] + [(h["lat"], h["lon"]) for h in hazards])  # danger zones and bombs
    view = view_for(net, view_pts)  # the camera
    cx, cy = view["lon"], view["lat"]  # its centre
    near = sorted(buildings, key=lambda b: (b["polygon"][0][0] - cx) ** 2 + (b["polygon"][0][1] - cy) ** 2)[:9000]  # the 9000 buildings closest to the centre

    data = {  # everything the page needs
        "times": times, "n": len(keep), "total": sum(bundle["stats"][0]) if bundle["stats"] else n_agents,  # frame times and counts
        "states": [[STATE_CODE[f[k]] for k in keep] for f in bundle["states"]],  # agent states per frame
        "lat": [[_r(f[k]) for k in keep] for f in bundle["lat"]],  # latitudes per frame
        "lon": [[_r(f[k]) for k in keep] for f in bundle["lon"]],  # longitudes per frame
        "stats": bundle["stats"], "occ": occ, "roads": roads, "graph": graph, "bombs": bombs, "people": people,  # statistics, roads, graph, bombs, tracked people
        "closures": ([[road_of[c.edge_id], c.close_time_seconds] for c in closures if c.edge_id in road_of]  # scheduled closures
                     + blast_closures),  # plus blast closures
        "buildings": [{"p": [[_r(x), _r(y)] for x, y in b["polygon"]], "h": b["height"], "c": b["color"][:3]} for b in near],  # building shapes, heights, colours
        "zones": [{"p": [[_r(x), _r(y)] for x, y in sc.circle(z["lat"], z["lon"], z["radius_m"])],  # danger zone outline
                   "pr": [[_r(x), _r(y)] for x, y in sc.circle(z["lat"], z["lon"], z["radius_m"] * 1.04)],  # its rim
                   "lon": z["lon"], "lat": z["lat"], "r": z["radius_m"]} for z in zones],  # its centre and radius
        "safe": safe,  # safe stops
        "origins": [{"lon": net.nodes[o.node_id].lon, "lat": net.nodes[o.node_id].lat,  # start points: position
                     "tip": f"START POINT: {o.label or o.node_id}"} for o in origins],  # and hover text
        "focus": focus, "view": view, "synthetic": net.is_synthetic,  # optional focus, camera, fake-map flag
        "icons": {"exit": sc.EXIT_ICON["url"], "danger": sc.DANGER_ICON["url"]},  # sign pictures
        "attribution": "Map data © OpenStreetMap contributors (ODbL)",  # required credit
        "sync": sync_url, "run": run_id,  # phone-server address and run id
    }
    if payload_only:                      # used by the Node unit tests of live_core.js
        return data  # just the data
    # the big library goes in LAST so its text can never be mistaken for a placeholder
    return (TEMPLATE.read_text(encoding="utf-8").replace("__HEIGHT__", str(height))  # fill the page height
            .replace("__CORE__", CORE.read_text(encoding="utf-8"))  # insert the shared logic
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))).replace("__DECK__", _deck_bundle()))  # insert the data, then the library


def build_phone_html(net: RoadNetwork, bundle: dict, destinations: Sequence[DestinationSpec], hazards: Sequence[dict],
                     closures: Sequence, run_id: int, sync_url: str = "/state") -> str:  # build the mobile page
    """Stand-alone mobile page: the phone UI only (alert, safe-stop route, mini-map, D-pad), no 3-D engine.

    It carries the walking graph and the frames of the tracked people only, so it is small enough for a phone.
    It talks to the phone server (``/state``) so that its clock, selected person and manual avatar stay in sync
    with the dashboard.
    """
    full = build_live_html(net, bundle, destinations, [], [], closures, [], hazards=hazards, payload_only=True,  # reuse the data builder (part 1)
                           run_id=run_id)  # (part 2)
    people = []  # tracked people with their own frames only
    for p in full["people"]:  # each tracked person
        k = p["k"]  # their agent index
        people.append({"id": k, "name": p["name"], "alert": p["alert"], "dep": p["dep"], "ch": p.get("ch"),  # identity, alert, departure, channel
                       "lon": [f[k] for f in full["lon"]], "lat": [f[k] for f in full["lat"]],  # their longitude and latitude per frame
                       "st": [f[k] for f in full["states"]]})  # their state per frame
    data = {"times": full["times"], "people": people, "graph": full["graph"], "bombs": full["bombs"],  # the phone page's data (part 1)
            "safe": [{k: v for k, v in s.items() if k not in ("p", "p2", "tip")} for s in full["safe"]],  # safe stops without drawing details
            "closures": full["closures"], "view": full["view"], "synthetic": full["synthetic"], "run": run_id,  # closures, camera, flags
            "sync": sync_url}  # where to sync
    return (PHONE_TEMPLATE.read_text(encoding="utf-8").replace("__CORE__", CORE.read_text(encoding="utf-8"))  # insert the shared logic
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))  # insert the data
