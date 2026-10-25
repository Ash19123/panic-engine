"""Tiny deterministic networks for tests (no internet, no OSM)."""
from __future__ import annotations  # modern type hints on older Python

from typing import Dict, Sequence, Tuple  # type hints

from geo import geographic_utils as geo  # offset helper
from simulation.models import (ClosureSpec, DestinationSpec, Edge, ModelParams, Node, OriginSpec, RoadNetwork,
                               ScenarioConfig)  # building blocks of networks and scenarios

LAT, LON = 16.45, 80.53  # where the test networks are placed


def make_net(links: Sequence[Tuple[int, int, float, int]], coords: Dict[int, Tuple[float, float]] = None,
             bidirectional: bool = True) -> RoadNetwork:  # build a small network from a list of links
    """links = (u, v, length_m, capacity). Nodes are laid out on a line unless coords are given."""
    net = RoadNetwork("test", "SYNTHETIC", center=(LAT, LON))  # empty network
    ids = sorted({n for l in links for n in l[:2]})  # every node id used by a link
    for i, n in enumerate(ids):  # each node
        c = (coords or {}).get(n)  # explicit coordinates, if given
        lat, lon = c if c else geo.offset_latlon(LAT, LON, 0, i * 50.0)  # otherwise on a line, 50 m apart
        net.add_node(Node(n, lat, lon))  # add it
    eid = 1  # next road id
    for u, v, length, cap in links:  # each link
        for s, t in ((u, v), (v, u)) if bidirectional else ((u, v),):  # both directions, or one
            net.add_edge(Edge(eid, s, t, length, length / 1.4, cap))  # add the road (walking time at 1.4 m/s)
            eid += 1  # next id
    return net  # the network


def make_cfg(net: RoadNetwork, origins: Sequence[int], dests: Sequence[Tuple[int, int, int, int]],
             strategy: str = "SHORTEST_PATH", agents: int = 20, seed: int = 1, limit: float = 3000.0,
             dt: float = 5.0, closures: Sequence[Tuple[int, float]] = (), **params) -> ScenarioConfig:  # build a scenario for a test
    """dests = (destination_id, node, capacity, throughput)."""
    p = ModelParams(departure_delay_max_seconds=params.pop("departure_delay_max_seconds", 0.0), **params)  # parameters; no reaction delay unless asked
    return ScenarioConfig(  # the scenario
        name="t", network_id="test-net", routing_strategy=strategy, agent_count=agents, random_seed=seed,  # identity and size
        timestep_seconds=dt, time_limit_seconds=limit, origins=[OriginSpec(o) for o in origins],  # timing and start places
        destinations=[DestinationSpec(i, n, f"D{i}", cap, thr) for i, n, cap, thr in dests],  # safe stops
        closures=[ClosureSpec(e, t) for e, t in closures], params=p)  # closures and parameters


def diamond(cap_short=50, cap_long=50) -> RoadNetwork:  # a network with a short and a long route
    """1 -- 2 -- 4 (short, 200 m)  and  1 -- 3 -- 4 (long, 300 m)."""
    return make_net([(1, 2, 100, cap_short), (2, 4, 100, cap_short), (1, 3, 150, cap_long), (3, 4, 150, cap_long)])  # four links


def edge_between(net: RoadNetwork, u: int, v: int) -> int:  # the road id from u to v
    return net.parallel_edges(u, v)[0]  # the first road between them
