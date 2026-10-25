from simulation.engine import EvacuationSimulation  # the engine
from tests.helpers import make_cfg, make_net  # test network and scenario builders


def test_narrow_edge_is_flagged_wide_busy_edge_is_not():  # only capacity-constrained roads are bottlenecks
    # everyone uses every edge, but only the narrow one forms a queue
    net = make_net([(1, 2, 100, 500), (2, 3, 100, 3), (3, 4, 100, 500)])  # wide, narrow, wide
    cfg = make_cfg(net, [1], [(10, 4, 500, 500)], agents=80, limit=6000)  # 80 people from one end to the other
    sim = EvacuationSimulation(net, cfg)  # the engine
    res = sim.run()  # run it
    narrow = net.parallel_edges(2, 3)[0]  # the narrow road
    flagged = {b.edge_id: b for b in res.bottlenecks}  # detected bottlenecks by road
    assert narrow in flagged and flagged[narrow].severity in ("MODERATE", "SEVERE")  # the narrow road is flagged and serious
    wide = [net.parallel_edges(1, 2)[0], net.parallel_edges(3, 4)[0]]  # the two wide roads
    assert all(w not in flagged for w in wide)  # they are not flagged
    assert all(sim.state.traversals[w] == 80 for w in wide)        # heavy traffic, still not a bottleneck
    assert flagged[narrow].contributing_metrics["queue_seconds"] >= 30  # the evidence is recorded


def test_no_queue_no_bottleneck():  # no waiting means no report
    net = make_net([(1, 2, 100, 500)])  # one wide road
    res = EvacuationSimulation(net, make_cfg(net, [1], [(10, 2, 500, 500)], agents=10)).run()  # ten people
    assert res.bottlenecks == []  # nothing flagged
