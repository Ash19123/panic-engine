"""Run all three routing strategies on identical drills and print one comparison table.

    python -m scripts.compare_strategies --seeds 1 2 3 4 5 6 7 8 9 10 --agents 250 [--map campus|mangalagiri|grid] [--lead 180]

Scenarios (all use the SAME crowd for the three strategies, so the only difference is the routing rule):
    bomb drill      default: a bomb at the map centre, residents of the alert area flee the strike
    crowd stress    --no-bomb: everyone leaves one start point at t = 0, nobody is in danger
    road closure    --no-bomb --closure-radius 150 --closure-time 300: roads inside a circle on the first leg of the
                    shortest route close while people are already walking
    alert channels  --phone-reach 0.5 [--neighbour-radius 50]: only part of the crowd hears the alert on a phone
Results are read back from SQLite (a throw-away database), summarised as mean +- standard deviation over the seeds,
and written (together with every single seed) to the JSON file given by --out, which the website and paper read.
"""
import argparse  # command-line options
import dataclasses  # copy the model settings with a few values changed
import json  # write the summary file
import statistics as stats  # mean and standard deviation
import tempfile  # throw-away database folder
from pathlib import Path  # file paths

import networkx as nx  # shortest paths, to place the road closure

from config import settings  # strategies list and cache folder
from database import analytics as an  # hazard_summary: people caught in the blast
from database import repositories as repo  # save networks / scenarios, load runs
from database.init_db import init_db  # create a fresh database
from geo import geographic_utils as geo  # distances between map points
from geo import network_cache as cache  # cached OpenStreetMap areas
from geo import network_loader as loader  # read a cached OpenStreetMap network
from geo.synthetic_network import build_synthetic_network  # offline grid
from simulation import drill  # scenario building shared with the dashboard
from simulation.experiments import run_scenario  # run one scenario with one strategy

AREAS = {"campus": (16.45605, 80.53816), "mangalagiri": (16.4318209, 80.5688069)}  # named study areas (latitude, longitude of the map centre)


def load_network(conn, which: str):  # the road network to use
    if which == "grid":  # offline labelled grid
        return repo.save_network(conn, build_synthetic_network())  # store and return it with database ids
    cached = cache.list_cached()  # real maps already downloaded
    if not cached:  # nothing to use
        raise SystemExit("No cached OpenStreetMap network: run `python -m scripts.download_network` or use --map grid")
    lat, lon = AREAS[which]  # where the requested area is centred
    best = min(cached, key=lambda m: geo.haversine_m(lat, lon, m["lat"], m["lon"]))  # the cached map centred nearest to it
    if geo.haversine_m(lat, lon, best["lat"], best["lon"]) > 50.0:  # nothing centred there
        raise SystemExit(f"No cached map centred on {which}: run python -m scripts.download_network --lat {lat} --lon {lon}")
    return repo.save_network(conn, loader.load_cached_network(best["key"]))  # parse it and store it in SQLite


def closure_zone(net, start: int, stops, radius: float) -> dict:
    """A circular danger zone centred on the middle of the free-flow shortest route from the start to the nearest safe stop."""
    g = net.forward_graph()  # directed walking graph
    best = min((nx.single_source_dijkstra(g, start, s, weight="length") for s in stops), key=lambda r: r[0])  # nearest stop by walking distance
    path = best[1]  # node ids of that route
    mid = net.nodes[path[len(path) // 2]]  # the node half-way along it
    return {"lat": mid.lat, "lon": mid.lon, "radius_m": float(radius)}  # one danger zone


def one_seed(conn, net, seed: int, agents: int, blast: float, lead: float, strike: float, delay: float, bomb_on: bool = True,
             limit: float = 2400.0, walk: float = 1.4, alpha: float = 0.6, beta: float = 2.0, safe_cap: int = 300,
             closure_radius: float = 0.0, closure_time: float = 300.0, phone_reach: float = 1.0,
             neighbour_radius: float = 0.0, neighbour_delay: float = 30.0, channels_on: bool = False) -> dict:
    """Build one drill, run it with each strategy, and return the outcome of each."""
    starts_default, dests = drill.default_layout(net)  # hypothetical start point and safe stops
    if bomb_on:  # bomb drill: residents of the alert area flee a strike
        bomb = drill.default_bomb_node(net)  # the bomb sits in the middle of the map
        hz = [drill.make_hazard(net, bomb, blast, max(blast * 3.5, 200.0), strike, lead, 0)]  # one bomb
        starts = drill.residents_nodes(net, hz, dests)  # people live inside the alert area
    else:  # no bomb: everyone leaves at once from the default start point
        hz, starts = [], starts_default  # no hazard; the default start point
    zones = [closure_zone(net, starts[0], dests, closure_radius)] if closure_radius > 0 else []  # optional road closure
    channels = {}  # how the alert reaches people (empty = everyone is reached by phone at once)
    if channels_on or phone_reach < 1.0 or neighbour_radius > 0:  # a channel scenario
        channels = {"phone_reach": phone_reach, "phone_delay_max_s": 15.0, "sirens": [],
                    "neighbour_radius_m": neighbour_radius, "neighbour_delay_s": neighbour_delay}  # phone share, relay between neighbours
    cfg = drill.build_config(net, name=f"compare seed {seed} lead {lead:.0f}", strategy="SHORTEST_PATH", agents=agents, seed=seed,
                             dt=5.0, limit=limit, starts=starts, safe_nodes=dests, safe_cap=safe_cap, safe_thr=8, zones=zones,
                             danger_t=closure_time, hazards=hz, dist="even", delay=delay, spread=0.0, walking_speed=walk,
                             alert_channels=channels)  # same defaults as the dashboard
    cfg.params = dataclasses.replace(cfg.params, bpr_alpha=alpha, bpr_beta=beta)  # congestion constants under test
    sid = repo.save_scenario(conn, cfg, net)  # one scenario row shared by all strategies
    repo.ensure_population(conn, sid, net, cfg)  # one crowd shared by all strategies
    offset = float(max(0.0, strike - lead)) if bomb_on else 0.0  # the alert goes out at this simulated second
    out = {}  # strategy -> outcome
    for strat in settings.ROUTING_STRATEGIES:  # every routing rule
        rid = run_scenario(conn, sid, strat, label=f"compare:{strat}")  # simulate and store
        run = repo.get_run(conn, rid)  # the stored summary row
        hzs = an.hazard_summary(conn, rid)  # alerts, caught in the blast, safe before the strike
        caught = int(hzs["caught_in_blast"] or 0)  # people inside the blast at the strike
        total = int(run["total_agents"])  # crowd size
        med = run["median_evacuation_time_seconds"]  # median over people who arrived, counted from simulated second 0
        out[strat] = {"evacuated": int(run["evacuated_agents"]), "caught": caught,
                      "unable": int(run["failed_agents"]) - caught,  # no route / safe stops full or lost
                      "unresolved": int(run["unresolved_agents"]), "total": total,
                      "safe_before_strike": int(hzs["safe_before_strike"] or 0),
                      "median_s": med, "median_since_alert_s": (med - offset) if med is not None else None,  # counted from the alert
                      "p95_since_alert_s": (run["percentile_95_evacuation_time_seconds"] - offset) if run["percentile_95_evacuation_time_seconds"] is not None else None}
    return out  # results for this seed


def summarise(rows: list) -> dict:  # mean and spread over the seeds
    def ms(vals):  # mean and sample standard deviation
        return {"mean": round(stats.fmean(vals), 4), "sd": round(stats.stdev(vals), 4) if len(vals) > 1 else 0.0,
                "min": round(min(vals), 4), "max": round(max(vals), 4)}
    summary = {}  # strategy -> metrics
    for strat in settings.ROUTING_STRATEGIES:  # every routing rule
        r = [x[strat] for x in rows]  # that strategy's result for every seed
        total = r[0]["total"]  # crowd size
        med = [x["median_s"] for x in r if x["median_s"] is not None]  # median walking times that exist
        meda = [x["median_since_alert_s"] for x in r if x["median_since_alert_s"] is not None]  # the same, counted from the alert
        p95 = [x["p95_since_alert_s"] for x in r if x["p95_since_alert_s"] is not None]  # 95th percentile, counted from the alert
        summary[strat] = {"safe_pct": ms([100.0 * x["safe_before_strike"] / total for x in r]),  # reached safety before the strike
                          "evacuated_pct": ms([100.0 * x["evacuated"] / total for x in r]),  # reached a safe stop at any time
                          "caught_pct": ms([100.0 * x["caught"] / total for x in r]),  # inside the blast at the strike
                          "caught": ms([float(x["caught"]) for x in r]),  # same, as a head count
                          "unable_pct": ms([100.0 * x["unable"] / total for x in r]),  # no route / full or lost safe stops
                          "median_s": ms(med) if med else None,  # median walking time from simulated second 0
                          "median_since_alert_s": ms(meda) if meda else None,  # median walking time from the alert
                          "p95_since_alert_s": ms(p95) if p95 else None}  # 95th percentile from the alert
    return summary  # the table


def main() -> None:  # command-line entry point
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # show the docstring
    ap.add_argument("--seeds", type=int, nargs="+", default=list(range(1, 11)))  # random crowds to average over
    ap.add_argument("--agents", type=int, default=250)  # people per drill
    ap.add_argument("--map", choices=["campus", "mangalagiri", "grid"], default="campus")  # which road network
    ap.add_argument("--blast", type=float, default=150.0)  # blast radius in metres
    ap.add_argument("--lead", type=float, default=180.0)  # warning lead time in seconds
    ap.add_argument("--strike", type=float, default=420.0)  # strike time in seconds
    ap.add_argument("--delay", type=float, default=90.0)  # longest reaction delay in seconds
    ap.add_argument("--no-bomb", action="store_true")  # no hazard: crowd stress test or road-closure scenario
    ap.add_argument("--limit", type=float, default=2400.0)  # simulated seconds before the run stops
    ap.add_argument("--walk", type=float, default=1.4)  # walking speed in m/s
    ap.add_argument("--alpha", type=float, default=0.6)  # BPR alpha
    ap.add_argument("--beta", type=float, default=2.0)  # BPR beta
    ap.add_argument("--safe-cap", type=int, default=300)  # people each safe stop can hold
    ap.add_argument("--closure-radius", type=float, default=0.0)  # metres; > 0 adds the road-closure scenario
    ap.add_argument("--closure-time", type=float, default=300.0)  # simulated second at which those roads close
    ap.add_argument("--phone-reach", type=float, default=1.0)  # share of the crowd with a phone at hand
    ap.add_argument("--neighbour-radius", type=float, default=0.0)  # metres within which people tell each other
    ap.add_argument("--channels", action="store_true")  # use the alert-channel model even when everyone has a phone
    ap.add_argument("--neighbour-delay", type=float, default=30.0)  # seconds for each hop of that relay
    ap.add_argument("--out", default=str(settings.DATA_DIR / "strategy_comparison.json"))  # summary file
    args = ap.parse_args()  # read the options
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # a throw-away database so the project database stays clean
        conn = init_db(str(Path(tmp) / "compare.db"))  # fresh schema
        try:  # always close the connection
            net = load_network(conn, args.map)  # the road network
            rows = [one_seed(conn, net, s, args.agents, args.blast, args.lead, args.strike, args.delay, not args.no_bomb, args.limit,
                             args.walk, args.alpha, args.beta, args.safe_cap, args.closure_radius, args.closure_time,
                             args.phone_reach, args.neighbour_radius, args.neighbour_delay, args.channels) for s in args.seeds]  # run every seed
            nodes, edges = len(net.nodes), len(net.edges)  # size of the map
        finally:  # close the connection
            conn.close()  # release the file
    summary = summarise(rows)  # mean +- sd over the seeds
    payload = {"map": args.map, "nodes": nodes, "edges": edges, "bomb": not args.no_bomb, "agents": args.agents, "seeds": args.seeds,
               "blast_m": args.blast, "lead_s": args.lead, "strike_s": args.strike, "reaction_delay_max_s": args.delay,
               "limit_s": args.limit, "walk_mps": args.walk, "bpr_alpha": args.alpha, "bpr_beta": args.beta, "safe_cap": args.safe_cap,
               "closure_radius_m": args.closure_radius, "closure_time_s": args.closure_time, "phone_reach": args.phone_reach,
               "neighbour_radius_m": args.neighbour_radius, "neighbour_delay_s": args.neighbour_delay, "channels": args.channels,
               "strategies": summary, "per_seed": rows}  # what the site and the paper read
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)  # make sure the folder exists
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")  # write the summary
    print(f"map={args.map} agents={args.agents} seeds={len(args.seeds)} lead={args.lead:.0f}s blast={args.blast:.0f}m")  # header
    print(f"{'strategy':18} {'safe %':>14} {'caught %':>14} {'caught n':>10} {'unable %':>10} {'median s':>10}")  # column titles
    for strat, m in summary.items():  # one row per strategy
        med = m["median_s"]["mean"] if m["median_s"] else float("nan")  # median walking time
        print(f"{strat:18} {m['safe_pct']['mean']:6.1f} +-{m['safe_pct']['sd']:5.1f} {m['caught_pct']['mean']:6.1f} +-{m['caught_pct']['sd']:5.1f} "
              f"{m['caught']['mean']:10.1f} {m['unable_pct']['mean']:10.1f} {med:10.1f}")  # formatted numbers
    print(f"written to {args.out}")  # where the summary went


if __name__ == "__main__":  # run only when called as a script
    main()
