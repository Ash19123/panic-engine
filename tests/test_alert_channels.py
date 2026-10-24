"""Alert channels: phone / siren / neighbour / nobody, the planner experiment and the siren recommender."""
from collections import Counter

from database import analytics as an
from database import repositories as repo
from simulation.agent import generate_population
from simulation.alerting import recommend_sirens
from simulation.engine import EvacuationSimulation
from simulation.experiments import run_alert_planner, run_scenario
from tests.helpers import make_cfg, make_net


def line(n=9, step=100):
    return make_net([(i, i + 1, step, 60) for i in range(1, n)])


def hz(net, node=1, blast=60.0, alert=2000.0, strike=400.0, alert_at=20.0):
    nd = net.nodes[node]
    return {"name": "B", "node": node, "lat": nd.lat, "lon": nd.lon, "blast_radius_m": blast,
            "alert_radius_m": alert, "strike_time_s": strike, "alert_time_s": alert_at}


def pop(net, channels, agents=400, seed=3, origins=(4, 5, 6, 7)):
    cfg = make_cfg(net, list(origins), [(10, 9, 999, 99)], agents=agents, seed=seed, hazards=[hz(net)],
                   alert_channels=channels, departure_delay_max_seconds=30)
    return cfg, generate_population(net, cfg)


def test_no_channels_means_everybody_gets_the_phone_alert():
    net = line()
    _, agents = pop(net, {})
    assert {a.alert_channel for a in agents} == {"PHONE"} and all(a.alert_time == 20.0 for a in agents)


def test_phone_reach_share_and_unreached_people_never_depart():
    net = line()
    cfg, agents = pop(net, {"phone_reach": 0.4})
    c = Counter(a.alert_channel for a in agents)
    assert 0.3 < c["PHONE"] / len(agents) < 0.5 and c["PHONE"] + c["NONE"] == len(agents)
    for a in agents:
        if a.alert_channel == "NONE":
            assert a.alert_time is None and a.departure_time > cfg.time_limit_seconds
        else:
            assert a.alert_time >= 20.0 and a.departure_time >= a.alert_time


def test_sirens_reach_people_without_phones_and_neighbours_relay_in_waves():
    net = line()
    nd = net.nodes[4]
    siren = {"node": 4, "lat": nd.lat, "lon": nd.lon, "radius_m": 20.0, "delay_s": 10.0}
    _, agents = pop(net, {"phone_reach": 0.0, "sirens": [siren], "neighbour_radius_m": 60.0, "neighbour_delay_s": 30.0})
    by_node = {}
    for a in agents:
        by_node.setdefault(a.origin_node, a)
    assert by_node[4].alert_channel == "SIREN" and by_node[4].alert_time == 30.0         # alert 20 s + siren delay 10 s
    assert by_node[5].alert_channel == "NEIGHBOUR" and by_node[5].alert_time == 60.0     # one hop later
    assert by_node[6].alert_time == 90.0 and by_node[7].alert_time == 120.0              # relay chain


def test_channel_variants_keep_the_same_people_and_reaction_delays():
    net = line()
    _, a = pop(net, {"phone_reach": 0.3})
    _, b = pop(net, {"phone_reach": 0.9, "neighbour_radius_m": 60.0})
    assert [x.origin_node for x in a] == [x.origin_node for x in b]                      # same places
    both = [(x, y) for x, y in zip(a, b) if x.alert_time is not None and y.alert_time is not None
            and x.alert_channel == y.alert_channel == "PHONE"]
    assert both and all(abs((x.departure_time - x.alert_time) - (y.departure_time - y.alert_time)) < 1e-2 for x, y in both)


def test_unalerted_people_inside_the_blast_are_caught_and_alerted_ones_get_away():
    net = line()
    cfg = make_cfg(net, [2, 8], [(10, 9, 999, 99)], agents=60, seed=5, hazards=[hz(net, blast=120.0, strike=200.0)],
                   alert_channels={"phone_reach": 0.5}, departure_delay_max_seconds=20)
    res = EvacuationSimulation(net, cfg).run()
    kinds = Counter(ev[2] for ev in res.events)
    n_phone = sum(1 for a in res.agents if a.alert_channel == "PHONE")
    assert kinds["ALERT_SENT"] == n_phone < 60
    m = res.metrics
    assert m["evacuated"] + m["failed"] + m["unresolved"] == m["total_agents"]
    near_unalerted = [a for a in res.agents if a.alert_channel == "NONE" and a.origin_node == 2]
    assert near_unalerted and all(a.state.value == "UNABLE_TO_EVACUATE" for a in near_unalerted)     # inside the blast


def test_recommender_picks_the_biggest_gap_and_avoids_the_blast_zone():
    net = line()
    people = [8] * 30 + [3] * 5
    sites = recommend_sirens(net, people, radius_m=60.0, k=2)
    nd = net.nodes[8]
    import geo.geographic_utils as geo
    first = net.nodes[sites[0]["node"]]
    assert sites[0]["covers"] == 30 and geo.haversine_m(first.lat, first.lon, nd.lat, nd.lon) <= 60.0
    banned = recommend_sirens(net, people, radius_m=60.0, k=1, avoid=[(nd.lat, nd.lon, 70.0)])
    assert all(geo.haversine_m(net.nodes[s["node"]].lat, net.nodes[s["node"]].lon, nd.lat, nd.lon) > 70.0 for s in banned)


def test_planner_end_to_end_in_sql(seeded):
    conn, sid = seeded
    cfg = repo.load_scenario_config(conn, sid)
    net = repo.load_network(conn, cfg.network_id)
    ids = sorted(net.nodes)
    centre = net.nodes[ids[35]]
    cfg.name = "Bomb drill planner"
    cfg.agent_count = 150
    cfg.params.departure_delay_max_seconds = 30
    cfg.params.hazards = [{"name": "B", "node": ids[35], "lat": centre.lat, "lon": centre.lon, "blast_radius_m": 140.0,
                           "alert_radius_m": 520.0, "strike_time_s": 420.0, "alert_time_s": 200.0}]
    import geo.geographic_utils as geo
    inside = [i for i in ids if geo.haversine_m(net.nodes[i].lat, net.nodes[i].lon, centre.lat, centre.lon) <= 520]
    from simulation.models import OriginSpec
    cfg.origins = [OriginSpec(i) for i in inside]
    new = repo.save_scenario(conn, cfg, net)
    repo.ensure_population(conn, new, net, cfg)
    out = run_alert_planner(conn, new, phone_reach=0.4, n_sirens=2, siren_radius=350.0)
    rows = {r["variant"][0]: r for r in out["rows"]}
    assert out["sirens"] and out["missed_by_phones"] > 0
    assert rows["A"]["never alerted"] > rows["B"]["never alerted"] >= rows["C"]["never alerted"] >= rows["D"]["never alerted"] == 0
    assert rows["B"]["by siren"] > 0 and rows["D"]["by phone"] == rows["D"]["people"]
    assert rows["A"]["people"] == rows["D"]["people"] == 150
    # alert channel is stored per person and queryable in SQL
    ch = an.alert_channel_summary(conn, rows["C"]["run"])
    assert set(ch["channel"]) <= {"PHONE", "SIREN", "NEIGHBOUR", "NONE"} and int(ch["people"].sum()) == 150
