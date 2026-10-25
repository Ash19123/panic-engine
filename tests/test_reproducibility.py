from simulation.agent import generate_population  # crowd generator
from simulation.engine import EvacuationSimulation  # the engine
from tests.helpers import diamond, make_cfg  # test network and scenario builders


def run(strategy, seed):  # one simulation run
    net = diamond(cap_short=6, cap_long=6)  # small roads, so congestion happens
    cfg = make_cfg(net, [1], [(10, 4, 200, 4)], strategy=strategy, agents=60, seed=seed,  # scenario (part 1)
                   departure_delay_max_seconds=60)  # random reaction delays (part 2)
    return EvacuationSimulation(net, cfg).run()  # run it


def test_same_seed_same_result_all_strategies():  # the same seed must give identical runs
    for s in ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED"):  # every strategy
        assert run(s, 5).fingerprint() == run(s, 5).fingerprint()  # two runs match exactly


def test_different_seed_changes_population():  # different seeds give different crowds
    net = diamond()  # test network
    a = generate_population(net, make_cfg(net, [1], [(10, 4, 9, 9)], seed=1, departure_delay_max_seconds=60))  # crowd with seed 1
    b = generate_population(net, make_cfg(net, [1], [(10, 4, 9, 9)], seed=2, departure_delay_max_seconds=60))  # crowd with seed 2
    assert [x.departure_time for x in a] != [x.departure_time for x in b]  # their departure times differ


def test_population_independent_of_strategy():  # the strategy must not change who the people are
    net = diamond()  # test network
    pops = [[(a.origin_node, a.departure_time) for a in generate_population(  # crowd for each strategy (part 1)
        net, make_cfg(net, [1, 2], [(10, 4, 9, 9)], strategy=s, seed=3, departure_delay_max_seconds=60))]  # (part 2)
        for s in ("SHORTEST_PATH", "CONGESTION_AWARE")]  # two strategies
    assert pops[0] == pops[1]  # identical crowds


def test_database_reproducibility_check(seeded):  # the stored-scenario check
    from simulation.experiments import check_reproducibility  # function under test
    conn, sid = seeded  # database with the demo scenario
    assert check_reproducibility(conn, sid, "CONGESTION_AWARE")  # two runs agree
