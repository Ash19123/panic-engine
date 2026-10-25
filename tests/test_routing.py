from simulation.congestion import CongestionState  # live road state
from simulation.engine import EvacuationSimulation  # the engine
from simulation.models import ModelParams  # default parameters
from simulation.routing import Router  # route finder
from tests.helpers import diamond, edge_between, make_cfg, make_net  # test network and scenario builders


def router(net, strategy):  # build a router and its state
    p = ModelParams()  # default parameters
    st = CongestionState(net, p)  # empty roads
    return Router(net, st, strategy, p), st  # the router and its state


def test_shortest_path_prefers_short_branch():  # on an empty network the short branch wins
    net = diamond()  # short and long branch
    r, _ = router(net, "SHORTEST_PATH")  # shortest-path router
    cost, route = r.route_from(1, 4, 0)  # route from node 1 to node 4
    assert [net.edges[e].target for e in route] == [2, 4]  # via node 2, the short branch
    assert abs(cost - 200 / 1.4) < 1e-9  # cost equals the free-flow time of 200 m


def test_shortest_path_ignores_congestion_but_congestion_aware_does_not():  # only the congestion-aware router reacts to crowds
    net = diamond(cap_short=10)  # short branch holds 10 people
    sp, st_sp = router(net, "SHORTEST_PATH")  # shortest-path router
    ca, st_ca = router(net, "CONGESTION_AWARE")  # congestion-aware router
    e12 = edge_between(net, 1, 2)  # the first road of the short branch
    for st in (st_sp, st_ca):  # both states
        st.occupancy[e12] = 10                       # edge 1->2 is saturated
        st.queue[e12] = 30  # and 30 people are waiting to enter it
    sp.invalidate()  # drop cached routes
    ca.invalidate()  # drop cached routes
    assert [net.edges[e].target for e in sp.route_from(1, 4, 0)[1]] == [2, 4]  # shortest path still goes through the jam
    assert [net.edges[e].target for e in ca.route_from(1, 4, 0)[1]] == [3, 4]  # congestion-aware takes the long branch


def test_congestion_costs_are_positive_and_at_least_free_flow():  # Dijkstra needs positive costs
    net = diamond()  # test network
    _, st = router(net, "CONGESTION_AWARE")  # congestion-aware state
    for eid, e in net.edges.items():  # every road
        st.occupancy[eid] = st.capacity[eid] // 2  # half full
        assert st.congestion_cost(eid) >= e.free_flow_time_s > 0  # cost is never below the free-flow time


def test_closure_changes_route_and_unreachable_is_none():  # closed roads are avoided
    net = diamond()  # test network
    r, st = router(net, "SHORTEST_PATH")  # shortest-path router
    st.closed.add(edge_between(net, 1, 2))  # close the first road of the short branch
    r.invalidate()  # drop cached routes
    assert [net.edges[e].target for e in r.route_from(1, 4, 0)[1]] == [3, 4]  # the long branch is used
    st.closed.add(edge_between(net, 1, 3))  # close the long branch too
    r.invalidate()  # drop cached routes
    assert r.route_from(1, 4, 0) is None  # no route remains


def test_parallel_edges_choose_cheaper_open_one():  # parallel roads between two nodes
    net = make_net([(1, 2, 100, 10), (1, 2, 200, 10)])  # a short and a long road between the same nodes
    r, st = router(net, "SHORTEST_PATH")  # shortest-path router
    short, long_ = net.parallel_edges(1, 2)  # their ids
    assert r.route_from(1, 2, 0)[1] == [short]  # the short one is chosen
    st.closed.add(short)  # close it
    r.invalidate()  # drop cached routes
    assert r.route_from(1, 2, 0)[1] == [long_]  # the long one is used instead


def test_unreachable_destination_fails_agents_without_crashing():  # no route means failure, not a crash
    net = make_net([(1, 2, 100, 10), (3, 4, 100, 10)])      # two disconnected components
    cfg = make_cfg(net, [1], [(10, 4, 100, 5)], agents=3)  # the safe stop is in the other component
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert res.metrics["failed"] == 3 and res.metrics["evacuated"] == 0  # all three fail
    assert res.metrics["unresolved"] == 0  # none is left hanging
