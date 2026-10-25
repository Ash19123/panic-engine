"""Dynamic edge state: occupancy, queues, closures and the congestion-aware cost.

Model assumptions (all documented in the README):

* An edge holds at most ``capacity`` agents at once. A full edge cannot be
  entered; agents wait in a holding area at its tail node (they do not block
  the previous edge, which keeps the model deadlock-free).
* Traversal time is fixed at entry using a BPR-style function of the occupancy
  ratio r = occupancy / capacity:  t = t0 * (1 + alpha * r**beta).
"""
from __future__ import annotations  # modern type hints on older Python

import math  # maths helpers
from collections import defaultdict  # dictionaries with automatic default values
from typing import Dict, Optional, Set  # type hints

from simulation.models import Edge, ModelParams, RoadNetwork  # road, parameters, road graph


def effective_capacity(edge: Edge, params: ModelParams) -> int:  # capacity after any scenario override
    return max(1, int(params.edge_capacity_overrides.get(edge.edge_id, edge.capacity)))  # override if present, never below 1


def bpr_travel_time(edge: Edge, occupancy: int, capacity: int, params: ModelParams) -> float:  # time to walk a road under load
    ratio = occupancy / capacity  # how full the road is
    return edge.free_flow_time_s * (1.0 + params.bpr_alpha * ratio ** params.bpr_beta)  # t0 * (1 + alpha * r^beta)


class CongestionState:  # tracks how busy every road is during a run
    """Mutable per-edge state shared by the movement model, the router and the recorder."""

    def __init__(self, network: RoadNetwork, params: ModelParams):  # start with empty roads
        self.network = network  # the road graph
        self.params = params  # model parameters
        self.capacity: Dict[int, int] = {eid: effective_capacity(e, params) for eid, e in network.edges.items()}  # capacity of each road
        self.occupancy: Dict[int, int] = {eid: 0 for eid in network.edges}  # people on each road now
        self.queue: Dict[int, int] = {eid: 0 for eid in network.edges}   # agents waiting to ENTER the edge
        self.closed: Set[int] = {eid for eid, e in network.edges.items() if not e.base_available}  # roads closed from the start
        # cumulative statistics used by bottleneck detection
        self.peak_occupancy: Dict[int, int] = defaultdict(int)  # highest occupancy per road
        self.queue_seconds: Dict[int, float] = defaultdict(float)  # time with a queue per road
        self.wait_seconds: Dict[int, float] = defaultdict(float)  # agent-seconds of waiting per road
        self.traversals: Dict[int, int] = defaultdict(int)  # how many times each road was entered
        self.first_queue_time: Dict[int, float] = {}  # when each road first had a queue
        self.affected_agents: Dict[int, Set[int]] = defaultdict(set)  # who had to wait for each road

    def ratio(self, edge_id: int) -> float:  # occupancy as a share of capacity
        return self.occupancy[edge_id] / self.capacity[edge_id]  # 0 = empty, 1 = full

    def is_full(self, edge_id: int) -> bool:  # can nobody else enter?
        return self.occupancy[edge_id] >= self.capacity[edge_id]  # at or above capacity

    def enter(self, edge_id: int) -> float:  # an agent steps onto a road
        """Register an entry and return the traversal time that applies."""
        e = self.network.edges[edge_id]  # the road
        duration = bpr_travel_time(e, self.occupancy[edge_id], self.capacity[edge_id], self.params)  # time fixed by the current load
        self.occupancy[edge_id] += 1  # one more person on the road
        self.traversals[edge_id] += 1  # count the entry
        if self.occupancy[edge_id] > self.peak_occupancy[edge_id]:  # a new record
            self.peak_occupancy[edge_id] = self.occupancy[edge_id]  # remember it
        return duration  # how long this agent will take

    def leave(self, edge_id: int) -> None:  # an agent leaves a road
        if self.occupancy[edge_id] <= 0:  # nobody to remove: a bug
            raise RuntimeError(f"edge {edge_id}: occupancy would become negative")  # fail loudly
        self.occupancy[edge_id] -= 1  # one fewer person

    # -- routing costs -----------------------------------------------------
    def free_flow_cost(self, edge_id: int) -> Optional[float]:  # cost used by shortest-path routing
        """Baseline edge weight: free-flow walking time (seconds). None = unusable."""
        if edge_id in self.closed:  # closed road
            return None  # routing must skip it
        return self.network.edges[edge_id].free_flow_time_s  # plain walking time

    def congestion_cost(self, edge_id: int) -> Optional[float]:  # cost used by congestion-aware routing
        """Congestion-aware weight (seconds), strictly positive:

            cost = t0 * (1 + alpha * r**beta)  +  gamma * q * t0 / capacity

        first term: expected traversal time under the current load (the same BPR
        function the movement model applies); second: expected wait before
        entry, ~ q agents ahead drained at capacity/t0 agents per second.
        """
        if edge_id in self.closed:  # closed road
            return None  # routing must skip it
        e = self.network.edges[edge_id]  # the road
        cap = self.capacity[edge_id]  # its capacity
        r = self.occupancy[edge_id] / cap  # how full it is
        p = self.params  # shorthand for the parameters
        travel = e.free_flow_time_s * (1.0 + p.bpr_alpha * r ** p.bpr_beta)  # slowed walking time
        wait = p.queue_penalty_gamma * self.queue[edge_id] * e.free_flow_time_s / cap  # expected wait behind the queue
        return travel + wait  # total expected cost in seconds

    def severe(self, edge_id: int, threshold: float = 0.9) -> bool:  # is the road nearly full?
        return self.ratio(edge_id) >= threshold  # true at 90% full or more by default
