"""Movement model: where an agent is at time t.

Agents never teleport: an agent on an edge is interpolated along that edge's
polyline between its entry and arrival times; otherwise it sits on a node.
"""
from __future__ import annotations  # modern type hints on older Python

from typing import Tuple  # type hint for (lat, lon)

from geo import geographic_utils as geo  # distance and polyline helpers
from simulation.agent import Agent  # the agent class
from simulation.models import AgentState, RoadNetwork  # states and the road graph


def agent_position(network: RoadNetwork, agent: Agent, t: float, dest_node: int = None) -> Tuple[float, float]:  # latitude and longitude at time t
    if agent.edge is not None and agent.state == AgentState.MOVING:  # the agent is walking along a road
        span = max(1e-9, agent.edge_arrival_time - agent.edge_enter_time)  # time the road takes (never zero)
        frac = (t - agent.edge_enter_time) / span  # how far along the road, 0 to 1
        return geo.interpolate_polyline(network.edge_polyline(agent.edge), frac)  # point at that fraction of the road's shape
    node = network.nodes[agent.node if agent.node is not None else agent.origin_node]  # otherwise the agent stands on a node
    return node.lat, node.lon  # that node's coordinates
