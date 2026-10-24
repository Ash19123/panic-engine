"""Movement model: where an agent is at time t.

Agents never teleport: an agent on an edge is interpolated along that edge's
polyline between its entry and arrival times; otherwise it sits on a node.
"""
from __future__ import annotations

from typing import Tuple

from geo import geographic_utils as geo
from simulation.agent import Agent
from simulation.models import AgentState, RoadNetwork


def agent_position(network: RoadNetwork, agent: Agent, t: float, dest_node: int = None) -> Tuple[float, float]:
    if agent.edge is not None and agent.state == AgentState.MOVING:
        span = max(1e-9, agent.edge_arrival_time - agent.edge_enter_time)
        frac = (t - agent.edge_enter_time) / span
        return geo.interpolate_polyline(network.edge_polyline(agent.edge), frac)
    node = network.nodes[agent.node if agent.node is not None else agent.origin_node]
    return node.lat, node.lon
