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
from __future__ import annotations  # modern type hints on older Python

import heapq  # priority queue of road arrivals
import logging  # progress messages
from collections import deque  # fast queue for safe-stop gates
from dataclasses import dataclass, field  # simple record classes
from typing import Deque, Dict, List, Optional, Sequence, Tuple  # type hints

from geo import geographic_utils as geo  # distance helpers
from simulation.agent import Agent, generate_population  # the agent class and the crowd generator
from simulation.bottlenecks import BottleneckReport, BottleneckThresholds, detect_bottlenecks  # bottleneck detection
from simulation.congestion import CongestionState  # live road occupancy and queues
from simulation.metrics import compute_run_metrics  # run summary numbers
from simulation.movement import agent_position  # position of an agent at a time
from simulation.models import AgentState, DestinationSpec, RoadNetwork, ScenarioConfig  # states, safe-stop spec, road graph, scenario
from simulation.routing import Router  # route finder
from simulation.snapshots import SnapshotRecorder  # frame recorder

log = logging.getLogger(__name__)  # logger for this module
EPS = 1e-9  # tolerance for comparing floating-point times


@dataclass
class DestState:  # live state of one safe stop
    spec: DestinationSpec  # its definition
    did: int  # destination id
    node: int  # node it sits on
    capacity: int  # total people it can admit
    throughput: int  # people it can admit per time step
    available: bool  # False once lost or closed
    admitted: int = 0  # people admitted so far
    assigned: int = 0  # people currently heading here
    queue: Deque[Agent] = field(default_factory=deque)  # people waiting at its gate


@dataclass
class SimulationResult:  # everything a finished run produces
    config: ScenarioConfig  # the scenario that was run
    agents: List[Agent]  # all agents with their final state
    events: List[Tuple]  # the event log
    occupancy_rows: List[Tuple]  # sparse road occupancy snapshots
    position_rows: List[Tuple]  # agent position snapshots
    frame_rows: List[Tuple]  # per-frame state counts
    bottlenecks: List[BottleneckReport]  # detected bottlenecks
    metrics: Dict  # run summary metrics
    total_simulated_seconds: float  # how long the run lasted
    dijkstra_searches: int  # number of route searches (performance)
    state: CongestionState  # final road state
    destinations: Dict[int, DestState]  # final safe-stop state

    def fingerprint(self) -> Tuple:  # a comparable summary of the whole run
        """Everything that must be identical for two runs to count as reproducible."""
        return (tuple((a.agent_id, a.state.value, a.destination_id, a.evacuation_time, round(a.wait_seconds, 6),  # per-agent outcome (part 1)
                       a.route_changes) for a in self.agents),  # per-agent outcome (part 2)
                tuple(self.events), self.total_simulated_seconds)  # the full event log and the duration


class EvacuationSimulation:  # the engine
    def __init__(self, network: RoadNetwork, cfg: ScenarioConfig, agents: Optional[Sequence[Agent]] = None,
                 thresholds: Optional[BottleneckThresholds] = None):  # set up a run
        self.net = network  # road graph
        self.cfg = cfg  # scenario
        self.p = cfg.params  # model parameters
        self.dt = cfg.timestep_seconds  # time step size
        self.strategy = cfg.routing_strategy  # routing policy
        self.thresholds = thresholds or BottleneckThresholds()  # bottleneck limits
        self.state = CongestionState(network, self.p)  # live road state
        self.router = Router(network, self.state, self.strategy, self.p)  # route finder
        self.agents: List[Agent] = list(agents) if agents is not None else generate_population(network, cfg)  # use given agents or generate the crowd
        self.by_id = {a.agent_id: a for a in self.agents}  # lookup by id
        if len(self.by_id) != len(self.agents):  # duplicate ids
            raise ValueError("agent ids must be unique")  # reject
        self.dests: Dict[int, DestState] = {}  # live safe stops by id
        for d in cfg.destinations:  # each safe stop of the scenario
            if d.destination_id is None:  # not saved yet
                raise ValueError("destinations need ids before simulation (save the scenario first)")  # reject
            self.dests[d.destination_id] = DestState(d, d.destination_id, d.node_id, d.capacity,  # create its live state (part 1)
                                                     d.throughput_per_timestep, d.available)  # (part 2)
        self.pending = sorted(self.agents, key=lambda a: (a.departure_time, a.agent_id))  # agents in departure order
        self._dep_i = 0  # index of the next agent to depart
        self.closures = sorted(cfg.closures, key=lambda c: (c.close_time_seconds, c.edge_id))  # closures in time order
        self._clo_i = 0  # index of the next closure
        self.at_node: List[Agent] = []  # agents standing at a node, ready to move on
        self._heap: List[Tuple[float, int]] = []  # (arrival time, agent id) for agents on roads
        self.events: List[Tuple] = []  # the event log
        self.recorder = SnapshotRecorder(network, self.p, self.dt)  # frame recorder
        self.terminal = 0  # how many agents have finished
        self.t = 0.0  # current simulation time
        self.hazards = sorted(cfg.params.hazards, key=lambda h: (h["strike_time_s"], h.get("name", "")))  # bombs in strike order
        self._haz_i = 0  # index of the next bomb to strike
        self._alerts = sorted((a for a in self.agents if a.alert_time is not None), key=lambda a: (a.alert_time, a.agent_id))  # alerted agents in alert order
        self._alert_i = 0  # index of the next alert to send
        if self.strategy == "LOAD_BALANCED":  # central planner policy
            self._assign_load_balanced()  # assign safe stops up front

    # ------------------------------------------------------------ helpers
    def _ev(self, a: Optional[Agent], t: float, kind: str, node=None, edge=None, dest=None, wait=None, details=None):  # log an event
        self.events.append((a.agent_id if a else None, t, kind, node, edge, dest, wait, details))  # a tuple per event

    def _dest_open(self, did: Optional[int]) -> bool:  # can this safe stop still take people?
        d = self.dests.get(did)  # look it up
        return d is not None and d.available and d.admitted < d.capacity  # exists, open and not full

    def _dest_penalty(self, d: DestState, a: Agent) -> float:  # extra cost for crowded safe stops
        if self.strategy == "SHORTEST_PATH":  # that policy ignores load
            return 0.0  # no penalty
        load = d.assigned - (1 if a.destination_id == d.did else 0)  # people heading there, not counting this agent
        return self.p.dest_load_weight_seconds * max(0, load) / max(1, d.capacity)  # grows with how full it will be

    def _set_destination(self, a: Agent, did: Optional[int]) -> None:  # change an agent's safe stop
        if a.destination_id == did:  # no change
            return  # nothing to do
        if a.destination_id in self.dests:  # leaving a previous stop
            self.dests[a.destination_id].assigned -= 1  # it has one fewer person heading to it
        a.destination_id = did  # remember the new stop
        if did is not None:  # a real stop
            self.dests[did].assigned += 1  # it has one more person heading to it

    def _assign_load_balanced(self) -> None:  # central planner for the LOAD_BALANCED policy
        """Centralised planner: greedy min-cost assignment with a load penalty.

        Agents are processed in (departure, id) order; each takes the destination
        minimising  path_cost + w * assigned/capacity,  skipping destinations whose
        projected load already reaches capacity (unless every one is full).
        """
        for a in self.pending:  # agents in departure order
            best, best_key = None, None  # best safe stop so far
            for did, d in sorted(self.dests.items()):  # every safe stop in a fixed order
                if not d.available or d.capacity <= 0:  # unusable
                    continue  # skip it
                r = self.router.route_from(a.origin_node, d.node, 0.0)  # route from the agent's start
                if r is None:  # unreachable
                    continue  # skip it
                full = d.assigned >= d.capacity  # would it already be full?
                score = r[0] + self.p.dest_load_weight_seconds * d.assigned / d.capacity  # path cost plus load penalty
                key = (full, score, did)  # not-full stops win, then lowest score, then lowest id
                if best_key is None or key < best_key:  # better than the best so far
                    best, best_key = did, key  # keep it
            if best is not None:  # a stop was found
                self._set_destination(a, best)  # assign it

    # ------------------------------------------------------------ planning
    def _plan(self, a: Agent, node: int, t: float, force: bool) -> bool:  # choose a safe stop and a route
        """Choose destination + route from ``node``. Returns False if none is feasible."""
        pool = list(self.dests.values())  # candidate safe stops
        if self.strategy == "LOAD_BALANCED" and self._dest_open(a.destination_id):  # keep the assigned stop if it is still usable
            pool = [self.dests[a.destination_id]]  # only that one
        for attempt in range(2):  # second attempt widens the pool
            cands = []  # candidate plans
            for d in sorted(pool, key=lambda x: x.did):  # each stop in a fixed order
                if not d.available or d.admitted >= d.capacity:  # closed or full
                    continue  # skip it
                r = self.router.route_from(node, d.node, t)  # route to it
                if r is None:  # unreachable
                    continue  # skip it
                cands.append((r[0] + self._dest_penalty(d, a), d.did, r[1]))  # (score, stop id, route)
            if cands or len(pool) == len(self.dests):  # found something, or already tried all stops
                break  # stop trying
            pool = list(self.dests.values())          # assigned destination unusable: fall back
        if not cands:  # no feasible plan
            return False  # tell the caller
        score, did, route = min(cands, key=lambda c: (c[0], c[1]))  # cheapest plan, ties by stop id
        initial = a.last_plan_time == float("-inf")  # is this the first plan of this agent?
        cur = a.destination_id  # the stop the agent is heading to now
        if not force and a.route and self._dest_open(cur):  # a voluntary re-plan while the current plan is still valid
            cur_score = self.router.route_cost(a.route) + self._dest_penalty(self.dests[cur], a)  # cost of staying on the current plan
            if score >= cur_score * (1.0 - self.p.replan_improvement_threshold):  # the new plan is not clearly better
                a.last_plan_time = t  # note that we looked
                return True  # keep the current plan
        changed = (route != a.route) or (did != cur)  # is the plan different?
        self._set_destination(a, did)  # take the new stop
        a.route = list(route)  # take the new route
        a.last_plan_time = t  # remember when we planned
        a.needs_replan = False  # the request is satisfied
        a.decisions.append((t, "PLAN" if initial else "REPLAN", did, len(route)))  # record the decision
        if changed and not initial:  # a real change of mind
            a.route_changes += 1  # count it
            self._ev(a, t, "ROUTE_CHANGED", node=node, dest=did, details=f"new route of {len(route)} edges")  # log it
        return True  # a plan exists

    # ------------------------------------------------------- state changes
    def _fail(self, a: Agent, t: float, reason: str) -> None:  # an agent cannot evacuate
        if a.state == AgentState.QUEUED and a.queued_since is not None:  # it was waiting
            self._ev(a, t, "QUEUE_LEFT", node=a.node, wait=t - a.queued_since)  # log the end of the wait
        a.transition(AgentState.UNABLE_TO_EVACUATE)  # final failed state
        a.queued_since = None  # no longer waiting
        self._set_destination(a, None)  # release its safe stop
        self.terminal += 1  # one more finished agent
        self._ev(a, t, "AGENT_FAILED", node=a.node, details=reason)  # log it with the reason

    def _join_dest_queue(self, a: Agent, t: float) -> None:  # an agent reaches a safe stop's gate
        d = self.dests[a.destination_id]  # the stop
        if a.state != AgentState.QUEUED:  # not already queued
            a.transition(AgentState.QUEUED)  # now waiting at the gate
            a.queued_since = t  # since when
            self._ev(a, t, "QUEUE_JOINED", node=a.node, dest=d.did, details="destination gate")  # log it
        d.queue.append(a)  # join the end of the gate queue

    def _enter_edge(self, a: Agent, eid: int, t: float) -> None:  # an agent steps onto a road
        if eid in self.state.closed:  # closed roads must never be entered
            raise RuntimeError(f"agent {a.agent_id} attempted to enter closed edge {eid}")  # a bug if it happens
        a.route.pop(0)  # this road is no longer ahead
        duration = self.state.enter(eid)  # register the entry and get the travel time
        if a.state == AgentState.QUEUED:  # the agent was waiting for this road
            self._ev(a, t, "QUEUE_LEFT", node=a.node, edge=eid, wait=t - a.queued_since)  # log the end of the wait
        a.queued_since = None  # no longer waiting
        a.transition(AgentState.MOVING)  # now walking
        a.node = None  # not at a node while on a road
        a.edge = eid  # the road being walked
        a.edge_enter_time = t  # entry time
        a.edge_arrival_time = t + duration  # exit time
        heapq.heappush(self._heap, (a.edge_arrival_time, a.agent_id))  # schedule the arrival
        if self.p.record_movement_events:  # optional detailed log
            self._ev(a, t, "EDGE_ENTERED", node=self.net.edges[eid].source, edge=eid)  # log the entry

    def _evacuate(self, a: Agent, d: DestState, t: float) -> None:  # a safe stop admits an agent
        wait = t - a.queued_since if a.queued_since is not None else 0.0  # time spent at the gate
        self._ev(a, t, "QUEUE_LEFT", node=a.node, dest=d.did, wait=wait)  # log leaving the gate queue
        a.transition(AgentState.EVACUATED)  # final safe state
        a.evacuation_time = t  # time since the alert
        a.queued_since = None  # no longer waiting
        d.admitted += 1  # the stop has one more person
        d.assigned -= 1  # one fewer is still heading there
        self.terminal += 1  # one more finished agent
        self._ev(a, t, "AGENT_EVACUATED", node=d.node, dest=d.did, wait=a.wait_seconds)  # log it

    # ---------------------------------------------------------------- steps
    def _apply_closures(self, t: float) -> None:  # close roads whose time has come
        while self._clo_i < len(self.closures) and self.closures[self._clo_i].close_time_seconds <= t + EPS:  # due closures
            c = self.closures[self._clo_i]  # the closure
            self._clo_i += 1  # move on
            self._close_edge(c.edge_id, t, "closure takes effect")  # apply it

    def _close_edge(self, edge_id: int, t: float, why: str) -> None:  # make a road unusable
        if edge_id in self.state.closed:  # already closed
            return  # nothing to do
        self.state.closed.add(edge_id)  # mark it closed
        self.router.invalidate()  # cached routes are stale
        self._ev(None, t, "ROAD_BLOCKED", edge=edge_id, details=why)  # log it
        for a in self.agents:  # every agent
            if not a.is_terminal and edge_id in a.route:  # still active and its route uses this road
                a.needs_replan = True  # it must find another way

    # ------------------------------------------------------------ alerts and hazards
    def _send_alerts(self, t: float) -> None:  # deliver alerts whose time has come
        while self._alert_i < len(self._alerts) and self._alerts[self._alert_i].alert_time <= t + EPS:  # due alerts
            a = self._alerts[self._alert_i]  # the agent
            self._alert_i += 1  # move on
            self._ev(a, t, "ALERT_SENT", node=a.node, details=f"hazard alert delivered by {(a.alert_channel or 'PHONE').lower()}")  # log it with the channel

    def _strike_hazards(self, t: float) -> None:  # bombs go off
        """At strike time: roads inside the blast radius close, safe areas inside it are lost, and anyone
        still inside (walking, queued or not yet departed) is caught - the drill's measure of a late alert."""
        while self._haz_i < len(self.hazards) and self.hazards[self._haz_i]["strike_time_s"] <= t + EPS:  # due strikes
            h = self.hazards[self._haz_i]  # the bomb
            self._haz_i += 1  # move on
            self._ev(None, t, "HAZARD_STRIKE", node=h.get("node"), details=f"{h.get('name', 'hazard')} blast r={h['blast_radius_m']:.0f} m")  # log it
            for eid in self.net.edges_within(h["lat"], h["lon"], h["blast_radius_m"]):  # roads inside the blast
                self._close_edge(eid, t, "inside blast radius")  # close them
            for d in self.dests.values():  # every safe stop
                nd = self.net.nodes[d.node]  # its node
                if d.available and geo.haversine_m(nd.lat, nd.lon, h["lat"], h["lon"]) <= h["blast_radius_m"]:  # inside the blast
                    d.available = False  # lost
            for a in self.agents:  # every agent
                if a.is_terminal:  # already finished
                    continue  # skip it
                lat, lon = agent_position(self.net, a, t)  # where the agent is now
                if geo.haversine_m(lat, lon, h["lat"], h["lon"]) <= h["blast_radius_m"]:  # inside the blast
                    self._catch(a, t)  # caught

    def _catch(self, a: Agent, t: float) -> None:  # an agent is inside the blast at the strike
        if a.edge is not None:                      # leave the road without finishing it; stay near where they were
            e = self.net.edges[a.edge]  # the road they were on
            frac = (t - a.edge_enter_time) / max(1e-9, a.edge_arrival_time - a.edge_enter_time)  # how far along they were
            a.node = e.target if frac >= 0.5 else e.source  # snap to the nearer end
            self.state.leave(a.edge)  # free their place on the road
            a.edge = None  # no longer on a road
        for d in self.dests.values():  # every safe stop
            if a in d.queue:  # the agent was at its gate
                d.queue.remove(a)  # remove them
        self._fail(a, t, "caught in blast area at strike")  # final failed state with the reason

    def _arrivals(self, t: float) -> None:  # agents finishing a road reach the next node
        while self._heap and self._heap[0][0] <= t + EPS:  # arrivals that are due
            arr, aid = heapq.heappop(self._heap)  # the earliest
            a = self.by_id[aid]  # the agent
            if a.is_terminal or a.edge is None or abs(a.edge_arrival_time - arr) > 1e-9:  # outdated entry
                continue                            # stale entry (agent was caught while walking)
            eid = a.edge  # the road just finished
            self.state.leave(eid)  # free their place
            edge = self.net.edges[eid]  # the road
            a.node, a.edge = edge.target, None  # now standing at its far end
            if self.p.record_movement_events:  # optional detailed log
                self._ev(a, arr, "EDGE_EXITED", node=edge.target, edge=eid)  # log it
            if a.route:  # more roads to walk
                self.at_node.append(a)  # wait at the node to continue
            else:  # route finished
                self._join_dest_queue(a, t)  # join the safe stop's gate queue

    def _departures(self, t: float) -> None:  # agents whose departure time has come start moving
        while self._dep_i < len(self.pending) and self.pending[self._dep_i].departure_time <= t + EPS:  # due departures
            a = self.pending[self._dep_i]  # the agent
            self._dep_i += 1  # move on
            if a.is_terminal:                       # caught by a blast before ever departing
                continue  # nothing to do
            self._ev(a, t, "AGENT_DEPARTED", node=a.origin_node)  # log it
            if not self._plan(a, a.origin_node, t, True):  # no feasible plan
                self._fail(a, t, "no reachable destination with remaining capacity")  # give up
            elif a.route:  # has a route to walk
                self.at_node.append(a)  # start from the node
            else:  # already at the safe stop
                self._join_dest_queue(a, t)  # join its gate queue

    def _destinations(self, t: float) -> None:  # safe stops admit people from their gates
        for did in sorted(self.dests):  # every stop in a fixed order
            d = self.dests[did]  # the stop
            admitted_now = 0  # admitted in this step
            while d.queue and admitted_now < d.throughput and d.admitted < d.capacity and d.available:  # room and time left this step
                self._evacuate(d.queue.popleft(), d, t)  # admit the first in line
                admitted_now += 1  # count it
            if d.queue and (d.admitted >= d.capacity or not d.available):  # people are stuck at a full or lost stop
                stuck, d.queue = list(d.queue), deque()  # take them all out of the queue
                for a in stuck:  # each stuck person
                    if not self._plan(a, d.node, t, True):  # try another stop
                        self._fail(a, t, "destination full/unavailable and no alternative reachable")  # nowhere else to go
                    elif a.route:  # a new route exists
                        self.at_node.append(a)  # set off again
                    else:  # already at the new stop
                        self._join_dest_queue(a, t)  # join its gate queue

    def _process_nodes(self, t: float) -> None:  # agents at nodes try to enter their next road
        still: List[Agent] = []  # agents that remain at a node after this step
        for a in self.at_node:  # each agent at a node
            if a.is_terminal:  # already finished
                continue  # skip it
            node = a.node  # where they stand
            force = a.needs_replan or not a.route or a.route[0] in self.state.closed  # must they re-plan now?
            if force or (self.router.dynamic and t - a.last_plan_time >= self.p.reroute_interval_seconds - EPS):  # forced, or time for a periodic re-plan
                if not self._plan(a, node, t, force):  # no feasible route
                    self._fail(a, t, "no feasible route")  # give up
                    continue  # next agent
                if not a.route:  # the new plan is to be at the stop already
                    self._join_dest_queue(a, t)  # join its gate queue
                    continue  # next agent
            eid = a.route[0]  # the next road
            if not self.state.is_full(eid):  # there is room
                self._enter_edge(a, eid, t)  # step onto it
            else:  # the road is full
                if a.state != AgentState.QUEUED:  # newly waiting
                    a.transition(AgentState.QUEUED)  # now queued
                    a.queued_since = t  # since when
                    self._ev(a, t, "QUEUE_JOINED", node=node, edge=eid)  # log it
                still.append(a)  # try again next step
        self.at_node = still  # keep only those still waiting

    def _account(self, t: float) -> None:  # update queue and waiting statistics
        st = self.state  # the road state
        for eid in st.queue:  # every road
            st.queue[eid] = 0  # reset its queue count
        self.at_node = [a for a in self.at_node if not a.is_terminal]  # drop finished agents
        for a in self.at_node:  # each waiting agent
            st.queue[a.route[0]] += 1  # counts toward the queue of its next road
            a.wait_seconds += self.dt  # waited one more step
            st.affected_agents[a.route[0]].add(a.agent_id)  # this road delayed them
        for d in self.dests.values():  # every safe stop
            for a in d.queue:  # each person at its gate
                a.wait_seconds += self.dt  # waited one more step
        for eid, q in st.queue.items():  # every road again
            if q:  # it has a queue
                st.queue_seconds[eid] += self.dt  # queue lasted another step
                st.wait_seconds[eid] += q * self.dt  # agent-seconds of waiting
                st.first_queue_time.setdefault(eid, t)  # remember when the first queue appeared

    def step(self, t: float) -> None:  # one time step
        self.t = t  # current time
        self._apply_closures(t)  # 1. close roads
        self._send_alerts(t)  # 2. deliver alerts
        self._strike_hazards(t)  # 3. blasts
        self._arrivals(t)  # 4. arrivals at nodes
        self._departures(t)  # 5. departures
        self._destinations(t)  # 6. safe-stop admissions
        self._process_nodes(t)  # 7. entering roads
        self._account(t)  # 8. statistics

    def run(self) -> SimulationResult:  # run until everyone finishes or the time limit
        log.info("Simulation start: strategy=%s agents=%d dt=%s limit=%s", self.strategy, len(self.agents),  # log the settings (part 1)
                 self.dt, self.cfg.time_limit_seconds)  # (part 2)
        t, last = 0.0, 0.0  # current time and last simulated time
        n = 0  # step counter
        while True:  # the main loop
            self.step(t)  # simulate this step
            last = t  # remember it
            if self.recorder.due(t):  # time for a snapshot
                self.recorder.record(t, self.state, self.agents)  # take it
            if self.terminal == len(self.agents):  # everybody has finished
                break  # stop
            n += 1  # next step
            t = n * self.dt  # computing from n avoids accumulating float error
            if t > self.cfg.time_limit_seconds + EPS:  # past the time limit
                break  # stop
        if not self.recorder.times or self.recorder.times[-1] != last:  # the last step has no snapshot yet
            self.recorder.record(last, self.state, self.agents)  # take one
        for a in self.agents:  # every agent
            if not a.is_terminal:  # still on the way
                self._ev(a, last, "AGENT_UNRESOLVED", node=a.node, edge=a.edge, dest=a.destination_id,  # log it (part 1)
                         details="time limit reached")  # (part 2)
        metrics = compute_run_metrics(self.agents)  # summary numbers
        log.info("Simulation end t=%.0f evacuated=%d/%d", last, metrics["evacuated"], metrics["total_agents"])  # log the outcome
        return SimulationResult(  # package everything
            self.cfg, self.agents, self.events, [(r[0], r[1], r[2], r[3]) for r in self.recorder.occupancy_rows],  # config, agents, events, occupancy rows
            list(self.recorder.position_rows), list(self.recorder.frame_rows),  # position and frame rows
            detect_bottlenecks(self.state, self.thresholds), metrics, last, self.router.search_count,  # bottlenecks, metrics, duration, searches
            self.state, self.dests)  # final road state and safe-stop state


def simulate(network: RoadNetwork, cfg: ScenarioConfig, agents: Optional[Sequence[Agent]] = None,
             thresholds: Optional[BottleneckThresholds] = None) -> SimulationResult:  # convenience function
    return EvacuationSimulation(network, cfg, agents, thresholds).run()  # build the engine and run it
