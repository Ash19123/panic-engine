import pytest  # the test framework

from simulation.agent import Agent, IllegalTransition  # agent and its error
from simulation.engine import EvacuationSimulation  # the engine
from simulation.models import AgentState  # agent states
from tests.helpers import diamond, edge_between, make_cfg, make_net  # test network and scenario builders


def test_state_machine_rules():  # allowed and forbidden state changes
    a = Agent(1, 1, 0)  # a new agent
    a.transition(AgentState.MOVING)  # WAITING -> MOVING is allowed
    a.transition(AgentState.MOVING)              # self transition allowed
    with pytest.raises(IllegalTransition):  # this must be refused
        a.transition(AgentState.EVACUATED)       # must pass through QUEUED
    a.transition(AgentState.QUEUED)  # MOVING -> QUEUED is allowed
    a.transition(AgentState.EVACUATED)  # QUEUED -> EVACUATED is allowed
    with pytest.raises(IllegalTransition):  # this must be refused
        a.transition(AgentState.MOVING)          # terminal


def test_everyone_evacuates_on_open_network():  # the simplest happy path
    net = diamond()  # test network
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=30)  # 30 people, a roomy safe stop
    res = EvacuationSimulation(net, cfg).run()  # run it
    m = res.metrics  # summary numbers
    assert m["evacuated"] == 30 and m["completion_rate"] == 1.0  # everyone made it
    assert all(a.evacuation_time is not None and a.evacuation_time >= a.departure_time for a in res.agents)  # times make sense
    assert min(a.evacuation_time for a in res.agents) >= 200 / 1.4 - 1e-6   # nobody beats free-flow time


def test_agents_follow_connected_routes_no_teleporting():  # people only walk along connected roads
    net = diamond()  # test network
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=5)  # five people
    res = EvacuationSimulation(net, cfg).run()  # run it
    per_agent = {}  # roads entered by each agent, in order
    for aid, t, kind, node, edge, *_ in res.events:  # every event
        if kind == "EDGE_ENTERED":  # a road entry
            per_agent.setdefault(aid, []).append(edge)  # remember it
    assert per_agent  # something was recorded
    for edges in per_agent.values():  # each agent's roads
        for e1, e2 in zip(edges, edges[1:]):  # each consecutive pair
            assert net.edges[e1].target == net.edges[e2].source  # the second starts where the first ends
        assert net.edges[edges[0]].source == 1 and net.edges[edges[-1]].target == 4  # from the origin to the safe stop


def test_road_closure_blocks_entry_and_agents_reroute():  # a closed road is never entered
    net = diamond()  # test network
    e12 = edge_between(net, 1, 2)  # the first road of the short branch
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=10, closures=[(e12, 0.0)])  # closed from the start
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert res.metrics["evacuated"] == 10  # everyone uses the other branch
    assert not any(ev[2] == "EDGE_ENTERED" and ev[4] == e12 for ev in res.events)  # nobody entered the closed road
    assert any(ev[2] == "ROAD_BLOCKED" and ev[4] == e12 for ev in res.events)  # the closure was logged


def test_midrun_closure_in_progress_agents_finish_then_nobody_enters():  # the in-progress edge policy
    net = make_net([(1, 2, 100, 50), (2, 3, 100, 50), (1, 4, 300, 50), (4, 3, 300, 50)])  # a short and a long route
    e12 = edge_between(net, 1, 2)  # the first road of the short route
    cfg = make_cfg(net, [1], [(10, 3, 100, 100)], agents=10, closures=[(e12, 20.0)], departure_delay_max_seconds=60)  # closes at 20 s
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert res.metrics["evacuated"] == 10  # everyone still evacuates
    t_block = [ev[1] for ev in res.events if ev[2] == "ROAD_BLOCKED"][0]  # when the closure took effect
    assert all(not (ev[2] == "EDGE_ENTERED" and ev[4] == e12 and ev[1] >= t_block) for ev in res.events)  # nobody entered after it
    assert any(ev[2] == "EDGE_ENTERED" and ev[4] == e12 for ev in res.events)    # early agents did use it


def test_closing_only_route_makes_agents_fail_not_walk_through():  # no alternative route
    net = make_net([(1, 2, 100, 50), (2, 3, 100, 50)])  # a single route
    e12 = edge_between(net, 1, 2)  # its first road
    cfg = make_cfg(net, [1], [(10, 3, 100, 100)], agents=4, closures=[(e12, 0.0)])  # closed from the start
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert res.metrics["failed"] == 4 and res.metrics["evacuated"] == 0  # all four fail


def test_time_limit_leaves_unresolved_agents_counted():  # a run that ends early
    net = diamond()  # test network
    cfg = make_cfg(net, [1], [(10, 4, 100, 100)], agents=10, limit=60.0)  # only 60 s allowed
    res = EvacuationSimulation(net, cfg).run()  # run it
    m = res.metrics  # summary numbers
    assert m["unresolved"] == 10 and m["evacuated"] == 0 and m["completion_rate"] == 0  # nobody finished
    assert m["evacuated"] + m["unresolved"] + m["failed"] == m["total_agents"]  # nobody is dropped
    assert sum(1 for ev in res.events if ev[2] == "AGENT_UNRESOLVED") == 10  # one event per unresolved agent
    assert res.total_simulated_seconds == 60.0  # the run lasted exactly the limit


def test_origin_equals_destination_evacuates_immediately():  # people who start at the safe stop
    net = diamond()  # test network
    cfg = make_cfg(net, [4], [(10, 4, 100, 100)], agents=3)  # origin = destination
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert res.metrics["evacuated"] == 3  # all three are evacuated


def test_load_balanced_uses_multiple_destinations():  # the central planner spreads people
    net = make_net([(1, 2, 100, 100), (1, 3, 100, 100)])  # two equal roads to two stops
    cfg = make_cfg(net, [1], [(10, 2, 50, 50), (11, 3, 50, 50)], strategy="LOAD_BALANCED", agents=40)  # 40 people
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert {a.destination_id for a in res.agents} == {10, 11} and res.metrics["evacuated"] == 40  # both stops are used
