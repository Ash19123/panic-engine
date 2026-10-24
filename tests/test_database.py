import sqlite3

import pytest

from database import repositories as repo
from database.connection import transaction
from database.init_db import current_version, init_db
from database.seed_data import DEMO_NAME, seed
from simulation.experiments import run_scenario


def test_initialization_creates_tables_and_version(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("scenarios", "network_nodes", "network_edges", "destinations", "agents", "simulation_runs",
              "simulation_events", "edge_occupancy_snapshots", "bottleneck_reports", "interventions",
              "agent_outcomes", "schema_migrations"):
        assert t in tables
    assert current_version(conn) == 3


def test_init_is_idempotent_and_preserves_data(tmp_path):
    path = tmp_path / "a.db"
    c = init_db(path)
    seed(c)
    n = c.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
    c.close()
    c2 = init_db(path)
    assert c2.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0] == n
    assert current_version(c2) == 3
    c2.close()


def test_foreign_keys_enforced(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO scenarios(scenario_name, geographic_data_source, network_id, network_fingerprint,"
                     " routing_strategy, agent_count, random_seed, timestep_seconds, simulation_time_limit)"
                     " VALUES ('x','S','missing','f','SHORTEST_PATH',1,1,1,10)")


def test_check_constraints(conn):
    seed(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE scenarios SET agent_count = 0")


def test_transaction_rolls_back(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO networks(network_id,name,source,is_synthetic,node_count,edge_count)"
                         " VALUES ('n','n','SYNTHETIC',1,0,0)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 0


def test_network_saved_once_with_parallel_edges_roundtrip(conn):
    from tests.helpers import make_net
    net = make_net([(1, 2, 100, 10), (1, 2, 120, 5)])      # two parallel segments
    saved = repo.save_network(conn, net)
    again = repo.save_network(conn, net)
    assert saved.network_id == again.network_id
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1
    ids = sorted(saved.nodes)
    assert len(saved.parallel_edges(ids[0], ids[1])) == 2


def test_scenario_roundtrip(seeded):
    conn, sid = seeded
    cfg = repo.load_scenario_config(conn, sid)
    assert cfg.name == DEMO_NAME and cfg.agent_count == 300 and len(cfg.origins) == 3
    assert all(d.hypothetical for d in cfg.destinations)
    assert cfg.params.snapshot_interval_seconds == 10.0
    seed(conn)      # seeding twice must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0] == 1


def test_event_persistence_and_atomic_failure(seeded):
    conn, sid = seeded
    run_id = run_scenario(conn, sid)
    assert repo.get_run(conn, run_id)["status"] == "COMPLETED"
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT event_type FROM simulation_events WHERE run_id=?", (run_id,))}
    assert {"AGENT_DEPARTED", "EDGE_ENTERED", "EDGE_EXITED", "AGENT_EVACUATED"} <= kinds

    class Broken:      # persistence that fails midway must leave no detail rows behind
        metrics = {}
        agents = []
        events = [(None, 0.0, "NOT_A_TYPE", None, None, None, None, None)]
        occupancy_rows = position_rows = frame_rows = bottlenecks = []
        total_simulated_seconds = 0
    rid = repo.create_run(conn, sid, "SHORTEST_PATH", 1, "{}")
    with pytest.raises(Exception):
        repo.persist_result(conn, rid, Broken)
    assert conn.execute("SELECT COUNT(*) FROM simulation_events WHERE run_id=?", (rid,)).fetchone()[0] == 0
    assert repo.get_run(conn, rid)["status"] == "RUNNING"
