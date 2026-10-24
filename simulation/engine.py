"""Timestep-based agent evacuation engine.

Step order at simulation time t (all deterministic, FIFO tie-breaking by agent id):
  1. apply scheduled road closures        (cached routes are invalidated)
  2. agents whose edge traversal finished arrive at a node
  3. agents whose departure time has come depart and plan a route
  4. destinations admit queued agents (throughput per step, total capacity)
  5. agents waiting at nodes try to enter their next edge (capacity check)
  6. waiting/queue statistics, then optional snapshot

Policies (documented assumptions):
* Closure while an agent is ON the edge: the agent finishes that edge ("in-progress
  edge policy"); nobody can ENTER a closed edge after the closure step.
* Closure times are applied at the first step with time >= the closure time.
* Throughput is "agents admitted per timestep", so it scales with the timestep.
* Arrival times are processed at step boundaries, so an agent may idle up to one
  timestep between finishing an edge and starting the next (discretisation error).
"""
from __future__ import annotations

import heapq
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from geo import geographic_utils as geo
from simulation.agent import Agent, generate_population
from simulation.bottlenecks import BottleneckReport, BottleneckThresholds, detect_bottlenecks
from simulation.congestion import CongestionState
from simulation.metrics import compute_run_metrics
from simulation.movement import agent_position
from simulation.models import AgentState, DestinationSpec, RoadNetwork, ScenarioConfig
from simulation.routing import Router
from simulation.snapshots import SnapshotRecorder

log = logging.getLogger(__name__)
EPS = 1e-9


@dataclass
class DestState:
    spec: DestinationSpec
    did: int
    node: int
    capacity: int
    throughput: int
    available: bool
    admitted: int = 0
    assigned: int = 0
    queue: Deque[Agent] = field(default_factory=deque)


@dataclass
class SimulationResult:
    config: ScenarioConfig
    agents: List[Agent]
    events: List[Tuple]
    occupancy_rows: List[Tuple]
    position_rows: List[Tuple]
    frame_rows: List[Tuple]
    bottlenecks: List[BottleneckReport]
    metrics: Dict
    total_simulated_seconds: float
    dijkstra_searches: int
    state: CongestionState
    destinations: Dict[int, DestState]

    def fingerprint(self) -> Tuple:
        """Everything that must be identical for two runs to count as reproducible."""
        return (tuple((a.agent_id, a.state.value, a.destination_id, a.evacuation_time, round(a.wait_seconds, 6),
                       a.route_changes) for a in self.agents),
                tuple(self.events), self.total_simulated_seconds)


class EvacuationSimulation:
    def __init__(self, network: RoadNetwork, cfg: ScenarioConfig, agents: Optional[Sequence[Agent]] = None,
                 thresholds: Optional[BottleneckThresholds] = None):
        self.net = network
        self.cfg = cfg
        self.p = cfg.params
        self.dt = cfg.timestep_seconds
        self.strategy = cfg.routing_strategy
        self.thresholds = thresholds or BottleneckThresholds()
        self.state = CongestionState(network, self.p)
        self.router = Router(network, self.state, self.strategy, self.p)
        self.agents: List[Agent] = list(agents) if agents is not None else generate_population(network, cfg)
        self.by_id = {a.agent_id: a for a in self.agents}
        if len(self.by_id) != len(self.agents):
            raise ValueError("agent ids must be unique")
        self.dests: Dict[int, DestState] = {}
        for d in cfg.destinations:
            if d.destination_id is None:
                raise ValueError("destinations need ids before simulation (save the scenario first)")
            self.dests[d.destination_id] = DestState(d, d.destination_id, d.node_id, d.capacity,
                                                     d.throughput_per_timestep, d.available)
        self.pending = sorted(self.agents, key=lambda a: (a.departure_time, a.agent_id))
        self._dep_i = 0
        self.closures = sorted(cfg.closures, key=lambda c: (c.close_time_seconds, c.edge_id))
        self._clo_i = 0
        self.at_node: List[Agent] = []
        self._heap: List[Tuple[float, int]] = []
        self.events: List[Tuple] = []
        self.recorder = SnapshotRecorder(network, self.p, self.dt)
        self.terminal = 0
        self.t = 0.0
        self.hazards = sorted(cfg.params.hazards, key=lambda h: (h["strike_time_s"], h.get("name", "")))
        self._haz_i = 0
        self._alerts = sorted((a for a in self.agents if a.alert_time is not None), key=lambda a: (a.alert_time, a.agent_id))
        self._alert_i = 0
        if self.strategy == "LOAD_BALANCED":
            self._assign_load_balanced()

    # ------------------------------------------------------------ helpers
    def _ev(self, a: Optional[Agent], t: float, kind: str, node=None, edge=None, dest=None, wait=None, details=None):
        self.events.append((a.agent_id if a else None, t, kind, node, edge, dest, wait, details))

    def _dest_open(self, did: Optional[int]) -> bool:
        d = self.dests.get(did)
        return d is not None and d.available and d.admitted < d.capacity

    def _dest_penalty(self, d: DestState, a: Agent) -> float:
        if self.strategy == "SHORTEST_PATH":
            return 0.0
        load = d.assigned - (1 if a.destination_id == d.did else 0)
        return self.p.dest_load_weight_seconds * max(0, load) / max(1, d.capacity)

    def _set_destination(self, a: Agent, did: Optional[int]) -> None:
        if a.destination_id == did:
            return
        if a.destination_id in self.dests:
            self.dests[a.destination_id].assigned -= 1
        a.destination_id = did
        if did is not None:
            self.dests[did].assigned += 1

    def _assign_load_balanced(self) -> None:
        """Centralised planner: greedy min-cost assignment with a load penalty.

        Agents are processed in (departure, id) order; each takes the destination
        minimising  path_cost + w * assigned/capacity,  skipping destinations whose
        projected load already reaches capacity (unless every one is full).
        """
        for a in self.pending:
            best, best_key = None, None
            for did, d in sorted(self.dests.items()):
                if not d.available or d.capacity <= 0:
                    continue
                r = self.router.route_from(a.origin_node, d.node, 0.0)
                if r is None:
                    continue
                full = d.assigned >= d.capacity
                score = r[0] + self.p.dest_load_weight_seconds * d.assigned / d.capacity
                key = (full, score, did)
                if best_key is None or key < best_key:
                    best, best_key = did, key
            if best is not None:
                self._set_destination(a, best)

    # ------------------------------------------------------------ planning
    def _plan(self, a: Agent, node: int, t: float, force: bool) -> bool:
        """Choose destination + route from ``node``. Returns False if none is feasible."""
        pool = list(self.dests.values())
        if self.strategy == "LOAD_BALANCED" and self._dest_open(a.destination_id):
            pool = [self.dests[a.destination_id]]
        for attempt in range(2):
            cands = []
            for d in sorted(pool, key=lambda x: x.did):
                if not d.available or d.admitted >= d.capacity:
                    continue
                r = self.router.route_from(node, d.node, t)
                if r is None:
                    continue
                cands.append((r[0] + self._dest_penalty(d, a), d.did, r[1]))
            if cands or len(pool) == len(self.dests):
                break
            pool = list(self.dests.values())          # assigned destination unusable: fall back
        if not cands:
            return False
        score, did, route = min(cands, key=lambda c: (c[0], c[1]))
        initial = a.last_plan_time == float("-inf")
        cur = a.destination_id
        if not force and a.route and self._dest_open(cur):
            cur_score = self.router.route_cost(a.route) + self._dest_penalty(self.dests[cur], a)
            if score >= cur_score * (1.0 - self.p.replan_improvement_threshold):
                a.last_plan_time = t                     # evaluated, current plan is good enough
                return True
        changed = (route != a.route) or (did != cur)
        self._set_destination(a, did)
        a.route = list(route)
        a.last_plan_time = t
        a.needs_replan = False
        a.decisions.append((t, "PLAN" if initial else "REPLAN", did, len(route)))
        if changed and not initial:
            a.route_changes += 1
            self._ev(a, t, "ROUTE_CHANGED", node=node, dest=did, details=f"new route of {len(route)} edges")
        return True

    # ------------------------------------------------------- state changes
    def _fail(self, a: Agent, t: float, reason: str) -> None:
        if a.state == AgentState.QUEUED and a.queued_since is not None:
            self._ev(a, t, "QUEUE_LEFT", node=a.node, wait=t - a.queued_since)
        a.transition(AgentState.UNABLE_TO_EVACUATE)
        a.queued_since = None
        self._set_destination(a, None)
        self.terminal += 1
        self._ev(a, t, "AGENT_FAILED", node=a.node, details=reason)

    def _join_dest_queue(self, a: Agent, t: float) -> None:
        d = self.dests[a.destination_id]
        if a.state != AgentState.QUEUED:
            a.transition(AgentState.QUEUED)
            a.queued_since = t
            self._ev(a, t, "QUEUE_JOINED", node=a.node, dest=d.did, details="destination gate")
        d.queue.append(a)

    def _enter_edge(self, a: Agent, eid: int, t: float) -> None:
        if eid in self.state.closed:
            raise RuntimeError(f"agent {a.agent_id} attempted to enter closed edge {eid}")
        a.route.pop(0)
        duration = self.state.enter(eid)
        if a.state == AgentState.QUEUED:
            self._ev(a, t, "QUEUE_LEFT", node=a.node, edge=eid, wait=t - a.queued_since)
        a.queued_since = None
        a.transition(AgentState.MOVING)
        a.node = None
        a.edge = eid
        a.edge_enter_time = t
        a.edge_arrival_time = t + duration
        heapq.heappush(self._heap, (a.edge_arrival_time, a.agent_id))
        if self.p.record_movement_events:
            self._ev(a, t, "EDGE_ENTERED", node=self.net.edges[eid].source, edge=eid)

    def _evacuate(self, a: Agent, d: DestState, t: float) -> None:
        wait = t - a.queued_since if a.queued_since is not None else 0.0
        self._ev(a, t, "QUEUE_LEFT", node=a.node, dest=d.did, wait=wait)
        a.transition(AgentState.EVACUATED)
        a.evacuation_time = t
        a.queued_since = None
        d.admitted += 1
        d.assigned -= 1
        self.terminal += 1
        self._ev(a, t, "AGENT_EVACUATED", node=d.node, dest=d.did, wait=a.wait_seconds)

    # ---------------------------------------------------------------- steps
    def _apply_closures(self, t: float) -> None:
        while self._clo_i < len(self.closures) and self.closures[self._clo_i].close_time_seconds <= t + EPS:
            c = self.closures[self._clo_i]
            self._clo_i += 1
            self._close_edge(c.edge_id, t, "closure takes effect")

    def _close_edge(self, edge_id: int, t: float, why: str) -> None:
        if edge_id in self.state.closed:
            return
        self.state.closed.add(edge_id)
        self.router.invalidate()
        self._ev(None, t, "ROAD_BLOCKED", edge=edge_id, details=why)
        for a in self.agents:
            if not a.is_terminal and edge_id in a.route:
                a.needs_replan = True

    # ------------------------------------------------------------ alerts and hazards
    def _send_alerts(self, t: float) -> None:
        while self._alert_i < len(self._alerts) and self._alerts[self._alert_i].alert_time <= t + EPS:
            a = self._alerts[self._alert_i]
            self._alert_i += 1
            self._ev(a, t, "ALERT_SENT", node=a.node, details=f"hazard alert delivered by {(a.alert_channel or 'PHONE').lower()}")

    def _strike_hazards(self, t: float) -> None:
        """At strike time: roads inside the blast radius close, safe areas inside it are lost, and anyone
        still inside (walking, queued or not yet departed) is caught - the drill's measure of a late alert."""
        while self._haz_i < len(self.hazards) and self.hazards[self._haz_i]["strike_time_s"] <= t + EPS:
            h = self.hazards[self._haz_i]
            self._haz_i += 1
            self._ev(None, t, "HAZARD_STRIKE", node=h.get("node"), details=f"{h.get('name', 'hazard')} blast r={h['blast_radius_m']:.0f} m")
            for eid in self.net.edges_within(h["lat"], h["lon"], h["blast_radius_m"]):
                self._close_edge(eid, t, "inside blast radius")
            for d in self.dests.values():
                nd = self.net.nodes[d.node]
                if d.available and geo.haversine_m(nd.lat, nd.lon, h["lat"], h["lon"]) <= h["blast_radius_m"]:
                    d.available = False
            for a in self.agents:
                if a.is_terminal:
                    continue
                lat, lon = agent_position(self.net, a, t)
                if geo.haversine_m(lat, lon, h["lat"], h["lon"]) <= h["blast_radius_m"]:
                    self._catch(a, t)

    def _catch(self, a: Agent, t: float) -> None:
        if a.edge is not None:                      # leave the road without finishing it; stay near where they were
            e = self.net.edges[a.edge]
            frac = (t - a.edge_enter_time) / max(1e-9, a.edge_arrival_time - a.edge_enter_time)
            a.node = e.target if frac >= 0.5 else e.source
            self.state.leave(a.edge)
            a.edge = None
        for d in self.dests.values():
            if a in d.queue:
                d.queue.remove(a)
        self._fail(a, t, "caught in blast area at strike")

    def _arrivals(self, t: float) -> None:
        while self._heap and self._heap[0][0] <= t + EPS:
            arr, aid = heapq.heappop(self._heap)
            a = self.by_id[aid]
            if a.is_terminal or a.edge is None or abs(a.edge_arrival_time - arr) > 1e-9:
                continue                            # stale entry (agent was caught while walking)
            eid = a.edge
            self.state.leave(eid)
            edge = self.net.edges[eid]
            a.node, a.edge = edge.target, None
            if self.p.record_movement_events:
                self._ev(a, arr, "EDGE_EXITED", node=edge.target, edge=eid)
            if a.route:
                self.at_node.append(a)
            else:
                self._join_dest_queue(a, t)

    def _departures(self, t: float) -> None:
        while self._dep_i < len(self.pending) and self.pending[self._dep_i].departure_time <= t + EPS:
            a = self.pending[self._dep_i]
            self._dep_i += 1
            if a.is_terminal:                       # caught by a blast before ever departing
                continue
            self._ev(a, t, "AGENT_DEPARTED", node=a.origin_node)
            if not self._plan(a, a.origin_node, t, True):
                self._fail(a, t, "no reachable destination with remaining capacity")
            elif a.route:
                self.at_node.append(a)
            else:
                self._join_dest_queue(a, t)

    def _destinations(self, t: float) -> None:
        for did in sorted(self.dests):
            d = self.dests[did]
            admitted_now = 0
            while d.queue and admitted_now < d.throughput and d.admitted < d.capacity and d.available:
                self._evacuate(d.queue.popleft(), d, t)
                admitted_now += 1
            if d.queue and (d.admitted >= d.capacity or not d.available):
                stuck, d.queue = list(d.queue), deque()
                for a in stuck:
                    if not self._plan(a, d.node, t, True):
                        self._fail(a, t, "destination full/unavailable and no alternative reachable")
                    elif a.route:
                        self.at_node.append(a)
                    else:
                        self._join_dest_queue(a, t)

    def _process_nodes(self, t: float) -> None:
        still: List[Agent] = []
        for a in self.at_node:
            if a.is_terminal:
                continue
            node = a.node
            force = a.needs_replan or not a.route or a.route[0] in self.state.closed
            if force or (self.router.dynamic and t - a.last_plan_time >= self.p.reroute_interval_seconds - EPS):
                if not self._plan(a, node, t, force):
                    self._fail(a, t, "no feasible route")
                    continue
                if not a.route:
                    self._join_dest_queue(a, t)
                    continue
            eid = a.route[0]
            if not self.state.is_full(eid):
                self._enter_edge(a, eid, t)
            else:
                if a.state != AgentState.QUEUED:
                    a.transition(AgentState.QUEUED)
                    a.queued_since = t
                    self._ev(a, t, "QUEUE_JOINED", node=node, edge=eid)
                still.append(a)
        self.at_node = still

    def _account(self, t: float) -> None:
        st = self.state
        for eid in st.queue:
            st.queue[eid] = 0
        self.at_node = [a for a in self.at_node if not a.is_terminal]
        for a in self.at_node:
            st.queue[a.route[0]] += 1
            a.wait_seconds += self.dt
            st.affected_agents[a.route[0]].add(a.agent_id)
        for d in self.dests.values():
            for a in d.queue:
                a.wait_seconds += self.dt
        for eid, q in st.queue.items():
            if q:
                st.queue_seconds[eid] += self.dt
                st.wait_seconds[eid] += q * self.dt
                st.first_queue_time.setdefault(eid, t)

    def step(self, t: float) -> None:
        self.t = t
        self._apply_closures(t)
        self._send_alerts(t)
        self._strike_hazards(t)
        self._arrivals(t)
        self._departures(t)
        self._destinations(t)
        self._process_nodes(t)
        self._account(t)

    def run(self) -> SimulationResult:
        log.info("Simulation start: strategy=%s agents=%d dt=%s limit=%s", self.strategy, len(self.agents),
                 self.dt, self.cfg.time_limit_seconds)
        t, last = 0.0, 0.0
        n = 0
        while True:
            self.step(t)
            last = t
            if self.recorder.due(t):
                self.recorder.record(t, self.state, self.agents)
            if self.terminal == len(self.agents):
                break
            n += 1
            t = n * self.dt
            if t > self.cfg.time_limit_seconds + EPS:
                break
        if not self.recorder.times or self.recorder.times[-1] != last:
            self.recorder.record(last, self.state, self.agents)
        for a in self.agents:
            if not a.is_terminal:
                self._ev(a, last, "AGENT_UNRESOLVED", node=a.node, edge=a.edge, dest=a.destination_id,
                         details="time limit reached")
        metrics = compute_run_metrics(self.agents)
        log.info("Simulation end t=%.0f evacuated=%d/%d", last, metrics["evacuated"], metrics["total_agents"])
        return SimulationResult(
            self.cfg, self.agents, self.events, [(r[0], r[1], r[2], r[3]) for r in self.recorder.occupancy_rows],
            list(self.recorder.position_rows), list(self.recorder.frame_rows),
            detect_bottlenecks(self.state, self.thresholds), metrics, last, self.router.search_count,
            self.state, self.dests)


def simulate(network: RoadNetwork, cfg: ScenarioConfig, agents: Optional[Sequence[Agent]] = None,
             thresholds: Optional[BottleneckThresholds] = None) -> SimulationResult:
    return EvacuationSimulation(network, cfg, agents, thresholds).run()
