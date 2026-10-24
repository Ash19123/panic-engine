"""Bomb / blast hazard drill: phone alerts, blast closure, people caught, persistence, migration."""
import sqlite3
from collections import Counter

from geo import geographic_utils as geo
from database import analytics as an
from database import repositories as repo
from database.init_db import SCHEMA_FILE, current_version, init_db
from simulation.engine import EvacuationSimulation
from simulation.experiments import consistency_checks, run_scenario
from tests.helpers import make_cfg, make_net


def line_net(n=9, step=100):
    return make_net([(i, i + 1, step, 60) for i in range(1, n)])


def hazard(net, node=1, blast=80.0, alert=1000.0, strike=100.0, alert_at=10.0):
    nd = net.nodes[node]
    return {"name": "Test blast", "node": node, "lat": nd.lat, "lon": nd.lon, "blast_radius_m": blast,
            "alert_radius_m": alert, "strike_time_s": strike, "alert_time_s": alert_at}


def test_alerts_sent_to_everyone_inside_and_people_react_after_alert():
    net = line_net()
    cfg = make_cfg(net, [5], [(10, 9, 100, 50)], agents=12, hazards=[hazard(net)], departure_delay_max_seconds=20)
    res = EvacuationSimulation(net, cfg).run()
    kinds = Counter(ev[2] for ev in res.events)
    assert kinds["ALERT_SENT"] == 12 and kinds["HAZARD_STRIKE"] == 1
    assert all(a.alert_time == 10.0 and a.departure_time >= 10.0 for a in res.agents)
    first_departure = min(ev[1] for ev in res.events if ev[2] == "AGENT_DEPARTED")
    assert first_departure >= 10.0                       # nobody moves before the alert
    assert res.metrics["evacuated"] == 12                # far enough from the blast: all reach the safe stop


def test_people_inside_the_blast_area_at_strike_are_caught_and_accounting_stays_exact():
    net = line_net()
    cfg = make_cfg(net, [2], [(10, 9, 100, 50)], agents=10, hazards=[hazard(net, blast=150.0, strike=15.0, alert_at=14.0)],
                   departure_delay_max_seconds=60)
    res = EvacuationSimulation(net, cfg).run()
    caught = [ev for ev in res.events if ev[2] == "AGENT_FAILED" and ev[7].startswith("caught in blast")]
    assert len(caught) == 10 and res.metrics["failed"] == 10 and res.metrics["evacuated"] == 0
    m = res.metrics
    assert m["evacuated"] + m["failed"] + m["unresolved"] == m["total_agents"]
    assert all(v >= 0 for v in res.state.occupancy.values())


def test_blast_closes_roads_inside_radius_only_and_survivors_still_finish():
    net = line_net()
    cfg = make_cfg(net, [5], [(10, 9, 100, 50)], agents=6, hazards=[hazard(net, blast=150.0, strike=30.0, alert_at=0.0)])
    res = EvacuationSimulation(net, cfg).run()
    blocked = [ev[4] for ev in res.events if ev[2] == "ROAD_BLOCKED"]
    assert blocked and all("inside blast radius" == ev[7] for ev in res.events if ev[2] == "ROAD_BLOCKED")
    for eid in blocked:                                   # every closed edge lies within the blast radius
        e = net.edges[eid]
        for n in (net.nodes[e.source], net.nodes[e.target]):
            assert geo.haversine_m(n.lat, n.lon, net.nodes[1].lat, net.nodes[1].lon) <= 150.0
    assert res.metrics["evacuated"] == 6


def test_hazard_roundtrip_in_sql_and_summary_queries(seeded):
    conn, sid = seeded
    cfg = repo.load_scenario_config(conn, sid)
    net = repo.load_network(conn, cfg.network_id)
    centre = net.nodes[sorted(net.nodes)[4 * 10 + 5]]
    cfg.name = "Bomb drill"
    cfg.agent_count = 80
    cfg.origins = cfg.origins[:1]
    cfg.params.hazards = [{"name": "Bomb A", "node": sorted(net.nodes)[45], "lat": centre.lat, "lon": centre.lon,
                           "blast_radius_m": 150.0, "alert_radius_m": 900.0, "strike_time_s": 300.0, "alert_time_s": 120.0}]
    cfg.destinations = [d for d in cfg.destinations]
    new_id = repo.save_scenario(conn, cfg, net)
    repo.ensure_population(conn, new_id, net, cfg)
    again = repo.load_scenario_config(conn, new_id)
    assert again.params.hazards[0]["name"] == "Bomb A"
    assert all(a.alert_time is not None for a in repo.load_population(conn, new_id))
    rid = run_scenario(conn, new_id, "CONGESTION_AWARE")
    h = an.hazard_summary(conn, rid)
    assert h["alerts_sent"] == 80 and h["first_strike_s"] == 300.0 and h["first_alert_s"] == 120.0
    assert h["caught_in_blast"] + h["safe_before_strike"] <= 80
    assert [c for c in consistency_checks(conn, rid) if not c["passed"]] == []


def test_migration_v1_to_v2_preserves_existing_data(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    c.executescript("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL DEFAULT '');INSERT INTO schema_migrations(version, description) VALUES (1,'v1');")
    c.execute("INSERT INTO networks(network_id,name,source,is_synthetic,node_count,edge_count) VALUES ('n','n','SYNTHETIC',1,0,0)")
    c.commit()
    c.close()
    conn = init_db(path)
    assert current_version(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)")]
    assert "alert_time_seconds" in cols
    conn.close()
