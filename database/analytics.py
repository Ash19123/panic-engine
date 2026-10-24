"""SQL analytics: runs the statements of ``sql_queries`` and returns pandas DataFrames.

Metrics here are *SQL-computed* (aggregated inside SQLite). Columns of
``simulation_runs`` written by the engine are *Python-computed*; the two are
cross-checked in ``simulation.experiments.consistency_checks`` and the tests.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Dict, Optional, Sequence

import pandas as pd

from database import sql_queries as q
from simulation.metrics import relative_change


def _df(conn: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)


def completion_by_run(conn): return _df(conn, q.COMPLETION_BY_RUN)
def evacuation_time_stats(conn): return _df(conn, q.EVACUATION_TIME_STATS)
def outcome_breakdown(conn, run_id): return _df(conn, q.OUTCOME_BREAKDOWN, (run_id, run_id))
def wait_by_destination(conn, run_id): return _df(conn, q.WAIT_BY_DESTINATION, (run_id,))
def peak_congestion_by_edge(conn, run_id, limit=20): return _df(conn, q.PEAK_CONGESTION_BY_EDGE, (run_id, limit))
def most_used_edges(conn, run_id, limit=20): return _df(conn, q.MOST_USED_EDGES, (run_id, limit))
def bottleneck_ranking(conn, run_id): return _df(conn, q.BOTTLENECK_RANKING, (run_id,))
def strategy_comparison(conn): return _df(conn, q.STRATEGY_COMPARISON)
def scenario_history(conn): return _df(conn, q.SCENARIO_HISTORY)
def intervention_comparison(conn): return _df(conn, q.INTERVENTION_COMPARISON)
def occupancy_time_series(conn, run_id): return _df(conn, q.OCCUPANCY_TIME_SERIES, (run_id,))
def edge_timeline(conn, run_id, edge_id): return _df(conn, q.EDGE_TIMELINE, (run_id, edge_id))
def evacuation_times(conn, run_id): return _df(conn, q.EVACUATION_TIMES, (run_id,))


def seed_variability(conn) -> pd.DataFrame:
    df = _df(conn, q.SEED_VARIABILITY)
    if not df.empty:
        df["sd_completion"] = df["var_completion"].map(math.sqrt)
        df = df.drop(columns=["var_completion"])
    return df


def edge_congestion_diff(conn, base_run: int, alt_run: int, limit: int = 25) -> pd.DataFrame:
    return _df(conn, q.EDGE_CONGESTION_DIFF, {"base": base_run, "alt": alt_run, "lim": limit})


def overview(conn: sqlite3.Connection) -> Dict:
    out = dict(conn.execute(q.OVERVIEW).fetchone())
    latest = conn.execute(q.LATEST_RUN).fetchone()
    worst = conn.execute(q.HIGHEST_CONGESTION).fetchone()
    out["latest_run"] = dict(latest) if latest else None
    out["highest_congestion"] = dict(worst) if worst else None
    return out


COMPARE_COLUMNS = [
    ("completion_rate", "Completion rate", True),
    ("median_evacuation_time_seconds", "Median evacuation time (s)", False),
    ("percentile_95_evacuation_time_seconds", "95th-percentile evacuation time (s)", False),
    ("total_waiting_time_seconds", "Total waiting time (s)", False),
    ("unresolved_agents", "Unresolved agents", False),
    ("failed_agents", "Failed agents", False),
]


def compare_runs(conn: sqlite3.Connection, run_ids: Sequence[int]) -> pd.DataFrame:
    """Side-by-side metrics for saved runs with peak occupancy and bottleneck counts (all from SQL)."""
    if not run_ids:
        return pd.DataFrame()
    marks = ",".join("?" * len(run_ids))
    return _df(conn, f"""
        SELECT r.run_id, s.scenario_name, r.routing_strategy, r.random_seed, r.total_agents, r.evacuated_agents,
               r.completion_rate, r.median_evacuation_time_seconds, r.percentile_95_evacuation_time_seconds,
               r.total_waiting_time_seconds, r.maximum_wait_time_seconds, r.unresolved_agents, r.failed_agents,
               (SELECT MAX(1.0 * o.occupancy / e.capacity) FROM edge_occupancy_snapshots o
                  JOIN network_edges e ON e.edge_id = o.edge_id WHERE o.run_id = r.run_id) AS peak_occupancy_ratio,
               (SELECT COUNT(*) FROM bottleneck_reports b WHERE b.run_id = r.run_id) AS bottleneck_count,
               (SELECT COUNT(*) FROM bottleneck_reports b WHERE b.run_id = r.run_id AND b.severity = 'SEVERE') AS severe_bottlenecks
        FROM simulation_runs r JOIN scenarios s USING (scenario_id)
        WHERE r.run_id IN ({marks}) AND r.status = 'COMPLETED' ORDER BY r.run_id""", tuple(run_ids))


def relative_changes(df: pd.DataFrame, baseline_run: int, columns: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Relative change of each run vs. the baseline run: (x - base) / base. NaN when base is 0/missing."""
    cols = list(columns or [c for c in df.columns if c in
                            ("completion_rate", "median_evacuation_time_seconds",
                             "percentile_95_evacuation_time_seconds", "total_waiting_time_seconds",
                             "unresolved_agents", "failed_agents", "peak_occupancy_ratio", "bottleneck_count")])
    base = df[df["run_id"] == baseline_run].iloc[0]
    rows = []
    for _, r in df.iterrows():
        row = {"run_id": r["run_id"], "routing_strategy": r["routing_strategy"]}
        for c in cols:
            rc = relative_change(base[c], r[c])
            row[c + "_rel_change"] = float("nan") if rc is None else rc
        rows.append(row)
    return pd.DataFrame(rows)


def hazard_summary(conn: sqlite3.Connection, run_id: int) -> Dict:
    """Alert-drill outcomes for a run (SQL over the event log)."""
    return dict(conn.execute(q.HAZARD_SUMMARY, {"r": run_id}).fetchone())


def alert_channel_summary(conn: sqlite3.Connection, run_id: int) -> pd.DataFrame:
    """People per alert channel (PHONE / SIREN / NEIGHBOUR / NONE) with their outcomes, from SQL."""
    return _df(conn, q.ALERT_CHANNELS, (run_id,))
