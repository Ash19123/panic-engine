"""Agents, their finite-state machine and reproducible population generation."""
from __future__ import annotations

import heapq
import math
import random
from typing import List, Optional, Tuple

from geo import geographic_utils as geo
from simulation.models import (ALLOWED_TRANSITIONS, TERMINAL_STATES, AgentState, RoadNetwork, ScenarioConfig)


class IllegalTransition(RuntimeError):
    pass


class Agent:
    __slots__ = ("agent_id", "origin_node", "departure_time", "state", "node", "edge", "edge_enter_time",
                 "edge_arrival_time", "route", "destination_id", "evacuation_time", "wait_seconds",
                 "queued_since", "last_plan_time", "route_changes", "needs_replan", "decisions", "alert_time", "alert_channel")

    def __init__(self, agent_id: int, origin_node: int, departure_time: float, alert_time: Optional[float] = None,
                 alert_channel: Optional[str] = None):
        self.agent_id = agent_id
        self.origin_node = origin_node
        self.departure_time = departure_time
        self.alert_channel = alert_channel             # PHONE / SIREN / NEIGHBOUR / NONE (None = not in an alert area)
        self.alert_time = alert_time                   # when a hazard alert reaches this person's phone (None = no alert)
        self.state = AgentState.WAITING
        self.node: Optional[int] = origin_node        # None while travelling on an edge
        self.edge: Optional[int] = None
        self.edge_enter_time = 0.0
        self.edge_arrival_time = 0.0
        self.route: List[int] = []                     # remaining edge ids
        self.destination_id: Optional[int] = None
        self.evacuation_time: Optional[float] = None   # seconds since the alert (t = 0)
        self.wait_seconds = 0.0                        # time spent QUEUED
        self.queued_since: Optional[float] = None
        self.last_plan_time = -math.inf
        self.route_changes = 0
        self.needs_replan = False
        self.decisions: List[Tuple[float, str, Optional[int], int]] = []   # (t, kind, destination, route length)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(self, new_state: AgentState) -> None:
        if new_state == self.state and not self.is_terminal:
            return
        if new_state not in ALLOWED_TRANSITIONS[self.state]:
            raise IllegalTransition(f"agent {self.agent_id}: {self.state.value} -> {new_state.value}")
        self.state = new_state

    def __repr__(self) -> str:
        return f"Agent({self.agent_id}, {self.state.value}, node={self.node}, edge={self.edge})"


def _origin_weights(cfg: ScenarioConfig) -> List[float]:
    n = len(cfg.origins)
    mode = cfg.params.crowd_distribution
    if mode == "even":
        return [1.0] * n
    if mode == "concentrated":
        # 70 % of the crowd at the first origin, the rest shared evenly.
        if n == 1:
            return [1.0]
        return [0.7] + [0.3 / (n - 1)] * (n - 1)
    return [o.weight for o in cfg.origins]


def alert_time_for(network: RoadNetwork, node_id: int, hazards) -> Optional[float]:
    """Earliest alert time among the hazards whose alert radius contains this node (None if none does)."""
    n = network.nodes[node_id]
    times = [h["alert_time_s"] for h in hazards
             if geo.haversine_m(n.lat, n.lon, h["lat"], h["lon"]) <= h["alert_radius_m"]]
    return min(times) if times else None


def apply_alert_channels(network: RoadNetwork, agents: List[Agent], cfg: ScenarioConfig, delays) -> None:
    """Decide HOW and WHEN each person inside an alert area learns about the hazard.

    * PHONE      a random ``phone_reach`` share has a phone at hand and gets the alert (plus 0..N s delivery delay)
    * SIREN      anyone within a siren's radius hears it ``delay_s`` after the alert time
    * NEIGHBOUR  anybody within ``neighbour_radius_m`` of an alerted person is told ``neighbour_delay_s`` later
                 (relay chains: Dijkstra over people)
    * NONE       nobody reaches this person: they never start moving
    A person starts reacting (``departure = alert + reaction delay``) only once alerted. With no channels configured
    everybody inside an alert area is alerted by PHONE at the hazard's alert time (the original behaviour).
    A separate random stream is used, so origins and reaction delays stay identical across channel variants.
    """
    channels = cfg.params.alert_channels
    inside = [a for a in agents if a.alert_time is not None]
    if not channels:
        for a in inside:
            a.alert_channel = "PHONE"
        return
    rng = random.Random(cfg.random_seed * 7919 + 17)
    reach = float(channels.get("phone_reach", 1.0))
    dmax = float(channels.get("phone_delay_max_s", 15.0))
    sirens = channels.get("sirens", [])
    when = {}                                              # agent id -> (alert time, channel)
    for a in sorted(inside, key=lambda x: x.agent_id):
        base = a.alert_time
        has_phone, jitter = rng.random() < reach, rng.uniform(0.0, dmax)      # always drawn: stable stream
        best = (base + jitter, "PHONE") if has_phone else (None, "NONE")
        nd = network.nodes[a.origin_node]
        for s in sirens:
            if geo.haversine_m(nd.lat, nd.lon, s["lat"], s["lon"]) <= s["radius_m"]:
                t = base + float(s.get("delay_s", 10.0))
                if best[0] is None or t < best[0]:
                    best = (t, "SIREN")
        when[a.agent_id] = best
    radius = float(channels.get("neighbour_radius_m", 0.0))
    if radius > 0:
        step = float(channels.get("neighbour_delay_s", 30.0))
        lat0 = sum(network.nodes[a.origin_node].lat for a in inside) / len(inside)
        kx, ky = math.cos(math.radians(lat0)) * 111_320.0, 110_540.0
        pos = {a.agent_id: (network.nodes[a.origin_node].lon * kx, network.nodes[a.origin_node].lat * ky) for a in inside}
        cells = {}
        for aid, (x, y) in pos.items():
            cells.setdefault((int(x // radius), int(y // radius)), []).append(aid)
        heap = [(t, aid) for aid, (t, _) in when.items() if t is not None]
        heapq.heapify(heap)
        done = set()
        while heap:
            t, aid = heapq.heappop(heap)
            if aid in done or t > when[aid][0] + 1e-9:
                continue
            done.add(aid)
            x, y = pos[aid]
            for cx in (int(x // radius) - 1, int(x // radius), int(x // radius) + 1):
                for cy in (int(y // radius) - 1, int(y // radius), int(y // radius) + 1):
                    for other in cells.get((cx, cy), ()):
                        ox, oy = pos[other]
                        if other != aid and math.hypot(ox - x, oy - y) <= radius:
                            nt = t + step
                            if when[other][0] is None or nt < when[other][0] - 1e-9:
                                when[other] = (nt, "NEIGHBOUR")
                                heapq.heappush(heap, (nt, other))
    never = cfg.time_limit_seconds + 1.0
    for a in inside:
        t, ch = when[a.agent_id]
        a.alert_channel = ch
        if t is None:
            a.alert_time = None
            a.departure_time = never                    # nobody told them: they stay where they are
        else:
            a.alert_time = round(t, 3)
            a.departure_time = round(t + delays[a.agent_id], 3)


def generate_population(network: RoadNetwork, cfg: ScenarioConfig) -> List[Agent]:
    """Deterministic for a given (network, config, seed): ``random.Random(seed)``.

    Routing strategy does NOT influence the population, which is what makes
    paired strategy comparisons fair.
    """
    if not cfg.origins:
        raise ValueError("scenario has no origins")
    rng = random.Random(cfg.random_seed)
    weights = _origin_weights(cfg)
    spread = cfg.params.spread_radius_m
    nearby = {}
    if spread > 0:
        for o in cfg.origins:
            c = network.nodes[o.node_id]
            nearby[o.node_id] = sorted(
                n.node_id for n in network.nodes.values()
                if geo.haversine_m(c.lat, c.lon, n.lat, n.lon) <= spread)
    agents = []
    delays = {}                                # reaction delay of each person (kept apart from the alert time)
    for i in range(cfg.agent_count):
        origin = rng.choices(cfg.origins, weights=weights, k=1)[0]
        node = rng.choice(nearby[origin.node_id]) if spread > 0 else origin.node_id
        delay = rng.uniform(0.0, cfg.params.departure_delay_max_seconds)
        alert = alert_time_for(network, node, cfg.params.hazards)
        agents.append(Agent(i + 1, node, round((alert or 0.0) + delay, 3), alert))
        delays[i + 1] = delay
    apply_alert_channels(network, agents, cfg, delays)
    return agents
