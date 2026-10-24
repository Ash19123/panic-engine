"""Reproducible evaluation matrix on the synthetic network.

    python -m scripts.run_benchmarks --seeds 1 2 3 --agents 300 [--db PATH]

Scenarios: normal, one blocked road, one unavailable destination, reduced destination
throughput, increased population, uneven origin distribution, multiple bottlenecks.
Each is run with every routing strategy and every seed. Everything is stored in SQLite and the
printed table / CSV is read back FROM SQL - no numbers are written by hand.
"""
import argparse
import csv
from pathlib import Path

from config import settings
from database import analytics as an
from database import repositories as repo
from database.init_db import init_db
from database.seed_data import create_demo_scenario
from simulation import experiments as ex

SUMMARY_SQL = """
SELECT s.scenario_name, r.routing_strategy, r.random_seed, r.total_agents, r.evacuated_agents, r.unresolved_agents,
       r.failed_agents, r.completion_rate, r.median_evacuation_time_seconds, r.percentile_95_evacuation_time_seconds,
       r.total_waiting_time_seconds, (SELECT COUNT(*) FROM bottleneck_reports b WHERE b.run_id = r.run_id) AS bottlenecks,
       r.run_id
FROM simulation_runs r JOIN scenarios s USING (scenario_id)
WHERE r.label LIKE 'bench:%' AND r.status = 'COMPLETED' ORDER BY s.scenario_name, r.random_seed, r.routing_strategy
"""


def main() -> None:
    settings.configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--agents", type=int, default=300)
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=str(settings.DATA_DIR / "benchmark_results.csv"))
    args = ap.parse_args()

    conn = init_db(args.db)
    try:
        for seed in args.seeds:
            base_id = create_demo_scenario(conn, f"Bench base seed {seed}", agents=args.agents, seed=seed)
            probe = ex.run_scenario(conn, base_id, "SHORTEST_PATH", label="bench-probe")
            busiest = [int(e) for e in an.most_used_edges(conn, probe, 6)["edge_id"]]
            base_cfg = repo.load_scenario_config(conn, base_id)
            for variant, mods in ex.evaluation_variants(base_cfg, busiest).items():
                sid, _ = ex.create_derived_scenario(conn, base_id, mods, name=f"{variant} (seed {seed})")
                for strategy in settings.ROUTING_STRATEGIES:
                    ex.run_scenario(conn, sid, strategy, label=f"bench:{variant}")
        cur = conn.execute(SUMMARY_SQL)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows([tuple(r) for r in rows])
        print(f"{len(rows)} runs stored; results written to {args.out}\n")
        print(f"{'scenario':42} {'strategy':17} {'seed':>4} {'done':>6} {'median':>8} {'p95':>8} {'wait':>9} {'bn':>3}")
        for r in rows:
            med = "n/a" if r["median_evacuation_time_seconds"] is None else f"{r['median_evacuation_time_seconds']:.0f}"
            p95 = "n/a" if r["percentile_95_evacuation_time_seconds"] is None else f"{r['percentile_95_evacuation_time_seconds']:.0f}"
            print(f"{r['scenario_name'][:42]:42} {r['routing_strategy']:17} {r['random_seed']:>4} "
                  f"{r['completion_rate']:>6.1%} {med:>8} {p95:>8} {r['total_waiting_time_seconds']:>9.0f} {r['bottlenecks']:>3}")
        bad = [(rid, c["check"]) for (rid,) in conn.execute(
            "SELECT run_id FROM simulation_runs WHERE label LIKE 'bench:%'") for c in ex.consistency_checks(conn, rid)
            if not c["passed"]]
        print("\nConsistency checks:", "all passed" if not bad else f"FAILED {bad}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
