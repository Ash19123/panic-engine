"""Data-access layer: every write/read of domain objects goes through here.

All SQL uses bound parameters. UI code and the simulation engine never build SQL.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from config import settings
from database.connection import transaction
from simulation.agent import Agent, generate_population
from simulation.models import (ClosureSpec, DestinationSpec, Edge, ModelParams, Node, OriginSpec,
                               RoadNetwork, ScenarioConfig)

log = logging.getLogger(__name__)


def _rows(cur: sqlite3.Cursor) -> List[dict]:
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ networks
def network_exists(conn: sqlite3.Connection, network_id: str) -> bool:
    return conn.execute("SELECT 1 FROM networks WHERE network_id = ?", (network_id,)).fetchone() is not None


def save_network(conn: sqlite3.Connection, net: RoadNetwork) -> RoadNetwork:
    """Persist a network once (idempotent) and return it with database ids.

    A network with the same content fingerprint is stored only once, so repeated
    Streamlit reruns or repeated seeding never duplicate road data.
    """
    network_id = net.network_id or f"{net.source.lower()}-{net.fingerprint()}"
    if network_exists(conn, network_id):
        return load_network(conn, network_id)
    node_ids = sorted(net.nodes)
    with transaction(conn):
        conn.execute(
            "INSERT INTO networks(network_id, name, source, is_synthetic, center_lat, center_lon, radius_m,"
            " node_count, edge_count, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (network_id, net.name, net.source, int(net.is_synthetic),
             net.center[0] if net.center else None, net.center[1] if net.center else None,
             net.radius_m, len(net.nodes), len(net.edges), json.dumps(net.metadata, default=str)))
        node_map: Dict[int, int] = {}
        for nid in node_ids:
            n = net.nodes[nid]
            cur = conn.execute(
                "INSERT INTO network_nodes(network_id, source_osm_node_id, latitude, longitude) VALUES (?,?,?,?)",
                (network_id, n.osm_id, n.lat, n.lon))
            node_map[nid] = cur.lastrowid
        for eid in sorted(net.edges):
            e = net.edges[eid]
            conn.execute(
                "INSERT INTO network_edges(network_id, source_node_id, target_node_id, distance_meters,"
                " estimated_travel_time_seconds, capacity, is_available, road_class, osm_way_id, geometry_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (network_id, node_map[e.source], node_map[e.target], e.length_m, e.free_flow_time_s, e.capacity,
                 int(e.base_available), e.road_class, e.osm_way_id,
                 json.dumps(e.geometry) if e.geometry else None))
    log.info("Saved network %s (%d nodes, %d edges)", network_id, len(net.nodes), len(net.edges))
    return load_network(conn, network_id)


def load_network(conn: sqlite3.Connection, network_id: str) -> RoadNetwork:
    row = conn.execute("SELECT * FROM networks WHERE network_id = ?", (network_id,)).fetchone()
    if row is None:
        raise KeyError(f"network {network_id!r} not found")
    center = (row["center_lat"], row["center_lon"]) if row["center_lat"] is not None else None
    net = RoadNetwork(row["name"], row["source"], network_id=network_id, center=center,
                      radius_m=row["radius_m"], metadata=json.loads(row["metadata_json"] or "{}"))
    for r in conn.execute("SELECT node_id, source_osm_node_id, latitude, longitude FROM network_nodes"
                          " WHERE network_id = ? ORDER BY node_id", (network_id,)):
        net.nodes[r["node_id"]] = Node(r["node_id"], r["latitude"], r["longitude"], r["source_osm_node_id"])
    for r in conn.execute("SELECT * FROM network_edges WHERE network_id = ? ORDER BY edge_id", (network_id,)):
        geom = json.loads(r["geometry_json"]) if r["geometry_json"] else None
        net.edges[r["edge_id"]] = Edge(
            r["edge_id"], r["source_node_id"], r["target_node_id"], r["distance_meters"],
            r["estimated_travel_time_seconds"] or r["distance_meters"] / settings.WALKING_SPEED_MPS,
            r["capacity"], r["road_class"] or "local", bool(r["is_available"]), r["osm_way_id"],
            [tuple(p) for p in geom] if geom else None)
    net._invalidate()
    return net


def list_networks(conn: sqlite3.Connection) -> List[dict]:
    return _rows(conn.execute("SELECT network_id, name, source, is_synthetic, node_count, edge_count, created_at"
                              " FROM networks ORDER BY created_at DESC"))


# -------------------------------------------------------------- destinations
def save_destination(conn: sqlite3.Connection, network_id: str, spec: DestinationSpec,
                     lat: float, lon: float) -> int:
    cur = conn.execute(
        "INSERT INTO destinations(network_id, node_id, destination_name, destination_type, latitude, longitude,"
        " capacity, throughput_per_timestep, source_type, hypothetical_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (network_id, spec.node_id, spec.name, spec.destination_type, lat, lon, spec.capacity,
         spec.throughput_per_timestep, spec.source_type, "HYPOTHETICAL" if spec.hypothetical else "VERIFIED"))
    return int(cur.lastrowid)


def list_destinations(conn: sqlite3.Connection, network_id: str) -> List[DestinationSpec]:
    out = []
    for r in conn.execute("SELECT * FROM destinations WHERE network_id = ? ORDER BY destination_id", (network_id,)):
        out.append(DestinationSpec(r["destination_id"], r["node_id"], r["destination_name"], r["capacity"],
                                   r["throughput_per_timestep"], True, r["destination_type"], r["source_type"],
                                   r["hypothetical_status"] == "HYPOTHETICAL"))
    return out


# ----------------------------------------------------------------- scenarios
def save_scenario(conn: sqlite3.Connection, cfg: ScenarioConfig, network: RoadNetwork) -> int:
    """Insert a scenario with its origins, destinations and closures atomically."""
    with transaction(conn):
        for d in cfg.destinations:
            if d.destination_id is None:
                node = network.nodes[d.node_id]
                d.destination_id = save_destination(conn, cfg.network_id, d, node.lat, node.lon)
        cur = conn.execute(
            "INSERT INTO scenarios(scenario_name, description, study_area, geographic_data_source, network_id,"
            " network_fingerprint, routing_strategy, agent_count, random_seed, timestep_seconds,"
            " simulation_time_limit, configuration_json, parent_scenario_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cfg.name, cfg.description, cfg.study_area, cfg.geographic_data_source, cfg.network_id,
             cfg.network_fingerprint or network.fingerprint(), cfg.routing_strategy, cfg.agent_count,
             cfg.random_seed, cfg.timestep_seconds, cfg.time_limit_seconds, cfg.to_json(),
             cfg.parent_scenario_id))
        sid = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO scenario_origins(scenario_id, node_id, weight, label, source_type) VALUES (?,?,?,?,?)",
            [(sid, o.node_id, o.weight, o.label, o.source_type) for o in cfg.origins])
        conn.executemany(
            "INSERT INTO scenario_destinations(scenario_id, destination_id, capacity, throughput_per_timestep,"
            " is_available) VALUES (?,?,?,?,?)",
            [(sid, d.destination_id, d.capacity, d.throughput_per_timestep, int(d.available))
             for d in cfg.destinations])
        conn.executemany(
            "INSERT INTO scenario_closures(scenario_id, edge_id, close_time_seconds) VALUES (?,?,?)",
            [(sid, c.edge_id, c.close_time_seconds) for c in cfg.closures])
    cfg.scenario_id = sid
    return sid


def load_scenario_config(conn: sqlite3.Connection, scenario_id: int) -> ScenarioConfig:
    s = conn.execute("SELECT * FROM scenarios WHERE scenario_id = ?", (scenario_id,)).fetchone()
    if s is None:
        raise KeyError(f"scenario {scenario_id} not found")
    cfg = ScenarioConfig(
        name=s["scenario_name"], network_id=s["network_id"], routing_strategy=s["routing_strategy"],
        agent_count=s["agent_count"], random_seed=s["random_seed"], timestep_seconds=s["timestep_seconds"],
        time_limit_seconds=s["simulation_time_limit"], description=s["description"] or "",
        study_area=s["study_area"] or "", geographic_data_source=s["geographic_data_source"],
        network_fingerprint=s["network_fingerprint"],
        params=ModelParams.from_dict(json.loads(s["configuration_json"] or "{}")),
        scenario_id=scenario_id, parent_scenario_id=s["parent_scenario_id"])
    cfg.origins = [OriginSpec(r["node_id"], r["weight"], r["label"] or "", r["source_type"])
                   for r in conn.execute("SELECT * FROM scenario_origins WHERE scenario_id = ? ORDER BY node_id",
                                         (scenario_id,))]
    for r in conn.execute(
            "SELECT d.destination_id, d.node_id, d.destination_name, d.destination_type, d.source_type,"
            " d.hypothetical_status, sd.capacity, sd.throughput_per_timestep, sd.is_available"
            " FROM scenario_destinations sd JOIN destinations d USING (destination_id)"
            " WHERE sd.scenario_id = ? ORDER BY d.destination_id", (scenario_id,)):
        cfg.destinations.append(DestinationSpec(
            r["destination_id"], r["node_id"], r["destination_name"], r["capacity"], r["throughput_per_timestep"],
            bool(r["is_available"]), r["destination_type"], r["source_type"], r["hypothetical_status"] == "HYPOTHETICAL"))
    cfg.closures = [ClosureSpec(r["edge_id"], r["close_time_seconds"])
                    for r in conn.execute("SELECT edge_id, close_time_seconds FROM scenario_closures"
                                          " WHERE scenario_id = ? ORDER BY edge_id", (scenario_id,))]
    return cfg


def list_scenarios(conn: sqlite3.Connection) -> List[dict]:
    return _rows(conn.execute(
        "SELECT s.scenario_id, s.scenario_name, s.routing_strategy, s.agent_count, s.random_seed,"
        " s.geographic_data_source, s.network_id, s.created_at, s.parent_scenario_id,"
        " (SELECT COUNT(*) FROM simulation_runs r WHERE r.scenario_id = s.scenario_id) AS run_count"
        " FROM scenarios s ORDER BY s.scenario_id DESC"))


def delete_scenario(conn: sqlite3.Connection, scenario_id: int) -> None:
    """Explicit user action only; cascades to agents, runs and their results."""
    with transaction(conn):
        conn.execute("DELETE FROM scenarios WHERE scenario_id = ?", (scenario_id,))


# ---------------------------------------------------------------- population
def save_population(conn: sqlite3.Connection, scenario_id: int, agents: Iterable[Agent]) -> List[Agent]:
    out = []
    with transaction(conn):
        for a in agents:
            cur = conn.execute(
                "INSERT INTO agents(scenario_id, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel)"
                " VALUES (?,?,?,?,?)",
                (scenario_id, a.origin_node, a.departure_time, a.alert_time, a.alert_channel))
            a.agent_id = int(cur.lastrowid)
            out.append(a)
    return out


def load_population(conn: sqlite3.Connection, scenario_id: int) -> List[Agent]:
    return [Agent(r["agent_id"], r["origin_node_id"], r["departure_time_seconds"], r["alert_time_seconds"], r["alert_channel"])
            for r in conn.execute("SELECT agent_id, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel FROM agents"
                                  " WHERE scenario_id = ? ORDER BY agent_id", (scenario_id,))]


def ensure_population(conn: sqlite3.Connection, scenario_id: int, network: RoadNetwork,
                      cfg: ScenarioConfig) -> List[Agent]:
    """Population is generated once per scenario, then reused by every run."""
    existing = load_population(conn, scenario_id)
    if existing:
        return existing
    return save_population(conn, scenario_id, generate_population(network, cfg))


def clone_population(conn: sqlite3.Connection, from_scenario: int, to_scenario: int) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO agents(scenario_id, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel)"
            " SELECT ?, origin_node_id, departure_time_seconds, alert_time_seconds, alert_channel FROM agents"
            " WHERE scenario_id = ? ORDER BY agent_id",
            (to_scenario, from_scenario))
    return cur.rowcount


# ---------------------------------------------------------------------- runs
def create_run(conn: sqlite3.Connection, scenario_id: int, strategy: str, seed: int,
               snapshot_json: str, label: Optional[str] = None) -> int:
    cur = conn.execute(
        "INSERT INTO simulation_runs(scenario_id, label, status, routing_strategy, random_seed, engine_version,"
        " configuration_snapshot) VALUES (?,?, 'RUNNING', ?,?,?,?)",
        (scenario_id, label, strategy, seed, settings.ENGINE_VERSION, snapshot_json))
    return int(cur.lastrowid)


def mark_run_failed(conn: sqlite3.Connection, run_id: int, reason: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE simulation_runs SET status = 'FAILED', failure_reason = ?,"
                     " completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE run_id = ?", (reason[:500], run_id))


def persist_result(conn: sqlite3.Connection, run_id: int, result: Any) -> None:
    """Write every detail row and flip the run to COMPLETED in ONE transaction.

    If anything fails the transaction rolls back, so a run is either fully
    stored or has no detail rows at all (and is then marked FAILED by the caller).
    """
    m = result.metrics
    with transaction(conn):
        conn.executemany(
            "INSERT INTO agent_outcomes(run_id, agent_id, destination_id, final_state, current_node_id,"
            " current_edge_id, departure_time_seconds, evacuation_time_seconds, accumulated_wait_seconds,"
            " route_change_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_id, a.agent_id, a.destination_id, a.state.value, a.node, a.edge, a.departure_time,
              a.evacuation_time, a.wait_seconds, a.route_changes) for a in result.agents])
        conn.executemany(
            "INSERT INTO simulation_events(run_id, agent_id, simulation_time_seconds, event_type, node_id, edge_id,"
            " destination_id, wait_time_seconds, event_details) VALUES (?,?,?,?,?,?,?,?,?)",
            [(run_id,) + ev for ev in result.events])
        conn.executemany(
            "INSERT INTO edge_occupancy_snapshots(run_id, simulation_time_seconds, edge_id, occupancy, queue_length)"
            " VALUES (?,?,?,?,?)", [(run_id,) + r for r in result.occupancy_rows])
        conn.executemany(
            "INSERT INTO agent_position_snapshots(run_id, simulation_time_seconds, agent_id, state, edge_id,"
            " latitude, longitude) VALUES (?,?,?,?,?,?,?)", [(run_id,) + r for r in result.position_rows])
        conn.executemany(
            "INSERT INTO run_frame_stats(run_id, simulation_time_seconds, waiting_count, moving_count,"
            " queued_count, evacuated_count, failed_count) VALUES (?,?,?,?,?,?,?)",
            [(run_id,) + r for r in result.frame_rows])
        conn.executemany(
            "INSERT INTO bottleneck_reports(run_id, edge_id, peak_occupancy, occupancy_ratio, queue_duration_seconds,"
            " agents_affected, total_wait_seconds, traversal_count, detected_at_seconds, severity, severity_score,"
            " contributing_metrics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, b.edge_id, b.peak_occupancy, b.occupancy_ratio, b.queue_duration_seconds, b.agents_affected,
              b.total_wait_seconds, b.traversal_count, b.detected_at_seconds, b.severity, b.severity_score,
              json.dumps(b.contributing_metrics)) for b in result.bottlenecks])
        conn.execute(
            "UPDATE simulation_runs SET status = 'COMPLETED', completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            " total_simulated_seconds = ?, total_agents = ?, evacuated_agents = ?, unresolved_agents = ?,"
            " failed_agents = ?, average_evacuation_time_seconds = ?, median_evacuation_time_seconds = ?,"
            " percentile_95_evacuation_time_seconds = ?, maximum_wait_time_seconds = ?,"
            " total_waiting_time_seconds = ?, completion_rate = ? WHERE run_id = ?",
            (result.total_simulated_seconds, m["total_agents"], m["evacuated"], m["unresolved"], m["failed"],
             m["average_evacuation_time"], m["median_evacuation_time"], m["p95_evacuation_time"],
             m["maximum_wait_time"], m["total_waiting_time"], m["completion_rate"], run_id))
    log.info("Persisted run %s: %d events, %d occupancy rows", run_id, len(result.events), len(result.occupancy_rows))


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM simulation_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def list_runs(conn: sqlite3.Connection, status: Optional[str] = "COMPLETED") -> List[dict]:
    sql = ("SELECT r.run_id, r.scenario_id, s.scenario_name, r.label, r.routing_strategy, r.random_seed, r.status,"
           " r.started_at, r.total_agents, r.evacuated_agents, r.completion_rate, s.network_id"
           " FROM simulation_runs r JOIN scenarios s USING (scenario_id)")
    if status:
        return _rows(conn.execute(sql + " WHERE r.status = ? ORDER BY r.run_id DESC", (status,)))
    return _rows(conn.execute(sql + " ORDER BY r.run_id DESC"))


def delete_run(conn: sqlite3.Connection, run_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM simulation_runs WHERE run_id = ?", (run_id,))


# --------------------------------------------------------------- interventions
def save_intervention(conn: sqlite3.Connection, name: str, mode: str, itype: str, baseline_run: int,
                      intervention_run: Optional[int], parameters: dict, description: str = "") -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO interventions(name, description, comparison_mode, intervention_type, baseline_run_id,"
            " intervention_run_id, parameters_json) VALUES (?,?,?,?,?,?,?)",
            (name, description, mode, itype, baseline_run, intervention_run, json.dumps(parameters, default=str)))
    return int(cur.lastrowid)


# ---------------------------------------------------------------- replay data
def load_frame_times(conn: sqlite3.Connection, run_id: int) -> List[float]:
    return [r[0] for r in conn.execute(
        "SELECT simulation_time_seconds FROM run_frame_stats WHERE run_id = ? ORDER BY simulation_time_seconds",
        (run_id,))]


def load_frame(conn: sqlite3.Connection, run_id: int, t: float) -> List[dict]:
    return _rows(conn.execute(
        "SELECT agent_id, state, edge_id, latitude, longitude FROM agent_position_snapshots"
        " WHERE run_id = ? AND simulation_time_seconds = ? ORDER BY agent_id", (run_id, t)))


def load_frame_stats(conn: sqlite3.Connection, run_id: int, t: float) -> Optional[dict]:
    r = conn.execute("SELECT * FROM run_frame_stats WHERE run_id = ? AND simulation_time_seconds = ?",
                     (run_id, t)).fetchone()
    return dict(r) if r else None


def load_edge_occupancy_at(conn: sqlite3.Connection, run_id: int, t: float) -> Dict[int, tuple]:
    return {r["edge_id"]: (r["occupancy"], r["queue_length"]) for r in conn.execute(
        "SELECT edge_id, occupancy, queue_length FROM edge_occupancy_snapshots"
        " WHERE run_id = ? AND simulation_time_seconds = ?", (run_id, t))}


# ------------------------------------------------------- bulk replay data (live view)
def load_replay_bundle(conn: sqlite3.Connection, run_id: int) -> dict:
    """Everything the client-side live view needs, in four set-based queries (no per-frame round trips).

    Returns {times, agent_ids, states, lat, lon, stats, occupancy} where states/lat/lon are
    frame-major lists aligned to ``agent_ids`` and occupancy[frame] = {edge_id: (occupancy, queue)}.
    """
    times = load_frame_times(conn, run_id)
    index = {t: i for i, t in enumerate(times)}
    agent_ids: List[int] = []
    pos: Dict[int, int] = {}
    states = [[] for _ in times]
    lat = [[] for _ in times]
    lon = [[] for _ in times]
    for r in conn.execute("SELECT simulation_time_seconds AS t, agent_id, state, latitude, longitude"
                          " FROM agent_position_snapshots WHERE run_id = ? ORDER BY t, agent_id", (run_id,)):
        i = index[r["t"]]
        if i == 0:
            pos[r["agent_id"]] = len(agent_ids)
            agent_ids.append(r["agent_id"])
        states[i].append(r["state"])
        lat[i].append(r["latitude"])
        lon[i].append(r["longitude"])
    stats = [[0, 0, 0, 0, 0] for _ in times]
    for r in conn.execute("SELECT simulation_time_seconds AS t, waiting_count, moving_count, queued_count,"
                          " evacuated_count, failed_count FROM run_frame_stats WHERE run_id = ?", (run_id,)):
        stats[index[r["t"]]] = [r["waiting_count"], r["moving_count"], r["queued_count"], r["evacuated_count"],
                                r["failed_count"]]
    occupancy: List[Dict[int, tuple]] = [{} for _ in times]
    for r in conn.execute("SELECT simulation_time_seconds AS t, edge_id, occupancy, queue_length"
                          " FROM edge_occupancy_snapshots WHERE run_id = ?", (run_id,)):
        occupancy[index[r["t"]]][r["edge_id"]] = (r["occupancy"], r["queue_length"])
    alert, departure, channel = {}, {}, {}
    for r in conn.execute("SELECT a.agent_id, a.alert_time_seconds, a.departure_time_seconds, a.alert_channel FROM agents a"
                          " JOIN simulation_runs r ON r.scenario_id = a.scenario_id WHERE r.run_id = ?", (run_id,)):
        alert[r["agent_id"]], departure[r["agent_id"]] = r["alert_time_seconds"], r["departure_time_seconds"]
        channel[r["agent_id"]] = r["alert_channel"]
    return {"times": times, "agent_ids": agent_ids, "states": states, "lat": lat, "lon": lon, "stats": stats,
            "occupancy": occupancy, "alert_times": [alert.get(a) for a in agent_ids],
            "departures": [departure.get(a) for a in agent_ids],
            "alert_channels": [channel.get(a) for a in agent_ids]}
