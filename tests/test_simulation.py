import pytest

from simulation.agent import Agent, IllegalTransition
from simulation.engine import EvacuationSimulation
from simulation.models import AgentState
from tests.helpers import diamond, edge_between, make_cfg, make_net


def test_state_machine_rules():
    a = Agent(1, 1, 0)
    a.transition(AgentState.MOVING)
    a.transition(AgentState.MOVING)              # self transition allowed
    with pytest.raises(IllegalTransition):
        a.transition(AgentState.EVACUATED)       # must pass through QUEUED
    a.transition(AgentState.QUEUED)
    a.transition(AgentState.EVACUATED)
    with pytest.raises(IllegalTransition):
        a.transition(AgentState.MOVING)          # terminal


def test_everyone_evacuates_on_open_network():
    net = diamond()
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=30)
    res = EvacuationSimulation(net, cfg).run()
    m = res.metrics
    assert m["evacuated"] == 30 and m["completion_rate"] == 1.0
    assert all(a.evacuation_time is not None and a.evacuation_time >= a.departure_time for a in res.agents)
    assert min(a.evacuation_time for a in res.agents) >= 200 / 1.4 - 1e-6   # nobody beats free-flow time


def test_agents_follow_connected_routes_no_teleporting():
    net = diamond()
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=5)
    res = EvacuationSimulation(net, cfg).run()
    per_agent = {}
    for aid, t, kind, node, edge, *_ in res.events:
        if kind == "EDGE_ENTERED":
            per_agent.setdefault(aid, []).append(edge)
    assert per_agent
    for edges in per_agent.values():
        for e1, e2 in zip(edges, edges[1:]):
            assert net.edges[e1].target == net.edges[e2].source
        assert net.edges[edges[0]].source == 1 and net.edges[edges[-1]].target == 4


def test_road_closure_blocks_entry_and_agents_reroute():
    net = diamond()
    e12 = edge_between(net, 1, 2)
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=10, closures=[(e12, 0.0)])
    res = EvacuationSimulation(net, cfg).run()
    assert res.metrics["evacuated"] == 10
    assert not any(ev[2] == "EDGE_ENTERED" and ev[4] == e12 for ev in res.events)
    assert any(ev[2] == "ROAD_BLOCKED" and ev[4] == e12 for ev in res.events)


def test_midrun_closure_in_progress_agents_finish_then_nobody_enters():
    net = make_net([(1, 2, 100, 50), (2, 3, 100, 50), (1, 4, 300, 50), (4, 3, 300, 50)])
    e12 = edge_between(net, 1, 2)
    cfg = make_cfg(net, [1], [(10, 3, 100, 100)], agents=10, closures=[(e12, 20.0)], departure_delay_max_seconds=60)
    res = EvacuationSimulation(net, cfg).run()
    assert res.metrics["evacuated"] == 10
    t_block = [ev[1] for ev in res.events if ev[2] == "ROAD_BLOCKED"][0]
    assert all(not (ev[2] == "EDGE_ENTERED" and ev[4] == e12 and ev[1] >= t_block) for ev in res.events)
    assert any(ev[2] == "EDGE_ENTERED" and ev[4] == e12 for ev in res.events)    # early agents did use it


def test_closing_only_route_makes_agents_fail_not_walk_through():
    net = make_net([(1, 2, 100, 50), (2, 3, 100, 50)])
    e12 = edge_between(net, 1, 2)
    cfg = make_cfg(net, [1], [(10, 3, 100, 100)], agents=4, closures=[(e12, 0.0)])
    res = EvacuationSimulation(net, cfg).run()
    assert res.metrics["failed"] == 4 and res.metrics["evacuated"] == 0


def test_time_limit_leaves_unresolved_agents_counted():
    net = diamond()
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=10, limit=60.0)
    res = EvacuationSimulation(net, cfg).run()
    m = res.metrics
    assert m["unresolved"] == 10 and m["evacuated"] == 0 and m["completion_rate"] == 0
    assert m["evacuated"] + m["unresolved"] + m["failed"] == m["total_agents"]
    assert sum(1 for ev in res.events if ev[2] == "AGENT_UNRESOLVED") == 10
    assert res.total_simulated_seconds == 60.0


def test_origin_equals_destination_evacuates_immediately():
    net = diamond()
    cfg = make_cfg(net, [4], [(10, 4, 100, 100)], agents=3)
    res = EvacuationSimulation(net, cfg).run()
    assert res.metrics["evacuated"] == 3


def test_load_balanced_uses_multiple_destinations():
    net = make_net([(1, 2, 100, 100), (1, 3, 100, 100)])
    cfg = make_cfg(net, [1], [(10, 2, 50, 50), (11, 3, 50, 50)], strategy="LOAD_BALANCED", agents=40)
    res = EvacuationSimulation(net, cfg).run()
    assert {a.destination_id for a in res.agents} == {10, 11} and res.metrics["evacuated"] == 40
