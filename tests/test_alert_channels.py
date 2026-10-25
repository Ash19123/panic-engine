"""Alert channels: phone / siren / neighbour / nobody, the planner experiment and the siren recommender."""
from collections import Counter  # count items

from database import analytics as an  # SQL analytics helpers
from database import repositories as repo  # save and load helpers
from simulation.agent import generate_population  # crowd generator
from simulation.alerting import recommend_sirens  # siren placement
from simulation.engine import EvacuationSimulation  # the engine
from simulation.experiments import run_alert_planner, run_scenario  # experiment helpers
from tests.helpers import make_cfg, make_net  # test network and scenario builders


def line(n=9, step=100):  # a straight line of roads
    return make_net([(i, i + 1, step, 60) for i in range(1, n)])  # nodes 1..n, 100 m apart


def hz(net, node=1, blast=60.0, alert=2000.0, strike=400.0, alert_at=20.0):  # a bomb definition for tests
    nd = net.nodes[node]  # where it goes off
    return {"name": "B", "node": node, "lat": nd.lat, "lon": nd.lon, "blast_radius_m": blast,  # identity, place, blast radius
            "alert_radius_m": alert, "strike_time_s": strike, "alert_time_s": alert_at}  # alert radius and times


def pop(net, channels, agents=400, seed=3, origins=(4, 5, 6, 7)):  # a crowd with given alert channels
    cfg = make_cfg(net, list(origins), [(10, 9, 999, 99)], agents=agents, seed=seed, hazards=[hz(net)],  # scenario (part 1)
                   alert_channels=channels, departure_delay_max_seconds=30)  # channels and reaction delays (part 2)
    return cfg, generate_population(net, cfg)  # the scenario and its crowd


def test_no_channels_means_everybody_gets_the_phone_alert():  # the default behaviour
    net = line()  # test network
    _, agents = pop(net, {})  # no channels configured
    assert {a.alert_channel for a in agents} == {"PHONE"} and all(a.alert_time == 20.0 for a in agents)  # everyone by phone at 20 s


def test_phone_reach_share_and_unreached_people_never_depart():  # a partial phone share
    net = line()  # test network
    cfg, agents = pop(net, {"phone_reach": 0.4})  # 40% have a phone
    c = Counter(a.alert_channel for a in agents)  # people per channel
    assert 0.3 < c["PHONE"] / len(agents) < 0.5 and c["PHONE"] + c["NONE"] == len(agents)  # about 40% by phone, the rest unreached
    for a in agents:  # each person
        if a.alert_channel == "NONE":  # unreached
            assert a.alert_time is None and a.departure_time > cfg.time_limit_seconds  # never alerted, never leaves
        else:  # reached
            assert a.alert_time >= 20.0 and a.departure_time >= a.alert_time  # leaves after the alert


def test_sirens_reach_people_without_phones_and_neighbours_relay_in_waves():  # siren and relay timing
    net = line()  # test network
    nd = net.nodes[4]  # a siren at node 4
    siren = {"node": 4, "lat": nd.lat, "lon": nd.lon, "radius_m": 20.0, "delay_s": 10.0}  # heard within 20 m
    _, agents = pop(net, {"phone_reach": 0.0, "sirens": [siren], "neighbour_radius_m": 60.0, "neighbour_delay_s": 30.0})  # nobody has a phone
    by_node = {}  # one agent per start node
    for a in agents:  # each person
        by_node.setdefault(a.origin_node, a)  # keep the first at each node
    assert by_node[4].alert_channel == "SIREN" and by_node[4].alert_time == 30.0         # alert 20 s + siren delay 10 s
    assert by_node[5].alert_channel == "NEIGHBOUR" and by_node[5].alert_time == 60.0     # one hop later
    assert by_node[6].alert_time == 90.0 and by_node[7].alert_time == 120.0              # relay chain


def test_channel_variants_keep_the_same_people_and_reaction_delays():  # fair comparison between variants
    net = line()  # test network
    _, a = pop(net, {"phone_reach": 0.3})  # variant 1
    _, b = pop(net, {"phone_reach": 0.9, "neighbour_radius_m": 60.0})  # variant 2
    assert [x.origin_node for x in a] == [x.origin_node for x in b]                      # same places
    both = [(x, y) for x, y in zip(a, b) if x.alert_time is not None and y.alert_time is not None  # people alerted in both variants (part 1)
            and x.alert_channel == y.alert_channel == "PHONE"]  # by phone in both (part 2)
    assert both and all(abs((x.departure_time - x.alert_time) - (y.departure_time - y.alert_time)) < 1e-2 for x, y in both)  # same reaction delay


def test_unalerted_people_inside_the_blast_are_caught_and_alerted_ones_get_away():  # outcome by channel
    net = line()  # test network
    cfg = make_cfg(net, [2, 8], [(10, 9, 999, 99)], agents=60, seed=5, hazards=[hz(net, blast=120.0, strike=200.0)],  # scenario (part 1)
                   alert_channels={"phone_reach": 0.5}, departure_delay_max_seconds=20)  # half have a phone (part 2)
    res = EvacuationSimulation(net, cfg).run()  # run it
    kinds = Counter(ev[2] for ev in res.events)  # event counts
    n_phone = sum(1 for a in res.agents if a.alert_channel == "PHONE")  # people alerted by phone
    assert kinds["ALERT_SENT"] == n_phone < 60  # alerts were sent only to them
    m = res.metrics  # summary numbers
    assert m["evacuated"] + m["failed"] + m["unresolved"] == m["total_agents"]  # nobody is lost from the accounting
    near_unalerted = [a for a in res.agents if a.alert_channel == "NONE" and a.origin_node == 2]  # unwarned people next to the bomb
    assert near_unalerted and all(a.state.value == "UNABLE_TO_EVACUATE" for a in near_unalerted)     # inside the blast


def test_recommender_picks_the_biggest_gap_and_avoids_the_blast_zone():  # siren placement
    net = line()  # test network
    people = [8] * 30 + [3] * 5  # 30 unwarned people at node 8, 5 at node 3
    sites = recommend_sirens(net, people, radius_m=60.0, k=2)  # two sirens
    nd = net.nodes[8]  # the big group's node
    import geo.geographic_utils as geo  # distance helper
    first = net.nodes[sites[0]["node"]]  # the first siren's node
    assert sites[0]["covers"] == 30 and geo.haversine_m(first.lat, first.lon, nd.lat, nd.lon) <= 60.0  # it covers the big group
    banned = recommend_sirens(net, people, radius_m=60.0, k=1, avoid=[(nd.lat, nd.lon, 70.0)])  # forbid sirens near that group
    assert all(geo.haversine_m(net.nodes[s["node"]].lat, net.nodes[s["node"]].lon, nd.lat, nd.lon) > 70.0 for s in banned)  # none is placed there


def test_planner_end_to_end_in_sql(seeded):  # the four-variant planner
    conn, sid = seeded  # database with the demo scenario
    cfg = repo.load_scenario_config(conn, sid)  # the scenario
    net = repo.load_network(conn, cfg.network_id)  # its network
    ids = sorted(net.nodes)  # node ids
    centre = net.nodes[ids[35]]  # a node near the middle
    cfg.name = "Bomb drill planner"  # rename it
    cfg.agent_count = 150  # 150 people
    cfg.params.departure_delay_max_seconds = 30  # short reaction delays
    cfg.params.hazards = [{"name": "B", "node": ids[35], "lat": centre.lat, "lon": centre.lon, "blast_radius_m": 140.0,  # one bomb (part 1)
                           "alert_radius_m": 520.0, "strike_time_s": 420.0, "alert_time_s": 200.0}]  # (part 2)
    import geo.geographic_utils as geo  # distance helper
    inside = [i for i in ids if geo.haversine_m(net.nodes[i].lat, net.nodes[i].lon, centre.lat, centre.lon) <= 520]  # nodes inside the alert area
    from simulation.models import OriginSpec  # start-place class
    cfg.origins = [OriginSpec(i) for i in inside]  # people start anywhere inside it
    new = repo.save_scenario(conn, cfg, net)  # store the scenario
    repo.ensure_population(conn, new, net, cfg)  # generate the crowd
    out = run_alert_planner(conn, new, phone_reach=0.4, n_sirens=2, siren_radius=350.0)  # run the planner
    rows = {r["variant"][0]: r for r in out["rows"]}  # results by variant letter
    assert out["sirens"] and out["missed_by_phones"] > 0  # sirens were recommended for the people phones missed
    assert rows["A"]["never alerted"] > rows["B"]["never alerted"] >= rows["C"]["never alerted"] >= rows["D"]["never alerted"] == 0  # each added channel reaches more people
    assert rows["B"]["by siren"] > 0 and rows["D"]["by phone"] == rows["D"]["people"]  # sirens used in B; everyone by phone in D
    assert rows["A"]["people"] == rows["D"]["people"] == 150  # same crowd in every variant
    # alert channel is stored per person and queryable in SQL
    ch = an.alert_channel_summary(conn, rows["C"]["run"])  # people per channel in variant C
    assert set(ch["channel"]) <= {"PHONE", "SIREN", "NEIGHBOUR", "NONE"} and int(ch["people"].sum()) == 150  # valid channels, all 150 people
