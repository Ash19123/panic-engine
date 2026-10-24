"""Time-indexed snapshot recording (occupancy, positions, population counts)."""
from __future__ import annotations

from collections import Counter
from typing import List, Sequence, Tuple

from simulation.agent import Agent
from simulation.congestion import CongestionState
from simulation.models import AgentState, ModelParams, RoadNetwork
from simulation.movement import agent_position


class SnapshotRecorder:
    def __init__(self, network: RoadNetwork, params: ModelParams, dt: float):
        self.network = network
        self.params = params
        self.interval = max(dt, round(params.snapshot_interval_seconds / dt) * dt)
        self.next_time = 0.0
        self.occupancy_rows: List[Tuple] = []   # (t, edge_id, occupancy, queue)
        self.position_rows: List[Tuple] = []    # (t, agent_id, state, edge_id, lat, lon)
        self.frame_rows: List[Tuple] = []       # (t, waiting, moving, queued, evacuated, failed)
        self.times: List[float] = []

    def due(self, t: float) -> bool:
        return t + 1e-9 >= self.next_time

    def record(self, t: float, state: CongestionState, agents: Sequence[Agent]) -> None:
        self.times.append(t)
        for eid, occ in state.occupancy.items():
            q = state.queue[eid]
            if occ or q:
                self.occupancy_rows.append((t, eid, occ, q))
        c = Counter(a.state for a in agents)
        self.frame_rows.append((t, c[AgentState.WAITING], c[AgentState.MOVING], c[AgentState.QUEUED],
                                c[AgentState.EVACUATED], c[AgentState.UNABLE_TO_EVACUATE]))
        if self.params.record_position_snapshots:
            stride = max(1, -(-len(agents) // max(1, self.params.max_snapshot_agents)))
            for a in agents:
                if a.agent_id % stride:
                    continue
                lat, lon = agent_position(self.network, a, t)
                self.position_rows.append((t, a.agent_id, a.state.value, a.edge if a.state == AgentState.MOVING else None,
                                           lat, lon))
        while self.next_time <= t + 1e-9:
            self.next_time += self.interval
