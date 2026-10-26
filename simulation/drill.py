"""Scenario building shared by the dashboard (app.py) and the command-line scripts.

Pure functions: they take a road network and choices, and return start/safe-stop nodes, bomb
descriptions and a ScenarioConfig. No Streamlit and no database access, so scripts can reuse them.
"""
import math  # square roots / trigonometry for map distances
import json  # danger zones are stored as JSON text in the scenario description

import networkx as nx  # graph library: walking distances over the road network

from geo import geographic_utils as geo  # haversine distance between latitude/longitude points
from geo.synthetic_network import build_synthetic_network, default_origins_destinations  # offline fallback map
from simulation.models import (ClosureSpec, DestinationSpec, ModelParams, OriginSpec,
                               ScenarioConfig)  # scenario data classes
from visualization import live_view as lv  # reaching_nodes: which nodes can walk to a safe stop


def node_label(net, nid: int) -> str:
    """Human-readable name of a road-network node (grid position on the synthetic map, coordinates on a real one)."""
    n = net.nodes[nid]  # the node object (id, latitude, longitude)
    if net.is_synthetic and net.metadata.get("cols"):  # the synthetic grid knows its number of columns
        idx = sorted(net.nodes).index(nid)  # position of this node in id order = row-major grid index
        return f"Grid r{idx // net.metadata['cols'] + 1}-c{idx % net.metadata['cols'] + 1}"  # e.g. "Grid r4-c6"
    return f"Node {nid} ({n.lat:.4f}, {n.lon:.4f})"  # real map: node id plus rounded coordinates


def candidate_nodes(net, keep=(), cell_m: float = 250.0):
    """Selectable nodes: real networks have thousands, so offer one node per ~250 m cell (plus chosen ones)."""
    if len(net.nodes) <= 600:  # small network (the synthetic grid): offer every node
        return sorted(net.nodes)  # all node ids in order
    s, w, n, e = net.bounds()  # south, west, north, east edges of the network
    dlat = cell_m / 111_320.0  # cell height in degrees of latitude (1 degree ~ 111.32 km)
    dlon = cell_m / (111_320.0 * math.cos(math.radians((s + n) / 2)))  # cell width in degrees of longitude at this latitude
    cells = {}  # (row, column) of a cell -> the first node found inside it
    for nid in sorted(net.nodes):  # visit nodes in a stable order
        nd = net.nodes[nid]  # this node
        cells.setdefault((int((nd.lat - s) / dlat), int((nd.lon - w) / dlon)), nid)  # keep only the first node of each cell
    return sorted(set(cells.values()) | set(keep))  # one node per cell, plus any node that must stay selectable


def default_layout(net):
    """Hypothetical start point and safe stops (reachable on foot at a moderate distance; not official shelters)."""
    if net.is_synthetic:  # offline grid: use the demo layout defined with the grid
        local = build_synthetic_network()  # a fresh copy whose node ids are 1..N
        to_db = dict(zip(sorted(local.nodes), sorted(net.nodes)))  # map those ids to the ids stored in SQLite
        origins, dests = default_origins_destinations(local)  # demo start points and safe stops on the grid
        return [to_db[n] for n, _ in origins], [to_db[d.node_id] for d in dests]  # translated to database node ids
    c = net.center or ((net.bounds()[0] + net.bounds()[2]) / 2, (net.bounds()[1] + net.bounds()[3]) / 2)  # map centre (or the middle of its bounding box)
    origin = net.nearest_node(*c)  # start point = the road node closest to the centre
    o = net.nodes[origin]  # its coordinates
    reachable = nx.single_source_dijkstra_path_length(net.forward_graph(), origin, weight="length", cutoff=3000)  # walking distance (m) to every node within 3 km, following one-way rules
    target = min(1000.0, 0.6 * max(reachable.values(), default=1000.0))  # aim for safe stops about 1 km away (less on a small map)
    best = {0: None, 1: None, 2: None}  # best candidate in each of three directions (sectors)
    for nid, dist in reachable.items():  # look at every reachable node
        if nid == origin:  # the start itself cannot be its own safe stop
            continue  # skip it
        n = net.nodes[nid]  # this candidate node
        k = int(((math.atan2(n.lat - o.lat, n.lon - o.lon) + math.pi) / (2 * math.pi)) * 3) % 3  # which third of the compass it lies in
        gap = abs(dist - target)  # how far its distance is from the 1 km target
        if best[k] is None or gap < best[k][0]:  # closer to the target than the current best in that sector?
            best[k] = (gap, nid)  # remember it
    return [origin], [v[1] for v in best.values() if v]  # one start node, up to three safe-stop nodes


def default_bomb_node(net) -> int:
    """Where the demo bomb goes: the middle of the map."""
    if net.is_synthetic:  # offline grid
        return sorted(net.nodes)[3 * net.metadata.get("cols", 10) + 5]  # a node near the grid centre
    c = net.center or ((net.bounds()[0] + net.bounds()[2]) / 2, (net.bounds()[1] + net.bounds()[3]) / 2)  # map centre
    return net.nearest_node(*c)  # the road node closest to it


def make_hazard(net, node, blast, alert_r, strike, lead, i):
    """Build one bomb description (stored with the scenario in SQLite as JSON)."""
    nd = net.nodes[node]  # the node where the bomb is placed
    return {"name": f"Bomb {i + 1}", "node": node, "lat": nd.lat, "lon": nd.lon,  # name and location
            "blast_radius_m": float(blast),  # inside this radius roads close and people are caught
            "alert_radius_m": float(max(alert_r, blast)),  # phones inside this radius get the alert (never smaller than the blast)
            "strike_time_s": float(strike),  # simulation second at which the bomb goes off
            "alert_time_s": float(max(0.0, strike - lead))}  # alert is sent `lead` seconds before the strike


def residents_nodes(net, hazards, safe_nodes=(), max_nodes: int = 60):
    """Start nodes for the people living inside the alert areas (evenly thinned so the crowd is spread out).

    Only nodes that can walk to a safe stop on the directed road graph are used, so nobody is stranded by data quirks."""
    reach = lv.reaching_nodes(net, list(safe_nodes)) if safe_nodes else None  # nodes from which some safe stop is reachable on foot
    inside = sorted({nid for nid, nd in net.nodes.items() for h in hazards  # every node ...
                     if geo.haversine_m(nd.lat, nd.lon, h["lat"], h["lon"]) <= h["alert_radius_m"]  # ... inside an alert radius ...
                     and (reach is None or nid in reach)})  # ... and able to reach a safe stop
    if not inside:  # nothing qualified (e.g. tiny alert radius)
        inside = [net.nearest_node(h["lat"], h["lon"]) for h in hazards]  # fall back to the node nearest each bomb
    step = max(1, math.ceil(len(inside) / max_nodes))  # thinning factor so at most ~60 start nodes remain
    return inside[::step]  # every `step`-th node


def build_config(net, *, name, strategy, agents, seed, dt, limit, starts, safe_nodes, safe_cap, safe_thr, zones,
                 danger_t, hazards, dist, delay, spread, alert_channels=None, existing=None, walking_speed=1.4):
    """Collect all sidebar choices into one ScenarioConfig object."""
    closures, seen = [], set()  # road closures caused by danger zones; edges already added
    for z in zones:  # each user-placed danger zone
        for eid in net.edges_within(z["lat"], z["lon"], z["radius_m"]):  # roads fully inside the circle
            if eid not in seen:  # avoid duplicates when zones overlap
                seen.add(eid)  # remember this edge
                closures.append(ClosureSpec(eid, float(danger_t)))  # close it at the chosen time
    existing = existing or {}  # (node, name) -> destination id of safe-stop rows already in SQLite (none for a fresh database)
    dests = []  # safe stops of this scenario
    for n in safe_nodes:  # each chosen safe-stop node
        nm = f"Safe area {node_label(net, n)}"  # its stored name
        dests.append(DestinationSpec(existing.get((n, nm)), n, nm, int(safe_cap), int(safe_thr)))  # reuse the existing row (id) if there is one
    return ScenarioConfig(  # the full scenario description
        name=name, network_id=net.network_id, routing_strategy=strategy, agent_count=int(agents), random_seed=int(seed),  # identity, strategy, crowd size, seed
        timestep_seconds=float(dt), time_limit_seconds=float(limit),  # simulation step and maximum duration
        description="danger_zones=" + json.dumps(zones), geographic_data_source=net.source,  # danger zones are kept as JSON text; data source label
        network_fingerprint=net.fingerprint(), origins=[OriginSpec(n, 1.0, node_label(net, n)) for n in starts],  # network version hash; start nodes
        destinations=dests, closures=closures,  # safe stops and closed roads
        params=ModelParams(crowd_distribution=dist, departure_delay_max_seconds=float(delay),  # crowd split and reaction delay
                           spread_radius_m=float(spread), hazards=list(hazards), walking_speed_mps=float(walking_speed),  # start spread, the bombs, walking speed
                           alert_channels=dict(alert_channels or {})))  # how the alert reaches people (phone / siren / neighbour)
