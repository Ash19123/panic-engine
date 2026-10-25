"""Experiment orchestration: runs, paired strategy comparisons, interventions, checks.

Fairness rule: a *paired* routing comparison runs several strategies on the SAME
scenario row, hence the same stored agent population, network, destinations,
closures and seed. An *intervention* comparison creates a derived scenario that
differs from its parent only by an explicit, recorded modification set.
"""
from __future__ import annotations  # modern type hints on older Python

import json  # (kept for callers that expect it)
import logging  # log failures
import sqlite3  # database type hints
from typing import Dict, List, Optional, Sequence, Tuple  # type hints

from database import repositories as repo  # save and load helpers
from simulation.bottlenecks import BottleneckThresholds  # bottleneck limits
from simulation.engine import EvacuationSimulation, SimulationResult  # the engine and its result type
from simulation.models import ClosureSpec, ScenarioConfig, validate_scenario  # scenario classes and validation

log = logging.getLogger(__name__)  # logger for this module


# ------------------------------------------------------------------- runs
def run_scenario(conn: sqlite3.Connection, scenario_id: int, strategy: Optional[str] = None,
                 label: Optional[str] = None, thresholds: Optional[BottleneckThresholds] = None) -> int:  # simulate and store one run
    """Simulate one scenario and persist the outcome. Returns the run id."""
    cfg = repo.load_scenario_config(conn, scenario_id)  # read the scenario from the database
    if strategy:  # caller overrides the routing policy
        cfg.routing_strategy = strategy  # use it
    network = repo.load_network(conn, cfg.network_id)  # read its road network
    errors, _ = validate_scenario(network, cfg)  # check the scenario makes sense
    if errors:  # something is wrong
        raise ValueError("Invalid scenario: " + "; ".join(errors))  # refuse to run
    agents = repo.ensure_population(conn, scenario_id, network, cfg)  # the stored crowd (generated once)
    run_id = repo.create_run(conn, scenario_id, cfg.routing_strategy, cfg.random_seed, cfg.snapshot_json(), label)  # run row with status RUNNING
    try:  # a failure must mark the run as failed
        result = EvacuationSimulation(network, cfg, agents, thresholds).run()  # simulate
        repo.persist_result(conn, run_id, result)  # store everything in one transaction
    except Exception as exc:  # any error
        log.exception("Run %s failed", run_id)  # log the traceback
        repo.mark_run_failed(conn, run_id, f"{type(exc).__name__}: {exc}")  # record the failure
        raise  # pass the error on
    return run_id  # the new run's id


def run_paired_strategies(conn: sqlite3.Connection, scenario_id: int,
                          strategies: Sequence[str] = ("SHORTEST_PATH", "CONGESTION_AWARE")) -> Dict[str, int]:  # same scenario, several strategies
    runs = {s: run_scenario(conn, scenario_id, s, label=f"paired:{s}") for s in strategies}  # one run per strategy
    base = runs[strategies[0]]  # the first strategy is the baseline
    for s in strategies[1:]:  # compare every other strategy to it
        repo.save_intervention(conn, f"Paired {strategies[0]} vs {s}", "PAIRED_STRATEGY", "ROUTING_POLICY",  # record the comparison (part 1)
                               base, runs[s], {"strategies": list(strategies), "scenario_id": scenario_id})  # (part 2)
    return runs  # strategy -> run id


# ------------------------------------------------------- interventions
def apply_modifications(cfg: ScenarioConfig, mods: dict) -> Tuple[ScenarioConfig, List[str]]:  # change a scenario copy
    """Return (modified copy, human-readable change list). Pure function."""
    new = cfg.copy()  # never change the original
    changes: List[str] = []  # description of each change
    for c in mods.get("add_closures", []):  # roads to close
        spec = ClosureSpec(int(c["edge_id"]), float(c.get("close_time_seconds", 0.0)))  # closure definition
        if all(x.edge_id != spec.edge_id for x in new.closures):  # not already closed
            new.closures.append(spec)  # add it
            changes.append(f"close edge {spec.edge_id} at t={spec.close_time_seconds:g}s")  # describe it
    for eid in mods.get("remove_closure_edges", []):  # roads to reopen
        new.closures = [c for c in new.closures if c.edge_id != eid]  # drop their closures
        changes.append(f"reopen edge {eid}")  # describe it
    scale = mods.get("throughput_scale")  # factor for safe-stop throughput
    for d in new.destinations:  # every safe stop
        if scale is not None:  # scaling requested
            d.throughput_per_timestep = max(1, int(round(d.throughput_per_timestep * scale)))  # scale it, minimum 1
        ov = mods.get("destination_overrides", {}).get(d.destination_id) or \
            mods.get("destination_overrides", {}).get(str(d.destination_id))  # per-stop overrides (id as number or text)
        if ov:  # this stop has overrides
            d.capacity = int(ov.get("capacity", d.capacity))  # new capacity
            d.throughput_per_timestep = int(ov.get("throughput_per_timestep", d.throughput_per_timestep))  # new throughput
            d.available = bool(ov.get("available", d.available))  # open or closed
            changes.append(f"destination {d.name!r}: {ov}")  # describe it
    if scale is not None:  # report the scaling once
        changes.append(f"destination throughput x{scale}")  # describe it
    for eid, cap in mods.get("edge_capacity_overrides", {}).items():  # road capacity changes
        new.params.edge_capacity_overrides[int(eid)] = int(cap)  # apply
        changes.append(f"edge {eid} capacity -> {cap}")  # describe it
    if "agent_count" in mods:  # different crowd size
        new.agent_count = int(mods["agent_count"])  # apply
        changes.append(f"agent_count {cfg.agent_count} -> {new.agent_count}")  # describe it
    if "crowd_distribution" in mods:  # different start distribution
        new.params.crowd_distribution = mods["crowd_distribution"]  # apply
        changes.append(f"crowd_distribution -> {mods['crowd_distribution']}")  # describe it
    if "alert_channels" in mods:  # different alert channels
        new.params.alert_channels = dict(mods["alert_channels"])  # apply
        changes.append(f"alert channels -> {mods['alert_channels']}")  # describe it
    if "hazard_alert_lead_s" in mods:  # different warning time
        lead = float(mods["hazard_alert_lead_s"])  # seconds before the strike
        for h in new.params.hazards:  # every bomb
            h["alert_time_s"] = max(0.0, h["strike_time_s"] - lead)  # alert that long before the strike (not before t=0)
        changes.append(f"alert lead time -> {lead:g}s before the strike")  # describe it
    if "time_limit_seconds" in mods:  # different time limit
        new.time_limit_seconds = float(mods["time_limit_seconds"])  # apply
        changes.append(f"time limit -> {new.time_limit_seconds:g}s")  # describe it
    return new, changes  # the modified copy and its change list


def intervention_type(mods: dict, strategy_changed: bool) -> str:  # label the kind of intervention
    kinds = set()  # kinds present
    if mods.get("add_closures") or mods.get("remove_closure_edges"):  # road closures changed
        kinds.add("ROAD_CLOSURE")  # label
    if mods.get("edge_capacity_overrides"):  # road capacities changed
        kinds.add("CAPACITY_CHANGE")  # label
    if mods.get("destination_overrides") or mods.get("throughput_scale") is not None:  # safe stops changed
        kinds.add("DESTINATION_CHANGE")  # label
    if strategy_changed:  # routing policy changed
        kinds.add("ROUTING_POLICY")  # label
    if not kinds:  # nothing changed
        return "NONE"  # no intervention
    return kinds.pop() if len(kinds) == 1 else "COMBINED"  # the single kind, or COMBINED


def create_derived_scenario(conn: sqlite3.Connection, parent_id: int, mods: dict,
                            name: Optional[str] = None) -> Tuple[int, List[str]]:  # save a modified copy of a scenario
    parent = repo.load_scenario_config(conn, parent_id)  # the original
    new, changes = apply_modifications(parent, mods)  # the modified copy
    new.name = name or f"{parent.name} [modified]"  # give it a name
    new.parent_scenario_id = parent_id  # link it to its parent
    network = repo.load_network(conn, parent.network_id)  # its road network
    errors, _ = validate_scenario(network, new)  # check it makes sense
    if errors:  # something is wrong
        raise ValueError("Invalid modified scenario: " + "; ".join(errors))  # refuse
    sid = repo.save_scenario(conn, new, network)  # store it
    same_population = new.agent_count == parent.agent_count and \
        new.params.crowd_distribution == parent.params.crowd_distribution and \
        new.params.hazards == parent.params.hazards and \
        new.params.alert_channels == parent.params.alert_channels  # can the parent's crowd be reused as is?
    if same_population:  # yes: identical people, so the comparison is fair
        repo.clone_population(conn, parent_id, sid)  # copy the agents
    else:  # the crowd definition changed
        repo.ensure_population(conn, sid, network, new)  # generate a new one
    return sid, changes  # new scenario id and what changed


def run_counterfactual(conn: sqlite3.Connection, baseline_scenario_id: int, mods: dict,
                       alt_strategy: str = "CONGESTION_AWARE", baseline_strategy: str = "SHORTEST_PATH",
                       name: Optional[str] = None) -> dict:  # baseline versus modified world
    """Counterfactual Evacuation Lab: baseline run, modified world, alternative run, recorded comparison."""
    base_run = run_scenario(conn, baseline_scenario_id, baseline_strategy, label="counterfactual:baseline")  # run the original
    sid, changes = create_derived_scenario(conn, baseline_scenario_id, mods, name)  # build the modified scenario
    alt_run = run_scenario(conn, sid, alt_strategy, label="counterfactual:alternative")  # run it
    itype = intervention_type(mods, alt_strategy != baseline_strategy)  # classify the intervention
    params = {"modifications": mods, "changes": changes, "baseline_strategy": baseline_strategy,  # what was changed (part 1)
              "alternative_strategy": alt_strategy, "derived_scenario_id": sid}  # (part 2)
    iid = repo.save_intervention(conn, name or f"Counterfactual on scenario {baseline_scenario_id}", "INTERVENTION",  # store the comparison (part 1)
                                 itype, base_run, alt_run, params,  # (part 2)
                                 "Simulated what-if under model assumptions; not a prediction of real events.")  # honest note
    return {"baseline_run": base_run, "alternative_run": alt_run, "derived_scenario_id": sid,  # result ids (part 1)
            "intervention_id": iid, "changes": changes}  # (part 2)


def evaluation_variants(base: ScenarioConfig, busiest_edges: Sequence[int]) -> Dict[str, dict]:  # the standard test conditions
    """Modification sets for the standard evaluation matrix (see scripts/run_benchmarks.py)."""
    first_dest = base.destinations[0].destination_id  # the first safe stop
    variants: Dict[str, dict] = {  # name -> modifications
        "normal": {},  # nothing changed
        "one_blocked_road": {"add_closures": [{"edge_id": busiest_edges[0], "close_time_seconds": 0}]},  # the busiest road is closed from the start
        "one_unavailable_destination": {"destination_overrides": {first_dest: {"available": False}}},  # one safe stop is closed
        "reduced_destination_throughput": {"throughput_scale": 0.34},  # safe stops admit about a third as fast
        "increased_population": {"agent_count": base.agent_count * 2},  # twice as many people
        "uneven_origin_distribution": {"crowd_distribution": "concentrated"},  # most people start at one place
    }
    if len(busiest_edges) >= 3:  # enough roads to pick three
        variants["multiple_bottlenecks"] = {"edge_capacity_overrides": {e: 4 for e in busiest_edges[:3]}}  # squeeze the three busiest roads
    return variants  # all conditions


# ------------------------------------------------------- consistency checks
def consistency_checks(conn: sqlite3.Connection, run_id: int) -> List[dict]:  # integrity checks on a stored run
    """Integrity checks over one stored run. Each returns passed + the offending count."""
    def one(sql: str, *args) -> int:  # run a query that returns one number
        return int(conn.execute(sql, args).fetchone()[0] or 0)  # first column of the first row, 0 if NULL

    checks = []  # results

    def add(name: str, bad: int, note: str) -> None:  # record one check
        checks.append({"check": name, "passed": bad == 0, "violations": bad, "note": note})  # passes when there are no violations

    run = repo.get_run(conn, run_id)  # the run row
    sid = run["scenario_id"]  # its scenario
    add("evacuated agents have a valid completion time",  # check 1: times make sense
        one("SELECT COUNT(*) FROM agent_outcomes WHERE run_id=? AND final_state='EVACUATED' AND "  # SQL part 1
            "(evacuation_time_seconds IS NULL OR evacuation_time_seconds < departure_time_seconds "  # SQL part 2
            "OR evacuation_time_seconds > ?)", run_id, run["total_simulated_seconds"]),  # SQL part 3 and its parameters
        "EVACUATED => 0 <= departure <= evacuation time <= simulated time")  # explanation
    add("no agent evacuated more than once",  # check 2: no duplicates
        one("SELECT COUNT(*) FROM (SELECT agent_id FROM simulation_events WHERE run_id=? AND "  # SQL part 1
            "event_type='AGENT_EVACUATED' GROUP BY agent_id HAVING COUNT(*) > 1)", run_id), "events per agent")  # SQL part 2, parameter, explanation
    add("evacuation events match EVACUATED outcomes",  # check 3: log equals table
        one("SELECT ABS((SELECT COUNT(*) FROM simulation_events WHERE run_id=? AND event_type='AGENT_EVACUATED') - "  # SQL part 1
            "(SELECT COUNT(*) FROM agent_outcomes WHERE run_id=? AND final_state='EVACUATED'))", run_id, run_id),  # SQL part 2 and parameters
        "event log vs outcome table")  # explanation
    add("no edge entered after its closure took effect",  # check 4: closures respected
        one("SELECT COUNT(*) FROM simulation_events e JOIN simulation_events b "  # SQL part 1
            "ON b.run_id=e.run_id AND b.edge_id=e.edge_id AND b.event_type='ROAD_BLOCKED' "  # SQL part 2
            "WHERE e.run_id=? AND e.event_type='EDGE_ENTERED' AND e.simulation_time_seconds >= b.simulation_time_seconds",  # SQL part 3
            run_id), "agents already on the edge may finish it (documented in-progress policy)")  # parameter and explanation
    add("destination capacity respected",  # check 5: safe stops not over capacity
        one("SELECT COUNT(*) FROM (SELECT o.destination_id, COUNT(*) n FROM agent_outcomes o WHERE o.run_id=? AND "  # SQL part 1
            "o.final_state='EVACUATED' GROUP BY o.destination_id) x JOIN scenario_destinations sd "  # SQL part 2
            "ON sd.destination_id = x.destination_id AND sd.scenario_id = ? WHERE x.n > sd.capacity", run_id, sid),  # SQL part 3 and parameters
        "admitted <= capacity")  # explanation
    add("destination throughput respected",  # check 6: per-step admissions within the limit
        one("SELECT COUNT(*) FROM (SELECT destination_id, simulation_time_seconds, COUNT(*) n FROM simulation_events "  # SQL part 1
            "WHERE run_id=? AND event_type='AGENT_EVACUATED' GROUP BY destination_id, simulation_time_seconds) x "  # SQL part 2
            "JOIN scenario_destinations sd ON sd.destination_id = x.destination_id AND sd.scenario_id = ? "  # SQL part 3
            "WHERE x.n > sd.throughput_per_timestep", run_id, sid), "admissions per timestep <= throughput")  # SQL part 4, parameters, explanation
    add("completion-rate denominator is every agent",  # check 7: nobody dropped from the denominator
        one("SELECT CASE WHEN r.total_agents = (SELECT COUNT(*) FROM agent_outcomes WHERE run_id=r.run_id) "  # SQL part 1
            "AND r.total_agents = (SELECT agent_count FROM scenarios WHERE scenario_id=r.scenario_id) "  # SQL part 2
            "AND ABS(r.completion_rate - 1.0*r.evacuated_agents/r.total_agents) < 1e-9 THEN 0 ELSE 1 END "  # SQL part 3
            "FROM simulation_runs r WHERE r.run_id=?", run_id), "rate = evacuated / all agents")  # SQL part 4, parameter, explanation
    add("evacuated + unresolved + failed = total",  # check 8: the totals add up
        one("SELECT CASE WHEN evacuated_agents + unresolved_agents + failed_agents = total_agents THEN 0 ELSE 1 END "  # SQL part 1
            "FROM simulation_runs WHERE run_id=?", run_id), "nobody silently dropped")  # SQL part 2, parameter, explanation
    add("stored metrics equal SQL recomputation",  # check 9: Python numbers equal SQL numbers
        one("SELECT CASE WHEN r.evacuated_agents = (SELECT COUNT(*) FROM agent_outcomes WHERE run_id=r.run_id "  # SQL part 1
            "AND final_state='EVACUATED') AND r.failed_agents = (SELECT COUNT(*) FROM agent_outcomes WHERE "  # SQL part 2
            "run_id=r.run_id AND final_state='UNABLE_TO_EVACUATE') THEN 0 ELSE 1 END FROM simulation_runs r "  # SQL part 3
            "WHERE r.run_id=?", run_id), "Python vs SQL counts")  # SQL part 4, parameter, explanation
    return checks  # all results


def check_reproducibility(conn: sqlite3.Connection, scenario_id: int, strategy: Optional[str] = None) -> bool:  # same input, same output?
    """Simulate twice in memory (no persistence) and compare full result fingerprints."""
    cfg = repo.load_scenario_config(conn, scenario_id)  # the scenario
    if strategy:  # caller overrides the policy
        cfg.routing_strategy = strategy  # use it
    net = repo.load_network(conn, cfg.network_id)  # its road network
    fps = []  # fingerprints of the two runs
    for _ in range(2):  # two runs
        agents = repo.load_population(conn, scenario_id) or repo.ensure_population(conn, scenario_id, net, cfg)  # fresh agent objects each time
        fps.append(EvacuationSimulation(net, cfg, agents).run().fingerprint())  # run and fingerprint
    return fps[0] == fps[1]  # identical means reproducible


# ------------------------------------------------------------------ alert coverage planner
def run_alert_planner(conn: sqlite3.Connection, scenario_id: int, phone_reach: float = 0.5, n_sirens: int = 2,
                      siren_radius: float = 400.0, neighbour_radius: float = 60.0, siren_delay: float = 10.0,
                      neighbour_delay: float = 30.0) -> Dict:  # compare four alert-channel variants
    """What helps when only part of the population has a phone at hand?

    Four paired runs (same people, start points, reaction delays and seed - only the alert channels differ):
      A  phones only (``phone_reach`` share of people)
      B  A + sirens at the sites recommended for the people A did not reach
      C  B + neighbours passing the word
      D  everybody has a phone (upper bound)
    """
    from database import analytics as an  # imported here to avoid a circular import
    from simulation.alerting import recommend_sirens  # siren placement helper
    base = repo.load_scenario_config(conn, scenario_id)  # the bomb scenario
    if not base.params.hazards:  # no bomb in it
        raise ValueError("The planner needs a scenario with a bomb hazard.")  # cannot plan
    net = repo.load_network(conn, base.network_id)  # its road network
    blasts = [(h["lat"], h["lon"], h["blast_radius_m"]) for h in base.params.hazards]  # areas where a siren would be destroyed
    phones = {"phone_reach": phone_reach}  # the phone share used in variants A to C

    def run(label: str, channels: dict):  # create a derived scenario with these channels and run it
        sid, _ = create_derived_scenario(conn, scenario_id, {"alert_channels": channels}, name=f"{base.name} [{label}]")  # derived scenario
        return sid, run_scenario(conn, sid, label=f"planner:{label}")  # its id and the run id

    sid_a, run_a = run("phones only", phones)  # variant A
    missed = [a.origin_node for a in repo.load_population(conn, sid_a) if a.alert_channel == "NONE"]  # where the people nobody reached start
    sirens = recommend_sirens(net, missed, siren_radius, n_sirens, siren_delay, avoid=blasts)  # best siren sites for them
    variants = [("A: phones only", sid_a, run_a),  # variant A (already run)
                ("B: + sirens", *run("phones + sirens", dict(phones, sirens=sirens))),  # variant B
                ("C: + neighbours", *run("phones + sirens + neighbours", dict(  # variant C (part 1)
                    phones, sirens=sirens, neighbour_radius_m=neighbour_radius, neighbour_delay_s=neighbour_delay))),  # variant C (part 2)
                ("D: everyone has a phone", *run("everyone has a phone", {}))]  # variant D, the upper bound
    rows = []  # one table row per variant
    for label, sid, rid in variants:  # each variant
        ch = an.alert_channel_summary(conn, rid).set_index("channel")  # people per channel
        h, r = an.hazard_summary(conn, rid), repo.get_run(conn, rid)  # hazard outcomes and the run row
        n = int(r["total_agents"])  # crowd size
        count = lambda name: int(ch.loc[name, "people"]) if name in ch.index else 0  # people in a channel, 0 if absent
        rows.append({"variant": label, "run": rid, "by phone": count("PHONE"), "by siren": count("SIREN"),  # how people were warned (part 1)
                     "by neighbour": count("NEIGHBOUR"), "never alerted": count("NONE"),  # (part 2)
                     "safe before blast": h["safe_before_strike"], "caught in blast": h["caught_in_blast"],  # outcomes
                     "completion": r["completion_rate"], "people": n})  # completion rate and crowd size
    return {"rows": rows, "sirens": sirens, "missed_by_phones": len(missed)}  # the table, the siren sites and the miss count
