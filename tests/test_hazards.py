"""Bomb / blast hazard drill: phone alerts, blast closure, people caught, persistence, migration."""
import sqlite3  # build an old-style database
from collections import Counter  # count events by type

from geo import geographic_utils as geo  # distance helper
from database import analytics as an  # SQL analytics helpers
from database import repositories as repo  # save and load helpers
from database.init_db import SCHEMA_FILE, current_version, init_db  # database creation
from simulation.engine import EvacuationSimulation  # the engine
from simulation.experiments import consistency_checks, run_scenario  # experiment helpers
from tests.helpers import make_cfg, make_net  # test network and scenario builders


def line_net(n=9, step=100):  # a straight line of roads
    return make_net([(i, i + 1, step, 60) for i in range(1, n)])  # nodes 1..n, 100 m apart


def hazard(net, node=1, blast=80.0, alert=1000.0, strike=100.0, alert_at=10.0):  # a bomb definition for tests
    nd = net.nodes[node]  # where it goes off
    return {"name": "Test blast", "node": node, "lat": nd.lat, "lon": nd.lon, "blast_radius_m": blast,  # identity, place, blast radius
            "alert_radius_m": alert, "strike_time_s": strike, "alert_time_s": alert_at}  # alert radius and times


def test_alerts_sent_to_everyone_inside_and_people_react_after_alert():  # alert delivery
    net = line_net()  # test network
    cfg = make_cfg(net, [5], [(10, 9, 100, 50)], agents=12, hazards=[hazard(net)], departure_delay_max_seconds=20)  # 12 people, one bomb
    res = EvacuationSimulation(net, cfg).run()  # run it
    kinds = Counter(ev[2] for ev in res.events)  # event counts
    assert kinds["ALERT_SENT"] == 12 and kinds["HAZARD_STRIKE"] == 1  # 12 alerts and one strike
    assert all(a.alert_time == 10.0 and a.departure_time >= 10.0 for a in res.agents)  # everyone alerted at 10 s and leaves after that
    first_departure = min(ev[1] for ev in res.events if ev[2] == "AGENT_DEPARTED")  # the earliest departure
    assert first_departure >= 10.0                       # nobody moves before the alert
    assert res.metrics["evacuated"] == 12                # far enough from the blast: all reach the safe stop


def test_people_inside_the_blast_area_at_strike_are_caught_and_accounting_stays_exact():  # the strike
    net = line_net()  # test network
    cfg = make_cfg(net, [2], [(10, 9, 100, 50)], agents=10, hazards=[hazard(net, blast=150.0, strike=15.0, alert_at=14.0)],  # a late alert (part 1)
                   departure_delay_max_seconds=60)  # long reaction delays (part 2)
    res = EvacuationSimulation(net, cfg).run()  # run it
    caught = [ev for ev in res.events if ev[2] == "AGENT_FAILED" and ev[7].startswith("caught in blast")]  # catch events
    assert len(caught) == 10 and res.metrics["failed"] == 10 and res.metrics["evacuated"] == 0  # everyone was inside the blast
    m = res.metrics  # summary numbers
    assert m["evacuated"] + m["failed"] + m["unresolved"] == m["total_agents"]  # nobody is lost from the accounting
    assert all(v >= 0 for v in res.state.occupancy.values())  # no road has negative occupancy


def test_blast_closes_roads_inside_radius_only_and_survivors_still_finish():  # which roads close
    net = line_net()  # test network
    cfg = make_cfg(net, [5], [(10, 9, 100, 50)], agents=6, hazards=[hazard(net, blast=150.0, strike=30.0, alert_at=0.0)])  # an early alert
    res = EvacuationSimulation(net, cfg).run()  # run it
    blocked = [ev[4] for ev in res.events if ev[2] == "ROAD_BLOCKED"]  # closed roads
    assert blocked and all("inside blast radius" == ev[7] for ev in res.events if ev[2] == "ROAD_BLOCKED")  # closed because of the blast
    for eid in blocked:                                   # every closed edge lies within the blast radius
        e = net.edges[eid]  # the road
        for n in (net.nodes[e.source], net.nodes[e.target]):  # both ends
            assert geo.haversine_m(n.lat, n.lon, net.nodes[1].lat, net.nodes[1].lon) <= 150.0  # inside the radius
    assert res.metrics["evacuated"] == 6  # everyone got away


def test_hazard_roundtrip_in_sql_and_summary_queries(seeded):  # storage and queries
    conn, sid = seeded  # database with the demo scenario
    cfg = repo.load_scenario_config(conn, sid)  # the scenario
    net = repo.load_network(conn, cfg.network_id)  # its network
    centre = net.nodes[sorted(net.nodes)[4 * 10 + 5]]  # a node near the middle of the grid
    cfg.name = "Bomb drill"  # rename it
    cfg.agent_count = 80  # 80 people
    cfg.origins = cfg.origins[:1]  # one origin
    cfg.params.hazards = [{"name": "Bomb A", "node": sorted(net.nodes)[45], "lat": centre.lat, "lon": centre.lon,  # one bomb (part 1)
                           "blast_radius_m": 150.0, "alert_radius_m": 900.0, "strike_time_s": 300.0, "alert_time_s": 120.0}]  # (part 2)
    cfg.destinations = [d for d in cfg.destinations]  # keep the safe stops
    new_id = repo.save_scenario(conn, cfg, net)  # store it
    repo.ensure_population(conn, new_id, net, cfg)  # generate the crowd
    again = repo.load_scenario_config(conn, new_id)  # load it back
    assert again.params.hazards[0]["name"] == "Bomb A"  # the bomb survived the round trip
    assert all(a.alert_time is not None for a in repo.load_population(conn, new_id))  # everyone is inside the alert area
    rid = run_scenario(conn, new_id, "CONGESTION_AWARE")  # run it
    h = an.hazard_summary(conn, rid)  # SQL summary
    assert h["alerts_sent"] == 80 and h["first_strike_s"] == 300.0 and h["first_alert_s"] == 120.0  # alert and strike facts
    assert h["caught_in_blast"] + h["safe_before_strike"] <= 80  # the two groups cannot exceed everyone
    assert [c for c in consistency_checks(conn, rid) if not c["passed"]] == []  # integrity checks pass


def test_migration_v1_to_v2_preserves_existing_data(tmp_path):  # upgrading an old database
    path = tmp_path / "old.db"  # database file
    c = sqlite3.connect(path)  # create it by hand
    c.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))  # the version 1 schema
    c.executescript("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, "  # bookkeeping table (part 1)
                    "applied_at TEXT NOT NULL DEFAULT '');INSERT INTO schema_migrations(version, description) VALUES (1,'v1');")  # mark version 1 as applied (part 2)
    c.execute("INSERT INTO networks(network_id,name,source,is_synthetic,node_count,edge_count) VALUES ('n','n','SYNTHETIC',1,0,0)")  # some existing data
    c.commit()  # save it
    c.close()  # close it
    conn = init_db(path)  # open it with the new code: migrations run
    assert current_version(conn) == 3  # now at version 3
    assert conn.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 1  # the old data is still there
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)")]  # columns of the agents table
    assert "alert_time_seconds" in cols  # the new column exists
    conn.close()  # close it


def test_routes_avoid_roads_inside_a_blast_radius():  # nobody should walk towards the bomb on the way out
    from simulation.congestion import CongestionState  # live road state
    from simulation.models import ModelParams  # model parameters
    from simulation.routing import Router  # route finder
    from geo import geographic_utils as geo  # offset helper
    from tests.helpers import LAT, LON, make_net  # tiny test network
    coords = {1: geo.offset_latlon(LAT, LON, 0, -100), 2: (LAT, LON), 4: geo.offset_latlon(LAT, LON, 0, 100), 3: geo.offset_latlon(LAT, LON, 400, 0)}  # 1 and 4 flank the bomb at 2; 3 is a far detour
    net = make_net([(1, 2, 100, 50), (2, 4, 100, 50), (1, 3, 150, 50), (3, 4, 150, 50)], coords=coords)  # short road through the bomb, longer road round it
    hz = [{"name": "b", "node": 2, "lat": LAT, "lon": LON, "blast_radius_m": 120.0, "alert_radius_m": 400.0, "strike_time_s": 300.0, "alert_time_s": 100.0}]  # one bomb at node 2
    for strategy in ("SHORTEST_PATH", "CONGESTION_AWARE"):  # the rule applies to every strategy
        aware = ModelParams(hazards=hz)  # default: avoid the blast area
        blind = ModelParams(hazards=hz, hazard_avoid_penalty_s=0.0)  # old behaviour: ignore the bomb
        r_aware = Router(net, CongestionState(net, aware), strategy, aware).route_from(1, 4, 0.0)  # route from 1 to 4
        r_blind = Router(net, CongestionState(net, blind), strategy, blind).route_from(1, 4, 0.0)  # same without the rule
        nodes = lambda r: {net.edges[e].target for e in r[1]}  # nodes reached along the route
        assert 2 in nodes(r_blind) and 3 not in nodes(r_blind)  # blind routing cuts through the bomb
        assert 3 in nodes(r_aware) and 2 not in nodes(r_aware)  # aware routing walks round it
