from simulation.congestion import CongestionState
from simulation.engine import EvacuationSimulation
from simulation.models import ModelParams
from simulation.routing import Router
from tests.helpers import diamond, edge_between, make_cfg, make_net


def router(net, strategy):
    p = ModelParams()
    st = CongestionState(net, p)
    return Router(net, st, strategy, p), st


def test_shortest_path_prefers_short_branch():
    net = diamond()
    r, _ = router(net, "SHORTEST_PATH")
    cost, route = r.route_from(1, 4, 0)
    assert [net.edges[e].target for e in route] == [2, 4]
    assert abs(cost - 200 / 1.4) < 1e-9


def test_shortest_path_ignores_congestion_but_congestion_aware_does_not():
    net = diamond(cap_short=10)
    sp, st_sp = router(net, "SHORTEST_PATH")
    ca, st_ca = router(net, "CONGESTION_AWARE")
    e12 = edge_between(net, 1, 2)
    for st in (st_sp, st_ca):
        st.occupancy[e12] = 10                       # edge 1->2 is saturated
        st.queue[e12] = 30
    sp.invalidate()
    ca.invalidate()
    assert [net.edges[e].target for e in sp.route_from(1, 4, 0)[1]] == [2, 4]
    assert [net.edges[e].target for e in ca.route_from(1, 4, 0)[1]] == [3, 4]


def test_congestion_costs_are_positive_and_at_least_free_flow():
    net = diamond()
    _, st = router(net, "CONGESTION_AWARE")
    for eid, e in net.edges.items():
        st.occupancy[eid] = st.capacity[eid] // 2
        assert st.congestion_cost(eid) >= e.free_flow_time_s > 0


def test_closure_changes_route_and_unreachable_is_none():
    net = diamond()
    r, st = router(net, "SHORTEST_PATH")
    st.closed.add(edge_between(net, 1, 2))
    r.invalidate()
    assert [net.edges[e].target for e in r.route_from(1, 4, 0)[1]] == [3, 4]
    st.closed.add(edge_between(net, 1, 3))
    r.invalidate()
    assert r.route_from(1, 4, 0) is None


def test_parallel_edges_choose_cheaper_open_one():
    net = make_net([(1, 2, 100, 10), (1, 2, 200, 10)])
    r, st = router(net, "SHORTEST_PATH")
    short, long_ = net.parallel_edges(1, 2)
    assert r.route_from(1, 2, 0)[1] == [short]
    st.closed.add(short)
    r.invalidate()
    assert r.route_from(1, 2, 0)[1] == [long_]


def test_unreachable_destination_fails_agents_without_crashing():
    net = make_net([(1, 2, 100, 10), (3, 4, 100, 10)])      # two disconnected components
    cfg = make_cfg(net, [1], [(10, 4, 100, 5)], agents=3)
    res = EvacuationSimulation(net, cfg).run()
    assert res.metrics["failed"] == 3 and res.metrics["evacuated"] == 0
    assert res.metrics["unresolved"] == 0
