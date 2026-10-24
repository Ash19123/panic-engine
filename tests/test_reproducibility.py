from simulation.agent import generate_population
from simulation.engine import EvacuationSimulation
from tests.helpers import diamond, make_cfg


def run(strategy, seed):
    net = diamond(cap_short=6, cap_long=6)
    cfg = make_cfg(net, [1], [(10, 4, 200, 4)], strategy=strategy, agents=60, seed=seed,
                   departure_delay_max_seconds=60)
    return EvacuationSimulation(net, cfg).run()


def test_same_seed_same_result_all_strategies():
    for s in ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED"):
        assert run(s, 5).fingerprint() == run(s, 5).fingerprint()


def test_different_seed_changes_population():
    net = diamond()
    a = generate_population(net, make_cfg(net, [1], [(10, 4, 9, 9)], seed=1, departure_delay_max_seconds=60))
    b = generate_population(net, make_cfg(net, [1], [(10, 4, 9, 9)], seed=2, departure_delay_max_seconds=60))
    assert [x.departure_time for x in a] != [x.departure_time for x in b]


def test_population_independent_of_strategy():
    net = diamond()
    pops = [[(a.origin_node, a.departure_time) for a in generate_population(
        net, make_cfg(net, [1, 2], [(10, 4, 9, 9)], strategy=s, seed=3, departure_delay_max_seconds=60))]
        for s in ("SHORTEST_PATH", "CONGESTION_AWARE")]
    assert pops[0] == pops[1]


def test_database_reproducibility_check(seeded):
    from simulation.experiments import check_reproducibility
    conn, sid = seeded
    assert check_reproducibility(conn, sid, "CONGESTION_AWARE")
