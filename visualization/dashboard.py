"""Analytics / strategy / SQL / about tabs of the single-page dashboard (all numbers come from SQL)."""
from __future__ import annotations  # modern type hints on older Python

import json  # parse stored JSON
import re  # check that user SQL starts with SELECT or WITH
import sqlite3  # database type hints and the read-only console

import pandas as pd  # tables
import streamlit as st  # the dashboard framework

from config import settings  # strategies list and database path
from database import analytics as an  # SQL analytics helpers
from database import repositories as repo  # load runs and scenarios
from database import sql_queries as q  # the SQL text of every query
from simulation import experiments as ex  # run experiments and integrity checks
from visualization import charts  # chart builders
from visualization.theme import STRETCH  # keyword that makes widgets fill their container

LOWER_IS_BETTER = {"median_evacuation_time_seconds": "median evacuation time",  # metrics where a smaller number is better (1)
                   "percentile_95_evacuation_time_seconds": "95th-percentile time", "total_waiting_time_seconds": "total waiting",  # (2)
                   "unresolved_agents": "unresolved people", "failed_agents": "failed / caught people",  # (3)
                   "bottleneck_count": "bottleneck count", "peak_occupancy_ratio": "peak road occupancy"}  # (4)


def _run_label(r: dict) -> str:  # readable name of a run
    return f"Run #{r['run_id']} · {r['scenario_name']} · {r['routing_strategy'].replace('_', ' ').title()} · {r['completion_rate']:.0%} safe"  # id, scenario, strategy, completion


def _pick_run(conn, key: str):  # a drop-down to choose a run
    runs = repo.list_runs(conn)  # all completed runs
    if not runs:  # none yet
        st.info("No simulation has been run yet, so nothing has been measured. Use **RUN SIMULATION** in the sidebar "  # message (1)
                "or the demo button on the Live Simulation tab.")  # message (2)
        return None, runs  # nothing chosen
    rid = st.selectbox("Run", [r["run_id"] for r in runs], key=key,  # the drop-down (part 1)
                       format_func=lambda i: _run_label(next(r for r in runs if r["run_id"] == i)))  # readable labels (part 2)
    return rid, runs  # the chosen run id and the list


# --------------------------------------------------------------------------------------- analytics
def render_analytics(conn: sqlite3.Connection) -> None:  # the Analytics tab
    rid, _ = _pick_run(conn, "an_run")  # choose a run
    if rid is None:  # no runs
        return  # nothing to show
    run = repo.get_run(conn, rid)  # the run row
    c = st.columns(5)  # five metric boxes
    c[0].metric("Safe", f"{run['evacuated_agents']:,} / {run['total_agents']:,}")  # people who reached safety
    c[1].metric("Completion rate", f"{run['completion_rate']:.1%}")  # share evacuated
    c[2].metric("Unresolved at limit", f"{run['unresolved_agents']:,}")  # still on the way at the time limit
    c[3].metric("Unable / caught", f"{run['failed_agents']:,}")  # failed or caught
    c[4].metric("Longest single wait", f"{run['maximum_wait_time_seconds']:,.0f} s")  # worst wait
    h = an.hazard_summary(conn, rid)  # bomb-drill outcomes
    if h["first_strike_s"] is not None:  # the run had a bomb
        st.markdown("##### 💣 Bomb alert drill outcome")  # heading
        c = st.columns(5)  # five metric boxes
        c[0].metric("Phone alerts sent", f"{h['alerts_sent']:,}")  # alerts delivered
        c[1].metric("Alert → strike", f"{h['first_strike_s'] - h['first_alert_s']:,.0f} s", help="Warning time people had")  # lead time
        c[2].metric("Safe before the blast", f"{h['safe_before_strike']:,}")  # evacuated before the strike
        c[3].metric("Caught in blast area", f"{h['caught_in_blast']:,}")  # inside the blast at the strike
        c[4].metric("Avg alert → safe", "n/a" if h["avg_alert_to_safe_s"] is None else f"{h['avg_alert_to_safe_s']:,.0f} s")  # average time to reach safety
    chans = an.alert_channel_summary(conn, rid)  # how people were warned
    if set(chans["channel"]) - {"OUTSIDE ALERT AREA"}:  # some people were inside an alert area
        st.markdown("##### How the alert reached people")  # heading
        st.dataframe(chans, **STRETCH, hide_index=True)  # table
    ts = an.occupancy_time_series(conn, rid)  # people on roads over time
    peak = an.peak_congestion_by_edge(conn, rid, 200)  # fullest roads
    c1, c2 = st.columns(2)  # two columns
    if not ts.empty:  # there is data
        c1.plotly_chart(charts.occupancy_timeline(ts), **STRETCH)  # occupancy over time
    if not peak.empty:  # there is data
        c2.plotly_chart(charts.peak_ratio_bars(peak.head(15)), **STRETCH)  # the 15 fullest roads
    ranking = an.bottleneck_ranking(conn, rid)  # bottlenecks ranked
    st.markdown("##### Bottleneck ranking")  # heading
    if ranking.empty:  # none found
        st.info("No road segment met the bottleneck criteria in this run.")  # say so
    else:  # some found
        st.dataframe(ranking.drop(columns=["contributing_metrics_json"]), **STRETCH, hide_index=True)  # table without the raw JSON column
        pick = st.selectbox("Why was this segment flagged?", list(ranking["edge_id"]), key="bn_pick")  # choose one
        row = ranking[ranking.edge_id == pick].iloc[0]  # its row
        c1, c2 = st.columns([2, 3])  # two columns
        c1.json(json.loads(row["contributing_metrics_json"]))  # the numbers behind the decision
        c2.plotly_chart(charts.edge_timeline(an.edge_timeline(conn, rid, int(pick)), int(row["capacity"]), int(pick)),  # its occupancy over time (part 1)
                        **STRETCH)  # (part 2)
    c1, c2 = st.columns(2)  # two columns
    c1.markdown("##### Outcome breakdown")  # heading
    c1.dataframe(an.outcome_breakdown(conn, rid), **STRETCH, hide_index=True)  # how many agents ended in each state
    c2.markdown("##### Wait and arrivals by safe stop")  # heading
    c2.dataframe(an.wait_by_destination(conn, rid), **STRETCH, hide_index=True)  # waiting per safe stop
    d = an.evacuation_times(conn, rid)  # every evacuation time
    if not d.empty:  # there is data
        c1, c2 = st.columns(2)  # two columns
        c1.plotly_chart(charts.histogram(d, "t", "Time until safe (people who made it)", "seconds"), **STRETCH)  # time distribution
        c2.plotly_chart(charts.histogram(d, "wait", "Time spent waiting in queues", "seconds"), **STRETCH)  # waiting distribution


# ------------------------------------------------------------------------------------ strategies
def _trade_offs(df: pd.DataFrame, base: int) -> None:  # explain what is better or worse than the baseline
    b = df[df.run_id == base].iloc[0]  # the baseline row
    for _, r in df[df.run_id != base].iterrows():  # every other run
        better, worse = [], []  # metrics that improved and worsened
        if r["completion_rate"] > b["completion_rate"]:  # more people evacuated
            better.append("completion rate")  # better
        elif r["completion_rate"] < b["completion_rate"]:  # fewer people evacuated
            worse.append("completion rate")  # worse
        for col, label in LOWER_IS_BETTER.items():  # metrics where lower is better
            if pd.isna(r[col]) or pd.isna(b[col]) or r[col] == b[col]:  # missing or equal
                continue  # nothing to compare
            (better if r[col] < b[col] else worse).append(label)  # lower is better
        st.markdown(f"**Run {int(r.run_id)} ({r.routing_strategy}) vs baseline run {base}:** better on: "  # sentence (part 1)
                    f"{', '.join(better) or 'none'}; worse on: {', '.join(worse) or 'none'}. "  # sentence (part 2)
                    "Which matters more depends on the decision-maker's priorities.")  # sentence (part 3)


def render_strategies(conn: sqlite3.Connection) -> None:  # the Strategies tab
    t1, t2, t3, t4 = st.tabs(["Compare saved runs", "Counterfactual lab", "Alert lead-time experiment", "Alert planner"])  # four sub-tabs
    scen = repo.list_scenarios(conn)  # all scenarios
    with t1:  # compare saved runs
        if scen:  # at least one scenario
            c = st.columns([2, 3, 1])  # three columns
            sid = c[0].selectbox("Scenario", [s["scenario_id"] for s in scen], key="pair_s", format_func=lambda i: next(  # choose a scenario (part 1)
                f"#{s['scenario_id']} {s['scenario_name']}" for s in scen if s["scenario_id"] == i))  # readable names (part 2)
            strategies = c[1].multiselect("Strategies", settings.ROUTING_STRATEGIES, default=["SHORTEST_PATH", "CONGESTION_AWARE"])  # choose strategies
            if c[2].button("Run paired", disabled=len(strategies) < 2):  # needs at least two
                with st.spinner("Running each strategy..."):  # progress indicator
                    ex.run_paired_strategies(conn, sid, strategies)  # run them on the same crowd
                st.rerun()  # redraw the page
        runs = repo.list_runs(conn)  # all completed runs
        if len(runs) >= 2:  # enough to compare
            chosen = st.multiselect("Runs to compare", [r["run_id"] for r in runs], default=[r["run_id"] for r in runs[:3]],  # choose runs (part 1)
                                    format_func=lambda i: _run_label(next(r for r in runs if r["run_id"] == i)))  # readable labels (part 2)
            if len(chosen) >= 2:  # at least two chosen
                df = an.compare_runs(conn, chosen)  # side-by-side metrics from SQL
                if df["scenario_name"].nunique() > 1:  # runs of different scenarios
                    st.warning("These runs come from different scenarios; differences mix routing effects with scenario "  # warning (part 1)
                               "differences. Use runs of the same scenario for a fair comparison.")  # warning (part 2)
                base = st.selectbox("Baseline run", chosen)  # choose the baseline
                st.dataframe(df.set_index("run_id").T.astype(str), **STRETCH)  # table with runs as columns
                rel = an.relative_changes(df, base)  # change versus the baseline
                st.markdown("**Relative change vs baseline**")  # heading
                st.dataframe(rel.style.format({c: "{:+.1%}" for c in rel.columns if c.endswith("_rel_change")}, na_rep="n/a"),  # percentages with a sign (part 1)
                             **STRETCH, hide_index=True)  # (part 2)
                c = st.columns(2)  # two columns
                c[0].plotly_chart(charts.bar_by_run(df, "completion_rate", "Completion rate", "fraction safe"), **STRETCH)  # chart: completion
                c[1].plotly_chart(charts.bar_by_run(df, "total_waiting_time_seconds", "Total waiting time", "person-seconds"), **STRETCH)  # chart: waiting
                _trade_offs(df, base)  # plain-language trade-offs
        else:  # fewer than two runs
            st.info("Need at least two completed runs.")  # explain
    with t2:  # counterfactual lab
        if not scen:  # no scenarios
            st.info("Save a scenario first.")  # explain
        else:  # at least one scenario
            sid = st.selectbox("Baseline scenario", [s["scenario_id"] for s in scen], key="cf_s", format_func=lambda i: next(  # choose a scenario (part 1)
                f"#{s['scenario_id']} {s['scenario_name']}" for s in scen if s["scenario_id"] == i))  # readable names (part 2)
            cfg = repo.load_scenario_config(conn, sid)  # its configuration
            net = repo.load_network(conn, cfg.network_id)  # its road network
            c = st.columns(2)  # two columns
            alt = c[0].selectbox("Alternative strategy", settings.ROUTING_STRATEGIES, index=1)  # strategy for the changed world
            cname = c[1].text_input("Experiment name", f"What-if on scenario {sid}")  # name of the experiment
            mods: dict = {}  # the modifications chosen below
            edge_pool = sorted(net.edges)[:4000]  # the first 4000 roads, to keep the list usable
            close_ids = st.multiselect("Close road segments (edge ids)", edge_pool, format_func=lambda e: (  # choose roads to close (part 1)
                f"edge {e}: {net.edges[e].road_class}, cap {net.edges[e].capacity}, {net.edges[e].length_m:.0f} m"))  # road details (part 2)
            if close_ids:  # some chosen
                mods["add_closures"] = [{"edge_id": e, "close_time_seconds": 0} for e in close_ids]  # close them from the start
            scale = st.slider("Safe-stop admission-rate multiplier", 0.1, 3.0, 1.0, 0.1)  # scale safe-stop throughput
            if scale != 1.0:  # changed
                mods["throughput_scale"] = scale  # apply it
            off = st.multiselect("Make safe stops unavailable", [d.destination_id for d in cfg.destinations],  # choose safe stops to close (part 1)
                                 format_func=lambda i: next(d.name for d in cfg.destinations if d.destination_id == i))  # readable names (part 2)
            if off:  # some chosen
                mods["destination_overrides"] = {i: {"available": False} for i in off}  # close them
            st.write("Modifications:", mods or "none (pure routing comparison)")  # show what will change
            if st.button("Run counterfactual experiment", type="primary"):  # the user starts it
                try:  # validation may fail
                    with st.spinner("Running baseline and alternative..."):  # progress indicator
                        st.session_state["cf_last"] = ex.run_counterfactual(conn, sid, mods, alt, name=cname)  # run and remember the result
                except ValueError as exc:  # invalid modified scenario
                    st.error(str(exc))  # show the reason
            res = st.session_state.get("cf_last")  # the last result
            if res:  # there is one
                st.success(f"Baseline run #{res['baseline_run']} vs alternative run #{res['alternative_run']}")  # headline
                st.write("Recorded changes:", res["changes"] or ["strategy only"])  # what was changed
                comp = an.compare_runs(conn, [res["baseline_run"], res["alternative_run"]])  # the two runs side by side
                st.dataframe(comp.set_index("run_id").T.astype(str), **STRETCH)  # table
                _trade_offs(comp, res["baseline_run"])  # plain-language trade-offs
                diff = an.edge_congestion_diff(conn, res["baseline_run"], res["alternative_run"])  # roads whose congestion changed
                if not diff.empty:  # there are differences
                    st.markdown("**Road segments whose congestion changed most**")  # heading
                    st.plotly_chart(charts.congestion_diff_bars(diff), **STRETCH)  # chart
    with t3:  # alert lead-time experiment
        bombs = [s for s in scen if "bomb" in s["scenario_name"].lower() or "drill" in s["scenario_name"].lower()]  # scenarios that look like bomb drills
        pool = bombs or scen  # fall back to all scenarios
        if not pool:  # none
            st.info("Create a bomb scenario first (sidebar → Bomb areas, then RUN).")  # explain
        else:  # some exist
            sid = st.selectbox("Bomb scenario", [s["scenario_id"] for s in pool], key="lead_s", format_func=lambda i: next(  # choose one (part 1)
                f"#{s['scenario_id']} {s['scenario_name']}" for s in pool if s["scenario_id"] == i))  # readable names (part 2)
            cfg = repo.load_scenario_config(conn, sid)  # its configuration
            if not cfg.params.hazards:  # it has no bomb
                st.warning("This scenario has no bomb hazard.")  # explain
            else:  # it has a bomb
                leads = st.multiselect("Alert lead times (s before the strike)", [30, 60, 120, 180, 240, 300, 420], default=[60, 120, 240])  # choose lead times
                if st.button("Run lead-time experiment", type="primary", disabled=not leads):  # the user starts it
                    rows = []  # result rows
                    with st.spinner("Simulating each lead time..."):  # progress indicator
                        for lead in sorted(leads):  # each lead time
                            new_id, _ = ex.create_derived_scenario(conn, sid, {"hazard_alert_lead_s": lead},  # derived scenario with that lead time (part 1)
                                                                   name=f"{cfg.name} (alert {lead}s before)")  # (part 2)
                            rid = ex.run_scenario(conn, new_id, label=f"lead:{lead}")  # run it
                            h, r = an.hazard_summary(conn, rid), repo.get_run(conn, rid)  # outcomes and run row
                            rows.append({"alert lead (s)": lead, "run": rid, "safe before blast": h["safe_before_strike"],  # one result row (part 1)
                                         "caught in blast": h["caught_in_blast"], "completion": r["completion_rate"],  # (part 2)
                                         "avg alert→safe (s)": h["avg_alert_to_safe_s"]})  # (part 3)
                    st.session_state["lead_rows"] = rows  # remember the results
                rows = st.session_state.get("lead_rows")  # the remembered results
                if rows:  # there are some
                    df = pd.DataFrame(rows)  # as a table
                    st.dataframe(df, **STRETCH, hide_index=True)  # show it
                    st.bar_chart(df.set_index("alert lead (s)")[["safe before blast", "caught in blast"]])  # chart: safe versus caught per lead time
    with t4:  # alert planner
        render_alert_planner(conn, scen)  # drawn by its own function


def render_alert_planner(conn: sqlite3.Connection, scen: list) -> None:  # the Alert planner sub-tab
    """Who does the alert miss when many people have no phone, and what would reach them?"""
    pool = [s for s in scen if "bomb" in s["scenario_name"].lower() or "drill" in s["scenario_name"].lower()] or scen  # bomb scenarios, or all
    if not pool:  # none
        st.info("Create a bomb scenario first.")  # explain
        return  # nothing more to show
    sid = st.selectbox("Bomb scenario", [s["scenario_id"] for s in pool], key="plan_s", format_func=lambda i: next(  # choose one (part 1)
        f"#{s['scenario_id']} {s['scenario_name']}" for s in pool if s["scenario_id"] == i))  # readable names (part 2)
    if not repo.load_scenario_config(conn, sid).params.hazards:  # it has no bomb
        st.warning("This scenario has no bomb hazard.")  # explain
        return  # nothing more to show
    c = st.columns(4)  # four controls
    reach = c[0].slider("People with a phone at hand (%)", 0, 100, 40, 5)  # share with a phone
    n_sir = c[1].slider("Sirens to place", 0, 4, 2)  # number of sirens
    rad = c[2].slider("Siren radius (m)", 100, 800, 400, 50)  # how far a siren is heard
    neigh = c[3].slider("Neighbour relay (m)", 0, 120, 60, 10)  # neighbour passing distance
    if st.button("Run alert planner", type="primary"):  # the user starts it
        with st.spinner("Running four paired simulations..."):  # progress indicator
            st.session_state["plan"] = ex.run_alert_planner(conn, sid, reach / 100.0, n_sir, float(rad), float(neigh))  # run and remember the result
    plan = st.session_state.get("plan")  # the remembered result
    if not plan:  # none yet
        return  # nothing to show
    df = pd.DataFrame(plan["rows"])  # as a table
    st.dataframe(df, **STRETCH, hide_index=True)  # show it
    st.bar_chart(df.set_index("variant")[["by phone", "by siren", "by neighbour", "never alerted"]])  # chart: how people were warned
    st.bar_chart(df.set_index("variant")[["safe before blast", "caught in blast"]])  # chart: outcomes
    if plan["sirens"]:  # sirens were recommended
        st.markdown("**Recommended siren sites** (chosen to cover people the phones missed, outside the blast radius)")  # heading
        st.dataframe(pd.DataFrame(plan["sirens"])[["node", "lat", "lon", "radius_m", "covers"]], **STRETCH, hide_index=True)  # the sites


# ----------------------------------------------------------------------------------------- SQL
CATALOGUE = {  # the query catalogue: name -> (SQL text, how its parameters are bound)
    "Completion rate by run": (q.COMPLETION_BY_RUN, "none"),  # no parameters
    "Average / median / 95th-percentile time": (q.EVACUATION_TIME_STATS, "none"),  # no parameters
    "Outcome breakdown": (q.OUTCOME_BREAKDOWN, "run2"),  # run id twice
    "Average waiting time by safe stop": (q.WAIT_BY_DESTINATION, "run"),  # run id
    "Peak congestion by road segment": (q.PEAK_CONGESTION_BY_EDGE, "run_limit"),  # run id and row limit
    "Most frequently used road segments": (q.MOST_USED_EDGES, "run_limit"),  # run id and row limit
    "Bottleneck severity ranking": (q.BOTTLENECK_RANKING, "run"),  # run id
    "Routing strategy comparison": (q.STRATEGY_COMPARISON, "none"),  # no parameters
    "Scenario history": (q.SCENARIO_HISTORY, "none"),  # no parameters
    "Before / after interventions": (q.INTERVENTION_COMPARISON, "none"),  # no parameters
    "Crowd occupancy time series": (q.OCCUPANCY_TIME_SERIES, "run"),  # run id
    "Repeated runs under different seeds": (q.SEED_VARIABILITY, "none"),  # no parameters
}


def render_sql(conn: sqlite3.Connection) -> None:  # the SQL tab
    name = st.selectbox("Query", list(CATALOGUE))  # choose a query
    sql, kind = CATALOGUE[name]  # its text and parameter style
    params: tuple = ()  # bound parameters
    if kind != "none":  # the query needs a run id
        runs = repo.list_runs(conn)  # completed runs
        if not runs:  # none
            st.info("No completed runs yet — nothing has been measured.")  # explain
            st.code(sql.strip(), language="sql")  # still show the SQL
            return  # nothing more to show
        rid = st.selectbox("Run", [r["run_id"] for r in runs], key="sql_run")  # choose a run
        lim = st.number_input("Row limit", 1, 500, 15) if kind == "run_limit" else None  # row limit when needed
        params = {"run": (rid,), "run2": (rid, rid), "run_limit": (rid, int(lim) if lim else 15)}[kind]  # parameters for this query
    st.code(sql.strip(), language="sql")  # show the SQL text
    cur = conn.execute(sql, params)  # run it with bound parameters
    df = pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=[d[0] for d in cur.description])  # result as a table
    st.dataframe(df, **STRETCH, hide_index=True)  # show it
    st.markdown("##### Read-only SQL console")  # heading
    user_sql = st.text_area("SELECT / WITH only", "SELECT event_type, COUNT(*) AS n FROM simulation_events GROUP BY event_type ORDER BY n DESC")  # the user's own query
    if st.button("Run query"):  # the user runs it
        stmt = user_sql.strip().rstrip(";")  # tidy the statement
        if not re.match(r"^(select|with)\b", stmt, re.I) or ";" in stmt:  # must be one SELECT or WITH
            st.error("Only a single SELECT/WITH statement is allowed.")  # refuse
        else:  # allowed
            ro = sqlite3.connect(f"file:{settings.DB_PATH.as_posix()}?mode=ro", uri=True)  # a separate read-only connection
            try:  # SQL may be wrong
                ro.execute("PRAGMA query_only = ON")  # a second safety lock against writes
                res = ro.execute(f"SELECT * FROM ({stmt}) LIMIT 1000")  # wrap it to cap the result at 1000 rows
                st.dataframe(pd.DataFrame(res.fetchall(), columns=[d[0] for d in res.description]), **STRETCH, hide_index=True)  # show the result
            except sqlite3.Error as exc:  # SQL error
                st.error(f"SQL error: {exc}")  # show it
            finally:  # always
                ro.close()  # close the connection
    st.markdown("##### History and integrity checks")  # heading
    st.dataframe(an.scenario_history(conn), **STRETCH, hide_index=True)  # every scenario and its runs
    runs = repo.list_runs(conn)  # completed runs
    if runs:  # at least one
        rid = st.selectbox("Run to check", [r["run_id"] for r in runs], key="chk_run")  # choose a run
        if st.button("Run integrity checks"):  # the user starts them
            res = ex.consistency_checks(conn, rid)  # run the nine checks
            st.dataframe(res, **STRETCH, hide_index=True)  # show the results
            (st.success if all(r["passed"] for r in res) else st.error)("All checks passed." if all(r["passed"] for r in res) else "Some checks failed.")  # green or red verdict
