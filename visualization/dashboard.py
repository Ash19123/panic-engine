"""Analytics / strategy / SQL / about tabs of the single-page dashboard (all numbers come from SQL)."""
from __future__ import annotations

import json
import re
import sqlite3

import pandas as pd
import streamlit as st

from config import settings
from database import analytics as an
from database import repositories as repo
from database import sql_queries as q
from simulation import experiments as ex
from visualization import charts
from visualization.theme import STRETCH

LOWER_IS_BETTER = {"median_evacuation_time_seconds": "median evacuation time",
                   "percentile_95_evacuation_time_seconds": "95th-percentile time", "total_waiting_time_seconds": "total waiting",
                   "unresolved_agents": "unresolved people", "failed_agents": "failed / caught people",
                   "bottleneck_count": "bottleneck count", "peak_occupancy_ratio": "peak road occupancy"}


def _run_label(r: dict) -> str:
    return f"Run #{r['run_id']} · {r['scenario_name']} · {r['routing_strategy'].replace('_', ' ').title()} · {r['completion_rate']:.0%} safe"


def _pick_run(conn, key: str):
    runs = repo.list_runs(conn)
    if not runs:
        st.info("No simulation has been run yet, so nothing has been measured. Use **RUN SIMULATION** in the sidebar "
                "or the demo button on the Live Simulation tab.")
        return None, runs
    rid = st.selectbox("Run", [r["run_id"] for r in runs], key=key,
                       format_func=lambda i: _run_label(next(r for r in runs if r["run_id"] == i)))
    return rid, runs


# --------------------------------------------------------------------------------------- analytics
def render_analytics(conn: sqlite3.Connection) -> None:
    rid, _ = _pick_run(conn, "an_run")
    if rid is None:
        return
    run = repo.get_run(conn, rid)
    c = st.columns(5)
    c[0].metric("Safe", f"{run['evacuated_agents']:,} / {run['total_agents']:,}")
    c[1].metric("Completion rate", f"{run['completion_rate']:.1%}")
    c[2].metric("Unresolved at limit", f"{run['unresolved_agents']:,}")
    c[3].metric("Unable / caught", f"{run['failed_agents']:,}")
    c[4].metric("Longest single wait", f"{run['maximum_wait_time_seconds']:,.0f} s")
    h = an.hazard_summary(conn, rid)
    if h["first_strike_s"] is not None:
        st.markdown("##### 💣 Bomb alert drill outcome")
        c = st.columns(5)
        c[0].metric("Phone alerts sent", f"{h['alerts_sent']:,}")
        c[1].metric("Alert → strike", f"{h['first_strike_s'] - h['first_alert_s']:,.0f} s", help="Warning time people had")
        c[2].metric("Safe before the blast", f"{h['safe_before_strike']:,}")
        c[3].metric("Caught in blast area", f"{h['caught_in_blast']:,}")
        c[4].metric("Avg alert → safe", "n/a" if h["avg_alert_to_safe_s"] is None else f"{h['avg_alert_to_safe_s']:,.0f} s")
    chans = an.alert_channel_summary(conn, rid)
    if set(chans["channel"]) - {"OUTSIDE ALERT AREA"}:
        st.markdown("##### How the alert reached people")
        st.dataframe(chans, **STRETCH, hide_index=True)
    ts = an.occupancy_time_series(conn, rid)
    peak = an.peak_congestion_by_edge(conn, rid, 200)
    c1, c2 = st.columns(2)
    if not ts.empty:
        c1.plotly_chart(charts.occupancy_timeline(ts), **STRETCH)
    if not peak.empty:
        c2.plotly_chart(charts.peak_ratio_bars(peak.head(15)), **STRETCH)
    ranking = an.bottleneck_ranking(conn, rid)
    st.markdown("##### Bottleneck ranking")
    if ranking.empty:
        st.info("No road segment met the bottleneck criteria in this run.")
    else:
        st.dataframe(ranking.drop(columns=["contributing_metrics_json"]), **STRETCH, hide_index=True)
        pick = st.selectbox("Why was this segment flagged?", list(ranking["edge_id"]), key="bn_pick")
        row = ranking[ranking.edge_id == pick].iloc[0]
        c1, c2 = st.columns([2, 3])
        c1.json(json.loads(row["contributing_metrics_json"]))
        c2.plotly_chart(charts.edge_timeline(an.edge_timeline(conn, rid, int(pick)), int(row["capacity"]), int(pick)),
                        **STRETCH)
    c1, c2 = st.columns(2)
    c1.markdown("##### Outcome breakdown")
    c1.dataframe(an.outcome_breakdown(conn, rid), **STRETCH, hide_index=True)
    c2.markdown("##### Wait and arrivals by safe stop")
    c2.dataframe(an.wait_by_destination(conn, rid), **STRETCH, hide_index=True)
    d = an.evacuation_times(conn, rid)
    if not d.empty:
        c1, c2 = st.columns(2)
        c1.plotly_chart(charts.histogram(d, "t", "Time until safe (people who made it)", "seconds"), **STRETCH)
        c2.plotly_chart(charts.histogram(d, "wait", "Time spent waiting in queues", "seconds"), **STRETCH)


# ------------------------------------------------------------------------------------ strategies
def _trade_offs(df: pd.DataFrame, base: int) -> None:
    b = df[df.run_id == base].iloc[0]
    for _, r in df[df.run_id != base].iterrows():
        better, worse = [], []
        if r["completion_rate"] > b["completion_rate"]:
            better.append("completion rate")
        elif r["completion_rate"] < b["completion_rate"]:
            worse.append("completion rate")
        for col, label in LOWER_IS_BETTER.items():
            if pd.isna(r[col]) or pd.isna(b[col]) or r[col] == b[col]:
                continue
            (better if r[col] < b[col] else worse).append(label)
        st.markdown(f"**Run {int(r.run_id)} ({r.routing_strategy}) vs baseline run {base}:** better on: "
                    f"{', '.join(better) or 'none'}; worse on: {', '.join(worse) or 'none'}. "
                    "Which matters more depends on the decision-maker's priorities.")


def render_strategies(conn: sqlite3.Connection) -> None:
    t1, t2, t3, t4 = st.tabs(["Compare saved runs", "Counterfactual lab", "Alert lead-time experiment", "Alert planner"])
    scen = repo.list_scenarios(conn)
    with t1:
        if scen:
            c = st.columns([2, 3, 1])
            sid = c[0].selectbox("Scenario", [s["scenario_id"] for s in scen], key="pair_s", format_func=lambda i: next(
                f"#{s['scenario_id']} {s['scenario_name']}" for s in scen if s["scenario_id"] == i))
            strategies = c[1].multiselect("Strategies", settings.ROUTING_STRATEGIES, default=["SHORTEST_PATH", "CONGESTION_AWARE"])
            if c[2].button("Run paired", disabled=len(strategies) < 2):
                with st.spinner("Running each strategy..."):
                    ex.run_paired_strategies(conn, sid, strategies)
                st.rerun()
        runs = repo.list_runs(conn)
        if len(runs) >= 2:
            chosen = st.multiselect("Runs to compare", [r["run_id"] for r in runs], default=[r["run_id"] for r in runs[:3]],
                                    format_func=lambda i: _run_label(next(r for r in runs if r["run_id"] == i)))
            if len(chosen) >= 2:
                df = an.compare_runs(conn, chosen)
                if df["scenario_name"].nunique() > 1:
                    st.warning("These runs come from different scenarios; differences mix routing effects with scenario "
                               "differences. Use runs of the same scenario for a fair comparison.")
                base = st.selectbox("Baseline run", chosen)
                st.dataframe(df.set_index("run_id").T.astype(str), **STRETCH)
                rel = an.relative_changes(df, base)
                st.markdown("**Relative change vs baseline**")
                st.dataframe(rel.style.format({c: "{:+.1%}" for c in rel.columns if c.endswith("_rel_change")}, na_rep="n/a"),
                             **STRETCH, hide_index=True)
                c = st.columns(2)
                c[0].plotly_chart(charts.bar_by_run(df, "completion_rate", "Completion rate", "fraction safe"), **STRETCH)
                c[1].plotly_chart(charts.bar_by_run(df, "total_waiting_time_seconds", "Total waiting time", "person-seconds"), **STRETCH)
                _trade_offs(df, base)
        else:
            st.info("Need at least two completed runs.")
    with t2:
        if not scen:
            st.info("Save a scenario first.")
        else:
            sid = st.selectbox("Baseline scenario", [s["scenario_id"] for s in scen], key="cf_s", format_func=lambda i: next(
                f"#{s['scenario_id']} {s['scenario_name']}" for s in scen if s["scenario_id"] == i))
            cfg = repo.load_scenario_config(conn, sid)
            net = repo.load_network(conn, cfg.network_id)
            c = st.columns(2)
            alt = c[0].selectbox("Alternative strategy", settings.ROUTING_STRATEGIES, index=1)
            cname = c[1].text_input("Experiment name", f"What-if on scenario {sid}")
            mods: dict = {}
            edge_pool = sorted(net.edges)[:4000]
            close_ids = st.multiselect("Close road segments (edge ids)", edge_pool, format_func=lambda e: (
                f"edge {e}: {net.edges[e].road_class}, cap {net.edges[e].capacity}, {net.edges[e].length_m:.0f} m"))
            if close_ids:
                mods["add_closures"] = [{"edge_id": e, "close_time_seconds": 0} for e in close_ids]
            scale = st.slider("Safe-stop admission-rate multiplier", 0.1, 3.0, 1.0, 0.1)
            if scale != 1.0:
                mods["throughput_scale"] = scale
            off = st.multiselect("Make safe stops unavailable", [d.destination_id for d in cfg.destinations],
                                 format_func=lambda i: next(d.name for d in cfg.destinations if d.destination_id == i))
            if off:
                mods["destination_overrides"] = {i: {"available": False} for i in off}
            st.write("Modifications:", mods or "none (pure routing comparison)")
            if st.button("Run counterfactual experiment", type="primary"):
                try:
                    with st.spinner("Running baseline and alternative..."):
                        st.session_state["cf_last"] = ex.run_counterfactual(conn, sid, mods, alt, name=cname)
                except ValueError as exc:
                    st.error(str(exc))
            res = st.session_state.get("cf_last")
            if res:
                st.success(f"Baseline run #{res['baseline_run']} vs alternative run #{res['alternative_run']}")
                st.write("Recorded changes:", res["changes"] or ["strategy only"])
                comp = an.compare_runs(conn, [res["baseline_run"], res["alternative_run"]])
                st.dataframe(comp.set_index("run_id").T.astype(str), **STRETCH)
                _trade_offs(comp, res["baseline_run"])
                diff = an.edge_congestion_diff(conn, res["baseline_run"], res["alternative_run"])
                if not diff.empty:
                    st.markdown("**Road segments whose congestion changed most**")
                    st.plotly_chart(charts.congestion_diff_bars(diff), **STRETCH)
    with t3:
        bombs = [s for s in scen if "bomb" in s["scenario_name"].lower() or "drill" in s["scenario_name"].lower()]
        pool = bombs or scen
        if not pool:
            st.info("Create a bomb scenario first (sidebar → Bomb areas, then RUN).")
        else:
            sid = st.selectbox("Bomb scenario", [s["scenario_id"] for s in pool], key="lead_s", format_func=lambda i: next(
                f"#{s['scenario_id']} {s['scenario_name']}" for s in pool if s["scenario_id"] == i))
            cfg = repo.load_scenario_config(conn, sid)
            if not cfg.params.hazards:
                st.warning("This scenario has no bomb hazard.")
            else:
                leads = st.multiselect("Alert lead times (s before the strike)", [30, 60, 120, 180, 240, 300, 420], default=[60, 120, 240])
                if st.button("Run lead-time experiment", type="primary", disabled=not leads):
                    rows = []
                    with st.spinner("Simulating each lead time..."):
                        for lead in sorted(leads):
                            new_id, _ = ex.create_derived_scenario(conn, sid, {"hazard_alert_lead_s": lead},
                                                                   name=f"{cfg.name} (alert {lead}s before)")
                            rid = ex.run_scenario(conn, new_id, label=f"lead:{lead}")
                            h, r = an.hazard_summary(conn, rid), repo.get_run(conn, rid)
                            rows.append({"alert lead (s)": lead, "run": rid, "safe before blast": h["safe_before_strike"],
                                         "caught in blast": h["caught_in_blast"], "completion": r["completion_rate"],
                                         "avg alert→safe (s)": h["avg_alert_to_safe_s"]})
                    st.session_state["lead_rows"] = rows
                rows = st.session_state.get("lead_rows")
                if rows:
                    df = pd.DataFrame(rows)
                    st.dataframe(df, **STRETCH, hide_index=True)
                    st.bar_chart(df.set_index("alert lead (s)")[["safe before blast", "caught in blast"]])
    with t4:
        render_alert_planner(conn, scen)


def render_alert_planner(conn: sqlite3.Connection, scen: list) -> None:
    """Who does the alert miss when many people have no phone, and what would reach them?"""
    pool = [s for s in scen if "bomb" in s["scenario_name"].lower() or "drill" in s["scenario_name"].lower()] or scen
    if not pool:
        st.info("Create a bomb scenario first.")
        return
    sid = st.selectbox("Bomb scenario", [s["scenario_id"] for s in pool], key="plan_s", format_func=lambda i: next(
        f"#{s['scenario_id']} {s['scenario_name']}" for s in pool if s["scenario_id"] == i))
    if not repo.load_scenario_config(conn, sid).params.hazards:
        st.warning("This scenario has no bomb hazard.")
        return
    c = st.columns(4)
    reach = c[0].slider("People with a phone at hand (%)", 0, 100, 40, 5)
    n_sir = c[1].slider("Sirens to place", 0, 4, 2)
    rad = c[2].slider("Siren radius (m)", 100, 800, 400, 50)
    neigh = c[3].slider("Neighbour relay (m)", 0, 120, 60, 10)
    if st.button("Run alert planner", type="primary"):
        with st.spinner("Running four paired simulations..."):
            st.session_state["plan"] = ex.run_alert_planner(conn, sid, reach / 100.0, n_sir, float(rad), float(neigh))
    plan = st.session_state.get("plan")
    if not plan:
        return
    df = pd.DataFrame(plan["rows"])
    st.dataframe(df, **STRETCH, hide_index=True)
    st.bar_chart(df.set_index("variant")[["by phone", "by siren", "by neighbour", "never alerted"]])
    st.bar_chart(df.set_index("variant")[["safe before blast", "caught in blast"]])
    if plan["sirens"]:
        st.markdown("**Recommended siren sites** (chosen to cover people the phones missed, outside the blast radius)")
        st.dataframe(pd.DataFrame(plan["sirens"])[["node", "lat", "lon", "radius_m", "covers"]], **STRETCH, hide_index=True)


# ----------------------------------------------------------------------------------------- SQL
CATALOGUE = {
    "Completion rate by run": (q.COMPLETION_BY_RUN, "none"),
    "Average / median / 95th-percentile time": (q.EVACUATION_TIME_STATS, "none"),
    "Outcome breakdown": (q.OUTCOME_BREAKDOWN, "run2"),
    "Average waiting time by safe stop": (q.WAIT_BY_DESTINATION, "run"),
    "Peak congestion by road segment": (q.PEAK_CONGESTION_BY_EDGE, "run_limit"),
    "Most frequently used road segments": (q.MOST_USED_EDGES, "run_limit"),
    "Bottleneck severity ranking": (q.BOTTLENECK_RANKING, "run"),
    "Routing strategy comparison": (q.STRATEGY_COMPARISON, "none"),
    "Scenario history": (q.SCENARIO_HISTORY, "none"),
    "Before / after interventions": (q.INTERVENTION_COMPARISON, "none"),
    "Crowd occupancy time series": (q.OCCUPANCY_TIME_SERIES, "run"),
    "Repeated runs under different seeds": (q.SEED_VARIABILITY, "none"),
}


def render_sql(conn: sqlite3.Connection) -> None:
    name = st.selectbox("Query", list(CATALOGUE))
    sql, kind = CATALOGUE[name]
    params: tuple = ()
    if kind != "none":
        runs = repo.list_runs(conn)
        if not runs:
            st.info("No completed runs yet — nothing has been measured.")
            st.code(sql.strip(), language="sql")
            return
        rid = st.selectbox("Run", [r["run_id"] for r in runs], key="sql_run")
        lim = st.number_input("Row limit", 1, 500, 15) if kind == "run_limit" else None
        params = {"run": (rid,), "run2": (rid, rid), "run_limit": (rid, int(lim) if lim else 15)}[kind]
    st.code(sql.strip(), language="sql")
    cur = conn.execute(sql, params)
    df = pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=[d[0] for d in cur.description])
    st.dataframe(df, **STRETCH, hide_index=True)
    st.markdown("##### Read-only SQL console")
    user_sql = st.text_area("SELECT / WITH only", "SELECT event_type, COUNT(*) AS n FROM simulation_events GROUP BY event_type ORDER BY n DESC")
    if st.button("Run query"):
        stmt = user_sql.strip().rstrip(";")
        if not re.match(r"^(select|with)\b", stmt, re.I) or ";" in stmt:
            st.error("Only a single SELECT/WITH statement is allowed.")
        else:
            ro = sqlite3.connect(f"file:{settings.DB_PATH.as_posix()}?mode=ro", uri=True)
            try:
                ro.execute("PRAGMA query_only = ON")
                res = ro.execute(f"SELECT * FROM ({stmt}) LIMIT 1000")
                st.dataframe(pd.DataFrame(res.fetchall(), columns=[d[0] for d in res.description]), **STRETCH, hide_index=True)
            except sqlite3.Error as exc:
                st.error(f"SQL error: {exc}")
            finally:
                ro.close()
    st.markdown("##### History and integrity checks")
    st.dataframe(an.scenario_history(conn), **STRETCH, hide_index=True)
    runs = repo.list_runs(conn)
    if runs:
        rid = st.selectbox("Run to check", [r["run_id"] for r in runs], key="chk_run")
        if st.button("Run integrity checks"):
            res = ex.consistency_checks(conn, rid)
            st.dataframe(res, **STRETCH, hide_index=True)
            (st.success if all(r["passed"] for r in res) else st.error)("All checks passed." if all(r["passed"] for r in res) else "Some checks failed.")
