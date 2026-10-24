"""Core data model: road network, scenario configuration, agent states.

Nothing here touches the database or the UI.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx

from geo import geographic_utils as geo

STRATEGIES = ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED")


class AgentState(str, Enum):
    WAITING = "WAITING"                      # alerted but has not departed yet
    MOVING = "MOVING"                        # traversing an edge
    QUEUED = "QUEUED"                        # held at a node (edge full) or at a destination gate
    EVACUATED = "EVACUATED"                  # admitted by a destination (terminal)
    UNABLE_TO_EVACUATE = "UNABLE_TO_EVACUATE"  # no feasible route / destination (terminal)


TERMINAL_STATES = frozenset({AgentState.EVACUATED, AgentState.UNABLE_TO_EVACUATE})

# Explicit finite-state machine. A self-transition (e.g. MOVING -> MOVING when an
# agent flows straight from one edge onto the next) is always allowed.
ALLOWED_TRANSITIONS: Dict[AgentState, frozenset] = {
    AgentState.WAITING: frozenset({AgentState.MOVING, AgentState.QUEUED, AgentState.UNABLE_TO_EVACUATE}),
    AgentState.MOVING: frozenset({AgentState.QUEUED, AgentState.UNABLE_TO_EVACUATE}),
    AgentState.QUEUED: frozenset({AgentState.MOVING, AgentState.EVACUATED, AgentState.UNABLE_TO_EVACUATE}),
    AgentState.EVACUATED: frozenset(),
    AgentState.UNABLE_TO_EVACUATE: frozenset(),
}


# ----------------------------------------------------------------- network
@dataclass
class Node:
    node_id: int
    lat: float
    lon: float
    osm_id: Optional[int] = None


@dataclass
class Edge:
    edge_id: int
    source: int
    target: int
    length_m: float
    free_flow_time_s: float
    capacity: int                       # max agents simultaneously on the edge
    road_class: str = "local"
    base_available: bool = True
    osm_way_id: Optional[int] = None
    geometry: Optional[List[Tuple[float, float]]] = None   # (lat, lon) polyline, real roads only


class RoadNetwork:
    """Directed road graph with parallel-edge support.

    ``edges`` is keyed by edge id so the same two nodes may be linked by several
    segments. A NetworkX ``MultiDiGraph`` (reversed) is built lazily for
    Dijkstra searches.
    """

    def __init__(self, name: str, source: str, network_id: Optional[str] = None,
                 center: Optional[Tuple[float, float]] = None, radius_m: Optional[float] = None,
                 metadata: Optional[dict] = None):
        if source not in ("OSM", "SYNTHETIC"):
            raise ValueError("source must be 'OSM' or 'SYNTHETIC'")
        self.name = name
        self.source = source
        self.network_id = network_id
        self.center = center
        self.radius_m = radius_m
        self.metadata = metadata or {}
        self.nodes: Dict[int, Node] = {}
        self.edges: Dict[int, Edge] = {}
        self._rev_graph: Optional[nx.MultiDiGraph] = None
        self._parallel: Optional[Dict[Tuple[int, int], List[int]]] = None

    @property
    def is_synthetic(self) -> bool:
        return self.source == "SYNTHETIC"

    def add_node(self, node: Node) -> None:
        self.nodes[node.node_id] = node
        self._invalidate()

    def add_edge(self, edge: Edge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            raise ValueError(f"edge {edge.edge_id} references an unknown node")
        if edge.length_m <= 0 or edge.free_flow_time_s <= 0 or edge.capacity < 1:
            raise ValueError(f"edge {edge.edge_id} has non-positive length/time/capacity")
        self.edges[edge.edge_id] = edge
        self._invalidate()

    def _invalidate(self) -> None:
        self._rev_graph = None
        self._parallel = None

    # -- graph views -------------------------------------------------------
    def reverse_graph(self) -> nx.MultiDiGraph:
        """Edges point target->source so one Dijkstra from a destination gives
        the cost-to-destination for every node (see simulation/routing.py)."""
        if self._rev_graph is None:
            g = nx.MultiDiGraph()
            g.add_nodes_from(self.nodes)
            for e in self.edges.values():
                g.add_edge(e.target, e.source, key=e.edge_id, edge_id=e.edge_id)
            self._rev_graph = g
        return self._rev_graph

    def forward_graph(self) -> nx.MultiDiGraph:
        g = nx.MultiDiGraph()
        for n in self.nodes.values():
            g.add_node(n.node_id, lat=n.lat, lon=n.lon)
        for e in self.edges.values():
            g.add_edge(e.source, e.target, key=e.edge_id, edge_id=e.edge_id,
                       length=e.length_m, capacity=e.capacity)
        return g

    def parallel_edges(self, u: int, v: int) -> List[int]:
        if self._parallel is None:
            idx: Dict[Tuple[int, int], List[int]] = {}
            for e in self.edges.values():
                idx.setdefault((e.source, e.target), []).append(e.edge_id)
            self._parallel = idx
        return self._parallel.get((u, v), [])

    # -- geometry ----------------------------------------------------------
    def edge_polyline(self, edge_id: int) -> List[Tuple[float, float]]:
        e = self.edges[edge_id]
        if e.geometry and len(e.geometry) >= 2:
            return e.geometry
        a, b = self.nodes[e.source], self.nodes[e.target]
        return [(a.lat, a.lon), (b.lat, b.lon)]

    def nearest_node(self, lat: float, lon: float) -> int:
        return min(self.nodes.values(), key=lambda n: geo.haversine_m(lat, lon, n.lat, n.lon)).node_id

    def nearest_edge(self, lat: float, lon: float) -> int:
        best, best_d = None, math.inf
        for e in self.edges.values():
            d = geo.distance_to_polyline_m(lat, lon, self.edge_polyline(e.edge_id))
            if d < best_d:
                best, best_d = e.edge_id, d
        if best is None:
            raise ValueError("network has no edges")
        return best

    def edges_within(self, lat: float, lon: float, radius_m: float) -> List[int]:
        """Edges whose two end nodes both lie inside the circle (used for danger zones)."""
        inside = {n.node_id for n in self.nodes.values() if geo.haversine_m(lat, lon, n.lat, n.lon) <= radius_m}
        return sorted(e.edge_id for e in self.edges.values() if e.source in inside and e.target in inside)

    def bounds(self) -> Tuple[float, float, float, float]:
        lats = [n.lat for n in self.nodes.values()]
        lons = [n.lon for n in self.nodes.values()]
        return min(lats), min(lons), max(lats), max(lons)

    def fingerprint(self) -> str:
        """Id-independent content hash (positions in sorted order)."""
        h = hashlib.sha1()
        order = {nid: i for i, nid in enumerate(sorted(self.nodes))}
        for nid in sorted(self.nodes):
            n = self.nodes[nid]
            h.update(f"N{order[nid]}:{n.lat:.6f},{n.lon:.6f};".encode())
        rows = sorted((order[e.source], order[e.target], round(e.length_m, 1), e.capacity)
                      for e in self.edges.values())
        for r in rows:
            h.update(f"E{r};".encode())
        return h.hexdigest()[:16]

    def strongly_connected_fraction(self) -> float:
        g = nx.DiGraph()
        g.add_nodes_from(self.nodes)
        g.add_edges_from((e.source, e.target) for e in self.edges.values())
        if not self.nodes:
            return 0.0
        biggest = max(nx.strongly_connected_components(g), key=len)
        return len(biggest) / len(self.nodes)


# ---------------------------------------------------------------- scenario
@dataclass
class OriginSpec:
    node_id: int
    weight: float = 1.0
    label: str = ""
    source_type: str = "USER_DEFINED"


@dataclass
class DestinationSpec:
    destination_id: Optional[int]
    node_id: int
    name: str
    capacity: int
    throughput_per_timestep: int
    available: bool = True
    destination_type: str = "SAFE_AREA"
    source_type: str = "USER_DEFINED"
    hypothetical: bool = True


@dataclass
class ClosureSpec:
    edge_id: int
    close_time_seconds: float = 0.0


@dataclass
class ModelParams:
    """Secondary model parameters (stored as configuration_json)."""
    crowd_distribution: str = "weighted"          # even | weighted | concentrated
    departure_delay_max_seconds: float = 120.0    # uniform response delay in [0, max]
    spread_radius_m: float = 0.0                  # agents start within this radius of an origin
    bpr_alpha: float = 0.6                        # travel-time inflation under load
    bpr_beta: float = 2.0
    queue_penalty_gamma: float = 1.0              # weight of expected queue wait in route cost
    dest_load_weight_seconds: float = 300.0       # penalty (s) at 100% projected destination load
    reroute_interval_seconds: float = 30.0        # how often a route may be re-evaluated
    replan_improvement_threshold: float = 0.15    # switch only if >=15% cheaper
    snapshot_interval_seconds: float = 10.0
    record_movement_events: bool = True
    record_position_snapshots: bool = True
    max_snapshot_agents: int = 2000
    edge_capacity_overrides: Dict[int, int] = field(default_factory=dict)
    walking_speed_mps: float = 1.4
    # Bomb / blast hazards (simulated emergency-alert drill). Each: {name, node, lat, lon, blast_radius_m,
    # alert_radius_m, strike_time_s, alert_time_s}. People inside an alert radius get a phone alert at alert_time_s;
    # at strike_time_s roads/safe areas inside the blast radius are lost and anyone still inside is caught.
    hazards: List[dict] = field(default_factory=list)
    # How the alert reaches people (empty = everybody gets the phone alert, the original behaviour):
    # {phone_reach: 0..1 share with a phone at hand, phone_delay_max_s, sirens: [{node, lat, lon, radius_m, delay_s}],
    #  neighbour_radius_m, neighbour_delay_s}  - people nobody reaches never start moving.
    alert_channels: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["edge_capacity_overrides"] = {str(k): v for k, v in self.edge_capacity_overrides.items()}
        return d

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ModelParams":
        data = dict(data or {})
        names = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in names}
        if "edge_capacity_overrides" in clean:
            clean["edge_capacity_overrides"] = {int(k): int(v) for k, v in clean["edge_capacity_overrides"].items()}
        return cls(**clean)


@dataclass
class ScenarioConfig:
    name: str
    network_id: str
    routing_strategy: str = "SHORTEST_PATH"
    agent_count: int = 200
    random_seed: int = 42
    timestep_seconds: float = 5.0
    time_limit_seconds: float = 1800.0
    description: str = ""
    study_area: str = ""
    geographic_data_source: str = "SYNTHETIC"
    network_fingerprint: str = ""
    origins: List[OriginSpec] = field(default_factory=list)
    destinations: List[DestinationSpec] = field(default_factory=list)
    closures: List[ClosureSpec] = field(default_factory=list)
    params: ModelParams = field(default_factory=ModelParams)
    scenario_id: Optional[int] = None
    parent_scenario_id: Optional[int] = None

    def copy(self) -> "ScenarioConfig":
        import copy
        return copy.deepcopy(self)

    def to_json(self) -> str:
        return json.dumps(self.params.to_dict(), sort_keys=True)

    def snapshot_json(self) -> str:
        d = asdict(self)
        d["params"] = self.params.to_dict()
        return json.dumps(d, sort_keys=True, default=str)


def validate_scenario(network: RoadNetwork, cfg: ScenarioConfig) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors block a run; warnings do not."""
    errors: List[str] = []
    warnings: List[str] = []
    if not cfg.name.strip():
        errors.append("Scenario needs a name.")
    if cfg.routing_strategy not in STRATEGIES:
        errors.append(f"Unknown routing strategy {cfg.routing_strategy!r}.")
    if cfg.agent_count < 1:
        errors.append("Agent count must be at least 1.")
    if cfg.timestep_seconds <= 0:
        errors.append("Timestep must be positive.")
    if cfg.time_limit_seconds <= cfg.timestep_seconds:
        errors.append("Time limit must exceed one timestep.")
    if not cfg.origins:
        errors.append("Define at least one origin.")
    for o in cfg.origins:
        if o.node_id not in network.nodes:
            errors.append(f"Origin node {o.node_id} is not in the network.")
        if o.weight <= 0:
            errors.append(f"Origin node {o.node_id} needs a positive weight.")
    avail = [d for d in cfg.destinations if d.available and d.capacity > 0]
    if not avail:
        errors.append("Define at least one available destination with capacity > 0.")
    for d in cfg.destinations:
        if d.node_id not in network.nodes:
            errors.append(f"Destination {d.name!r} is not on a network node.")
        if d.capacity < 0 or d.throughput_per_timestep < 1:
            errors.append(f"Destination {d.name!r}: capacity >= 0 and throughput >= 1 required.")
    for h in cfg.params.hazards:
        if h.get("blast_radius_m", 0) <= 0 or h.get("alert_radius_m", 0) < h.get("blast_radius_m", 0):
            errors.append("Hazard: blast radius must be > 0 and the alert radius at least as large.")
        if h.get("alert_time_s", 0) > h.get("strike_time_s", 0) or h.get("alert_time_s", 0) < 0:
            errors.append("Hazard: the alert must be sent between t=0 and the strike time.")
    for c in cfg.closures:
        if c.edge_id not in network.edges:
            errors.append(f"Closure references unknown edge {c.edge_id}.")
        if c.close_time_seconds < 0:
            errors.append("Closure times cannot be negative.")
    if cfg.params.crowd_distribution not in ("even", "weighted", "concentrated"):
        errors.append("crowd_distribution must be even, weighted or concentrated.")
    if cfg.params.snapshot_interval_seconds <= 0:
        errors.append("Snapshot interval must be positive.")
    if not errors:
        total_cap = sum(d.capacity for d in avail)
        if total_cap < cfg.agent_count:
            warnings.append(f"Total available destination capacity ({total_cap}) is below the agent count "
                            f"({cfg.agent_count}); some agents cannot be evacuated by design.")
        if network.is_synthetic:
            warnings.append("Network is SYNTHETIC - results do not describe real roads.")
        if any(d.hypothetical for d in avail):
            warnings.append("Some destinations are hypothetical (user-defined), not official shelters.")
    return errors, warnings
