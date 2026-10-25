from collections import Counter  # count events per time

from simulation.engine import EvacuationSimulation  # the engine
from tests.helpers import make_cfg, make_net  # test network and scenario builders


def test_edge_capacity_never_exceeded_and_queue_forms():  # roads respect their capacity
    net = make_net([(1, 2, 100, 3)])             # tiny capacity
    cfg = make_cfg(net, [1], [(10, 2, 100, 100)], agents=20)  # 20 people, a roomy safe stop
    sim = EvacuationSimulation(net, cfg)  # the engine
    res = sim.run()  # run it
    assert max(sim.state.peak_occupancy.values()) <= 3  # never more than 3 people on the road
    assert all(row[2] <= 3 for row in res.occupancy_rows)  # also true in every stored snapshot
    assert res.metrics["evacuated"] == 20  # everyone still gets out
    assert res.metrics["total_waiting_time"] > 0          # agents really waited


def test_destination_throughput_limits_admissions_per_step():  # safe stops admit a limited number per step
    net = make_net([(1, 2, 100, 100)])  # one wide road
    cfg = make_cfg(net, [1], [(10, 2, 100, 2)], agents=20)  # the safe stop admits 2 per step
    res = EvacuationSimulation(net, cfg).run()  # run it
    per_step = Counter(ev[1] for ev in res.events if ev[2] == "AGENT_EVACUATED")  # admissions per time
    assert max(per_step.values()) <= 2  # never more than 2 at once
    assert res.metrics["evacuated"] == 20  # everyone is admitted eventually


def test_destination_capacity_limits_total_and_rest_fail_without_alternative():  # a full safe stop with nowhere else to go
    net = make_net([(1, 2, 100, 100)])  # one wide road
    cfg = make_cfg(net, [1], [(10, 2, 5, 10)], agents=12)  # the safe stop holds only 5
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert res.metrics["evacuated"] == 5  # five get in
    assert res.metrics["failed"] == 7  # seven cannot
    assert res.destinations[10].admitted == 5  # the stop admitted exactly its capacity


def test_full_destination_redirects_to_alternative():  # people re-route when a stop is full
    net = make_net([(1, 2, 100, 100), (1, 3, 120, 100)])  # two roads to two stops
    cfg = make_cfg(net, [1], [(10, 2, 5, 10), (11, 3, 50, 10)], agents=12)  # a small stop and a big stop
    res = EvacuationSimulation(net, cfg).run()  # run it
    assert res.metrics["evacuated"] == 12  # everyone is evacuated
    assert res.destinations[10].admitted <= 5  # the small stop respects its capacity
    assert res.destinations[11].admitted >= 7  # the rest went to the big stop
