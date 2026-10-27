"""Numerical checks of the formulas used in the paper (run: python -m scripts.verify_model).

Each check compares a closed-form expression with what the code computes, or tests an inequality that every run must
satisfy. The results are printed and written to scripts/results/verification.json, which the paper reads.

    V1  BPR travel time              t = t0 (1 + alpha r^beta), capacity C = max(2, round(0.25 w L)), t0 = L / v
    V2  haversine distance           against the closed form for one degree of latitude and an independent projection
    V3  reaction delay               U[0, d_max]: sample mean and variance against d_max / 2 and d_max^2 / 12
    V4  free-flow lower bound        every evacuated person: evacuation time >= departure + free-flow walking time to the stop
    V5  blast lower bound            caught >= #{ i in blast : delta_i + T_i > lead }, T_i = free-flow time to the nearest
                                     blast-boundary node (a person cannot be outside the circle earlier than that)
    V7  bottleneck discharge         everyone leaves one point and shortest-path routing sends all through the same roads:
                                     the k-th person arrives about T0 + k / q (the median of those arriving before the limit uses k = m/2), q = min over route roads of C_e / (t0_e (1 + alpha)),
                                     compared with the simulated shortest-path stress test
    V6  expected lower bound         E[bound] = N/|O| * sum over start nodes o in the blast of clip(1 - (lead - T_o)/d_max, 0, 1),
                                     compared with the mean of the realised bound over ten crowds (no simulation needed)
"""
import json  # write the report
import math  # square roots
import statistics as stats  # sample mean and variance
import tempfile  # throw-away database
from pathlib import Path  # file paths

import networkx as nx  # shortest paths

from config import settings  # strategies and results folder
from database import analytics as an  # hazard_summary
from database import repositories as repo  # scenario and population storage
from database.init_db import init_db  # fresh database
from geo import geographic_utils as geo  # distances
from scripts.compare_strategies import load_network  # the study areas
from simulation import drill  # scenario building
from simulation.congestion import CongestionState, bpr_travel_time  # congestion model
from simulation.experiments import run_scenario  # run one scenario with one strategy
from simulation.models import ModelParams  # model parameters
from tests.helpers import make_net  # tiny test networks

OUT = Path(__file__).resolve().parent / "results" / "verification.json"  # where the report goes


def v1_bpr() -> dict:  # travel time against the formula
    net = make_net([(1, 2, 100.0, 25)])  # one road of 100 m with capacity 25 (what a residential road of 100 m gets)
    edge = next(iter(net.edges.values()))  # that road
    out = []  # (occupancy, code value, formula value)
    for alpha, beta, occ in [(0.6, 2.0, 0), (0.6, 2.0, 10), (0.6, 2.0, 25), (1.2, 2.0, 10), (0.6, 4.0, 10), (0.3, 1.0, 20)]:
        p = ModelParams(bpr_alpha=alpha, bpr_beta=beta)  # parameters under test
        code = bpr_travel_time(edge, occ, edge.capacity, p)  # the model's value
        t0 = edge.length_m / 1.4  # free-flow time = length / speed
        formula = t0 * (1 + alpha * (occ / edge.capacity) ** beta)  # Eq. (BPR)
        out.append({"alpha": alpha, "beta": beta, "occupancy": occ, "code_s": code, "formula_s": formula})
    from geo.network_loader import osm_capacity  # capacity rule of the real maps
    caps = {hw: osm_capacity(hw, 100.0) for hw in ("footway", "residential", "tertiary", "primary")}  # 100 m of each class
    return {"cases": out, "max_abs_error_s": max(abs(c["code_s"] - c["formula_s"]) for c in out), "capacity_of_100_m": caps}


def v2_haversine() -> dict:  # distance function
    one_degree = geo.haversine_m(0.0, 0.0, 1.0, 0.0)  # one degree of latitude
    closed_form = 2 * math.pi * geo.EARTH_RADIUS_M / 360.0  # arc length of one degree on a sphere
    (la1, lo1), (la2, lo2) = (16.45605, 80.53816), (16.4318209, 80.5688069)  # campus and Mangalagiri centres
    k = math.cos(math.radians((la1 + la2) / 2))  # shrink of longitude at that latitude
    flat = geo.EARTH_RADIUS_M * math.radians(1) * math.hypot((la2 - la1), (lo2 - lo1) * k)  # equirectangular projection
    exact = geo.haversine_m(la1, lo1, la2, lo2)  # the model's distance
    return {"one_degree_m": one_degree, "closed_form_m": closed_form, "campus_to_mangalagiri_m": exact,
            "equirectangular_m": flat, "relative_difference": abs(exact - flat) / exact}


def v3_delay() -> dict:  # reaction delay distribution
    from simulation.agent import generate_population  # crowd generator
    net = make_net([(1, 2, 100.0, 50), (2, 3, 100.0, 50)])  # tiny network
    from tests.helpers import make_cfg  # tiny scenario
    cfg = make_cfg(net, [1], [(10, 3, 5000, 5000)], agents=20000, departure_delay_max_seconds=90.0)  # 20 000 people
    agents = generate_population(net, cfg)  # their delays (no hazard, so departure time = delay)
    d = [a.departure_time for a in agents]  # reaction delays
    return {"n": len(d), "sample_mean_s": stats.fmean(d), "expected_mean_s": 45.0, "sample_var": stats.variance(d), "expected_var": 90.0 ** 2 / 12}


def setup(conn, net, seed, lead, blast=150.0, strike=420.0, delay=90.0, agents=250):  # one bomb drill, as in compare_strategies
    starts_default, dests = drill.default_layout(net)  # start point and safe stops
    bomb = drill.default_bomb_node(net)  # the bomb at the map centre
    hz = [drill.make_hazard(net, bomb, blast, max(blast * 3.5, 200.0), strike, lead, 0)]  # one bomb
    starts = drill.residents_nodes(net, hz, dests)  # residents of the alert area
    cfg = drill.build_config(net, name=f"verify {seed}", strategy="SHORTEST_PATH", agents=agents, seed=seed, dt=5.0, limit=2400.0,
                             starts=starts, safe_nodes=dests, safe_cap=300, safe_thr=8, zones=[], danger_t=0.0, hazards=hz,
                             dist="even", delay=delay, spread=0.0)  # defaults of the paper
    sid = repo.save_scenario(conn, cfg, net)  # store the scenario
    repo.ensure_population(conn, sid, net, cfg)  # store the crowd
    return cfg, sid, hz[0], dests  # what the checks need


def free_flow_graph(net, speed=1.4):  # directed graph weighted by walking time
    g = nx.DiGraph()  # simple digraph is enough for shortest times
    for e in net.edges.values():  # every road
        t = e.length_m / speed  # free-flow time
        if not g.has_edge(e.source, e.target) or g[e.source][e.target]["t"] > t:  # keep the quickest parallel road
            g.add_edge(e.source, e.target, t=t)
    return g


def v4_v5(area: str, leads, seeds) -> dict:  # lower bounds against real runs
    res = {"area": area, "runs": []}  # report
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # throw-away database
        conn = init_db(str(Path(tmp) / "v.db"))  # fresh schema
        try:
            net = load_network(conn, area)  # the study area
            g = free_flow_graph(net)  # walking-time graph
            rev = g.reverse(copy=False)  # for searches towards a node
            for lead in leads:  # warning times
                for seed in seeds:  # crowds
                    cfg, sid, hz, dests = setup(conn, net, seed, lead)  # the drill
                    inside = {n for n, nd in net.nodes.items() if geo.haversine_m(nd.lat, nd.lon, hz["lat"], hz["lon"]) <= hz["blast_radius_m"]}  # nodes in the blast circle
                    boundary = [n for n in inside if any(v not in inside for v in g.successors(n))]  # inside nodes with a road leading out
                    to_boundary = nx.multi_source_dijkstra_path_length(rev, boundary, weight="t") if boundary else {}  # free-flow time from every node to the nearest boundary node
                    rows = conn.execute("SELECT agent_id, origin_node_id, departure_time_seconds FROM agents WHERE scenario_id=?", (sid,)).fetchall()
                    alert = hz["alert_time_s"]  # the simulated second the alert goes out
                    certain = sum(1 for r in rows if r["origin_node_id"] in inside
                                  and (r["departure_time_seconds"] - alert) + to_boundary.get(r["origin_node_id"], math.inf) > lead)  # still inside at the strike whatever happens
                    for strat in settings.ROUTING_STRATEGIES:  # every routing rule
                        rid = run_scenario(conn, sid, strat, label=f"verify:{strat}")  # simulate
                        caught = int(an.hazard_summary(conn, rid)["caught_in_blast"] or 0)  # simulated caught
                        out = conn.execute("SELECT agent_id, destination_id, evacuation_time_seconds, departure_time_seconds FROM agent_outcomes "
                                           "WHERE run_id=? AND final_state='EVACUATED'", (rid,)).fetchall()  # people who arrived
                        dnode = {d["destination_id"]: d["node_id"] for d in conn.execute("SELECT destination_id, node_id FROM destinations")}  # stop id -> node
                        origin = {r["agent_id"]: r["origin_node_id"] for r in rows}  # agent -> start node
                        slack = []  # evacuation time minus its lower bound
                        for o in out:  # each evacuated person
                            lb = o["departure_time_seconds"] + nx.dijkstra_path_length(g, origin[o["agent_id"]], dnode[o["destination_id"]], weight="t")
                            slack.append(o["evacuation_time_seconds"] - lb)  # must be >= 0
                        res["runs"].append({"lead_s": lead, "seed": seed, "strategy": strat, "caught": caught, "bound": certain,
                                            "bound_holds": caught >= certain, "evacuated": len(out), "min_slack_s": min(slack) if slack else None,
                                            "all_slack_nonnegative": all(s >= -1e-6 for s in slack)})
        finally:
            conn.close()
    r = res["runs"]
    res["n_runs"] = len(r)
    res["bound_holds_all"] = all(x["bound_holds"] for x in r)
    res["free_flow_bound_holds_all"] = all(x["all_slack_nonnegative"] for x in r)
    res["mean_bound_over_caught"] = (sum(x["bound"] for x in r) / max(1, sum(x["caught"] for x in r)))  # how tight the bound is
    res["min_slack_s"] = min((x["min_slack_s"] for x in r if x["min_slack_s"] is not None), default=None)
    return res


def v6_expected(area: str, leads, seeds) -> dict:  # expectation of the blast lower bound against its realised mean
    res = {"area": area, "rows": []}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = init_db(str(Path(tmp) / "v6.db"))
        try:
            net = load_network(conn, area)
            g = free_flow_graph(net)
            rev = g.reverse(copy=False)
            for lead in leads:
                realised, expected = [], None
                for seed in seeds:
                    cfg, sid, hz, dests = setup(conn, net, seed, lead)
                    inside = {n for n, nd in net.nodes.items() if geo.haversine_m(nd.lat, nd.lon, hz["lat"], hz["lon"]) <= hz["blast_radius_m"]}
                    boundary = [n for n in inside if any(v not in inside for v in g.successors(n))]
                    tb = nx.multi_source_dijkstra_path_length(rev, boundary, weight="t") if boundary else {}
                    rows = conn.execute("SELECT origin_node_id, departure_time_seconds FROM agents WHERE scenario_id=?", (sid,)).fetchall()
                    alert = hz["alert_time_s"]
                    realised.append(sum(1 for r in rows if r["origin_node_id"] in inside and (r["departure_time_seconds"] - alert) + tb.get(r["origin_node_id"], math.inf) > lead))
                    if expected is None:  # the start nodes do not depend on the seed
                        origins = sorted({r["origin_node_id"] for r in rows}) if False else None
                        starts = drill.residents_nodes(net, [hz], dests)
                        dmax = float(cfg.params.departure_delay_max_seconds)
                        tot = 0.0
                        for o in starts:
                            if o in inside:
                                x = 1.0 - (lead - tb.get(o, math.inf)) / dmax  # P(delay > lead - T_o) for a uniform delay on [0, d_max]
                                tot += min(1.0, max(0.0, x))
                        expected = cfg.agent_count / len(starts) * tot
                res["rows"].append({"lead_s": lead, "expected_bound": expected, "mean_realised_bound": stats.fmean(realised), "seeds": len(realised)})
        finally:
            conn.close()
    return res


def v7_bottleneck() -> dict:  # discharge-time prediction for the shortest-path stress test
    res = {}
    alpha = 0.6  # the model's BPR alpha (a full road has r = 1)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = init_db(str(Path(tmp) / "v7.db"))
        try:
            for area in ("campus", "mangalagiri"):
                net = load_network(conn, area)
                starts, dests = drill.default_layout(net)
                g = nx.DiGraph()
                for e in net.edges.values():
                    t0 = e.length_m / 1.4
                    if not g.has_edge(e.source, e.target) or g[e.source][e.target]["t"] > t0:
                        g.add_edge(e.source, e.target, t=t0, cap=e.capacity)
                t_walk, stop = min((nx.dijkstra_path_length(g, starts[0], d, weight="t"), d) for d in dests)
                path = nx.dijkstra_path(g, starts[0], stop, weight="t")
                q = min(g[u][v]["cap"] / (g[u][v]["t"] * (1 + alpha)) for u, v in zip(path, path[1:]))  # persons per second through the narrowest road
                sim = json.loads((OUT.parent / "areas" / f"{area}_stress.json").read_text(encoding="utf-8"))
                n, limit = sim["agents"], sim["limit_s"]
                arrived = min(float(n), q * max(0.0, limit - t_walk))  # people who can pass the narrowest road before the time limit
                res[area] = {"free_flow_walk_s": t_walk, "route_edges": len(path) - 1, "q_min_per_s": q,
                             "predicted_median_s": t_walk + (arrived / 2) / q, "predicted_share_by_limit_pct": 100.0 * arrived / n,
                             "simulated_median_s": sim["strategies"]["SHORTEST_PATH"]["median_since_alert_s"]["mean"],
                             "simulated_share_by_limit_pct": sim["strategies"]["SHORTEST_PATH"]["evacuated_pct"]["mean"]}
        finally:
            conn.close()
    return res


def main() -> None:  # run every check
    import sys  # command line
    if len(sys.argv) > 1 and sys.argv[1] == "v7":  # only the bottleneck prediction
        report = json.loads(OUT.read_text(encoding="utf-8"))
        report["V7_bottleneck"] = v7_bottleneck()
        OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["V7_bottleneck"], indent=1))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "v6":  # only the cheap expectation check, keep the other results
        report = json.loads(OUT.read_text(encoding="utf-8"))
        report["V6_expected_mangalagiri"] = v6_expected("mangalagiri", [30, 60, 120, 180], range(1, 11))
        report["V6_expected_campus"] = v6_expected("campus", [60, 180, 300], range(1, 11))
        OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in report if k.startswith("V6")}, indent=1))
        return
    report = {"V1_bpr": v1_bpr(), "V2_haversine": v2_haversine(), "V3_delay": v3_delay(),
              "V4_V5_mangalagiri": v4_v5("mangalagiri", [30, 60, 120], range(1, 6)),
              "V4_V5_campus": v4_v5("campus", [60, 180], range(1, 4)),
              "V6_expected_mangalagiri": v6_expected("mangalagiri", [30, 60, 120, 180], range(1, 11)),
              "V6_expected_campus": v6_expected("campus", [60, 180, 300], range(1, 11)), "V7_bottleneck": v7_bottleneck()}
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")  # save for the paper
    print(json.dumps({k: (v if not isinstance(v, dict) or "runs" not in v else {kk: vv for kk, vv in v.items() if kk != "runs"}) for k, v in report.items()}, indent=2))


if __name__ == "__main__":  # run only when called as a script
    main()
