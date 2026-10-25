"""Data-access layer: every write/read of domain objects goes through here.

All SQL uses bound parameters. UI code and the simulation engine never build SQL.
"""
from __future__ import annotations  # modern type hints on older Python

import json  # store lists and dictionaries as text
import logging  # log saves
import sqlite3  # database type hints
from typing import Any, Dict, Iterable, List, Optional  # type hints

from config import settings  # walking speed and engine version
from database.connection import transaction  # all-or-nothing blocks
from simulation.agent import Agent, generate_population  # agents and the crowd generator
from simulation.models import (ClosureSpec, DestinationSpec, Edge, ModelParams, Node, OriginSpec,
                               RoadNetwork, ScenarioConfig)  # domain classes

log = logging.getLogger(__name__)  # logger for this module


def _rows(cur: sqlite3.Cursor) -> List[dict]:  # turn a cursor into a list of dictionaries
    return [dict(r) for r in cur.fetchall()]  # one dictionary per row


# ------------------------------------------------------------------ networks
def network_exists(conn: sqlite3.Connection, network_id: str) -> bool:  # is this network already stored?
    return conn.execute("SELECT 1 FROM networks WHERE network_id = ?", (network_id,)).fetchone() is not None  # any row means yes


def save_network(conn: sqlite3.Connection, net: RoadNetwork) -> RoadNetwork:  # store a network once
    """Persist a network once (idempotent) and return it with database ids.

    A network with the same content fingerprint is stored only once, so repeated
    Streamlit reruns or repeated seeding never duplicate road data.
    """
    network_id = net.network_id or f"{net.source.lower()}-{net.fingerprint()}"  # id from the content hash
    if network_exists(conn, network_id):  # already stored
        return load_network(conn, network_id)  # return the stored copy
    node_ids = sorted(net.nodes)  # nodes in a fixed order
    with transaction(conn):  # all-or-nothing
        conn.execute(  # the network row
            "INSERT INTO networks(network_id, name, source, is_synthetic, center_lat, center_lon, radius_m,"  # columns (1)
            " node_count, edge_count, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",  # columns (2) and placeholders
            (network_id, net.name, net.source, int(net.is_synthetic),  # values (1)
             net.center[0] if net.center else None, net.center[1] if net.center else None,  # values (2): centre
             net.radius_m, len(net.nodes), len(net.edges), json.dumps(net.metadata, default=str)))  # values (3)
        node_map: Dict[int, int] = {}  # local node id -> database node id
        for nid in node_ids:  # each node
            n = net.nodes[nid]  # the node
            cur = conn.execute(  # insert it
                "INSERT INTO network_nodes(network_id, source_osm_node_id, latitude, longitude) VALUES (?,?,?,?)",  # SQL
                (network_id, n.osm_id, n.lat, n.lon))  # values
            node_map[nid] = cur.lastrowid  # remember the database id
        for eid in sorted(net.edges):  # each road in a fixed order
            e = net.edges[eid]  # the road
            conn.execute(  # insert it
                "INSERT INTO network_edges(network_id, source_node_id, target_node_id, distance_meters,"  # columns (1)
                " estimated_travel_time_seconds, capacity, is_available, road_class, osm_way_id, geometry_json)"  # columns (2)
                " VALUES (?,?,?,?,?,?,?,?,?,?)",  # placeholders
                (network_id, node_map[e.source], node_map[e.target], e.length_m, e.free_flow_time_s, e.capacity,  # values (1): ends, length, time, capacity
                 int(e.base_available), e.road_class, e.osm_way_id,  # values (2)
                 json.dumps(e.geometry) if e.geometry else None))  # values (3): shape as JSON
    log.info("Saved network %s (%d nodes, %d edges)", network_id, len(net.nodes), len(net.edges))  # log it
    return load_network(conn, network_id)  # return the stored copy with database ids


def load_network(conn: sqlite3.Connection, network_id: str) -> RoadNetwork:  # read a network from the database
    row = conn.execute("SELECT * FROM networks WHERE network_id = ?", (network_id,)).fetchone()  # the network row
    if row is None:  # not found
        raise KeyError(f"network {network_id!r} not found")  # report it
    center = (row["center_lat"], row["center_lon"]) if row["center_lat"] is not None else None  # centre, if stored
    net = RoadNetwork(row["name"], row["source"], network_id=network_id, center=center,  # rebuild the object (part 1)
                      radius_m=row["radius_m"], metadata=json.loads(row["metadata_json"] or "{}"))  # (part 2)
    for r in conn.execute("SELECT node_id, source_osm_node_id, latitude, longitude FROM network_nodes"  # every node (part 1)
                          " WHERE network_id = ? ORDER BY node_id", (network_id,)):  # (part 2)
        net.nodes[r["node_id"]] = Node(r["node_id"], r["latitude"], r["longitude"], r["source_osm_node_id"])  # add it
    for r in conn.execute("SELECT * FROM network_edges WHERE network_id = ? ORDER BY edge_id", (network_id,)):  # every road
        geom = json.loads(r["geometry_json"]) if r["geometry_json"] else None  # its shape, if stored
        net.edges[r["edge_id"]] = Edge(  # add it
            r["edge_id"], r["source_node_id"], r["target_node_id"], r["distance_meters"],  # id, ends, length
            r["estimated_travel_time_seconds"] or r["distance_meters"] / settings.WALKING_SPEED_MPS,  # walking time (computed if missing)
            r["capacity"], r["road_class"] or "local", bool(r["is_available"]), r["osm_way_id"],  # capacity, class, availability, way id
            [tuple(p) for p in geom] if geom else None)  # shape as tuples
    net._invalidate()  # make sure cached graph views are rebuilt
    return net  # the network


def list_networks(conn: sqlite3.Connection) -> List[dict]:  # summary of stored networks
    return _rows(conn.execute("SELECT network_id, name, source, is_synthetic, node_count, edge_count, created_at"  # columns
                              " FROM networks ORDER BY created_at DESC"))  # newest first


# -------------------------------------------------------------- destinations
def save_destination(conn: sqlite3.Connection, network_id: str, spec: DestinationSpec,
                     lat: float, lon: float) -> int:  # store a safe stop
    cur = conn.execute(  # insert it
        "INSERT INTO destinations(network_id, node_id, destination_name, destination_type, latitude, longitude,"  # columns (1)
        " capacity, throughput_per_timestep, source_type, hypothetical_status) VALUES (?,?,?,?,?,?,?,?,?,?)",  # columns (2) and placeholders
        (network_id, spec.node_id, spec.name, spec.destination_type, lat, lon, spec.capacity,  # values (1)
         spec.throughput_per_timestep, spec.source_type, "HYPOTHETICAL" if spec.hypothetical else "VERIFIED"))  # values (2)
    return int(cur.lastrowid)  # its new id


def list_destinations(conn: sqlite3.Connection, network_id: str) -> List[DestinationSpec]:  # safe stops of a network
    out = []  # results
    for r in conn.execute("SELECT * FROM destinations WHERE network_id = ? ORDER BY destination_id", (network_id,)):  # each stored stop
        out.append(DestinationSpec(r["destination_id"], r["node_id"], r["destination_name"], r["capacity"],  # build the object (part 1)
                                   r["throughput_per_timestep"], True, r["destination_type"], r["source_type"],  # (part 2)
                                   r["hypothetical_status"] == "HYPOTHETICAL"))  # (part 3)
    return out  # the list


# ----------------------------------------------------------------- scenarios
def save_scenario(conn: sqlite3.Connection, cfg: ScenarioConfig, network: RoadNetwork) -> int:  # store a scenario
    """Insert a scenario with its origins, destinations and closures atomically."""
    with transaction(conn):  # all-or-nothing
        for d in cfg.destinations:  # each safe stop
            if d.destination_id is None:  # not stored yet
                node = network.nodes[d.node_id]  # its node
                d.destination_id = save_destination(conn, cfg.network_id, d, node.lat, node.lon)  # store it
        cur = conn.execute(  # the scenario row
            "INSERT INTO scenarios(scenario_name, description, study_area, geographic_data_source, network_id,"  # columns (1)
            " network_fingerprint, routing_strategy, agent_count, random_seed, timestep_seconds,"  # columns (2)
            " simulation_time_limit, configuration_json, parent_scenario_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",  # columns (3) and placeholders
            (cfg.name, cfg.description, cfg.study_area, cfg.geographic_data_source, cfg.network_id,  # values (1)
             cfg.network_fingerprint or network.fingerprint(), cfg.routing_strategy, cfg.agent_count,  # values (2)
             cfg.random_seed, cfg.timestep_seconds, cfg.time_limit_seconds, cfg.to_json(),  # values (3)
             cfg.parent_scenario_id))  # values (4)
        sid = int(cur.lastrowid)  # the new scenario id
        conn.executemany(  # its origins
            "INSERT INTO scenario_origins(scenario_id, node_id, weight, label, source_type) VALUES (?,?,?,?,?)",  # SQL
            [(sid, o.node_id, o.weight, o.label, o.source_type) for o in cfg.origins])  # one row per origin
        conn.executemany(  # its safe-stop settings
            "INSERT INTO scenario_destinations(scenario_id, destination_id, capacity, throughput_per_timestep,"  # columns (1)
            " is_available) VALUES (?,?,?,?,?)",  # columns (2) and placeholders
            [(sid, d.destination_id, d.capacity, d.throughput_per_timestep, int(d.available))  # one row per safe stop (part 1)
             for d in cfg.destinations])  # (part 2)
        conn.executemany(  # its road closures
            "INSERT INTO scenario_closures(scenario_id, edge_id, close_time_seconds) VALUES (?,?,?)",  # SQL
            [(sid, c.edge_id, c.close_time_seconds) for c in cfg.closures])  # one row per closure
    cfg.scenario_id = sid  # remember the id on the object
    return sid  # the new id


def load_scenario_config(conn: sqlite3.Connection, scenario_id: int) -> ScenarioConfig:  # read a scenario
    s = conn.execute("SELECT * FROM scenarios WHERE scenario_id = ?", (scenario_id,)).fetchone()  # the scenario row
    if s is None:  # not found
        raise KeyError(f"scenario {scenario_id} not found")  # report it
    cfg = ScenarioConfig(  # rebuild the object
        name=s["scenario_name"], network_id=s["network_id"], routing_strategy=s["routing_strategy"],  # identity and policy
        agent_count=s["agent_count"], random_seed=s["random_seed"], timestep_seconds=s["timestep_seconds"],  # size, seed, step
        time_limit_seconds=s["simulation_time_limit"], description=s["description"] or "",  # limit and description
        study_area=s["study_area"] or "", geographic_data_source=s["geographic_data_source"],  # place and data source
        network_fingerprint=s["network_fingerprint"],  # network hash
        params=ModelParams.from_dict(json.loads(s["configuration_json"] or "{}")),  # tunable settings from JSON
        scenario_id=scenario_id, parent_scenario_id=s["parent_scenario_id"])  # ids
    cfg.origins = [OriginSpec(r["node_id"], r["weight"], r["label"] or "", r["source_type"])  # its origins (part 1)
                   for r in conn.execute("SELECT * FROM scenario_origins WHERE scenario_id = ? ORDER BY node_id",  # (part 2)
                                         (scenario_id,))]  # (part 3)
    for r in conn.execute(  # its safe stops with this scenario's capacity settings
            "SELECT d.destination_id, d.node_id, d.destination_name, d.destination_type, d.source_type,"  # columns (1)
            " d.hypothetical_status, sd.capacity, sd.throughput_per_timestep, sd.is_available"  # columns (2)
            " FROM scenario_destinations sd JOIN destinations d USING (destination_id)"  # join the two tables
            " WHERE sd.scenario_id = ? ORDER BY d.destination_id", (scenario_id,)):  # filter and order
        cfg.destinations.append(DestinationSpec(  # add each one
            r["destination_id"], r["node_id"], r["destination_name"], r["capacity"], r["throughput_per_timestep"],  # identity and capacity
            bool(r["is_available"]), r["destination_type"], r["source_type"], r["hypothetical_status"] == "HYPOTHETICAL"))  # status and flags
    cfg.closures = [ClosureSpec(r["edge_id"], r["close_time_seconds"])  # its closures (part 1)
                    for r in conn.execute("SELECT edge_id, close_time_seconds FROM scenario_closures"  # (part 2)
                                          " WHERE scenario_id = ? ORDER BY edge_id", (scenario_id,))]  # (part 3)
    return cfg  # the scenario


def list_scenarios(conn: sqlite3.Connection) -> List[dict]:  # summary of all scenarios
    return _rows(conn.execute(  # run and convert
        "SELECT s.scenario_id, s.scenario_name, s.routing_strategy, s.agent_count, s.random_seed,"  # columns (1)
        " s.geographic_data_source, s.network_id, s.created_at, s.parent_scenario_id,"  # columns (2)
        " (SELECT COUNT(*) FROM simulation_runs r WHERE r.scenario_id = s.scenario_id) AS run_count"  # number of runs
        " FROM scenarios s ORDER BY s.scenario_id DESC"))  # newest first


def delete_scenario(conn: sqlite3.Connection, scenario_id: int) -> None:  # remove a scenario
    """Explicit user action only; cascades to agents, runs and their results."""
    with transaction(conn):  # all-or-nothing
        conn.execute("DELETE FROM scenarios WHERE scenario_id = ?", (scenario_id,))  # foreign keys cascade the rest


# ---------------------------------------------------------------- population
def save_population(conn: sqlite3.Connection, scenario_id: int, agents: Iterable[Agent]) -> List[Agent]:  # store the crowd
    out = []  # agents with their database ids
    with transaction(conn):  # all-or-nothing
        for a in agents:  # each agent
            cur = conn.execute(  # insert it
                "INSERT INTO agents(scenario_id, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel)"  # columns
                " VALUES (?,?,?,?,?)",  # placeholders
                (scenario_id, a.origin_node, a.departure_time, a.alert_time, a.alert_channel))  # values
            a.agent_id = int(cur.lastrowid)  # use the database id from now on
            out.append(a)  # keep it
    return out  # the stored agents


def load_population(conn: sqlite3.Connection, scenario_id: int) -> List[Agent]:  # read the crowd
    return [Agent(r["agent_id"], r["origin_node_id"], r["departure_time_seconds"], r["alert_time_seconds"], r["alert_channel"])  # build each agent
            for r in conn.execute("SELECT agent_id, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel FROM agents"  # query (1)
                                  " WHERE scenario_id = ? ORDER BY agent_id", (scenario_id,))]  # query (2)


def ensure_population(conn: sqlite3.Connection, scenario_id: int, network: RoadNetwork,
                      cfg: ScenarioConfig) -> List[Agent]:  # the crowd, generated once
    """Population is generated once per scenario, then reused by every run."""
    existing = load_population(conn, scenario_id)  # already stored?
    if existing:  # yes
        return existing  # reuse it
    return save_population(conn, scenario_id, generate_population(network, cfg))  # generate, store and return


def clone_population(conn: sqlite3.Connection, from_scenario: int, to_scenario: int) -> int:  # copy a crowd to another scenario
    with transaction(conn):  # all-or-nothing
        cur = conn.execute(  # copy inside SQL
            "INSERT INTO agents(scenario_id, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel)"  # target columns
            " SELECT ?, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel FROM agents"  # source columns
            " WHERE scenario_id = ? ORDER BY agent_id",  # from this scenario
            (to_scenario, from_scenario))  # values
    return cur.rowcount  # number of agents copied


# ---------------------------------------------------------------------- runs
def create_run(conn: sqlite3.Connection, scenario_id: int, strategy: str, seed: int,
               snapshot_json: str, label: Optional[str] = None) -> int:  # start a run record
    cur = conn.execute(  # insert it
        "INSERT INTO simulation_runs(scenario_id, label, status, routing_strategy, random_seed, engine_version,"  # columns (1)
        " configuration_snapshot) VALUES (?,?, 'RUNNING', ?,?,?,?)",  # columns (2); status starts as RUNNING
        (scenario_id, label, strategy, seed, settings.ENGINE_VERSION, snapshot_json))  # values
    return int(cur.lastrowid)  # the run id


def mark_run_failed(conn: sqlite3.Connection, run_id: int, reason: str) -> None:  # record a failure
    with transaction(conn):  # all-or-nothing
        conn.execute("UPDATE simulation_runs SET status = 'FAILED', failure_reason = ?,"  # set status and reason
                     " completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE run_id = ?", (reason[:500], run_id))  # time stamp; reason cut to 500 characters


def persist_result(conn: sqlite3.Connection, run_id: int, result: Any) -> None:  # store a finished run
    """Write every detail row and flip the run to COMPLETED in ONE transaction.

    If anything fails the transaction rolls back, so a run is either fully
    stored or has no detail rows at all (and is then marked FAILED by the caller).
    """
    m = result.metrics  # summary numbers
    with transaction(conn):  # all-or-nothing
        conn.executemany(  # one outcome row per agent
            "INSERT INTO agent_outcomes(run_id, agent_id, destination_id, final_state, current_node_id,"  # columns (1)
            " current_edge_id, departure_time_seconds, evacuation_time_seconds, accumulated_wait_seconds,"  # columns (2)
            " route_change_count) VALUES (?,?,?,?,?,?,?,?,?,?)",  # columns (3) and placeholders
            [(run_id, a.agent_id, a.destination_id, a.state.value, a.node, a.edge, a.departure_time,  # values (1)
              a.evacuation_time, a.wait_seconds, a.route_changes) for a in result.agents])  # values (2)
        conn.executemany(  # the event log
            "INSERT INTO simulation_events(run_id, agent_id, simulation_time_seconds, event_type, node_id, edge_id,"  # columns (1)
            " destination_id, wait_time_seconds, event_details) VALUES (?,?,?,?,?,?,?,?,?)",  # columns (2) and placeholders
            [(run_id,) + ev for ev in result.events])  # prefix every event with the run id
        conn.executemany(  # road occupancy snapshots
            "INSERT INTO edge_occupancy_snapshots(run_id, simulation_time_seconds, edge_id, occupancy, queue_length)"  # columns
            " VALUES (?,?,?,?,?)", [(run_id,) + r for r in result.occupancy_rows])  # placeholders and rows
        conn.executemany(  # agent position snapshots
            "INSERT INTO agent_position_snapshots(run_id, simulation_time_seconds, agent_id, state, edge_id,"  # columns (1)
            " latitude, longitude) VALUES (?,?,?,?,?,?,?)", [(run_id,) + r for r in result.position_rows])  # columns (2), placeholders and rows
        conn.executemany(  # per-frame counts
            "INSERT INTO run_frame_stats(run_id, simulation_time_seconds, waiting_count, moving_count,"  # columns (1)
            " queued_count, evacuated_count, failed_count) VALUES (?,?,?,?,?,?,?)",  # columns (2) and placeholders
            [(run_id,) + r for r in result.frame_rows])  # rows
        conn.executemany(  # bottleneck reports
            "INSERT INTO bottleneck_reports(run_id, edge_id, peak_occupancy, occupancy_ratio, queue_duration_seconds,"  # columns (1)
            " agents_affected, total_wait_seconds, traversal_count, detected_at_seconds, severity, severity_score,"  # columns (2)
            " contributing_metrics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",  # columns (3) and placeholders
            [(run_id, b.edge_id, b.peak_occupancy, b.occupancy_ratio, b.queue_duration_seconds, b.agents_affected,  # values (1)
              b.total_wait_seconds, b.traversal_count, b.detected_at_seconds, b.severity, b.severity_score,  # values (2)
              json.dumps(b.contributing_metrics)) for b in result.bottlenecks])  # values (3): evidence as JSON
        conn.execute(  # finally mark the run completed with its summary
            "UPDATE simulation_runs SET status = 'COMPLETED', completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"  # SQL (1)
            " total_simulated_seconds = ?, total_agents = ?, evacuated_agents = ?, unresolved_agents = ?,"  # SQL (2)
            " failed_agents = ?, average_evacuation_time_seconds = ?, median_evacuation_time_seconds = ?,"  # SQL (3)
            " percentile_95_evacuation_time_seconds = ?, maximum_wait_time_seconds = ?,"  # SQL (4)
            " total_waiting_time_seconds = ?, completion_rate = ? WHERE run_id = ?",  # SQL (5)
            (result.total_simulated_seconds, m["total_agents"], m["evacuated"], m["unresolved"], m["failed"],  # values (1)
             m["average_evacuation_time"], m["median_evacuation_time"], m["p95_evacuation_time"],  # values (2)
             m["maximum_wait_time"], m["total_waiting_time"], m["completion_rate"], run_id))  # values (3)
    log.info("Persisted run %s: %d events, %d occupancy rows", run_id, len(result.events), len(result.occupancy_rows))  # log it


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[dict]:  # read one run
    r = conn.execute("SELECT * FROM simulation_runs WHERE run_id = ?", (run_id,)).fetchone()  # the run row
    return dict(r) if r else None  # as a dictionary, or None


def list_runs(conn: sqlite3.Connection, status: Optional[str] = "COMPLETED") -> List[dict]:  # list runs
    sql = ("SELECT r.run_id, r.scenario_id, s.scenario_name, r.label, r.routing_strategy, r.random_seed, r.status,"  # columns (1)
           " r.started_at, r.total_agents, r.evacuated_agents, r.completion_rate, s.network_id"  # columns (2)
           " FROM simulation_runs r JOIN scenarios s USING (scenario_id)")  # join with the scenario
    if status:  # filter by status
        return _rows(conn.execute(sql + " WHERE r.status = ? ORDER BY r.run_id DESC", (status,)))  # newest first
    return _rows(conn.execute(sql + " ORDER BY r.run_id DESC"))  # all runs, newest first


def delete_run(conn: sqlite3.Connection, run_id: int) -> None:  # remove a run
    with transaction(conn):  # all-or-nothing
        conn.execute("DELETE FROM simulation_runs WHERE run_id = ?", (run_id,))  # foreign keys cascade the rest


# --------------------------------------------------------------- interventions
def save_intervention(conn: sqlite3.Connection, name: str, mode: str, itype: str, baseline_run: int,
                      intervention_run: Optional[int], parameters: dict, description: str = "") -> int:  # record a comparison
    with transaction(conn):  # all-or-nothing
        cur = conn.execute(  # insert it
            "INSERT INTO interventions(name, description, comparison_mode, intervention_type, baseline_run_id,"  # columns (1)
            " intervention_run_id, parameters_json) VALUES (?,?,?,?,?,?,?)",  # columns (2) and placeholders
            (name, description, mode, itype, baseline_run, intervention_run, json.dumps(parameters, default=str)))  # values
    return int(cur.lastrowid)  # its id


# ---------------------------------------------------------------- replay data
def load_frame_times(conn: sqlite3.Connection, run_id: int) -> List[float]:  # the times of all stored frames
    return [r[0] for r in conn.execute(  # first column of each row
        "SELECT simulation_time_seconds FROM run_frame_stats WHERE run_id = ? ORDER BY simulation_time_seconds",  # SQL
        (run_id,))]  # value


def load_frame(conn: sqlite3.Connection, run_id: int, t: float) -> List[dict]:  # agent positions at one time
    return _rows(conn.execute(  # run and convert
        "SELECT agent_id, state, edge_id, latitude, longitude FROM agent_position_snapshots"  # columns
        " WHERE run_id = ? AND simulation_time_seconds = ? ORDER BY agent_id", (run_id, t)))  # filter and order


def load_frame_stats(conn: sqlite3.Connection, run_id: int, t: float) -> Optional[dict]:  # state counts at one time
    r = conn.execute("SELECT * FROM run_frame_stats WHERE run_id = ? AND simulation_time_seconds = ?",  # SQL
                     (run_id, t)).fetchone()  # values; one row
    return dict(r) if r else None  # as a dictionary, or None


def load_edge_occupancy_at(conn: sqlite3.Connection, run_id: int, t: float) -> Dict[int, tuple]:  # road occupancy at one time
    return {r["edge_id"]: (r["occupancy"], r["queue_length"]) for r in conn.execute(  # road id -> (occupancy, queue)
        "SELECT edge_id, occupancy, queue_length FROM edge_occupancy_snapshots"  # columns
        " WHERE run_id = ? AND simulation_time_seconds = ?", (run_id, t))}  # filter and values


# ------------------------------------------------------- bulk replay data (live view)
def load_replay_bundle(conn: sqlite3.Connection, run_id: int) -> dict:  # everything the live view needs
    """Everything the client-side live view needs, in four set-based queries (no per-frame round trips).

    Returns {times, agent_ids, states, lat, lon, stats, occupancy} where states/lat/lon are
    frame-major lists aligned to ``agent_ids`` and occupancy[frame] = {edge_id: (occupancy, queue)}.
    """
    times = load_frame_times(conn, run_id)  # all frame times
    index = {t: i for i, t in enumerate(times)}  # time -> frame number
    agent_ids: List[int] = []  # agent ids in a fixed order
    pos: Dict[int, int] = {}  # agent id -> position in that order
    states = [[] for _ in times]  # per frame: list of agent states
    lat = [[] for _ in times]  # per frame: list of latitudes
    lon = [[] for _ in times]  # per frame: list of longitudes
    for r in conn.execute("SELECT simulation_time_seconds AS t, agent_id, state, latitude, longitude"  # all positions (1)
                          " FROM agent_position_snapshots WHERE run_id = ? ORDER BY t, agent_id", (run_id,)):  # (2)
        i = index[r["t"]]  # which frame
        if i == 0:  # the first frame defines the agent order
            pos[r["agent_id"]] = len(agent_ids)  # remember the position
            agent_ids.append(r["agent_id"])  # add the agent
        states[i].append(r["state"])  # state in this frame
        lat[i].append(r["latitude"])  # latitude in this frame
        lon[i].append(r["longitude"])  # longitude in this frame
    stats = [[0, 0, 0, 0, 0] for _ in times]  # per frame: waiting, moving, queued, evacuated, failed
    for r in conn.execute("SELECT simulation_time_seconds AS t, waiting_count, moving_count, queued_count,"  # frame counts (1)
                          " evacuated_count, failed_count FROM run_frame_stats WHERE run_id = ?", (run_id,)):  # (2)
        stats[index[r["t"]]] = [r["waiting_count"], r["moving_count"], r["queued_count"], r["evacuated_count"],  # fill in the counts (1)
                                r["failed_count"]]  # (2)
    occupancy: List[Dict[int, tuple]] = [{} for _ in times]  # per frame: road -> (occupancy, queue)
    for r in conn.execute("SELECT simulation_time_seconds AS t, edge_id, occupancy, queue_length"  # road occupancy (1)
                          " FROM edge_occupancy_snapshots WHERE run_id = ?", (run_id,)):  # (2)
        occupancy[index[r["t"]]][r["edge_id"]] = (r["occupancy"], r["queue_length"])  # store it
    alert, departure, channel = {}, {}, {}  # per-agent alert time, departure time and alert channel
    for r in conn.execute("SELECT a.agent_id, a.alert_time_seconds, a.departure_time_seconds, a.alert_channel FROM agents a"  # agent facts (1)
                          " JOIN simulation_runs r ON r.scenario_id = a.scenario_id WHERE r.run_id = ?", (run_id,)):  # (2)
        alert[r["agent_id"]], departure[r["agent_id"]] = r["alert_time_seconds"], r["departure_time_seconds"]  # alert and departure times
        channel[r["agent_id"]] = r["alert_channel"]  # alert channel
    return {"times": times, "agent_ids": agent_ids, "states": states, "lat": lat, "lon": lon, "stats": stats,  # the bundle (1)
            "occupancy": occupancy, "alert_times": [alert.get(a) for a in agent_ids],  # (2)
            "departures": [departure.get(a) for a in agent_ids],  # (3)
            "alert_channels": [channel.get(a) for a in agent_ids]}  # (4)
