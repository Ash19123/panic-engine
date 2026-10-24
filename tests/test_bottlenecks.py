from simulation.engine import EvacuationSimulation
from tests.helpers import make_cfg, make_net


def test_narrow_edge_is_flagged_wide_busy_edge_is_not():
    # everyone uses every edge, but only the narrow one forms a queue
    net = make_net([(1, 2, 100, 500), (2, 3, 100, 3), (3, 4, 100, 500)])
    cfg = make_cfg(net, [1], [(10, 4, 500, 500)], agents=80, limit=6000)
    sim = EvacuationSimulation(net, cfg)
    res = sim.run()
    narrow = net.parallel_edges(2, 3)[0]
    flagged = {b.edge_id: b for b in res.bottlenecks}
    assert narrow in flagged and flagged[narrow].severity in ("MODERATE", "SEVERE")
    wide = [net.parallel_edges(1, 2)[0], net.parallel_edges(3, 4)[0]]
    assert all(w not in flagged for w in wide)
    assert all(sim.state.traversals[w] == 80 for w in wide)        # heavy traffic, still not a bottleneck
    assert flagged[narrow].contributing_metrics["queue_seconds"] >= 30


def test_no_queue_no_bottleneck():
    net = make_net([(1, 2, 100, 500)])
    res = EvacuationSimulation(net, make_cfg(net, [1], [(10, 2, 500, 500)], agents=10)).run()
    assert res.bottlenecks == []
