"""Routing: Dijkstra shortest-path trees under two different edge-cost models.

Strategies
----------
SHORTEST_PATH     Baseline. Edge weight = free-flow walking time t0 = length / speed
                  (time, not raw distance, so slow/long edges are treated consistently).
                  Ignores congestion; only closures change the answer.
CONGESTION_AWARE  Edge weight = t0 * (1 + a * r^b) + gamma * q * t0 / capacity
                  (see ``CongestionState.congestion_cost``). All terms are >= 0 and
                  t0 > 0, so Dijkstra's non-negativity requirement holds. Trees are
                  cached per ``reroute_interval`` so costs are not recomputed every step.
LOAD_BALANCED     Centralised policy: a planner with global knowledge assigns agents to
                  destinations in proportion to remaining capacity (see ``engine``),
                  then routes with the congestion-aware cost. It differs from the other
                  two in WHO decides the destination, not only in edge weights.

One reverse-graph Dijkstra per destination yields the cost-to-destination of every node,
so replanning hundreds of agents costs a handful of searches, not one per agent.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import networkx as nx

from simulation.congestion import CongestionState
from simulation.models import ModelParams, RoadNetwork

Tree = Tuple[Dict[int, float], Dict[int, List[int]]]


class Router:
    def __init__(self, network: RoadNetwork, state: CongestionState, strategy: str, params: ModelParams):
        self.network = network
        self.state = state
        self.strategy = strategy
        self.params = params
        self.closure_epoch = 0
        self._graph = network.reverse_graph()
        self._cache: Dict[int, Tree] = {}
        self._cache_key: Optional[Tuple[int, int]] = None
        self.search_count = 0     # number of Dijkstra runs (performance diagnostics)

    @property
    def dynamic(self) -> bool:
        return self.strategy != "SHORTEST_PATH"

    def edge_cost(self, edge_id: int) -> Optional[float]:
        if self.dynamic:
            return self.state.congestion_cost(edge_id)
        return self.state.free_flow_cost(edge_id)

    def _weight(self, u: int, v: int, parallel: dict) -> Optional[float]:
        best = None
        for attrs in parallel.values():
            c = self.edge_cost(attrs["edge_id"])
            if c is not None and (best is None or c < best):
                best = c
        return best                       # None => networkx skips the edge (closed)

    def invalidate(self) -> None:
        """Call when an edge closes: every cached tree is stale."""
        self.closure_epoch += 1

    def tree(self, dest_node: int, t: float) -> Tree:
        bucket = int(t // self.params.reroute_interval_seconds) if self.dynamic else 0
        key = (self.closure_epoch, bucket)
        if key != self._cache_key:
            self._cache.clear()
            self._cache_key = key
        if dest_node not in self._cache:
            dist, paths = nx.single_source_dijkstra(self._graph, dest_node, weight=self._weight)
            self._cache[dest_node] = (dist, paths)
            self.search_count += 1
        return self._cache[dest_node]

    def route_from(self, node: int, dest_node: int, t: float) -> Optional[Tuple[float, List[int]]]:
        """(cost, edge-id route) from ``node`` to ``dest_node`` or None when unreachable."""
        dist, paths = self.tree(dest_node, t)
        if node not in dist:
            return None
        node_path = list(reversed(paths[node]))
        return dist[node], self.path_edges(node_path)

    def path_edges(self, node_path: List[int]) -> List[int]:
        edges: List[int] = []
        for u, v in zip(node_path, node_path[1:]):
            best, best_c = None, math.inf
            for eid in self.network.parallel_edges(u, v):
                c = self.edge_cost(eid)
                if c is not None and c < best_c:
                    best, best_c = eid, c
            if best is None:
                raise RuntimeError(f"no usable edge between {u} and {v}")
            edges.append(best)
        return edges

    def route_cost(self, route: List[int]) -> float:
        total = 0.0
        for eid in route:
            c = self.edge_cost(eid)
            if c is None:
                return math.inf
            total += c
        return total
