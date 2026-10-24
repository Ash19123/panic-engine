"""Dynamic edge state: occupancy, queues, closures and the congestion-aware cost.

Model assumptions (all documented in the README):

* An edge holds at most ``capacity`` agents at once. A full edge cannot be
  entered; agents wait in a holding area at its tail node (they do not block
  the previous edge, which keeps the model deadlock-free).
* Traversal time is fixed at entry using a BPR-style function of the occupancy
  ratio r = occupancy / capacity:  t = t0 * (1 + alpha * r**beta).
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, Optional, Set

from simulation.models import Edge, ModelParams, RoadNetwork


def effective_capacity(edge: Edge, params: ModelParams) -> int:
    return max(1, int(params.edge_capacity_overrides.get(edge.edge_id, edge.capacity)))


def bpr_travel_time(edge: Edge, occupancy: int, capacity: int, params: ModelParams) -> float:
    ratio = occupancy / capacity
    return edge.free_flow_time_s * (1.0 + params.bpr_alpha * ratio ** params.bpr_beta)


class CongestionState:
    """Mutable per-edge state shared by the movement model, the router and the recorder."""

    def __init__(self, network: RoadNetwork, params: ModelParams):
        self.network = network
        self.params = params
        self.capacity: Dict[int, int] = {eid: effective_capacity(e, params) for eid, e in network.edges.items()}
        self.occupancy: Dict[int, int] = {eid: 0 for eid in network.edges}
        self.queue: Dict[int, int] = {eid: 0 for eid in network.edges}   # agents waiting to ENTER the edge
        self.closed: Set[int] = {eid for eid, e in network.edges.items() if not e.base_available}
        # cumulative statistics used by bottleneck detection
        self.peak_occupancy: Dict[int, int] = defaultdict(int)
        self.queue_seconds: Dict[int, float] = defaultdict(float)
        self.wait_seconds: Dict[int, float] = defaultdict(float)
        self.traversals: Dict[int, int] = defaultdict(int)
        self.first_queue_time: Dict[int, float] = {}
        self.affected_agents: Dict[int, Set[int]] = defaultdict(set)

    def ratio(self, edge_id: int) -> float:
        return self.occupancy[edge_id] / self.capacity[edge_id]

    def is_full(self, edge_id: int) -> bool:
        return self.occupancy[edge_id] >= self.capacity[edge_id]

    def enter(self, edge_id: int) -> float:
        """Register an entry and return the traversal time that applies."""
        e = self.network.edges[edge_id]
        duration = bpr_travel_time(e, self.occupancy[edge_id], self.capacity[edge_id], self.params)
        self.occupancy[edge_id] += 1
        self.traversals[edge_id] += 1
        if self.occupancy[edge_id] > self.peak_occupancy[edge_id]:
            self.peak_occupancy[edge_id] = self.occupancy[edge_id]
        return duration

    def leave(self, edge_id: int) -> None:
        if self.occupancy[edge_id] <= 0:
            raise RuntimeError(f"edge {edge_id}: occupancy would become negative")
        self.occupancy[edge_id] -= 1

    # -- routing costs -----------------------------------------------------
    def free_flow_cost(self, edge_id: int) -> Optional[float]:
        """Baseline edge weight: free-flow walking time (seconds). None = unusable."""
        if edge_id in self.closed:
            return None
        return self.network.edges[edge_id].free_flow_time_s

    def congestion_cost(self, edge_id: int) -> Optional[float]:
        """Congestion-aware weight (seconds), strictly positive:

            cost = t0 * (1 + alpha * r**beta)  +  gamma * q * t0 / capacity

        first term: expected traversal time under the current load (the same BPR
        function the movement model applies); second: expected wait before
        entry, ~ q agents ahead drained at capacity/t0 agents per second.
        """
        if edge_id in self.closed:
            return None
        e = self.network.edges[edge_id]
        cap = self.capacity[edge_id]
        r = self.occupancy[edge_id] / cap
        p = self.params
        travel = e.free_flow_time_s * (1.0 + p.bpr_alpha * r ** p.bpr_beta)
        wait = p.queue_penalty_gamma * self.queue[edge_id] * e.free_flow_time_s / cap
        return travel + wait

    def severe(self, edge_id: int, threshold: float = 0.9) -> bool:
        return self.ratio(edge_id) >= threshold
