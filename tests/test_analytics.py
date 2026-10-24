import pytest

from database import analytics as an
from database import repositories as repo
from simulation.experiments import (apply_modifications, consistency_checks, run_counterfactual,
                                    run_paired_strategies, run_scenario)


@pytest.fixture
def runs(seeded):
    conn, sid = seeded
    r = run_paired_strategies(conn, sid, ("SHORTEST_PATH", "CONGESTION_AWARE"))
    return conn, sid, r


def test_sql_metrics_match_python_metrics(runs):
    conn, sid, r = runs
    stats = an.evacuation_time_stats(conn).set_index("run_id")
    comp = an.completion_by_run(conn).set_index("run_id")
    for run_id in r.values():
        row = repo.get_run(conn, run_id)
        assert stats.loc[run_id, "median_seconds"] == pytest.approx(row["median_evacuation_time_seconds"])
        assert stats.loc[run_id, "p95_seconds"] == pytest.approx(row["percentile_95_evacuation_time_seconds"])
        assert stats.loc[run_id, "average_seconds"] == pytest.approx(row["average_evacuation_time_seconds"])
        assert comp.loc[run_id, "completion_rate"] == pytest.approx(row["completion_rate"])
        assert comp.loc[run_id, "total_agents"] == row["total_agents"]


def test_consistency_checks_all_pass(runs):
    conn, sid, r = runs
    for run_id in r.values():
        assert [c for c in consistency_checks(conn, run_id) if not c["passed"]] == []


def test_queries_return_real_rows(runs):
    conn, sid, r = runs
    rid = r["SHORTEST_PATH"]
    assert not an.most_used_edges(conn, rid).empty
    assert not an.peak_congestion_by_edge(conn, rid).empty
    assert not an.occupancy_time_series(conn, rid).empty
    assert not an.wait_by_destination(conn, rid).empty
    assert an.outcome_breakdown(conn, rid)["agents"].sum() == 300
    rank = an.bottleneck_ranking(conn, rid)
    assert list(rank["severity_rank"]) == sorted(rank["severity_rank"])
    sc = an.strategy_comparison(conn)
    assert set(sc["routing_strategy"]) == {"SHORTEST_PATH", "CONGESTION_AWARE"}
    assert not an.scenario_history(conn).empty
    assert len(an.intervention_comparison(conn)) == 1


def test_overview_on_empty_database(conn):
    o = an.overview(conn)
    assert o["completed_runs"] == 0 and o["latest_run"] is None and o["highest_congestion"] is None
    assert an.strategy_comparison(conn).empty and an.completion_by_run(conn).empty


def test_relative_changes_are_correct(runs):
    conn, sid, r = runs
    df = an.compare_runs(conn, list(r.values()))
    base, alt = r["SHORTEST_PATH"], r["CONGESTION_AWARE"]
    rel = an.relative_changes(df, base).set_index("run_id")
    b = df.set_index("run_id")
    w = "total_waiting_time_seconds"
    expect = (b.loc[alt, w] - b.loc[base, w]) / b.loc[base, w]
    assert rel.loc[alt, w + "_rel_change"] == pytest.approx(expect)
    assert rel.loc[base, w + "_rel_change"] == 0


def test_seed_variability_query(seeded):
    conn, sid = seeded
    from database.seed_data import create_demo_scenario
    for s in (1, 2, 3):
        run_scenario(conn, create_demo_scenario(conn, "Seeds", agents=60, seed=s))
    sv = an.seed_variability(conn)
    row = sv[sv.scenario_name == "Seeds"].iloc[0]
    assert row["runs"] == 3 and row["distinct_seeds"] == 3 and row["sd_completion"] >= 0


def test_counterfactual_before_after(seeded):
    conn, sid = seeded
    edge = int(an.most_used_edges(conn, run_scenario(conn, sid), 1).iloc[0]["edge_id"])
    out = run_counterfactual(conn, sid, {"add_closures": [{"edge_id": edge, "close_time_seconds": 0}]},
                             alt_strategy="CONGESTION_AWARE")
    comp = an.intervention_comparison(conn)
    row = comp[comp.intervention_id == out["intervention_id"]].iloc[0]
    assert row["intervention_type"] == "COMBINED"
    assert row["baseline_run"] == out["baseline_run"] and row["alternative_run"] == out["alternative_run"]
    assert any("close edge" in c for c in out["changes"])
    assert not an.edge_congestion_diff(conn, out["baseline_run"], out["alternative_run"]).empty
    pa = repo.load_population(conn, sid)           # paired design: same population in the derived scenario
    pb = repo.load_population(conn, out["derived_scenario_id"])
    assert [(a.origin_node, a.departure_time) for a in pa] == [(a.origin_node, a.departure_time) for a in pb]


def test_apply_modifications_is_pure(seeded):
    conn, sid = seeded
    cfg = repo.load_scenario_config(conn, sid)
    before = cfg.snapshot_json()
    new, changes = apply_modifications(cfg, {"throughput_scale": 0.5, "agent_count": 10})
    assert cfg.snapshot_json() == before and new.agent_count == 10 and changes
    assert new.destinations[0].throughput_per_timestep == 3
