import pytest  # the test framework

from database import analytics as an  # SQL analytics helpers
from database import repositories as repo  # save and load helpers
from simulation.experiments import (apply_modifications, consistency_checks, run_counterfactual,
                                    run_paired_strategies, run_scenario)  # experiment helpers


@pytest.fixture
def runs(seeded):  # two paired runs on the demo scenario
    conn, sid = seeded  # database with the demo scenario
    r = run_paired_strategies(conn, sid, ("SHORTEST_PATH", "CONGESTION_AWARE"))  # run both strategies
    return conn, sid, r  # connection, scenario id and the run ids


def test_sql_metrics_match_python_metrics(runs):  # SQL and Python must agree
    conn, sid, r = runs  # fixtures
    stats = an.evacuation_time_stats(conn).set_index("run_id")  # SQL statistics
    comp = an.completion_by_run(conn).set_index("run_id")  # SQL completion rates
    for run_id in r.values():  # each run
        row = repo.get_run(conn, run_id)  # the Python-computed values stored with the run
        assert stats.loc[run_id, "median_seconds"] == pytest.approx(row["median_evacuation_time_seconds"])  # median agrees
        assert stats.loc[run_id, "p95_seconds"] == pytest.approx(row["percentile_95_evacuation_time_seconds"])  # 95th percentile agrees
        assert stats.loc[run_id, "average_seconds"] == pytest.approx(row["average_evacuation_time_seconds"])  # average agrees
        assert comp.loc[run_id, "completion_rate"] == pytest.approx(row["completion_rate"])  # completion rate agrees
        assert comp.loc[run_id, "total_agents"] == row["total_agents"]  # agent count agrees


def test_consistency_checks_all_pass(runs):  # the integrity checks pass on real runs
    conn, sid, r = runs  # fixtures
    for run_id in r.values():  # each run
        assert [c for c in consistency_checks(conn, run_id) if not c["passed"]] == []  # no failed check


def test_queries_return_real_rows(runs):  # every query returns data
    conn, sid, r = runs  # fixtures
    rid = r["SHORTEST_PATH"]  # one run
    assert not an.most_used_edges(conn, rid).empty  # most-used roads
    assert not an.peak_congestion_by_edge(conn, rid).empty  # peak congestion
    assert not an.occupancy_time_series(conn, rid).empty  # occupancy over time
    assert not an.wait_by_destination(conn, rid).empty  # waiting per safe stop
    assert an.outcome_breakdown(conn, rid)["agents"].sum() == 300  # every agent is accounted for
    rank = an.bottleneck_ranking(conn, rid)  # bottleneck ranking
    assert list(rank["severity_rank"]) == sorted(rank["severity_rank"])  # sorted by rank
    sc = an.strategy_comparison(conn)  # strategy comparison
    assert set(sc["routing_strategy"]) == {"SHORTEST_PATH", "CONGESTION_AWARE"}  # both strategies appear
    assert not an.scenario_history(conn).empty  # scenario history
    assert len(an.intervention_comparison(conn)) == 1  # the paired comparison was recorded


def test_overview_on_empty_database(conn):  # an empty database is handled honestly
    o = an.overview(conn)  # headline numbers
    assert o["completed_runs"] == 0 and o["latest_run"] is None and o["highest_congestion"] is None  # nothing measured
    assert an.strategy_comparison(conn).empty and an.completion_by_run(conn).empty  # empty tables


def test_relative_changes_are_correct(runs):  # relative change maths
    conn, sid, r = runs  # fixtures
    df = an.compare_runs(conn, list(r.values()))  # side-by-side table
    base, alt = r["SHORTEST_PATH"], r["CONGESTION_AWARE"]  # baseline and alternative runs
    rel = an.relative_changes(df, base).set_index("run_id")  # relative changes versus the baseline
    b = df.set_index("run_id")  # raw values
    w = "total_waiting_time_seconds"  # the column to check
    expect = (b.loc[alt, w] - b.loc[base, w]) / b.loc[base, w]  # expected change
    assert rel.loc[alt, w + "_rel_change"] == pytest.approx(expect)  # matches
    assert rel.loc[base, w + "_rel_change"] == 0  # the baseline has no change against itself


def test_seed_variability_query(seeded):  # variation across seeds
    conn, sid = seeded  # database with the demo scenario
    from database.seed_data import create_demo_scenario  # scenario builder
    for s in (1, 2, 3):  # three seeds
        run_scenario(conn, create_demo_scenario(conn, "Seeds", agents=60, seed=s))  # run each
    sv = an.seed_variability(conn)  # variability table
    row = sv[sv.scenario_name == "Seeds"].iloc[0]  # the row for these runs
    assert row["runs"] == 3 and row["distinct_seeds"] == 3 and row["sd_completion"] >= 0  # three runs, three seeds, valid spread


def test_counterfactual_before_after(seeded):  # the what-if lab
    conn, sid = seeded  # database with the demo scenario
    edge = int(an.most_used_edges(conn, run_scenario(conn, sid), 1).iloc[0]["edge_id"])  # the busiest road
    out = run_counterfactual(conn, sid, {"add_closures": [{"edge_id": edge, "close_time_seconds": 0}]},  # close it and compare (part 1)
                             alt_strategy="CONGESTION_AWARE")  # with a different strategy (part 2)
    comp = an.intervention_comparison(conn)  # stored comparisons
    row = comp[comp.intervention_id == out["intervention_id"]].iloc[0]  # this one
    assert row["intervention_type"] == "COMBINED"  # closure plus strategy change
    assert row["baseline_run"] == out["baseline_run"] and row["alternative_run"] == out["alternative_run"]  # the right runs are linked
    assert any("close edge" in c for c in out["changes"])  # the change is described
    assert not an.edge_congestion_diff(conn, out["baseline_run"], out["alternative_run"]).empty  # congestion changed somewhere
    pa = repo.load_population(conn, sid)           # paired design: same population in the derived scenario
    pb = repo.load_population(conn, out["derived_scenario_id"])  # the derived scenario's crowd
    assert [(a.origin_node, a.departure_time) for a in pa] == [(a.origin_node, a.departure_time) for a in pb]  # identical people


def test_apply_modifications_is_pure(seeded):  # modifications never change the original
    conn, sid = seeded  # database with the demo scenario
    cfg = repo.load_scenario_config(conn, sid)  # the scenario
    before = cfg.snapshot_json()  # its state before
    new, changes = apply_modifications(cfg, {"throughput_scale": 0.5, "agent_count": 10})  # modify a copy
    assert cfg.snapshot_json() == before and new.agent_count == 10 and changes  # the original is untouched
    assert new.destinations[0].throughput_per_timestep == 3  # the copy has the scaled throughput
