"""PANIC ENGINE - single-page LIVE 3-D evacuation + bomb-alert simulator.   Launch with:  streamlit run app.py
Toggle
Sidebar: MAP, SIMULATION, BOMB AREAS, DANGER ZONES, PEOPLE.
Tabs: Live simulation (3-D replay + phone + manual control), Analytics, Strategies, SQL.
A phone page (phone_server.py) mirrors the simulation on a real phone.
SQLite (Python's built-in sqlite3 module) is the database: this file opens the connection itself
and runs its own SQL queries; repositories.py does the transactional writes.
"""
# ----------------------------------------------------------------------------- imports
import json  # JSON text <-> Python objects (scenario descriptions store danger zones as JSON)
import math  # square roots / trigonometry for map distances
import sqlite3  # Python's built-in SQLite driver: the project's database engine
from contextlib import contextmanager  # lets us write a "with open_db() as conn:" helper
from datetime import datetime  # timestamps used in scenario names

import networkx as nx  # graph library: shortest-path distances over the road network
import streamlit as st  # the web-app framework that draws this page
import streamlit.components.v1 as components  # older Streamlit way of embedding custom HTML

from config import settings  # paths (database file, cache folder), defaults, strategy names
from database import analytics as an  # SQL analytics (queries live in database/sql_queries.py)
from database import repositories as repo  # reads/writes of scenarios, runs, networks (all SQL, all parameterised)
from geo import geographic_utils as geo  # haversine distance between latitude/longitude points
from geo import network_cache as cache  # list of OpenStreetMap networks cached on disk
from geo import network_loader as loader  # OpenStreetMap download + building footprints
from geo.synthetic_network import build_synthetic_network, default_origins_destinations  # offline fallback map
from simulation.experiments import run_scenario  # runs the agent simulation and stores the result in SQLite
from simulation.models import (ClosureSpec, DestinationSpec, ModelParams, OriginSpec, ScenarioConfig,
                               validate_scenario)  # scenario data classes + validation
from visualization import dashboard  # the Analytics / Strategies / SQL tabs
from visualization import live_view as lv  # builds the browser-side 3-D live replay page
from visualization import phone_server  # tiny LAN server that serves the page for a real phone
from visualization import scene3d as sc  # decorative 3-D building blocks
from visualization.theme import STRETCH, setup_page  # page styling + "fill the width" keyword for this Streamlit version

settings.configure_logging()  # send log messages (errors, run summaries) to the console
phone_port = phone_server.ensure_started()  # start the phone web server once; remember which port it got
setup_page("PANIC ENGINE", icon="⬛", header=False)  # page title, dark theme CSS, and make sure the SQLite schema exists

# ----------------------------------------------------------------------------- SQLite access (direct sqlite3)
# SQL used directly by this page. "?" placeholders are bound parameters, so user input is never pasted into SQL.
RUNS_SQL = """
SELECT r.run_id, r.scenario_id, s.scenario_name, r.routing_strategy, r.total_agents, r.evacuated_agents,
       r.unresolved_agents, r.failed_agents, r.completion_rate, r.median_evacuation_time_seconds
FROM simulation_runs AS r
JOIN scenarios AS s ON s.scenario_id = r.scenario_id
WHERE r.status = 'COMPLETED' AND s.network_id = ?
ORDER BY r.run_id DESC
"""  # every finished run on the chosen road network, newest first (a JOIN between runs and scenarios)
FRAME_COUNT_SQL = "SELECT COUNT(*) FROM run_frame_stats WHERE run_id = ?"  # how many replay frames were stored for a run
DB_STATS_SQL = """
SELECT 'scenarios' AS item, COUNT(*) AS n FROM scenarios
UNION ALL SELECT 'runs', COUNT(*) FROM simulation_runs
UNION ALL SELECT 'people (agents)', COUNT(*) FROM agents
UNION ALL SELECT 'events', COUNT(*) FROM simulation_events
UNION ALL SELECT 'occupancy snapshots', COUNT(*) FROM edge_occupancy_snapshots
"""  # row counts of the main tables, shown in the sidebar as proof the data really lives in SQLite


@contextmanager  # turns the generator below into a "with" block that always cleans up
def open_db():
    """Open the SQLite database file with the settings this project relies on, and always close it afterwards."""
    conn = sqlite3.connect(str(settings.DB_PATH), timeout=30, isolation_level=None)  # open the .db file; wait up to 30 s if locked; autocommit so repositories control transactions
    conn.row_factory = sqlite3.Row  # rows behave like dictionaries: row["run_id"]
    conn.execute("PRAGMA foreign_keys = ON")  # make SQLite enforce FOREIGN KEY constraints (it ignores them by default)
    conn.execute("PRAGMA journal_mode = WAL")  # write-ahead log: readers (this page) and the writer (a run) do not block each other
    try:  # hand the connection to the caller's "with" block
        yield conn  # the caller uses the connection here
    finally:  # runs even if the caller raised an error
        conn.close()  # release the file handle


# ----------------------------------------------------------------------------- helpers
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


@st.cache_resource(show_spinner="Loading the saved road network...")  # computed once per session and reused on every rerun
def osm_network(key: str, db_path: str):
    """Parse the GraphML cache and store it in SQLite ONCE per session (not on every Streamlit rerun)."""
    with open_db() as c:  # a short-lived SQLite connection
        return repo.save_network(c, loader.load_cached_network(key))  # INSERT the network if new, and return it with database ids


@st.cache_resource(show_spinner=False)  # cached for the session
def synthetic_network_saved(db_path: str):
    """Store the synthetic grid in SQLite once and return it."""
    with open_db() as c:  # a short-lived SQLite connection
        return repo.save_network(c, build_synthetic_network())  # idempotent: identical content is stored only once


@st.cache_data(show_spinner=False)  # cached by its arguments
def buildings_cached(lat: float, lon: float, radius_m: float, stamp: float):
    """OpenStreetMap building footprints from the on-disk cache (empty list if none)."""
    return loader.load_buildings(lat, lon, radius_m)  # reads data/cache/buildings_*.json


@st.cache_data(show_spinner="Preparing the live 3-D view...", max_entries=3)  # keep the last 3 built pages
def live_page(run_id: int, height: int, db_path: str, port: int) -> str:
    """Self-contained HTML for one run. Cached, so reruns (sidebar edits) do not rebuild or reload it."""
    with open_db() as c:  # read everything the page needs from SQLite in one connection
        run = repo.get_run(c, run_id)  # SELECT the run row
        cfg = repo.load_scenario_config(c, run["scenario_id"])  # SELECT the scenario, its origins, destinations, closures
        net = repo.load_network(c, cfg.network_id)  # SELECT the road network (nodes + edges)
        bundle = repo.load_replay_bundle(c, run_id)  # SELECT all stored frames, counters and occupancy for the replay
    zones = json.loads(cfg.description.split("danger_zones=", 1)[1]) if cfg.description.startswith("danger_zones=") else []  # danger zones were saved as JSON text in the description
    keep_clear = ([(net.nodes[d.node_id].lat, net.nodes[d.node_id].lon, 70.0) for d in cfg.destinations]  # no decorative buildings on safe stops ...
                  + [(z["lat"], z["lon"], z["radius_m"]) for z in zones]  # ... nor inside danger zones ...
                  + [(h["lat"], h["lon"], h["blast_radius_m"]) for h in cfg.params.hazards])  # ... nor inside blast radii
    if net.is_synthetic:  # offline grid: generate decorative blocks
        blds = sc.synthetic_buildings(net, keep_clear)  # deterministic blocks between the grid roads
    else:  # real map: use OpenStreetMap footprints if they were fetched
        raw = []  # footprints found so far
        for m in cache.list_cached():  # try each cached area
            raw = buildings_cached(m["lat"], m["lon"], m["radius_m"], 0.0)  # footprints of that area
            if raw:  # found some
                break  # stop searching
        blds = sc.osm_buildings(raw)  # convert to drawable 3-D blocks
    return lv.build_live_html(net, bundle, cfg.destinations, cfg.origins, zones, cfg.closures, blds,  # assemble the HTML page
                              cfg.params.edge_capacity_overrides, None, height, hazards=cfg.params.hazards,  # hazards drive the alert / blast visuals
                              sync_url=f"http://127.0.0.1:{port}/state", run_id=run_id)  # where the page syncs with the phone


def build_config(net, *, name, strategy, agents, seed, dt, limit, starts, safe_nodes, safe_cap, safe_thr, zones,
                 danger_t, hazards, dist, delay, spread, alert_channels=None):
    """Collect all sidebar choices into one ScenarioConfig object."""
    closures, seen = [], set()  # road closures caused by danger zones; edges already added
    for z in zones:  # each user-placed danger zone
        for eid in net.edges_within(z["lat"], z["lon"], z["radius_m"]):  # roads fully inside the circle
            if eid not in seen:  # avoid duplicates when zones overlap
                seen.add(eid)  # remember this edge
                closures.append(ClosureSpec(eid, float(danger_t)))  # close it at the chosen time
    with open_db() as c:  # look up safe-stop rows that already exist in SQLite
        existing = {(d.node_id, d.name): d.destination_id for d in repo.list_destinations(c, net.network_id)}  # (node, name) -> destination id
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
                           spread_radius_m=float(spread), hazards=list(hazards),  # start spread and the bombs
                           alert_channels=dict(alert_channels or {})))  # how the alert reaches people (phone / siren / neighbour)


def run_config(conn, net, cfg):
    """Validate, save and simulate. Returns run id or None (errors are shown)."""
    errors, warnings = validate_scenario(net, cfg)  # check the scenario before spending time on it
    for e in errors:  # show every blocking problem
        st.error(e)  # red message on the page
    if errors:  # cannot run
        return None  # signal "no run"
    for h in cfg.params.hazards:  # warn about safe stops that a bomb would destroy
        for d in cfg.destinations:  # every safe stop
            nd = net.nodes[d.node_id]  # its location
            if geo.haversine_m(nd.lat, nd.lon, h["lat"], h["lon"]) <= h["blast_radius_m"]:  # inside the blast circle?
                st.warning(f"{node_label(net, d.node_id)} is inside the blast radius: that safe stop is lost at the strike.")  # tell the user
    for w in warnings:  # non-blocking notes
        if "SYNTHETIC" not in w and "hypothetical" not in w:  # skip notes already obvious from the page
            st.warning(w)  # show the rest
    with st.spinner("Simulating and saving to SQL..."):  # progress indicator while the simulation runs
        sid = repo.save_scenario(conn, cfg, net)  # INSERT scenario, origins, destinations, closures in one transaction
        repo.ensure_population(conn, sid, net, cfg)  # INSERT the generated people (agents) once per scenario
        return run_scenario(conn, sid, cfg.routing_strategy, label="live")  # simulate, then INSERT events, snapshots, outcomes atomically


# ----------------------------------------------------------------------------- app
with open_db() as conn:  # one SQLite connection for this whole page run
    # ----------------------------------------------------------- MAP
    st.sidebar.markdown("### Map")  # sidebar section title
    cached = cache.list_cached()  # real-map networks already downloaded to disk
    with st.sidebar.expander("Download real campus-area map", expanded=not bool(cached)):  # collapsible box; open when no map exists yet
        with st.form("osm_download_form", clear_on_submit=False):  # a form: nothing happens until the button is pressed
            map_lat = st.number_input("Centre latitude", value=float(settings.CENTER_LAT), format="%.5f", key="map_lat")  # latitude to download around
            map_lon = st.number_input("Centre longitude", value=float(settings.CENTER_LON), format="%.5f", key="map_lon")  # longitude to download around
            map_radius = st.slider("Coverage radius (km)", 0.5, 8.0, 2.5, 0.5, key="map_radius",  # how far around the centre
                                   help="Bigger areas need far more data and can time out on the public Overpass server. 2-3 km covers the campus.")  # tooltip
            download_submitted = st.form_submit_button("DOWNLOAD REAL ROAD NETWORK", **STRETCH)  # the only thing that triggers a download
        if download_submitted:  # the user pressed the button
            try:  # a failed download must not crash the page
                with st.spinner("Requesting pedestrian roads from OpenStreetMap..."):  # progress indicator
                    key = loader.download_osm_network(float(map_lat), float(map_lon), int(map_radius * 1000),  # download + cache as GraphML
                                                      settings.NETWORK_TYPE, status=st.info)  # walking network; status lines shown on the page
                st.session_state["preferred_osm_cache_key"] = key  # select the new map afterwards
                st.session_state["map_download_message"] = ("success", f"Road network cached successfully: {key}")  # message for the next run
                st.rerun()  # redraw the page with the new map
            except Exception as exc:  # network timeout, Overpass busy, missing package ...
                st.session_state["map_download_message"] = ("error", f"Could not download the real map: {type(exc).__name__}: {exc}")  # keep the reason
        message = st.session_state.pop("map_download_message", None)  # take (and clear) the pending message
        if message:  # there is one
            if message[0] == "success":  # download worked
                st.success(message[1])  # green message
            else:  # download failed
                st.error(message[1])  # red message with the reason
                st.info("Try a smaller radius (1-3 km): large areas often time out on the public Overpass server. An existing cached map stays usable.")  # advice
    cached = cache.list_cached()  # re-read: a download may just have finished
    choices = ["Synthetic grid"] + (["OpenStreetMap (cached)"] if cached else [])  # offer the real map only if one exists
    choice = st.sidebar.radio("Road network", choices, index=1 if cached else 0, key="road_network_choice",  # pick the map; default to the real one
                              help="Synthetic grid is a labelled offline fallback. OpenStreetMap uses cached real road geometry.")  # tooltip
    if choice.startswith("Synthetic"):  # offline grid chosen
        net = synthetic_network_saved(str(settings.DB_PATH))  # make sure it is in SQLite and load it
    else:  # real map chosen
        preferred_key = st.session_state.get("preferred_osm_cache_key")  # the map downloaded most recently, if any
        default_cache_index = next((i for i, m in enumerate(cached) if m["key"] == preferred_key), 0)  # its position in the list
        meta = st.sidebar.selectbox("Cached area", cached, index=default_cache_index, key="cached_osm_area",  # choose among cached areas
                                    format_func=lambda m: f"r={int(m['radius_m'])} m · {m['nodes']} nodes")  # label: radius and size
        st.session_state["preferred_osm_cache_key"] = meta["key"]  # remember the choice
        net = osm_network(meta["key"], str(settings.DB_PATH))  # parse it, store it in SQLite, load it
        if st.sidebar.button("Fetch building footprints (once)"):  # optional: download 3-D building shapes
            with st.spinner("Downloading OpenStreetMap buildings..."):  # progress indicator
                got = loader.load_buildings(meta["lat"], meta["lon"], meta["radius_m"], download=True)  # download and cache them
            buildings_cached.clear()  # forget stale empty results
            live_page.clear()  # rebuild pages so they include the buildings
            st.sidebar.success(f"{len(got)} buildings cached" if got else "No buildings available")  # report the result

    def_origins, def_dests = default_layout(net)  # default start point and safe stops for this map
    bomb_default = default_bomb_node(net)  # default bomb position
    node_ids = candidate_nodes(net, keep=list(def_origins) + list(def_dests) + [bomb_default])  # nodes offered in the dropdowns

    # ------------------------------------------------------ SIMULATION
    st.sidebar.markdown("### Simulation")  # sidebar section title
    strategy = st.sidebar.selectbox("Routing strategy", settings.ROUTING_STRATEGIES, index=1,  # how people choose routes (default: congestion-aware)
                                    format_func=lambda s: s.replace("_", " ").title(),  # "CONGESTION_AWARE" -> "Congestion Aware"
                                    help="Shortest path ignores crowds; congestion-aware avoids full roads; load-balanced assigns safe stops centrally.")  # tooltip
    c1, c2 = st.sidebar.columns(2)  # two side-by-side input boxes
    limit = c1.number_input("Time limit (s)", 300.0, 21600.0, 2400.0, step=300.0)  # maximum simulated seconds
    dt = c2.number_input("Timestep (s)", 1.0, 30.0, 5.0)  # simulated seconds per step
    seed = st.sidebar.number_input("Random seed", 0, 2**31 - 1, 42)  # same seed = same crowd = reproducible runs
    safe_nodes = st.sidebar.multiselect("Safe stops (green)", node_ids, default=def_dests,  # where people are heading
                                        format_func=lambda n: node_label(net, n),  # readable names
                                        help="Hypothetical safe stops you choose - not official shelters.")  # tooltip
    c1, c2 = st.sidebar.columns(2)  # two more side-by-side boxes
    safe_cap = c1.number_input("Capacity each", 1, 100000, 300)  # people a safe stop can take in total
    safe_thr = c2.number_input("Admit / step", 1, 1000, 8)  # people a safe stop can admit per timestep

    # ----------------------------------------------------------- BOMBS
    st.sidebar.markdown("### Bomb areas 💣")  # sidebar section title
    n_bombs = st.sidebar.slider("Bomb areas", 0, 3, 0, key="n_bombs",  # how many bombs (0 = none)
                                help="Each bomb has a blast radius, a larger alert radius and a strike time. Phones in the alert area warn people.")  # tooltip
    hazards = []  # one dictionary per bomb
    for i in range(n_bombs):  # one settings box per bomb
        with st.sidebar.expander(f"Bomb {i + 1}", expanded=(i == 0)):  # first one open, others collapsed
            idx0 = node_ids.index(bomb_default) if bomb_default in node_ids else 0  # list position of the default location
            node = st.selectbox("Location", node_ids, index=min(len(node_ids) - 1, idx0 + 7 * i), key=f"bn{i}",  # offset extra bombs so they do not overlap
                                format_func=lambda n: node_label(net, n))  # readable names
            blast = st.slider("Blast radius (m)", 50, 500, 150, 10, key=f"bb{i}")  # inside: roads close, people caught
            alert_r = st.slider("Alert radius (m)", 100, 2000, max(int(blast * 3.5), 200), 10, key=f"ba{i}",  # default 3.5x the blast radius
                                help="Phones inside this radius receive the emergency alert.")  # tooltip
            strike = st.number_input("Strike time (s)", 60, 3600, 420, 30, key=f"bs{i}")  # when it goes off
            lead = st.slider("Warning lead time (s)", 0, 600, 180, 10, key=f"bl{i}",  # how early people are warned
                             help="The alert is sent this many seconds before the strike.")  # tooltip
            hazards.append(make_hazard(net, node, blast, alert_r, strike, lead, i))  # store this bomb's settings

    # ------------------------------------------------------ ALERT CHANNELS
    alert_channels = {}  # how the alert reaches people; empty = everybody is reached by phone
    if hazards:  # alert channels only matter when there is a bomb
        st.sidebar.markdown("### Alert channels 📢")  # sidebar section title
        phone_pct = st.sidebar.slider("People with a phone at hand (%)", 0, 100, 100, 5, key="phone_pct",  # most people will NOT have their phone with them
                                      help="Only these people get the phone alert. The rest need a siren or a neighbour to warn them.")  # tooltip
        n_sirens = st.sidebar.slider("Sirens", 0, 3, 0, key="n_sirens",  # public warning sirens (0 = none)
                                     help="A siren warns everyone within its radius, with or without a phone.")  # tooltip
        sirens = []  # one dictionary per siren
        for i in range(n_sirens):  # one row per siren
            s1, s2 = st.sidebar.columns([3, 2])  # location box and radius slider
            idx_s = node_ids.index(bomb_default) if bomb_default in node_ids else 0  # list position of the bomb's default node
            sn = s1.selectbox(f"Siren {i + 1}", node_ids, index=min(len(node_ids) - 1, idx_s + 3 * (i + 1)), key=f"sn{i}",  # spread default sites around the bomb
                              format_func=lambda n: node_label(net, n))  # readable names
            sr = s2.slider("Radius m", 100, 800, 400, 50, key=f"sr{i}")  # how far the siren can be heard
            sirens.append({"node": sn, "lat": net.nodes[sn].lat, "lon": net.nodes[sn].lon, "radius_m": float(sr), "delay_s": 10.0})  # sounds 10 s after the alert time
        neigh_r = st.sidebar.slider("Neighbours pass the word within (m)", 0, 120, 0, 10, key="neigh_r",  # word-of-mouth relay (0 = off)
                                    help="Anybody this close to a warned person is told 30 s later, and passes it on.")  # tooltip
        if phone_pct < 100 or sirens or neigh_r:  # anything other than "everybody has a phone"
            alert_channels = {"phone_reach": phone_pct / 100.0, "phone_delay_max_s": 15.0, "sirens": sirens,  # share with a phone, delivery delay, sirens
                              "neighbour_radius_m": float(neigh_r), "neighbour_delay_s": 30.0}  # relay radius and delay

    # ------------------------------------------------------ DANGER ZONES
    st.sidebar.markdown("### Other danger zones")  # sidebar section title
    n_zones = st.sidebar.slider("Danger zones (roads closed)", 0, 3, 0)  # how many road-closing zones
    zones = []  # one dictionary per zone
    for i in range(n_zones):  # one row per zone
        z1, z2 = st.sidebar.columns([3, 2])  # location box (wide) and radius slider (narrow)
        zn = z1.selectbox(f"Zone {i + 1}", node_ids, index=min(len(node_ids) - 1, (i + 1) * len(node_ids) // 5),  # spread default positions
                          format_func=lambda n: node_label(net, n), key=f"dz{i}")  # readable names
        zr = z2.slider("Radius m", 50, 400, 150, 10, key=f"dr{i}")  # zone size
        zones.append({"node": zn, "lat": net.nodes[zn].lat, "lon": net.nodes[zn].lon, "radius_m": float(zr)})  # store the zone
    danger_t = st.sidebar.number_input("Zones close at (s)", 0.0, 3600.0, 0.0, step=30.0) if zones else 0.0  # closing time (only asked when zones exist)

    # ----------------------------------------------------------- PEOPLE
    st.sidebar.markdown("### People")  # sidebar section title
    agents = st.sidebar.number_input("Number of people", 1, 3000, 250, step=50)  # crowd size
    if hazards:  # with a bomb, residents live inside its alert area
        st.sidebar.caption("Residents are placed inside the bomb alert areas.")  # short note
        start_nodes = residents_nodes(net, hazards, safe_nodes)  # start nodes spread over the alert area
    else:  # without a bomb the user picks start points
        start_nodes = st.sidebar.multiselect("Start points", node_ids, default=def_origins,  # where people begin
                                             format_func=lambda n: node_label(net, n))  # readable names
    dist = st.sidebar.selectbox("Distribution", ["even", "concentrated", "weighted"],  # how people are split over start points
                                help="even: equal per start point; concentrated: 70 % at the first start point")  # tooltip
    delay = st.sidebar.slider("Reaction delay up to (s)", 0, 600, 90, 10,  # people wait a random 0..N seconds before moving
                              help="After the alert (or t=0 without a bomb) each person waits a random 0..N seconds before moving.")  # tooltip
    spread = st.sidebar.slider("Start spread radius (m)", 0, 300, 0, 10)  # scatter start positions around the start nodes

    st.sidebar.markdown("---")  # divider line
    run_clicked = st.sidebar.button("▶ RUN SIMULATION", type="primary", **STRETCH,  # the main action button
                                    disabled=st.session_state.get("busy", False))  # disabled while a run is in progress
    if run_clicked:  # the user pressed RUN SIMULATION
        st.session_state["busy"] = True  # guard against duplicate runs caused by Streamlit reruns
        try:  # make sure the guard is released afterwards
            cfg = build_config(net, name=f"{'Bomb drill' if hazards else 'Sim'} {datetime.now():%Y-%m-%d %H:%M:%S}",  # unique scenario name
                               strategy=strategy, agents=agents, seed=seed, dt=dt, limit=limit, starts=start_nodes,  # strategy, crowd, timing, starts
                               safe_nodes=safe_nodes, safe_cap=safe_cap, safe_thr=safe_thr, zones=zones, danger_t=danger_t,  # safe stops and zones
                               hazards=hazards, dist=dist, delay=delay, spread=spread, alert_channels=alert_channels)  # bombs, crowd behaviour, alert channels
            rid = run_config(conn, net, cfg)  # validate, store in SQLite, simulate
            if rid:  # success
                st.session_state["replay_run"] = rid  # show this run in the replay
        finally:  # always runs
            st.session_state["busy"] = False  # allow the next run

    # ----------------------------------------------------------- DATABASE (sidebar status)
    with st.sidebar.expander("SQLite database"):  # collapsible box proving what is stored
        st.code(f"{settings.DB_PATH}", language=None)  # location of the .db file
        schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]  # latest applied migration number
        fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]  # 1 if foreign keys are enforced on this connection
        st.write(f"SQLite {sqlite3.sqlite_version} · schema v{schema_version} · foreign keys {'ON' if fk_on else 'OFF'}")  # engine version and settings
        for row in conn.execute(DB_STATS_SQL):  # run the COUNT(*) query defined above
            st.write(f"{row['item']}: **{row['n']:,}**")  # one line per table

    # ------------------------------------------------------------- HEADER
    runs = conn.execute(RUNS_SQL, (net.network_id,)).fetchall()  # SQL: all finished runs on this network (bound parameter)
    chips = ['<span class="pe-chip white">REAL OPENSTREETMAP DATA</span>' if not net.is_synthetic  # label: real map ...
             else '<span class="pe-chip amber">SYNTHETIC NETWORK · NOT REAL ROADS</span>',  # ... or the synthetic fallback
             f'<span class="pe-chip">{len(net.nodes):,} nodes · {len(net.edges):,} road segments</span>',  # size of the network
             f'<span class="pe-chip">{len(runs)} saved run(s)</span>']  # number of runs in SQLite
    if n_bombs:  # bombs are configured in the sidebar
        chips.append(f'<span class="pe-chip red">💣 {n_bombs} bomb area(s) configured</span>')  # remind the user
    st.markdown(f'<div class="pe-top"><div><div class="pe-brand">PANIC<i>.</i>ENGINE</div>'  # brand name
                f'</div>'  # close the brand block
                f'<div class="pe-chips">{"".join(chips)}</div></div>', unsafe_allow_html=True)  # status chips, drawn as HTML

    tabs = st.tabs(["▶ LIVE SIMULATION", "📊 ANALYTICS", "⚖ STRATEGIES", "🗄 SQL"])  # the four tabs
    with tabs[0]:  # ---- LIVE SIMULATION tab
        c1, c2, c3, c4 = st.columns([2, 3.6, 1.3, 1.5])  # demo button | run picker | phone button | safe-stop card button
        if c1.button("💣 RUN BOMB-ALERT DEMO", type="primary", **STRETCH,  # one-click demo
                     disabled=st.session_state.get("busy", False)):  # disabled while busy
            st.session_state["busy"] = True  # block duplicate clicks
            try:  # release the guard afterwards
                hz = [make_hazard(net, bomb_default, 150, 525, 420, 180, 0)]  # a bomb in the middle: 150 m blast, 525 m alert, strikes at 420 s, alert at 240 s
                cfg = build_config(net, name=f"Bomb drill demo {datetime.now():%H:%M:%S}", strategy="CONGESTION_AWARE",  # demo scenario
                                   agents=250, seed=42, dt=5.0, limit=1800.0, starts=residents_nodes(net, hz, def_dests),  # 250 residents in the alert area
                                   safe_nodes=def_dests, safe_cap=300, safe_thr=8, zones=[], danger_t=0.0, hazards=hz,  # default safe stops, no extra zones
                                   dist="even", delay=90, spread=0)  # even split, up to 90 s reaction delay
                rid = run_config(conn, net, cfg)  # validate, store in SQLite, simulate
                if rid:  # success
                    st.session_state["replay_run"] = rid  # replay it
                    st.rerun()  # redraw the page so the new run appears
            finally:  # always runs
                st.session_state["busy"] = False  # allow the next click
        runs = conn.execute(RUNS_SQL, (net.network_id,)).fetchall()  # SQL again: the list may have just grown
        if not runs:  # nothing to replay yet
            c2.info("Press the demo button, or set up the sidebar and press RUN SIMULATION.")  # hint
        else:  # at least one run exists
            rids = [r["run_id"] for r in runs]  # run ids, newest first
            cur = st.session_state.get("replay_run", rids[0])  # the run being shown (default: newest)
            by_id = {r["run_id"]: r for r in runs}  # quick lookup run id -> row
            run_id = c2.selectbox("Replay run", rids, index=rids.index(cur) if cur in rids else 0,  # choose which run to replay
                                  label_visibility="collapsed", format_func=lambda i: (  # hide the label to save space
                f"Run #{i} · {by_id[i]['scenario_name']} · {by_id[i]['routing_strategy'].replace('_', ' ').title()} · "  # name and strategy
                f"{by_id[i]['total_agents']} people · {by_id[i]['completion_rate']:.0%} safe"))  # crowd size and completion rate
            st.session_state["replay_run"] = run_id  # remember the choice
            with c3.popover("📱 PHONE VIEW", **STRETCH):  # pop-up with the address for a real phone
                url = phone_server.phone_url(phone_port, run_id)  # http://<this PC's address>:<port>/phone?run=<id>
                st.code(url, language=None)  # show the address
                try:  # the QR code needs the optional "segno" package
                    import io  # in-memory byte buffer
                    import segno  # QR code generator
                    buf = io.BytesIO()  # buffer to hold the PNG
                    segno.make(url, error="m").save(buf, kind="png", scale=6, border=2)  # draw the QR code as a PNG
                    st.image(buf.getvalue(), width=190)  # show it
                except ImportError:  # segno not installed
                    st.caption("pip install segno to show a QR code")  # tell the user how to get it
                st.caption("Same Wi-Fi as this PC")  # the phone must be on the same network
            with c4.popover("🏠 SAFE-STOP CARD", **STRETCH):  # page that works BEFORE any alert: where is my nearest safe stop?
                surl = phone_server.safe_url(phone_port, by_id[run_id]["scenario_id"])  # http://<this PC>:<port>/safe?scenario=<id>
                st.code(surl, language=None)  # show the address
                try:  # QR code needs the optional "segno" package
                    import io as _io  # in-memory byte buffer
                    import segno as _segno  # QR code generator
                    sbuf = _io.BytesIO()  # buffer for the PNG
                    _segno.make(surl, error="m").save(sbuf, kind="png", scale=6, border=2)  # draw the QR code
                    st.image(sbuf.getvalue(), width=190)  # show it
                except ImportError:  # segno missing
                    st.caption("pip install segno to show a QR code")  # tell the user how to get it
                st.link_button("Open the page", surl)  # open it in this browser (print the card from there)
            run = by_id[run_id]  # the SQL row of the chosen run (already fetched above)
            frames = conn.execute(FRAME_COUNT_SQL, (run_id,)).fetchone()[0]  # SQL: number of stored replay frames
            if not frames:  # nothing to animate
                st.warning("This run has no stored frames.")  # explain
            else:  # frames exist
                page = live_page(run_id, 760, str(settings.DB_PATH), phone_port)  # the 3-D replay page (built from SQLite, cached)
                if hasattr(st, "iframe"):  # newer Streamlit: components.html is deprecated
                    st.iframe(page, height=775)  # embed the page
                else:  # older Streamlit
                    components.html(page, height=775, scrolling=False)  # embed the page the old way
                m = st.columns(5)  # five metric boxes
                m[0].metric("Reached a safe stop", f"{run['evacuated_agents']:,} / {run['total_agents']:,}")  # people who made it
                m[1].metric("Completion", f"{run['completion_rate']:.0%}")  # fraction who made it
                m[2].metric("Unresolved at limit", f"{run['unresolved_agents']:,}")  # still walking when time ran out
                m[3].metric("Unable / caught", f"{run['failed_agents']:,}")  # no route, or caught in the blast
                med = run["median_evacuation_time_seconds"]  # median seconds to safety (None if nobody made it)
                m[4].metric("Median time to safety", "n/a" if med is None else f"{med:,.0f} s")  # shown in seconds
                hz = an.hazard_summary(conn, run_id)  # SQL over the event log: alerts, strike time, people caught
                if hz["first_strike_s"] is not None:  # this run contained a bomb
                    h = st.columns(4)  # four more metric boxes
                    h[0].metric("Phone alerts sent", f"{hz['alerts_sent']:,}")  # number of ALERT_SENT events
                    h[1].metric("Warning time", f"{hz['first_strike_s'] - hz['first_alert_s']:,.0f} s")  # seconds between alert and strike
                    h[2].metric("Safe before the blast", f"{hz['safe_before_strike']:,}")  # people already at a safe stop at the strike
                    h[3].metric("Caught in blast area", f"{hz['caught_in_blast']:,}")  # people inside the blast radius at the strike
                    chans = an.alert_channel_summary(conn, run_id)  # SQL: how many people each channel reached, and what happened to them
                    counts = dict(zip(chans["channel"], chans["people"]))  # channel -> number of people
                    if set(counts) - {"PHONE", "OUTSIDE ALERT AREA"}:  # some people were not reached by phone
                        k = st.columns(4)  # four more metric boxes
                        k[0].metric("Alerted by phone", f"{int(counts.get('PHONE', 0)):,}")  # had a phone at hand
                        k[1].metric("Alerted by siren", f"{int(counts.get('SIREN', 0)):,}")  # heard a siren
                        k[2].metric("Told by a neighbour", f"{int(counts.get('NEIGHBOUR', 0)):,}")  # word of mouth
                        k[3].metric("Never alerted", f"{int(counts.get('NONE', 0)):,}")  # nobody reached them
    with tabs[1]:  # ---- ANALYTICS tab
        dashboard.render_analytics(conn)  # congestion, bottlenecks, outcomes (SQL)
    with tabs[2]:  # ---- STRATEGIES tab
        dashboard.render_strategies(conn)  # compare routing strategies, what-ifs, alert lead-time experiment
    with tabs[3]:  # ---- SQL tab
        dashboard.render_sql(conn)  # every query, a read-only SQL console, integrity checks
