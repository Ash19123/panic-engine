from collections import Counter

from simulation.engine import EvacuationSimulation
from tests.helpers import make_cfg, make_net


def test_edge_capacity_never_exceeded_and_queue_forms():
    net = make_net([(1, 2, 100, 3)])             # tiny capacity
    cfg = make_cfg(net, [1], [(10, 2, 100, 100)], agents=20)
    sim = EvacuationSimulation(net, cfg)
    res = sim.run()
    assert max(sim.state.peak_occupancy.values()) <= 3
    assert all(row[2] <= 3 for row in res.occupancy_rows)
    assert res.metrics["evacuated"] == 20
    assert res.metrics["total_waiting_time"] > 0          # agents really waited


def test_destination_throughput_limits_admissions_per_step():
    net = make_net([(1, 2, 100, 100)])
    cfg = make_cfg(net, [1], [(10, 2, 100, 2)], agents=20)
    res = EvacuationSimulation(net, cfg).run()
    per_step = Counter(ev[1] for ev in res.events if ev[2] == "AGENT_EVACUATED")
    assert max(per_step.values()) <= 2
    assert res.metrics["evacuated"] == 20


def test_destination_capacity_limits_total_and_rest_fail_without_alternative():
    net = make_net([(1, 2, 100, 100)])
    cfg = make_cfg(net, [1], [(10, 2, 5, 10)], agents=12)
    res = EvacuationSimulation(net, cfg).run()
    assert res.metrics["evacuated"] == 5
    assert res.metrics["failed"] == 7
    assert res.destinations[10].admitted == 5


def test_full_destination_redirects_to_alternative():
    net = make_net([(1, 2, 100, 100), (1, 3, 120, 100)])
    cfg = make_cfg(net, [1], [(10, 2, 5, 10), (11, 3, 50, 10)], agents=12)
    res = EvacuationSimulation(net, cfg).run()
    assert res.metrics["evacuated"] == 12
    assert res.destinations[10].admitted <= 5
    assert res.destinations[11].admitted >= 7
