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
from __future__ import annotations  # modern type hints on older Python

import math  # infinity
from typing import Dict, List, Optional, Tuple  # type hints

import networkx as nx  # graph algorithms (Dijkstra)

from simulation.congestion import CongestionState  # live road costs
from simulation.models import ModelParams, RoadNetwork  # parameters and road graph

Tree = Tuple[Dict[int, float], Dict[int, List[int]]]  # (cost to destination per node, path per node)


class Router:  # finds routes to safe stops
    def __init__(self, network: RoadNetwork, state: CongestionState, strategy: str, params: ModelParams):  # set up the router
        self.network = network  # road graph
        self.state = state  # live congestion state
        self.strategy = strategy  # which routing policy
        self.params = params  # model parameters
        self.closure_epoch = 0  # increases whenever a road closes
        self._graph = network.reverse_graph()  # graph with edges flipped, so one search from a safe stop reaches everyone
        self._cache: Dict[int, Tree] = {}  # computed trees by destination node
        self._cache_key: Optional[Tuple[int, int]] = None  # (closure epoch, time bucket) the cache belongs to
        self.search_count = 0     # number of Dijkstra runs (performance diagnostics)

    @property
    def dynamic(self) -> bool:  # does this strategy react to congestion?
        return self.strategy != "SHORTEST_PATH"  # everything except plain shortest path does

    def edge_cost(self, edge_id: int) -> Optional[float]:  # cost of one road under this strategy
        if self.dynamic:  # congestion-aware family
            return self.state.congestion_cost(edge_id)  # includes crowding and queues
        return self.state.free_flow_cost(edge_id)  # plain walking time

    def _weight(self, u: int, v: int, parallel: dict) -> Optional[float]:  # networkx weight callback for a node pair
        best = None  # cheapest usable road so far
        for attrs in parallel.values():  # every parallel road between u and v
            c = self.edge_cost(attrs["edge_id"])  # its cost
            if c is not None and (best is None or c < best):  # usable and cheaper
                best = c  # keep it
        return best                       # None => networkx skips the edge (closed)

    def invalidate(self) -> None:  # called when a road closes
        """Call when an edge closes: every cached tree is stale."""
        self.closure_epoch += 1  # changes the cache key, so trees are rebuilt

    def tree(self, dest_node: int, t: float) -> Tree:  # search tree for one safe stop
        bucket = int(t // self.params.reroute_interval_seconds) if self.dynamic else 0  # time bucket (dynamic strategies only)
        key = (self.closure_epoch, bucket)  # what the cache is valid for
        if key != self._cache_key:  # closures or time moved on
            self._cache.clear()  # throw the old trees away
            self._cache_key = key  # remember the new key
        if dest_node not in self._cache:  # not computed yet
            dist, paths = nx.single_source_dijkstra(self._graph, dest_node, weight=self._weight)  # one search from the safe stop
            self._cache[dest_node] = (dist, paths)  # store it
            self.search_count += 1  # count the search
        return self._cache[dest_node]  # the tree

    def route_from(self, node: int, dest_node: int, t: float) -> Optional[Tuple[float, List[int]]]:  # route from a node to a safe stop
        """(cost, edge-id route) from ``node`` to ``dest_node`` or None when unreachable."""
        dist, paths = self.tree(dest_node, t)  # the search tree for that stop
        if node not in dist:  # the node cannot reach the stop
            return None  # unreachable
        node_path = list(reversed(paths[node]))  # the tree path runs stop-to-node, so reverse it
        return dist[node], self.path_edges(node_path)  # total cost and the list of road ids

    def path_edges(self, node_path: List[int]) -> List[int]:  # turn a node path into road ids
        edges: List[int] = []  # road ids in order
        for u, v in zip(node_path, node_path[1:]):  # each consecutive pair of nodes
            best, best_c = None, math.inf  # cheapest road between them
            for eid in self.network.parallel_edges(u, v):  # every road from u to v
                c = self.edge_cost(eid)  # its cost
                if c is not None and c < best_c:  # usable and cheaper
                    best, best_c = eid, c  # keep it
            if best is None:  # no usable road (should not happen)
                raise RuntimeError(f"no usable edge between {u} and {v}")  # fail loudly
            edges.append(best)  # add the chosen road
        return edges  # the full route

    def route_cost(self, route: List[int]) -> float:  # total cost of a list of roads
        total = 0.0  # running total
        for eid in route:  # each road
            c = self.edge_cost(eid)  # its cost
            if c is None:  # a closed road on the route
                return math.inf  # the route is unusable
            total += c  # add its cost
        return total  # total cost
