"""Core data model: road network, scenario configuration, agent states.

Nothing here touches the database or the UI.
"""
from __future__ import annotations  # modern type hints on older Python

import hashlib  # content hash for the network fingerprint
import json  # turn configuration into text
import math  # infinity
from dataclasses import asdict, dataclass, field, fields  # simple record classes
from enum import Enum  # named constants
from typing import Any, Dict, List, Optional, Tuple  # type hints

import networkx as nx  # graph library

from geo import geographic_utils as geo  # distance helpers

STRATEGIES = ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED")  # the supported routing policies


class AgentState(str, Enum):  # the states of the agent life cycle
    WAITING = "WAITING"                      # alerted but has not departed yet
    MOVING = "MOVING"                        # traversing an edge
    QUEUED = "QUEUED"                        # held at a node (edge full) or at a destination gate
    EVACUATED = "EVACUATED"                  # admitted by a destination (terminal)
    UNABLE_TO_EVACUATE = "UNABLE_TO_EVACUATE"  # no feasible route / destination (terminal)


TERMINAL_STATES = frozenset({AgentState.EVACUATED, AgentState.UNABLE_TO_EVACUATE})  # states an agent can never leave

# Explicit finite-state machine. A self-transition (e.g. MOVING -> MOVING when an
# agent flows straight from one edge onto the next) is always allowed.
ALLOWED_TRANSITIONS: Dict[AgentState, frozenset] = {  # which state may follow which
    AgentState.WAITING: frozenset({AgentState.MOVING, AgentState.QUEUED, AgentState.UNABLE_TO_EVACUATE}),  # from waiting
    AgentState.MOVING: frozenset({AgentState.QUEUED, AgentState.UNABLE_TO_EVACUATE}),  # from moving
    AgentState.QUEUED: frozenset({AgentState.MOVING, AgentState.EVACUATED, AgentState.UNABLE_TO_EVACUATE}),  # from queued
    AgentState.EVACUATED: frozenset(),  # final: nothing may follow
    AgentState.UNABLE_TO_EVACUATE: frozenset(),  # final: nothing may follow
}


# ----------------------------------------------------------------- network
@dataclass
class Node:  # a junction or end point
    node_id: int  # unique id
    lat: float  # latitude
    lon: float  # longitude
    osm_id: Optional[int] = None  # id in OpenStreetMap, if any


@dataclass
class Edge:  # one directed road segment
    edge_id: int  # unique id
    source: int  # start node
    target: int  # end node
    length_m: float  # length in metres
    free_flow_time_s: float  # walking time with no crowding
    capacity: int                       # max agents simultaneously on the edge
    road_class: str = "local"  # kind of road
    base_available: bool = True  # False = closed from the start
    osm_way_id: Optional[int] = None  # id of the OpenStreetMap way
    geometry: Optional[List[Tuple[float, float]]] = None   # (lat, lon) polyline, real roads only


class RoadNetwork:  # the whole road graph
    """Directed road graph with parallel-edge support.

    ``edges`` is keyed by edge id so the same two nodes may be linked by several
    segments. A NetworkX ``MultiDiGraph`` (reversed) is built lazily for
    Dijkstra searches.
    """

    def __init__(self, name: str, source: str, network_id: Optional[str] = None,
                 center: Optional[Tuple[float, float]] = None, radius_m: Optional[float] = None,
                 metadata: Optional[dict] = None):  # create an empty network
        if source not in ("OSM", "SYNTHETIC"):  # only two sources exist
            raise ValueError("source must be 'OSM' or 'SYNTHETIC'")  # reject anything else
        self.name = name  # display name
        self.source = source  # OSM or SYNTHETIC
        self.network_id = network_id  # database id (set when saved)
        self.center = center  # map centre
        self.radius_m = radius_m  # area radius
        self.metadata = metadata or {}  # free-form notes
        self.nodes: Dict[int, Node] = {}  # all nodes by id
        self.edges: Dict[int, Edge] = {}  # all roads by id
        self._rev_graph: Optional[nx.MultiDiGraph] = None  # cached reversed graph
        self._parallel: Optional[Dict[Tuple[int, int], List[int]]] = None  # cached lookup of roads between two nodes

    @property
    def is_synthetic(self) -> bool:  # is this the fake grid?
        return self.source == "SYNTHETIC"  # true for the synthetic network

    def add_node(self, node: Node) -> None:  # add a junction
        self.nodes[node.node_id] = node  # store it
        self._invalidate()  # cached graphs are now stale

    def add_edge(self, edge: Edge) -> None:  # add a road
        if edge.source not in self.nodes or edge.target not in self.nodes:  # both ends must exist
            raise ValueError(f"edge {edge.edge_id} references an unknown node")  # reject it
        if edge.length_m <= 0 or edge.free_flow_time_s <= 0 or edge.capacity < 1:  # numbers must make sense
            raise ValueError(f"edge {edge.edge_id} has non-positive length/time/capacity")  # reject it
        self.edges[edge.edge_id] = edge  # store it
        self._invalidate()  # cached graphs are now stale

    def _invalidate(self) -> None:  # forget cached graph views
        self._rev_graph = None  # reversed graph
        self._parallel = None  # parallel-edge index

    # -- graph views -------------------------------------------------------
    def reverse_graph(self) -> nx.MultiDiGraph:  # graph with every road flipped
        """Edges point target->source so one Dijkstra from a destination gives
        the cost-to-destination for every node (see simulation/routing.py)."""
        if self._rev_graph is None:  # not built yet
            g = nx.MultiDiGraph()  # empty directed multigraph
            g.add_nodes_from(self.nodes)  # all nodes
            for e in self.edges.values():  # every road
                g.add_edge(e.target, e.source, key=e.edge_id, edge_id=e.edge_id)  # flipped, tagged with its road id
            self._rev_graph = g  # cache it
        return self._rev_graph  # the reversed graph

    def forward_graph(self) -> nx.MultiDiGraph:  # the normal-direction graph with attributes
        g = nx.MultiDiGraph()  # empty directed multigraph
        for n in self.nodes.values():  # every node
            g.add_node(n.node_id, lat=n.lat, lon=n.lon)  # with its coordinates
        for e in self.edges.values():  # every road
            g.add_edge(e.source, e.target, key=e.edge_id, edge_id=e.edge_id,  # in its real direction
                       length=e.length_m, capacity=e.capacity)  # with length and capacity
        return g  # the graph

    def parallel_edges(self, u: int, v: int) -> List[int]:  # roads that run from u to v
        if self._parallel is None:  # index not built yet
            idx: Dict[Tuple[int, int], List[int]] = {}  # (source, target) -> road ids
            for e in self.edges.values():  # every road
                idx.setdefault((e.source, e.target), []).append(e.edge_id)  # file it under its end nodes
            self._parallel = idx  # cache it
        return self._parallel.get((u, v), [])  # road ids, or an empty list

    # -- geometry ----------------------------------------------------------
    def edge_polyline(self, edge_id: int) -> List[Tuple[float, float]]:  # the shape of a road
        e = self.edges[edge_id]  # the road
        if e.geometry and len(e.geometry) >= 2:  # it has a real curved shape
            return e.geometry  # use it
        a, b = self.nodes[e.source], self.nodes[e.target]  # otherwise a straight line between its ends
        return [(a.lat, a.lon), (b.lat, b.lon)]  # two points

    def nearest_node(self, lat: float, lon: float) -> int:  # closest junction to a point
        return min(self.nodes.values(), key=lambda n: geo.haversine_m(lat, lon, n.lat, n.lon)).node_id  # smallest distance wins

    def nearest_edge(self, lat: float, lon: float) -> int:  # closest road to a point
        best, best_d = None, math.inf  # best road and its distance so far
        for e in self.edges.values():  # every road
            d = geo.distance_to_polyline_m(lat, lon, self.edge_polyline(e.edge_id))  # distance to its shape
            if d < best_d:  # closer than any so far
                best, best_d = e.edge_id, d  # keep it
        if best is None:  # no roads at all
            raise ValueError("network has no edges")  # nothing to return
        return best  # id of the nearest road

    def edges_within(self, lat: float, lon: float, radius_m: float) -> List[int]:  # roads inside a circle
        """Edges whose two end nodes both lie inside the circle (used for danger zones)."""
        inside = {n.node_id for n in self.nodes.values() if geo.haversine_m(lat, lon, n.lat, n.lon) <= radius_m}  # nodes inside the circle
        return sorted(e.edge_id for e in self.edges.values() if e.source in inside and e.target in inside)  # roads with both ends inside

    def bounds(self) -> Tuple[float, float, float, float]:  # bounding box of the network
        lats = [n.lat for n in self.nodes.values()]  # all latitudes
        lons = [n.lon for n in self.nodes.values()]  # all longitudes
        return min(lats), min(lons), max(lats), max(lons)  # south, west, north, east

    def fingerprint(self) -> str:  # short hash of the network content
        """Id-independent content hash (positions in sorted order)."""
        h = hashlib.sha1()  # hash object
        order = {nid: i for i, nid in enumerate(sorted(self.nodes))}  # node id -> position in sorted order
        for nid in sorted(self.nodes):  # nodes in a fixed order
            n = self.nodes[nid]  # the node
            h.update(f"N{order[nid]}:{n.lat:.6f},{n.lon:.6f};".encode())  # add its position
        rows = sorted((order[e.source], order[e.target], round(e.length_m, 1), e.capacity)  # roads described by ends, length, capacity
                      for e in self.edges.values())  # sorted so ids do not matter
        for r in rows:  # each road
            h.update(f"E{r};".encode())  # add it
        return h.hexdigest()[:16]  # first 16 hex characters

    def strongly_connected_fraction(self) -> float:  # how much of the network can reach itself
        g = nx.DiGraph()  # simple directed graph
        g.add_nodes_from(self.nodes)  # all nodes
        g.add_edges_from((e.source, e.target) for e in self.edges.values())  # all roads
        if not self.nodes:  # empty network
            return 0.0  # nothing connected
        biggest = max(nx.strongly_connected_components(g), key=len)  # the largest mutually reachable group
        return len(biggest) / len(self.nodes)  # its share of all nodes


# ---------------------------------------------------------------- scenario
@dataclass
class OriginSpec:  # a place where people start
    node_id: int  # which node
    weight: float = 1.0  # relative share of the crowd
    label: str = ""  # display name
    source_type: str = "USER_DEFINED"  # where it came from


@dataclass
class DestinationSpec:  # a safe stop
    destination_id: Optional[int]  # database id, None until saved
    node_id: int  # which node
    name: str  # display name
    capacity: int  # how many people it can take in total
    throughput_per_timestep: int  # how many it can admit per time step
    available: bool = True  # False = closed
    destination_type: str = "SAFE_AREA"  # kind of destination
    source_type: str = "USER_DEFINED"  # where it came from
    hypothetical: bool = True  # not an official shelter


@dataclass
class ClosureSpec:  # a road closure
    edge_id: int  # which road
    close_time_seconds: float = 0.0  # when it closes


@dataclass
class ModelParams:  # tunable model settings
    """Secondary model parameters (stored as configuration_json)."""
    crowd_distribution: str = "weighted"          # even | weighted | concentrated
    departure_delay_max_seconds: float = 120.0    # uniform response delay in [0, max]
    spread_radius_m: float = 0.0                  # agents start within this radius of an origin
    bpr_alpha: float = 0.6                        # travel-time inflation under load
    bpr_beta: float = 2.0  # steepness of the slowdown
    queue_penalty_gamma: float = 1.0              # weight of expected queue wait in route cost
    dest_load_weight_seconds: float = 300.0       # penalty (s) at 100% projected destination load
    reroute_interval_seconds: float = 30.0        # how often a route may be re-evaluated
    replan_improvement_threshold: float = 0.15    # switch only if >=15% cheaper
    snapshot_interval_seconds: float = 10.0  # how often frames are recorded
    record_movement_events: bool = True  # log every road entry and exit
    record_position_snapshots: bool = True  # store agent positions for the replay
    max_snapshot_agents: int = 2000  # cap on agents stored per frame
    edge_capacity_overrides: Dict[int, int] = field(default_factory=dict)  # road id -> capacity override
    walking_speed_mps: float = 1.4  # walking speed
    # Bomb / blast hazards (simulated emergency-alert drill). Each: {name, node, lat, lon, blast_radius_m,
    # alert_radius_m, strike_time_s, alert_time_s}. People inside an alert radius get a phone alert at alert_time_s;
    # at strike_time_s roads/safe areas inside the blast radius are lost and anyone still inside is caught.
    hazards: List[dict] = field(default_factory=list)  # list of bomb definitions
    # How the alert reaches people (empty = everybody gets the phone alert, the original behaviour):
    # {phone_reach: 0..1 share with a phone at hand, phone_delay_max_s, sirens: [{node, lat, lon, radius_m, delay_s}],
    #  neighbour_radius_m, neighbour_delay_s}  - people nobody reaches never start moving.
    alert_channels: Dict[str, Any] = field(default_factory=dict)  # optional alert-channel settings

    def to_dict(self) -> dict:  # convert to a JSON-friendly dictionary
        d = asdict(self)  # dataclass to dictionary
        d["edge_capacity_overrides"] = {str(k): v for k, v in self.edge_capacity_overrides.items()}  # JSON keys must be text
        return d  # the dictionary

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ModelParams":  # build from a dictionary
        data = dict(data or {})  # copy, tolerate None
        names = {f.name for f in fields(cls)}  # valid field names
        clean = {k: v for k, v in data.items() if k in names}  # drop unknown keys
        if "edge_capacity_overrides" in clean:  # keys came back as text
            clean["edge_capacity_overrides"] = {int(k): int(v) for k, v in clean["edge_capacity_overrides"].items()}  # convert to numbers
        return cls(**clean)  # create the object


@dataclass
class ScenarioConfig:  # everything needed to run one scenario
    name: str  # scenario name
    network_id: str  # which road network
    routing_strategy: str = "SHORTEST_PATH"  # default routing policy
    agent_count: int = 200  # number of people
    random_seed: int = 42  # seed for reproducibility
    timestep_seconds: float = 5.0  # simulation step size
    time_limit_seconds: float = 1800.0  # when to stop
    description: str = ""  # free text
    study_area: str = ""  # place name
    geographic_data_source: str = "SYNTHETIC"  # where the map came from
    network_fingerprint: str = ""  # hash of the network used
    origins: List[OriginSpec] = field(default_factory=list)  # start places
    destinations: List[DestinationSpec] = field(default_factory=list)  # safe stops
    closures: List[ClosureSpec] = field(default_factory=list)  # road closures
    params: ModelParams = field(default_factory=ModelParams)  # tunable settings
    scenario_id: Optional[int] = None  # database id, None until saved
    parent_scenario_id: Optional[int] = None  # the scenario this was derived from

    def copy(self) -> "ScenarioConfig":  # independent deep copy
        import copy  # imported here to keep the module header small
        return copy.deepcopy(self)  # copies nested lists and objects too

    def to_json(self) -> str:  # parameters as JSON text
        return json.dumps(self.params.to_dict(), sort_keys=True)  # stable key order

    def snapshot_json(self) -> str:  # the whole configuration as JSON text
        d = asdict(self)  # dataclass to dictionary
        d["params"] = self.params.to_dict()  # use the JSON-friendly parameters
        return json.dumps(d, sort_keys=True, default=str)  # stable order; unknown types become text


def validate_scenario(network: RoadNetwork, cfg: ScenarioConfig) -> Tuple[List[str], List[str]]:  # check a scenario before running it
    """Return (errors, warnings). Errors block a run; warnings do not."""
    errors: List[str] = []  # problems that stop the run
    warnings: List[str] = []  # things to be aware of
    if not cfg.name.strip():  # blank name
        errors.append("Scenario needs a name.")  # report it
    if cfg.routing_strategy not in STRATEGIES:  # unknown policy
        errors.append(f"Unknown routing strategy {cfg.routing_strategy!r}.")  # report it
    if cfg.agent_count < 1:  # nobody to simulate
        errors.append("Agent count must be at least 1.")  # report it
    if cfg.timestep_seconds <= 0:  # invalid step
        errors.append("Timestep must be positive.")  # report it
    if cfg.time_limit_seconds <= cfg.timestep_seconds:  # run would last less than one step
        errors.append("Time limit must exceed one timestep.")  # report it
    if not cfg.origins:  # nowhere to start
        errors.append("Define at least one origin.")  # report it
    for o in cfg.origins:  # each origin
        if o.node_id not in network.nodes:  # not a real node
            errors.append(f"Origin node {o.node_id} is not in the network.")  # report it
        if o.weight <= 0:  # would never be chosen
            errors.append(f"Origin node {o.node_id} needs a positive weight.")  # report it
    avail = [d for d in cfg.destinations if d.available and d.capacity > 0]  # usable safe stops
    if not avail:  # nowhere to go
        errors.append("Define at least one available destination with capacity > 0.")  # report it
    for d in cfg.destinations:  # each safe stop
        if d.node_id not in network.nodes:  # not a real node
            errors.append(f"Destination {d.name!r} is not on a network node.")  # report it
        if d.capacity < 0 or d.throughput_per_timestep < 1:  # invalid numbers
            errors.append(f"Destination {d.name!r}: capacity >= 0 and throughput >= 1 required.")  # report it
    for h in cfg.params.hazards:  # each bomb
        if h.get("blast_radius_m", 0) <= 0 or h.get("alert_radius_m", 0) < h.get("blast_radius_m", 0):  # radii do not make sense
            errors.append("Hazard: blast radius must be > 0 and the alert radius at least as large.")  # report it
        if h.get("alert_time_s", 0) > h.get("strike_time_s", 0) or h.get("alert_time_s", 0) < 0:  # alert not before the strike
            errors.append("Hazard: the alert must be sent between t=0 and the strike time.")  # report it
    for c in cfg.closures:  # each closure
        if c.edge_id not in network.edges:  # not a real road
            errors.append(f"Closure references unknown edge {c.edge_id}.")  # report it
        if c.close_time_seconds < 0:  # in the past
            errors.append("Closure times cannot be negative.")  # report it
    if cfg.params.crowd_distribution not in ("even", "weighted", "concentrated"):  # unknown mode
        errors.append("crowd_distribution must be even, weighted or concentrated.")  # report it
    if cfg.params.snapshot_interval_seconds <= 0:  # invalid interval
        errors.append("Snapshot interval must be positive.")  # report it
    if not errors:  # only worth warning when the scenario is otherwise valid
        total_cap = sum(d.capacity for d in avail)  # total space in all safe stops
        if total_cap < cfg.agent_count:  # not enough room for everyone
            warnings.append(f"Total available destination capacity ({total_cap}) is below the agent count "  # first half of the message
                            f"({cfg.agent_count}); some agents cannot be evacuated by design.")  # second half
        if network.is_synthetic:  # fake map
            warnings.append("Network is SYNTHETIC - results do not describe real roads.")  # remind the user
        if any(d.hypothetical for d in avail):  # made-up safe stops
            warnings.append("Some destinations are hypothetical (user-defined), not official shelters.")  # remind the user
    return errors, warnings  # both lists
