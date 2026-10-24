"""Export a Markdown report of everything stored in the database.

    python -m scripts.export_report [--out data/report.md]

All numbers are read from SQL. With an empty database the report says that nothing has been measured.
"""
import argparse
from datetime import datetime
from pathlib import Path

from config import settings
from database import analytics as an
from database.init_db import init_db
from simulation import experiments as ex


def md_table(df) -> str:
    if df.empty:
        return "_no rows_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if v is None or v != v else (f"{v:.4g}" if isinstance(v, float) else str(v)) for v in r) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    settings.configure_logging()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(settings.DATA_DIR / "report.md"))
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    conn = init_db(args.db)
    try:
        o = an.overview(conn)
        out = [f"# PANIC ENGINE experiment report", f"Generated {datetime.now():%Y-%m-%d %H:%M}.",
               "", "> Educational simulation; results depend on stated model assumptions and are not validated predictions.", ""]
        if not o["completed_runs"]:
            out.append("**No simulation runs have been executed, so no results have been measured yet.**")
        else:
            out += ["## Summary", f"- completed runs: {o['completed_runs']}", f"- agents simulated: {o['agents_simulated']}",
                    f"- pooled completion rate: {o['pooled_completion_rate']:.1%}", "",
                    "## Completion by run", md_table(an.completion_by_run(conn)),
                    "## Evacuation-time statistics (evacuated agents only; nearest-rank p95)", md_table(an.evacuation_time_stats(conn)),
                    "## Strategy comparison (means over runs)", md_table(an.strategy_comparison(conn)),
                    "## Interventions / paired experiments", md_table(an.intervention_comparison(conn)),
                    "## Variability across seeds", md_table(an.seed_variability(conn)), "## Integrity checks"]
            for (rid,) in conn.execute("SELECT run_id FROM simulation_runs WHERE status='COMPLETED' ORDER BY run_id"):
                failed = [c["check"] for c in ex.consistency_checks(conn, rid) if not c["passed"]]
                out.append(f"- run {rid}: " + ("all checks passed" if not failed else "FAILED: " + ", ".join(failed)))
        out += ["", f"_{settings.OSM_ATTRIBUTION}_"]
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(out), encoding="utf-8")
        print(f"Report written to {args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
