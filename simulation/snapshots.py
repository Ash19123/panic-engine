"""Time-indexed snapshot recording (occupancy, positions, population counts)."""
from __future__ import annotations  # modern type hints on older Python

from collections import Counter  # count agents per state
from typing import List, Sequence, Tuple  # type hints

from simulation.agent import Agent  # the agent class
from simulation.congestion import CongestionState  # per-road occupancy and queues
from simulation.models import AgentState, ModelParams, RoadNetwork  # states, parameters, road graph
from simulation.movement import agent_position  # where an agent is at a given time


class SnapshotRecorder:  # collects frames of the run for replay and analysis
    def __init__(self, network: RoadNetwork, params: ModelParams, dt: float):  # set up the recorder
        self.network = network  # road graph (needed for positions)
        self.params = params  # model parameters (snapshot interval, limits)
        self.interval = max(dt, round(params.snapshot_interval_seconds / dt) * dt)  # snapshot spacing, a multiple of the time step
        self.next_time = 0.0  # time of the next snapshot
        self.occupancy_rows: List[Tuple] = []   # (t, edge_id, occupancy, queue)
        self.position_rows: List[Tuple] = []    # (t, agent_id, state, edge_id, lat, lon)
        self.frame_rows: List[Tuple] = []       # (t, waiting, moving, queued, evacuated, failed)
        self.times: List[float] = []  # the times at which snapshots were taken

    def due(self, t: float) -> bool:  # is it time for a snapshot?
        return t + 1e-9 >= self.next_time  # true once the clock reaches the next snapshot time (with float tolerance)

    def record(self, t: float, state: CongestionState, agents: Sequence[Agent]) -> None:  # take one snapshot
        self.times.append(t)  # remember the time
        for eid, occ in state.occupancy.items():  # every road
            q = state.queue[eid]  # agents waiting to enter this road
            if occ or q:  # store only roads that have someone on or waiting for them
                self.occupancy_rows.append((t, eid, occ, q))  # sparse occupancy row
        c = Counter(a.state for a in agents)  # number of agents in each state
        self.frame_rows.append((t, c[AgentState.WAITING], c[AgentState.MOVING], c[AgentState.QUEUED],  # one counts row
                                c[AgentState.EVACUATED], c[AgentState.UNABLE_TO_EVACUATE]))  # evacuated and failed counts
        if self.params.record_position_snapshots:  # positions are optional (they are large)
            stride = max(1, -(-len(agents) // max(1, self.params.max_snapshot_agents)))  # sample every n-th agent to cap the size
            for a in agents:  # every agent
                if a.agent_id % stride:  # not part of the sample
                    continue  # skip it
                lat, lon = agent_position(self.network, a, t)  # interpolated position at time t
                self.position_rows.append((t, a.agent_id, a.state.value, a.edge if a.state == AgentState.MOVING else None,  # row start: time, id, state, road
                                           lat, lon))  # coordinates
        while self.next_time <= t + 1e-9:  # advance the schedule past the current time
            self.next_time += self.interval  # next snapshot time
