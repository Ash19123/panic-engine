"""Agents, their finite-state machine and reproducible population generation."""
from __future__ import annotations  # modern type hints on older Python

import heapq  # priority queue for the neighbour relay
import math  # trigonometry and infinity
import random  # seeded random numbers
from typing import List, Optional, Tuple  # type hints

from geo import geographic_utils as geo  # distance helpers
from simulation.models import (ALLOWED_TRANSITIONS, TERMINAL_STATES, AgentState, RoadNetwork, ScenarioConfig)  # states, rules, road graph, scenario


class IllegalTransition(RuntimeError):  # raised when an agent tries a state change that is not allowed
    pass  # no extra behaviour needed


class Agent:  # one simulated person
    __slots__ = ("agent_id", "origin_node", "departure_time", "state", "node", "edge", "edge_enter_time",  # fixed attribute list saves memory (part 1)
                 "edge_arrival_time", "route", "destination_id", "evacuation_time", "wait_seconds",  # (part 2)
                 "queued_since", "last_plan_time", "route_changes", "needs_replan", "decisions", "alert_time", "alert_channel")  # (part 3)

    def __init__(self, agent_id: int, origin_node: int, departure_time: float, alert_time: Optional[float] = None,
                 alert_channel: Optional[str] = None):  # create an agent that has not started yet
        self.agent_id = agent_id  # unique number
        self.origin_node = origin_node  # where the agent starts
        self.departure_time = departure_time  # when the agent leaves (alert time + reaction delay)
        self.alert_channel = alert_channel             # PHONE / SIREN / NEIGHBOUR / NONE (None = not in an alert area)
        self.alert_time = alert_time                   # when a hazard alert reaches this person's phone (None = no alert)
        self.state = AgentState.WAITING  # everybody starts waiting
        self.node: Optional[int] = origin_node        # None while travelling on an edge
        self.edge: Optional[int] = None  # the road being walked, if any
        self.edge_enter_time = 0.0  # when the current road was entered
        self.edge_arrival_time = 0.0  # when the current road will be finished
        self.route: List[int] = []                     # remaining edge ids
        self.destination_id: Optional[int] = None  # the chosen safe stop
        self.evacuation_time: Optional[float] = None   # seconds since the alert (t = 0)
        self.wait_seconds = 0.0                        # time spent QUEUED
        self.queued_since: Optional[float] = None  # when the current wait began
        self.last_plan_time = -math.inf  # when the route was last planned (never)
        self.route_changes = 0  # how many times the route changed
        self.needs_replan = False  # set when a road ahead closes
        self.decisions: List[Tuple[float, str, Optional[int], int]] = []   # (t, kind, destination, route length)

    @property
    def is_terminal(self) -> bool:  # has the agent finished (evacuated or failed)?
        return self.state in TERMINAL_STATES  # true for EVACUATED and UNABLE_TO_EVACUATE

    def transition(self, new_state: AgentState) -> None:  # change state, enforcing the rules
        if new_state == self.state and not self.is_terminal:  # staying in the same live state is fine
            return  # nothing to do
        if new_state not in ALLOWED_TRANSITIONS[self.state]:  # not an allowed move
            raise IllegalTransition(f"agent {self.agent_id}: {self.state.value} -> {new_state.value}")  # fail loudly
        self.state = new_state  # apply the change

    def __repr__(self) -> str:  # readable text for debugging
        return f"Agent({self.agent_id}, {self.state.value}, node={self.node}, edge={self.edge})"  # id, state, position


def _origin_weights(cfg: ScenarioConfig) -> List[float]:  # how likely each origin is to be chosen
    n = len(cfg.origins)  # number of origins
    mode = cfg.params.crowd_distribution  # distribution setting
    if mode == "even":  # same share everywhere
        return [1.0] * n  # equal weights
    if mode == "concentrated":  # most people at the first origin
        # 70 % of the crowd at the first origin, the rest shared evenly.
        if n == 1:  # only one origin
            return [1.0]  # all weight on it
        return [0.7] + [0.3 / (n - 1)] * (n - 1)  # 70% first, 30% split over the rest
    return [o.weight for o in cfg.origins]  # otherwise use each origin's own weight


def alert_time_for(network: RoadNetwork, node_id: int, hazards) -> Optional[float]:  # when would this place be alerted?
    """Earliest alert time among the hazards whose alert radius contains this node (None if none does)."""
    n = network.nodes[node_id]  # the place
    times = [h["alert_time_s"] for h in hazards  # alert times of every hazard ...
             if geo.haversine_m(n.lat, n.lon, h["lat"], h["lon"]) <= h["alert_radius_m"]]  # ... whose alert area contains the place
    return min(times) if times else None  # the earliest, or None if outside every alert area


def apply_alert_channels(network: RoadNetwork, agents: List[Agent], cfg: ScenarioConfig, delays) -> None:  # decide how each person learns of the hazard
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
    channels = cfg.params.alert_channels  # channel settings from the scenario
    inside = [a for a in agents if a.alert_time is not None]  # people inside some alert area
    if not channels:  # no channels configured: the original behaviour
        for a in inside:  # everybody inside an alert area
            a.alert_channel = "PHONE"  # is reached by phone
        return  # nothing more to decide
    rng = random.Random(cfg.random_seed * 7919 + 17)  # separate random stream so other draws stay unchanged
    reach = float(channels.get("phone_reach", 1.0))  # share of people with a phone at hand
    dmax = float(channels.get("phone_delay_max_s", 15.0))  # longest phone delivery delay
    sirens = channels.get("sirens", [])  # list of sirens
    when = {}                                              # agent id -> (alert time, channel)
    for a in sorted(inside, key=lambda x: x.agent_id):  # fixed order keeps the random stream stable
        base = a.alert_time  # the hazard's alert time for this person
        has_phone, jitter = rng.random() < reach, rng.uniform(0.0, dmax)      # always drawn: stable stream
        best = (base + jitter, "PHONE") if has_phone else (None, "NONE")  # phone alert if they have a phone, otherwise nothing yet
        nd = network.nodes[a.origin_node]  # where the person is
        for s in sirens:  # each siren
            if geo.haversine_m(nd.lat, nd.lon, s["lat"], s["lon"]) <= s["radius_m"]:  # person is within hearing range
                t = base + float(s.get("delay_s", 10.0))  # siren sounds after its delay
                if best[0] is None or t < best[0]:  # earlier than anything so far
                    best = (t, "SIREN")  # the siren wins
        when[a.agent_id] = best  # remember the earliest warning for this person
    radius = float(channels.get("neighbour_radius_m", 0.0))  # how close a neighbour must be to pass the word
    if radius > 0:  # neighbour relay is switched on
        step = float(channels.get("neighbour_delay_s", 30.0))  # delay for each hop of the relay
        lat0 = sum(network.nodes[a.origin_node].lat for a in inside) / len(inside)  # mean latitude for metre conversion
        kx, ky = math.cos(math.radians(lat0)) * 111_320.0, 110_540.0  # metres per degree of longitude and latitude
        pos = {a.agent_id: (network.nodes[a.origin_node].lon * kx, network.nodes[a.origin_node].lat * ky) for a in inside}  # positions in metres
        cells = {}  # grid of buckets so neighbours can be found quickly
        for aid, (x, y) in pos.items():  # every person
            cells.setdefault((int(x // radius), int(y // radius)), []).append(aid)  # file them in their grid cell
        heap = [(t, aid) for aid, (t, _) in when.items() if t is not None]  # people already warned, ordered by warning time
        heapq.heapify(heap)  # turn the list into a priority queue
        done = set()  # people whose neighbours have been processed
        while heap:  # Dijkstra over people
            t, aid = heapq.heappop(heap)  # earliest warned person not yet processed
            if aid in done or t > when[aid][0] + 1e-9:  # already handled, or a stale (later) entry
                continue  # skip it
            done.add(aid)  # mark as handled
            x, y = pos[aid]  # their position
            for cx in (int(x // radius) - 1, int(x // radius), int(x // radius) + 1):  # the 3 x 3 block of grid cells around them (columns)
                for cy in (int(y // radius) - 1, int(y // radius), int(y // radius) + 1):  # (rows)
                    for other in cells.get((cx, cy), ()):  # people in that cell
                        ox, oy = pos[other]  # their position
                        if other != aid and math.hypot(ox - x, oy - y) <= radius:  # a different person within passing distance
                            nt = t + step  # they would be told one hop later
                            if when[other][0] is None or nt < when[other][0] - 1e-9:  # that is earlier than their current warning
                                when[other] = (nt, "NEIGHBOUR")  # warned by a neighbour
                                heapq.heappush(heap, (nt, other))  # and they can pass the word on
    never = cfg.time_limit_seconds + 1.0  # a departure time after the run ends
    for a in inside:  # write the decisions back to the agents
        t, ch = when[a.agent_id]  # alert time and channel
        a.alert_channel = ch  # remember the channel
        if t is None:  # nobody reached this person
            a.alert_time = None  # never alerted
            a.departure_time = never                    # nobody told them: they stay where they are
        else:  # alerted
            a.alert_time = round(t, 3)  # exact alert time
            a.departure_time = round(t + delays[a.agent_id], 3)  # leave after their reaction delay


def generate_population(network: RoadNetwork, cfg: ScenarioConfig) -> List[Agent]:  # create the crowd
    """Deterministic for a given (network, config, seed): ``random.Random(seed)``.

    Routing strategy does NOT influence the population, which is what makes
    paired strategy comparisons fair.
    """
    if not cfg.origins:  # no starting places
        raise ValueError("scenario has no origins")  # cannot build a crowd
    rng = random.Random(cfg.random_seed)  # seeded random generator: same seed, same crowd
    weights = _origin_weights(cfg)  # how likely each origin is
    spread = cfg.params.spread_radius_m  # people start within this distance of an origin
    nearby = {}  # origin node -> nodes within the spread radius
    if spread > 0:  # spreading is switched on
        for o in cfg.origins:  # each origin
            c = network.nodes[o.node_id]  # its node
            nearby[o.node_id] = sorted(  # sorted for reproducibility
                n.node_id for n in network.nodes.values()  # every node ...
                if geo.haversine_m(c.lat, c.lon, n.lat, n.lon) <= spread)  # ... close enough to the origin
    agents = []  # the crowd
    delays = {}                                # reaction delay of each person (kept apart from the alert time)
    for i in range(cfg.agent_count):  # one iteration per person
        origin = rng.choices(cfg.origins, weights=weights, k=1)[0]  # pick an origin by weight
        node = rng.choice(nearby[origin.node_id]) if spread > 0 else origin.node_id  # exact start node
        delay = rng.uniform(0.0, cfg.params.departure_delay_max_seconds)  # random reaction delay
        alert = alert_time_for(network, node, cfg.params.hazards)  # when this place is alerted, if ever
        agents.append(Agent(i + 1, node, round((alert or 0.0) + delay, 3), alert))  # create the agent (ids start at 1)
        delays[i + 1] = delay  # remember the delay
    apply_alert_channels(network, agents, cfg, delays)  # decide how each person is warned
    return agents  # the finished crowd
