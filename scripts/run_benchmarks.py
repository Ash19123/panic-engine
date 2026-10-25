"""Reproducible evaluation matrix on the synthetic network.

    python -m scripts.run_benchmarks --seeds 1 2 3 --agents 300 [--db PATH]

Scenarios: normal, one blocked road, one unavailable destination, reduced destination
throughput, increased population, uneven origin distribution, multiple bottlenecks.
Each is run with every routing strategy and every seed. Everything is stored in SQLite and the
printed table / CSV is read back FROM SQL - no numbers are written by hand.
"""
import argparse  # command-line options
import csv  # write the results file
from pathlib import Path  # file paths

from config import settings  # strategies list and default output folder
from database import analytics as an  # SQL analytics helpers
from database import repositories as repo  # load scenarios
from database.init_db import init_db  # open or create the database
from database.seed_data import create_demo_scenario  # build the base scenario
from simulation import experiments as ex  # run scenarios and variants

SUMMARY_SQL = """
SELECT s.scenario_name, r.routing_strategy, r.random_seed, r.total_agents, r.evacuated_agents, r.unresolved_agents,
       r.failed_agents, r.completion_rate, r.median_evacuation_time_seconds, r.percentile_95_evacuation_time_seconds,
       r.total_waiting_time_seconds, (SELECT COUNT(*) FROM bottleneck_reports b WHERE b.run_id = r.run_id) AS bottlenecks,
       r.run_id
FROM simulation_runs r JOIN scenarios s USING (scenario_id)
WHERE r.label LIKE 'bench:%' AND r.status = 'COMPLETED' ORDER BY s.scenario_name, r.random_seed, r.routing_strategy
"""  # SQL text: one row per benchmark run, joined to its scenario name, with a bottleneck count


def main() -> None:  # command-line entry point
    settings.configure_logging()  # set up logging
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # show the docstring as help
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])  # random seeds to run
    ap.add_argument("--agents", type=int, default=300)  # people per scenario
    ap.add_argument("--db", default=None)  # optional database path
    ap.add_argument("--out", default=str(settings.DATA_DIR / "benchmark_results.csv"))  # CSV output file
    args = ap.parse_args()  # read the options

    conn = init_db(args.db)  # open or create the database
    try:  # always close the connection
        for seed in args.seeds:  # each seed gives a different crowd
            base_id = create_demo_scenario(conn, f"Bench base seed {seed}", agents=args.agents, seed=seed)  # base scenario for this seed
            probe = ex.run_scenario(conn, base_id, "SHORTEST_PATH", label="bench-probe")  # a first run to find the busiest roads
            busiest = [int(e) for e in an.most_used_edges(conn, probe, 6)["edge_id"]]  # the six most used roads
            base_cfg = repo.load_scenario_config(conn, base_id)  # reload the base configuration
            for variant, mods in ex.evaluation_variants(base_cfg, busiest).items():  # each test condition
                sid, _ = ex.create_derived_scenario(conn, base_id, mods, name=f"{variant} (seed {seed})")  # scenario with the change applied
                for strategy in settings.ROUTING_STRATEGIES:  # every routing strategy
                    ex.run_scenario(conn, sid, strategy, label=f"bench:{variant}")  # run and store it
        cur = conn.execute(SUMMARY_SQL)  # read the results back from SQL
        cols = [d[0] for d in cur.description]  # column names
        rows = cur.fetchall()  # all result rows
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)  # make sure the output folder exists
        with open(args.out, "w", newline="", encoding="utf-8") as fh:  # open the CSV file
            w = csv.writer(fh)  # CSV writer
            w.writerow(cols)  # header row
            w.writerows([tuple(r) for r in rows])  # data rows
        print(f"{len(rows)} runs stored; results written to {args.out}\n")  # summary line
        print(f"{'scenario':42} {'strategy':17} {'seed':>4} {'done':>6} {'median':>8} {'p95':>8} {'wait':>9} {'bn':>3}")  # table header
        for r in rows:  # each run
            med = "n/a" if r["median_evacuation_time_seconds"] is None else f"{r['median_evacuation_time_seconds']:.0f}"  # median text
            p95 = "n/a" if r["percentile_95_evacuation_time_seconds"] is None else f"{r['percentile_95_evacuation_time_seconds']:.0f}"  # p95 text
            print(f"{r['scenario_name'][:42]:42} {r['routing_strategy']:17} {r['random_seed']:>4} "  # row: scenario, strategy, seed
                  f"{r['completion_rate']:>6.1%} {med:>8} {p95:>8} {r['total_waiting_time_seconds']:>9.0f} {r['bottlenecks']:>3}")  # completion, times, wait, bottlenecks
        bad = [(rid, c["check"]) for (rid,) in conn.execute(  # collect failed integrity checks
            "SELECT run_id FROM simulation_runs WHERE label LIKE 'bench:%'") for c in ex.consistency_checks(conn, rid)  # for every benchmark run
            if not c["passed"]]  # keep only failures
        print("\nConsistency checks:", "all passed" if not bad else f"FAILED {bad}")  # final verdict
    finally:  # cleanup
        conn.close()  # close the connection


if __name__ == "__main__":  # run only when executed as a script
    main()  # start
