"""Tiny deterministic networks for tests (no internet, no OSM)."""
from __future__ import annotations

from typing import Dict, Sequence, Tuple

from geo import geographic_utils as geo
from simulation.models import (ClosureSpec, DestinationSpec, Edge, ModelParams, Node, OriginSpec, RoadNetwork,
                               ScenarioConfig)

LAT, LON = 16.45, 80.53


def make_net(links: Sequence[Tuple[int, int, float, int]], coords: Dict[int, Tuple[float, float]] = None,
             bidirectional: bool = True) -> RoadNetwork:
    """links = (u, v, length_m, capacity). Nodes are laid out on a line unless coords are given."""
    net = RoadNetwork("test", "SYNTHETIC", center=(LAT, LON))
    ids = sorted({n for l in links for n in l[:2]})
    for i, n in enumerate(ids):
        c = (coords or {}).get(n)
        lat, lon = c if c else geo.offset_latlon(LAT, LON, 0, i * 50.0)
        net.add_node(Node(n, lat, lon))
    eid = 1
    for u, v, length, cap in links:
        for s, t in ((u, v), (v, u)) if bidirectional else ((u, v),):
            net.add_edge(Edge(eid, s, t, length, length / 1.4, cap))
            eid += 1
    return net


def make_cfg(net: RoadNetwork, origins: Sequence[int], dests: Sequence[Tuple[int, int, int, int]],
             strategy: str = "SHORTEST_PATH", agents: int = 20, seed: int = 1, limit: float = 3000.0,
             dt: float = 5.0, closures: Sequence[Tuple[int, float]] = (), **params) -> ScenarioConfig:
    """dests = (destination_id, node, capacity, throughput)."""
    p = ModelParams(departure_delay_max_seconds=params.pop("departure_delay_max_seconds", 0.0), **params)
    return ScenarioConfig(
        name="t", network_id="test-net", routing_strategy=strategy, agent_count=agents, random_seed=seed,
        timestep_seconds=dt, time_limit_seconds=limit, origins=[OriginSpec(o) for o in origins],
        destinations=[DestinationSpec(i, n, f"D{i}", cap, thr) for i, n, cap, thr in dests],
        closures=[ClosureSpec(e, t) for e, t in closures], params=p)


def diamond(cap_short=50, cap_long=50) -> RoadNetwork:
    """1 -- 2 -- 4 (short, 200 m)  and  1 -- 3 -- 4 (long, 300 m)."""
    return make_net([(1, 2, 100, cap_short), (2, 4, 100, cap_short), (1, 3, 150, cap_long), (3, 4, 150, cap_long)])


def edge_between(net: RoadNetwork, u: int, v: int) -> int:
    return net.parallel_edges(u, v)[0]
