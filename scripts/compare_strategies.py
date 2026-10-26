"""Run all three routing strategies on identical bomb drills and print one comparison table.

    python -m scripts.compare_strategies --seeds 1 2 3 4 5 6 7 8 9 10 --agents 250 [--map campus|grid] [--lead 180]

Every strategy sees the SAME crowd (same scenario row, same seed), so the only difference is the routing rule.
Results are read back from SQLite (a throw-away database), then summarised as mean +- standard deviation over the
seeds and written to data/strategy_comparison.json, which the website builder reads.
"""
import argparse  # command-line options
import json  # write the summary file
import statistics as stats  # mean and standard deviation
import tempfile  # throw-away database folder
from pathlib import Path  # file paths

from config import settings  # strategies list and cache folder
from database import analytics as an  # hazard_summary: people caught in the blast
from database import repositories as repo  # save networks / scenarios, load runs
from database.init_db import init_db  # create a fresh database
from geo import network_cache as cache  # cached OpenStreetMap areas
from geo import network_loader as loader  # read a cached OpenStreetMap network
from geo.synthetic_network import build_synthetic_network  # offline grid
from simulation import drill  # scenario building shared with the dashboard
from simulation.experiments import run_scenario  # run one scenario with one strategy


def load_network(conn, which: str):  # the road network to use
    if which == "grid":  # offline labelled grid
        return repo.save_network(conn, build_synthetic_network())  # store and return it with database ids
    cached = cache.list_cached()  # real maps already downloaded
    if not cached:  # nothing to use
        raise SystemExit("No cached OpenStreetMap network: run `python -m scripts.download_network` or use --map grid")
    best = max(cached, key=lambda m: m.get("nodes", 0))  # the largest cached area
    return repo.save_network(conn, loader.load_cached_network(best["key"]))  # parse it and store it in SQLite


def one_seed(conn, net, seed: int, agents: int, blast: float, lead: float, strike: float, delay: float, bomb_on: bool = True,
             limit: float = 2400.0) -> dict:
    """Build one bomb drill, run it with each strategy, and return the outcome of each."""
    starts_default, dests = drill.default_layout(net)  # hypothetical start point and safe stops
    if bomb_on:  # bomb drill: residents of the alert area flee a strike
        bomb = drill.default_bomb_node(net)  # the bomb sits in the middle of the map
        hz = [drill.make_hazard(net, bomb, blast, max(blast * 3.5, 200.0), strike, lead, 0)]  # one bomb
        starts = drill.residents_nodes(net, hz, dests)  # people live inside the alert area
    else:  # congestion stress test: one crowd, no bomb, everyone leaves at once
        hz, starts = [], starts_default  # no hazard; the default start point
    cfg = drill.build_config(net, name=f"compare seed {seed} lead {lead:.0f}", strategy="SHORTEST_PATH", agents=agents, seed=seed,
                             dt=5.0, limit=limit, starts=starts, safe_nodes=dests, safe_cap=300, safe_thr=8, zones=[],
                             danger_t=0.0, hazards=hz, dist="even", delay=delay, spread=0.0)  # same defaults as the dashboard
    sid = repo.save_scenario(conn, cfg, net)  # one scenario row shared by all strategies
    repo.ensure_population(conn, sid, net, cfg)  # one crowd shared by all strategies
    out = {}  # strategy -> outcome
    for strat in settings.ROUTING_STRATEGIES:  # every routing rule
        rid = run_scenario(conn, sid, strat, label=f"compare:{strat}")  # simulate and store
        run = repo.get_run(conn, rid)  # the stored summary row
        hzs = an.hazard_summary(conn, rid)  # alerts, caught in the blast, safe before the strike
        caught = int(hzs["caught_in_blast"] or 0)  # people inside the blast at the strike
        total = int(run["total_agents"])  # crowd size
        out[strat] = {"evacuated": int(run["evacuated_agents"]), "caught": caught,
                      "unable": int(run["failed_agents"]) - caught,  # no route / safe stops full or lost
                      "unresolved": int(run["unresolved_agents"]), "total": total,
                      "safe_before_strike": int(hzs["safe_before_strike"] or 0),
                      "median_s": run["median_evacuation_time_seconds"]}  # one row of numbers
    return out  # results for this seed


def summarise(rows: list) -> dict:  # mean and spread over the seeds
    def ms(vals):  # mean and sample standard deviation
        return {"mean": round(stats.fmean(vals), 2), "sd": round(stats.stdev(vals), 2) if len(vals) > 1 else 0.0,
                "min": round(min(vals), 2), "max": round(max(vals), 2)}
    summary = {}  # strategy -> metrics
    for strat in settings.ROUTING_STRATEGIES:  # every routing rule
        r = [x[strat] for x in rows]  # that strategy's result for every seed
        total = r[0]["total"]  # crowd size
        med = [x["median_s"] for x in r if x["median_s"] is not None]  # median walking times that exist
        summary[strat] = {"safe_pct": ms([100.0 * x["safe_before_strike"] / total for x in r]),  # reached safety before the strike
                          "evacuated_pct": ms([100.0 * x["evacuated"] / total for x in r]),  # reached a safe stop at any time
                          "caught_pct": ms([100.0 * x["caught"] / total for x in r]),  # inside the blast at the strike
                          "caught": ms([float(x["caught"]) for x in r]),  # same, as a head count
                          "unable_pct": ms([100.0 * x["unable"] / total for x in r]),  # no route / full or lost safe stops
                          "median_s": ms(med) if med else None}  # median walking time
    return summary  # the table


def main() -> None:  # command-line entry point
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # show the docstring
    ap.add_argument("--seeds", type=int, nargs="+", default=list(range(1, 11)))  # random crowds to average over
    ap.add_argument("--agents", type=int, default=250)  # people per drill
    ap.add_argument("--map", choices=["campus", "grid"], default="campus")  # which road network
    ap.add_argument("--blast", type=float, default=150.0)  # blast radius in metres
    ap.add_argument("--lead", type=float, default=180.0)  # warning lead time in seconds
    ap.add_argument("--strike", type=float, default=420.0)  # strike time in seconds
    ap.add_argument("--delay", type=float, default=90.0)  # longest reaction delay in seconds
    ap.add_argument("--no-bomb", action="store_true")  # congestion stress test instead of a bomb drill
    ap.add_argument("--limit", type=float, default=2400.0)  # simulated seconds before the run stops
    ap.add_argument("--out", default=str(settings.DATA_DIR / "strategy_comparison.json"))  # summary file
    args = ap.parse_args()  # read the options
    with tempfile.TemporaryDirectory() as tmp:  # a throw-away database so the project database stays clean
        conn = init_db(str(Path(tmp) / "compare.db"))  # fresh schema
        try:  # always close the connection
            net = load_network(conn, args.map)  # the road network
            rows = [one_seed(conn, net, s, args.agents, args.blast, args.lead, args.strike, args.delay, not args.no_bomb, args.limit) for s in args.seeds]  # run every seed
        finally:  # close the connection
            conn.close()  # release the file
    summary = summarise(rows)  # mean +- sd over the seeds
    payload = {"map": args.map, "bomb": not args.no_bomb, "agents": args.agents, "seeds": args.seeds, "blast_m": args.blast, "lead_s": args.lead,
               "strike_s": args.strike, "reaction_delay_max_s": args.delay, "strategies": summary}  # what the site reads
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
