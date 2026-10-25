import sqlite3  # database errors

import pytest  # the test framework

from database import repositories as repo  # save and load helpers
from database.connection import transaction  # all-or-nothing blocks
from database.init_db import current_version, init_db  # create the database
from database.seed_data import DEMO_NAME, seed  # demo data
from simulation.experiments import run_scenario  # run and store a scenario


def test_initialization_creates_tables_and_version(conn):  # a new database has every table
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}  # names of all tables
    for t in ("scenarios", "network_nodes", "network_edges", "destinations", "agents", "simulation_runs",  # required tables (1)
              "simulation_events", "edge_occupancy_snapshots", "bottleneck_reports", "interventions",  # (2)
              "agent_outcomes", "schema_migrations"):  # (3)
        assert t in tables  # each one exists
    assert current_version(conn) == 3  # all three migrations were applied


def test_init_is_idempotent_and_preserves_data(tmp_path):  # running init twice keeps the data
    path = tmp_path / "a.db"  # database file
    c = init_db(path)  # create it
    seed(c)  # insert the demo data
    n = c.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]  # number of scenarios
    c.close()  # close it
    c2 = init_db(path)  # open it again
    assert c2.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0] == n  # the data is still there
    assert current_version(c2) == 3  # and still at version 3
    c2.close()  # close it


def test_foreign_keys_enforced(conn):  # foreign keys must be switched on
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # the setting is on
    with pytest.raises(sqlite3.IntegrityError):  # inserting a bad reference must fail
        conn.execute("INSERT INTO scenarios(scenario_name, geographic_data_source, network_id, network_fingerprint,"  # SQL (1)
                     " routing_strategy, agent_count, random_seed, timestep_seconds, simulation_time_limit)"  # SQL (2)
                     " VALUES ('x','S','missing','f','SHORTEST_PATH',1,1,1,10)")  # SQL (3): the network does not exist


def test_check_constraints(conn):  # CHECK constraints reject nonsense
    seed(conn)  # insert the demo data
    with pytest.raises(sqlite3.IntegrityError):  # zero agents is not allowed
        conn.execute("UPDATE scenarios SET agent_count = 0")  # violates the CHECK


def test_transaction_rolls_back(conn):  # failed blocks leave no trace
    with pytest.raises(RuntimeError):  # the error must be passed on
        with transaction(conn):  # all-or-nothing
            conn.execute("INSERT INTO networks(network_id,name,source,is_synthetic,node_count,edge_count)"  # SQL (1)
                         " VALUES ('n','n','SYNTHETIC',1,0,0)")  # SQL (2)
            raise RuntimeError("boom")  # fail on purpose
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 0  # the insert was undone


def test_network_saved_once_with_parallel_edges_roundtrip(conn):  # saving is idempotent and keeps parallel roads
    from tests.helpers import make_net  # test network builder
    net = make_net([(1, 2, 100, 10), (1, 2, 120, 5)])      # two parallel segments
    saved = repo.save_network(conn, net)  # save it
    again = repo.save_network(conn, net)  # save it again
    assert saved.network_id == again.network_id  # same id
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1  # stored once
    ids = sorted(saved.nodes)  # node ids
    assert len(saved.parallel_edges(ids[0], ids[1])) == 2  # both parallel roads survived


def test_scenario_roundtrip(seeded):  # a stored scenario loads back identical
    conn, sid = seeded  # database with the demo scenario
    cfg = repo.load_scenario_config(conn, sid)  # load it
    assert cfg.name == DEMO_NAME and cfg.agent_count == 300 and len(cfg.origins) == 3  # key fields match
    assert all(d.hypothetical for d in cfg.destinations)  # safe stops are flagged hypothetical
    assert cfg.params.snapshot_interval_seconds == 10.0  # parameters survived the JSON round trip
    seed(conn)      # seeding twice must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0] == 1  # still one scenario


def test_event_persistence_and_atomic_failure(seeded):  # events are stored; a failed save leaves nothing
    conn, sid = seeded  # database with the demo scenario
    run_id = run_scenario(conn, sid)  # run and store it
    assert repo.get_run(conn, run_id)["status"] == "COMPLETED"  # finished
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT event_type FROM simulation_events WHERE run_id=?", (run_id,))}  # event types stored
    assert {"AGENT_DEPARTED", "EDGE_ENTERED", "EDGE_EXITED", "AGENT_EVACUATED"} <= kinds  # the main ones are present

    class Broken:      # persistence that fails midway must leave no detail rows behind
        metrics = {}  # no metrics
        agents = []  # no agents
        events = [(None, 0.0, "NOT_A_TYPE", None, None, None, None, None)]  # an invalid event type
        occupancy_rows = position_rows = frame_rows = bottlenecks = []  # empty lists
        total_simulated_seconds = 0  # zero duration
    rid = repo.create_run(conn, sid, "SHORTEST_PATH", 1, "{}")  # a fresh run record
    with pytest.raises(Exception):  # saving must fail
        repo.persist_result(conn, rid, Broken)  # try to save the broken result
    assert conn.execute("SELECT COUNT(*) FROM simulation_events WHERE run_id=?", (rid,)).fetchone()[0] == 0  # nothing was stored
    assert repo.get_run(conn, rid)["status"] == "RUNNING"  # the run was not marked completed
