"""SQL analytics: runs the statements of ``sql_queries`` and returns pandas DataFrames.

Metrics here are *SQL-computed* (aggregated inside SQLite). Columns of
``simulation_runs`` written by the engine are *Python-computed*; the two are
cross-checked in ``simulation.experiments.consistency_checks`` and the tests.
"""
from __future__ import annotations  # modern type hints on older Python

import math  # square root for the standard deviation
import sqlite3  # database type hints
from typing import Dict, Optional, Sequence  # type hints

import pandas as pd  # tables for the dashboard

from database import sql_queries as q  # the SQL text of every query
from simulation.metrics import relative_change  # (other - base) / base helper


def _df(conn: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:  # run a query and return a DataFrame
    cur = conn.execute(sql, params)  # execute with bound parameters
    cols = [d[0] for d in cur.description]  # column names from the cursor
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)  # rows as a table


def completion_by_run(conn): return _df(conn, q.COMPLETION_BY_RUN)  # completion rate of every run
def evacuation_time_stats(conn): return _df(conn, q.EVACUATION_TIME_STATS)  # mean, median and p95 evacuation time per run
def outcome_breakdown(conn, run_id): return _df(conn, q.OUTCOME_BREAKDOWN, (run_id, run_id))  # how many agents ended in each state
def wait_by_destination(conn, run_id): return _df(conn, q.WAIT_BY_DESTINATION, (run_id,))  # average wait per safe stop
def peak_congestion_by_edge(conn, run_id, limit=20): return _df(conn, q.PEAK_CONGESTION_BY_EDGE, (run_id, limit))  # fullest roads
def most_used_edges(conn, run_id, limit=20): return _df(conn, q.MOST_USED_EDGES, (run_id, limit))  # most travelled roads
def bottleneck_ranking(conn, run_id): return _df(conn, q.BOTTLENECK_RANKING, (run_id,))  # bottlenecks ranked by severity
def strategy_comparison(conn): return _df(conn, q.STRATEGY_COMPARISON)  # strategies side by side
def scenario_history(conn): return _df(conn, q.SCENARIO_HISTORY)  # every scenario and how often it was run
def intervention_comparison(conn): return _df(conn, q.INTERVENTION_COMPARISON)  # before and after for what-if experiments
def occupancy_time_series(conn, run_id): return _df(conn, q.OCCUPANCY_TIME_SERIES, (run_id,))  # people on roads over time
def edge_timeline(conn, run_id, edge_id): return _df(conn, q.EDGE_TIMELINE, (run_id, edge_id))  # occupancy of one road over time
def evacuation_times(conn, run_id): return _df(conn, q.EVACUATION_TIMES, (run_id,))  # every evacuation time (for a histogram)


def seed_variability(conn) -> pd.DataFrame:  # how much results change with the random seed
    df = _df(conn, q.SEED_VARIABILITY)  # variance per scenario and strategy
    if not df.empty:  # there is data
        df["sd_completion"] = df["var_completion"].map(math.sqrt)  # SQLite has no square root, so take it here
        df = df.drop(columns=["var_completion"])  # the variance column is no longer needed
    return df  # the table


def edge_congestion_diff(conn, base_run: int, alt_run: int, limit: int = 25) -> pd.DataFrame:  # which roads changed most between two runs
    return _df(conn, q.EDGE_CONGESTION_DIFF, {"base": base_run, "alt": alt_run, "lim": limit})  # named parameters


def overview(conn: sqlite3.Connection) -> Dict:  # headline numbers for the dashboard
    out = dict(conn.execute(q.OVERVIEW).fetchone())  # totals across all runs
    latest = conn.execute(q.LATEST_RUN).fetchone()  # the newest run
    worst = conn.execute(q.HIGHEST_CONGESTION).fetchone()  # the most congested road
    out["latest_run"] = dict(latest) if latest else None  # add it, or None
    out["highest_congestion"] = dict(worst) if worst else None  # add it, or None
    return out  # one dictionary


COMPARE_COLUMNS = [  # metrics shown when comparing runs: (column, label, higher is better)
    ("completion_rate", "Completion rate", True),  # more is better
    ("median_evacuation_time_seconds", "Median evacuation time (s)", False),  # less is better
    ("percentile_95_evacuation_time_seconds", "95th-percentile evacuation time (s)", False),  # less is better
    ("total_waiting_time_seconds", "Total waiting time (s)", False),  # less is better
    ("unresolved_agents", "Unresolved agents", False),  # less is better
    ("failed_agents", "Failed agents", False),  # less is better
]


def compare_runs(conn: sqlite3.Connection, run_ids: Sequence[int]) -> pd.DataFrame:  # side-by-side table of chosen runs
    """Side-by-side metrics for saved runs with peak occupancy and bottleneck counts (all from SQL)."""
    if not run_ids:  # nothing chosen
        return pd.DataFrame()  # empty table
    marks = ",".join("?" * len(run_ids))  # one "?" placeholder per run id
    return _df(conn, f"""
        SELECT r.run_id, s.scenario_name, r.routing_strategy, r.random_seed, r.total_agents, r.evacuated_agents,
               r.completion_rate, r.median_evacuation_time_seconds, r.percentile_95_evacuation_time_seconds,
               r.total_waiting_time_seconds, r.maximum_wait_time_seconds, r.unresolved_agents, r.failed_agents,
               (SELECT MAX(1.0 * o.occupancy / e.capacity) FROM edge_occupancy_snapshots o
                  JOIN network_edges e ON e.edge_id = o.edge_id WHERE o.run_id = r.run_id) AS peak_occupancy_ratio,
               (SELECT COUNT(*) FROM bottleneck_reports b WHERE b.run_id = r.run_id) AS bottleneck_count,
               (SELECT COUNT(*) FROM bottleneck_reports b WHERE b.run_id = r.run_id AND b.severity = 'SEVERE') AS severe_bottlenecks
        FROM simulation_runs r JOIN scenarios s USING (scenario_id)
        WHERE r.run_id IN ({marks}) AND r.status = 'COMPLETED' ORDER BY r.run_id""", tuple(run_ids))  # the SQL above is built with the placeholders and run with the ids bound


def relative_changes(df: pd.DataFrame, baseline_run: int, columns: Optional[Sequence[str]] = None) -> pd.DataFrame:  # change of each run versus a baseline
    """Relative change of each run vs. the baseline run: (x - base) / base. NaN when base is 0/missing."""
    cols = list(columns or [c for c in df.columns if c in  # chosen columns, or the standard metric columns that exist
                            ("completion_rate", "median_evacuation_time_seconds",  # standard metric columns (1)
                             "percentile_95_evacuation_time_seconds", "total_waiting_time_seconds",  # (2)
                             "unresolved_agents", "failed_agents", "peak_occupancy_ratio", "bottleneck_count")])  # (3)
    base = df[df["run_id"] == baseline_run].iloc[0]  # the baseline row
    rows = []  # result rows
    for _, r in df.iterrows():  # each run
        row = {"run_id": r["run_id"], "routing_strategy": r["routing_strategy"]}  # identify the run
        for c in cols:  # each metric
            rc = relative_change(base[c], r[c])  # change versus the baseline
            row[c + "_rel_change"] = float("nan") if rc is None else rc  # NaN when undefined
        rows.append(row)  # keep the row
    return pd.DataFrame(rows)  # as a table


def hazard_summary(conn: sqlite3.Connection, run_id: int) -> Dict:  # bomb-drill outcomes of a run
    """Alert-drill outcomes for a run (SQL over the event log)."""
    return dict(conn.execute(q.HAZARD_SUMMARY, {"r": run_id}).fetchone())  # one row as a dictionary


def alert_channel_summary(conn: sqlite3.Connection, run_id: int) -> pd.DataFrame:  # how people were warned and what happened to them
    """People per alert channel (PHONE / SIREN / NEIGHBOUR / NONE) with their outcomes, from SQL."""
    return _df(conn, q.ALERT_CHANNELS, (run_id,))  # table per channel
