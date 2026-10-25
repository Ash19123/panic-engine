"""Export a Markdown report of everything stored in the database.

    python -m scripts.export_report [--out data/report.md]

All numbers are read from SQL. With an empty database the report says that nothing has been measured.
"""
import argparse  # command-line options
from datetime import datetime  # timestamp in the report
from pathlib import Path  # file paths

from config import settings  # default output folder and attribution
from database import analytics as an  # SQL analytics helpers
from database.init_db import init_db  # open or create the database
from simulation import experiments as ex  # integrity checks


def md_table(df) -> str:  # turn a pandas table into a Markdown table
    if df.empty:  # nothing to show
        return "_no rows_\n"  # placeholder text
    cols = list(df.columns)  # column names
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]  # header row and separator row
    for _, r in df.iterrows():  # each data row
        lines.append("| " + " | ".join("" if v is None or v != v else (f"{v:.4g}" if isinstance(v, float) else str(v)) for v in r) + " |")  # blank for missing, 4 significant digits for floats
    return "\n".join(lines) + "\n"  # join into one text block


def main() -> None:  # command-line entry point
    settings.configure_logging()  # set up logging
    ap = argparse.ArgumentParser(description=__doc__)  # option parser
    ap.add_argument("--out", default=str(settings.DATA_DIR / "report.md"))  # output file
    ap.add_argument("--db", default=None)  # optional database path
    args = ap.parse_args()  # read the options
    conn = init_db(args.db)  # open or create the database
    try:  # always close the connection
        o = an.overview(conn)  # headline numbers from SQL
        out = [f"# PANIC ENGINE experiment report", f"Generated {datetime.now():%Y-%m-%d %H:%M}.",  # title and date
               "", "> Educational simulation; results depend on stated model assumptions and are not validated predictions.", ""]  # disclaimer
        if not o["completed_runs"]:  # nothing has been run
            out.append("**No simulation runs have been executed, so no results have been measured yet.**")  # say so honestly
        else:  # there are results
            out += ["## Summary", f"- completed runs: {o['completed_runs']}", f"- agents simulated: {o['agents_simulated']}",  # summary list
                    f"- pooled completion rate: {o['pooled_completion_rate']:.1%}", "",  # overall completion rate
                    "## Completion by run", md_table(an.completion_by_run(conn)),  # per-run table
                    "## Evacuation-time statistics (evacuated agents only; nearest-rank p95)", md_table(an.evacuation_time_stats(conn)),  # time statistics
                    "## Strategy comparison (means over runs)", md_table(an.strategy_comparison(conn)),  # strategies side by side
                    "## Interventions / paired experiments", md_table(an.intervention_comparison(conn)),  # what-if experiments
                    "## Variability across seeds", md_table(an.seed_variability(conn)), "## Integrity checks"]  # spread over seeds, then the checks heading
            for (rid,) in conn.execute("SELECT run_id FROM simulation_runs WHERE status='COMPLETED' ORDER BY run_id"):  # every finished run
                failed = [c["check"] for c in ex.consistency_checks(conn, rid) if not c["passed"]]  # names of failed checks
                out.append(f"- run {rid}: " + ("all checks passed" if not failed else "FAILED: " + ", ".join(failed)))  # one line per run
        out += ["", f"_{settings.OSM_ATTRIBUTION}_"]  # map data credit
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)  # make sure the output folder exists
        Path(args.out).write_text("\n".join(out), encoding="utf-8")  # write the report
        print(f"Report written to {args.out}")  # tell the user where
    finally:  # cleanup
        conn.close()  # close the connection


if __name__ == "__main__":  # run only when executed as a script
    main()  # start
