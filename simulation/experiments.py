"""Experiment orchestration: runs, paired strategy comparisons, interventions, checks.

Fairness rule: a *paired* routing comparison runs several strategies on the SAME
scenario row, hence the same stored agent population, network, destinations,
closures and seed. An *intervention* comparison creates a derived scenario that
differs from its parent only by an explicit, recorded modification set.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Dict, List, Optional, Sequence, Tuple

from database import repositories as repo
from simulation.bottlenecks import BottleneckThresholds
from simulation.engine import EvacuationSimulation, SimulationResult
from simulation.models import ClosureSpec, ScenarioConfig, validate_scenario

log = logging.getLogger(__name__)


# ------------------------------------------------------------------- runs
def run_scenario(conn: sqlite3.Connection, scenario_id: int, strategy: Optional[str] = None,
                 label: Optional[str] = None, thresholds: Optional[BottleneckThresholds] = None) -> int:
    """Simulate one scenario and persist the outcome. Returns the run id."""
    cfg = repo.load_scenario_config(conn, scenario_id)
    if strategy:
        cfg.routing_strategy = strategy
    network = repo.load_network(conn, cfg.network_id)
    errors, _ = validate_scenario(network, cfg)
    if errors:
        raise ValueError("Invalid scenario: " + "; ".join(errors))
    agents = repo.ensure_population(conn, scenario_id, network, cfg)
    run_id = repo.create_run(conn, scenario_id, cfg.routing_strategy, cfg.random_seed, cfg.snapshot_json(), label)
    try:
        result = EvacuationSimulation(network, cfg, agents, thresholds).run()
        repo.persist_result(conn, run_id, result)
    except Exception as exc:
        log.exception("Run %s failed", run_id)
        repo.mark_run_failed(conn, run_id, f"{type(exc).__name__}: {exc}")
        raise
    return run_id


def run_paired_strategies(conn: sqlite3.Connection, scenario_id: int,
                          strategies: Sequence[str] = ("SHORTEST_PATH", "CONGESTION_AWARE")) -> Dict[str, int]:
    runs = {s: run_scenario(conn, scenario_id, s, label=f"paired:{s}") for s in strategies}
    base = runs[strategies[0]]
    for s in strategies[1:]:
        repo.save_intervention(conn, f"Paired {strategies[0]} vs {s}", "PAIRED_STRATEGY", "ROUTING_POLICY",
                               base, runs[s], {"strategies": list(strategies), "scenario_id": scenario_id})
    return runs


# ------------------------------------------------------- interventions
def apply_modifications(cfg: ScenarioConfig, mods: dict) -> Tuple[ScenarioConfig, List[str]]:
    """Return (modified copy, human-readable change list). Pure function."""
    new = cfg.copy()
    changes: List[str] = []
    for c in mods.get("add_closures", []):
        spec = ClosureSpec(int(c["edge_id"]), float(c.get("close_time_seconds", 0.0)))
        if all(x.edge_id != spec.edge_id for x in new.closures):
            new.closures.append(spec)
            changes.append(f"close edge {spec.edge_id} at t={spec.close_time_seconds:g}s")
    for eid in mods.get("remove_closure_edges", []):
        new.closures = [c for c in new.closures if c.edge_id != eid]
        changes.append(f"reopen edge {eid}")
    scale = mods.get("throughput_scale")
    for d in new.destinations:
        if scale is not None:
            d.throughput_per_timestep = max(1, int(round(d.throughput_per_timestep * scale)))
        ov = mods.get("destination_overrides", {}).get(d.destination_id) or \
            mods.get("destination_overrides", {}).get(str(d.destination_id))
        if ov:
            d.capacity = int(ov.get("capacity", d.capacity))
            d.throughput_per_timestep = int(ov.get("throughput_per_timestep", d.throughput_per_timestep))
            d.available = bool(ov.get("available", d.available))
            changes.append(f"destination {d.name!r}: {ov}")
    if scale is not None:
        changes.append(f"destination throughput x{scale}")
    for eid, cap in mods.get("edge_capacity_overrides", {}).items():
        new.params.edge_capacity_overrides[int(eid)] = int(cap)
        changes.append(f"edge {eid} capacity -> {cap}")
    if "agent_count" in mods:
        new.agent_count = int(mods["agent_count"])
        changes.append(f"agent_count {cfg.agent_count} -> {new.agent_count}")
    if "crowd_distribution" in mods:
        new.params.crowd_distribution = mods["crowd_distribution"]
        changes.append(f"crowd_distribution -> {mods['crowd_distribution']}")
    if "alert_channels" in mods:
        new.params.alert_channels = dict(mods["alert_channels"])
        changes.append(f"alert channels -> {mods['alert_channels']}")
    if "hazard_alert_lead_s" in mods:
        lead = float(mods["hazard_alert_lead_s"])
        for h in new.params.hazards:
            h["alert_time_s"] = max(0.0, h["strike_time_s"] - lead)
        changes.append(f"alert lead time -> {lead:g}s before the strike")
    if "time_limit_seconds" in mods:
        new.time_limit_seconds = float(mods["time_limit_seconds"])
        changes.append(f"time limit -> {new.time_limit_seconds:g}s")
    return new, changes


def intervention_type(mods: dict, strategy_changed: bool) -> str:
    kinds = set()
    if mods.get("add_closures") or mods.get("remove_closure_edges"):
        kinds.add("ROAD_CLOSURE")
    if mods.get("edge_capacity_overrides"):
        kinds.add("CAPACITY_CHANGE")
    if mods.get("destination_overrides") or mods.get("throughput_scale") is not None:
        kinds.add("DESTINATION_CHANGE")
    if strategy_changed:
        kinds.add("ROUTING_POLICY")
    if not kinds:
        return "NONE"
    return kinds.pop() if len(kinds) == 1 else "COMBINED"


def create_derived_scenario(conn: sqlite3.Connection, parent_id: int, mods: dict,
                            name: Optional[str] = None) -> Tuple[int, List[str]]:
    parent = repo.load_scenario_config(conn, parent_id)
    new, changes = apply_modifications(parent, mods)
    new.name = name or f"{parent.name} [modified]"
    new.parent_scenario_id = parent_id
    network = repo.load_network(conn, parent.network_id)
    errors, _ = validate_scenario(network, new)
    if errors:
        raise ValueError("Invalid modified scenario: " + "; ".join(errors))
    sid = repo.save_scenario(conn, new, network)
    same_population = new.agent_count == parent.agent_count and \
        new.params.crowd_distribution == parent.params.crowd_distribution and \
        new.params.hazards == parent.params.hazards and \
        new.params.alert_channels == parent.params.alert_channels
    if same_population:
        repo.clone_population(conn, parent_id, sid)
    else:
        repo.ensure_population(conn, sid, network, new)
    return sid, changes


def run_counterfactual(conn: sqlite3.Connection, baseline_scenario_id: int, mods: dict,
                       alt_strategy: str = "CONGESTION_AWARE", baseline_strategy: str = "SHORTEST_PATH",
                       name: Optional[str] = None) -> dict:
    """Counterfactual Evacuation Lab: baseline run, modified world, alternative run, recorded comparison."""
    base_run = run_scenario(conn, baseline_scenario_id, baseline_strategy, label="counterfactual:baseline")
    sid, changes = create_derived_scenario(conn, baseline_scenario_id, mods, name)
    alt_run = run_scenario(conn, sid, alt_strategy, label="counterfactual:alternative")
    itype = intervention_type(mods, alt_strategy != baseline_strategy)
    params = {"modifications": mods, "changes": changes, "baseline_strategy": baseline_strategy,
              "alternative_strategy": alt_strategy, "derived_scenario_id": sid}
    iid = repo.save_intervention(conn, name or f"Counterfactual on scenario {baseline_scenario_id}", "INTERVENTION",
                                 itype, base_run, alt_run, params,
                                 "Simulated what-if under model assumptions; not a prediction of real events.")
    return {"baseline_run": base_run, "alternative_run": alt_run, "derived_scenario_id": sid,
            "intervention_id": iid, "changes": changes}


def evaluation_variants(base: ScenarioConfig, busiest_edges: Sequence[int]) -> Dict[str, dict]:
    """Modification sets for the standard evaluation matrix (see scripts/run_benchmarks.py)."""
    first_dest = base.destinations[0].destination_id
    variants: Dict[str, dict] = {
        "normal": {},
        "one_blocked_road": {"add_closures": [{"edge_id": busiest_edges[0], "close_time_seconds": 0}]},
        "one_unavailable_destination": {"destination_overrides": {first_dest: {"available": False}}},
        "reduced_destination_throughput": {"throughput_scale": 0.34},
        "increased_population": {"agent_count": base.agent_count * 2},
        "uneven_origin_distribution": {"crowd_distribution": "concentrated"},
    }
    if len(busiest_edges) >= 3:
        variants["multiple_bottlenecks"] = {"edge_capacity_overrides": {e: 4 for e in busiest_edges[:3]}}
    return variants


# ------------------------------------------------------- consistency checks
def consistency_checks(conn: sqlite3.Connection, run_id: int) -> List[dict]:
    """Integrity checks over one stored run. Each returns passed + the offending count."""
    def one(sql: str, *args) -> int:
        return int(conn.execute(sql, args).fetchone()[0] or 0)

    checks = []

    def add(name: str, bad: int, note: str) -> None:
        checks.append({"check": name, "passed": bad == 0, "violations": bad, "note": note})

    run = repo.get_run(conn, run_id)
    sid = run["scenario_id"]
    add("evacuated agents have a valid completion time",
        one("SELECT COUNT(*) FROM agent_outcomes WHERE run_id=? AND final_state='EVACUATED' AND "
            "(evacuation_time_seconds IS NULL OR evacuation_time_seconds < departure_time_seconds "
            "OR evacuation_time_seconds > ?)", run_id, run["total_simulated_seconds"]),
        "EVACUATED => 0 <= departure <= evacuation time <= simulated time")
    add("no agent evacuated more than once",
        one("SELECT COUNT(*) FROM (SELECT agent_id FROM simulation_events WHERE run_id=? AND "
            "event_type='AGENT_EVACUATED' GROUP BY agent_id HAVING COUNT(*) > 1)", run_id), "events per agent")
    add("evacuation events match EVACUATED outcomes",
        one("SELECT ABS((SELECT COUNT(*) FROM simulation_events WHERE run_id=? AND event_type='AGENT_EVACUATED') - "
            "(SELECT COUNT(*) FROM agent_outcomes WHERE run_id=? AND final_state='EVACUATED'))", run_id, run_id),
        "event log vs outcome table")
    add("no edge entered after its closure took effect",
        one("SELECT COUNT(*) FROM simulation_events e JOIN simulation_events b "
            "ON b.run_id=e.run_id AND b.edge_id=e.edge_id AND b.event_type='ROAD_BLOCKED' "
            "WHERE e.run_id=? AND e.event_type='EDGE_ENTERED' AND e.simulation_time_seconds >= b.simulation_time_seconds",
            run_id), "agents already on the edge may finish it (documented in-progress policy)")
    add("destination capacity respected",
        one("SELECT COUNT(*) FROM (SELECT o.destination_id, COUNT(*) n FROM agent_outcomes o WHERE o.run_id=? AND "
            "o.final_state='EVACUATED' GROUP BY o.destination_id) x JOIN scenario_destinations sd "
            "ON sd.destination_id = x.destination_id AND sd.scenario_id = ? WHERE x.n > sd.capacity", run_id, sid),
        "admitted <= capacity")
    add("destination throughput respected",
        one("SELECT COUNT(*) FROM (SELECT destination_id, simulation_time_seconds, COUNT(*) n FROM simulation_events "
            "WHERE run_id=? AND event_type='AGENT_EVACUATED' GROUP BY destination_id, simulation_time_seconds) x "
            "JOIN scenario_destinations sd ON sd.destination_id = x.destination_id AND sd.scenario_id = ? "
            "WHERE x.n > sd.throughput_per_timestep", run_id, sid), "admissions per timestep <= throughput")
    add("completion-rate denominator is every agent",
        one("SELECT CASE WHEN r.total_agents = (SELECT COUNT(*) FROM agent_outcomes WHERE run_id=r.run_id) "
            "AND r.total_agents = (SELECT agent_count FROM scenarios WHERE scenario_id=r.scenario_id) "
            "AND ABS(r.completion_rate - 1.0*r.evacuated_agents/r.total_agents) < 1e-9 THEN 0 ELSE 1 END "
            "FROM simulation_runs r WHERE r.run_id=?", run_id), "rate = evacuated / all agents")
    add("evacuated + unresolved + failed = total",
        one("SELECT CASE WHEN evacuated_agents + unresolved_agents + failed_agents = total_agents THEN 0 ELSE 1 END "
            "FROM simulation_runs WHERE run_id=?", run_id), "nobody silently dropped")
    add("stored metrics equal SQL recomputation",
        one("SELECT CASE WHEN r.evacuated_agents = (SELECT COUNT(*) FROM agent_outcomes WHERE run_id=r.run_id "
            "AND final_state='EVACUATED') AND r.failed_agents = (SELECT COUNT(*) FROM agent_outcomes WHERE "
            "run_id=r.run_id AND final_state='UNABLE_TO_EVACUATE') THEN 0 ELSE 1 END FROM simulation_runs r "
            "WHERE r.run_id=?", run_id), "Python vs SQL counts")
    return checks


def check_reproducibility(conn: sqlite3.Connection, scenario_id: int, strategy: Optional[str] = None) -> bool:
    """Simulate twice in memory (no persistence) and compare full result fingerprints."""
    cfg = repo.load_scenario_config(conn, scenario_id)
    if strategy:
        cfg.routing_strategy = strategy
    net = repo.load_network(conn, cfg.network_id)
    fps = []
    for _ in range(2):
        agents = repo.load_population(conn, scenario_id) or repo.ensure_population(conn, scenario_id, net, cfg)
        fps.append(EvacuationSimulation(net, cfg, agents).run().fingerprint())
    return fps[0] == fps[1]


# ------------------------------------------------------------------ alert coverage planner
def run_alert_planner(conn: sqlite3.Connection, scenario_id: int, phone_reach: float = 0.5, n_sirens: int = 2,
                      siren_radius: float = 400.0, neighbour_radius: float = 60.0, siren_delay: float = 10.0,
                      neighbour_delay: float = 30.0) -> Dict:
    """What helps when only part of the population has a phone at hand?

    Four paired runs (same people, start points, reaction delays and seed - only the alert channels differ):
      A  phones only (``phone_reach`` share of people)
      B  A + sirens at the sites recommended for the people A did not reach
      C  B + neighbours passing the word
      D  everybody has a phone (upper bound)
    """
    from database import analytics as an
    from simulation.alerting import recommend_sirens
    base = repo.load_scenario_config(conn, scenario_id)
    if not base.params.hazards:
        raise ValueError("The planner needs a scenario with a bomb hazard.")
    net = repo.load_network(conn, base.network_id)
    blasts = [(h["lat"], h["lon"], h["blast_radius_m"]) for h in base.params.hazards]
    phones = {"phone_reach": phone_reach}

    def run(label: str, channels: dict):
        sid, _ = create_derived_scenario(conn, scenario_id, {"alert_channels": channels}, name=f"{base.name} [{label}]")
        return sid, run_scenario(conn, sid, label=f"planner:{label}")

    sid_a, run_a = run("phones only", phones)
    missed = [a.origin_node for a in repo.load_population(conn, sid_a) if a.alert_channel == "NONE"]
    sirens = recommend_sirens(net, missed, siren_radius, n_sirens, siren_delay, avoid=blasts)
    variants = [("A: phones only", sid_a, run_a),
                ("B: + sirens", *run("phones + sirens", dict(phones, sirens=sirens))),
                ("C: + neighbours", *run("phones + sirens + neighbours", dict(
                    phones, sirens=sirens, neighbour_radius_m=neighbour_radius, neighbour_delay_s=neighbour_delay))),
                ("D: everyone has a phone", *run("everyone has a phone", {}))]
    rows = []
    for label, sid, rid in variants:
        ch = an.alert_channel_summary(conn, rid).set_index("channel")
        h, r = an.hazard_summary(conn, rid), repo.get_run(conn, rid)
        n = int(r["total_agents"])
        count = lambda name: int(ch.loc[name, "people"]) if name in ch.index else 0
        rows.append({"variant": label, "run": rid, "by phone": count("PHONE"), "by siren": count("SIREN"),
                     "by neighbour": count("NEIGHBOUR"), "never alerted": count("NONE"),
                     "safe before blast": h["safe_before_strike"], "caught in blast": h["caught_in_blast"],
                     "completion": r["completion_rate"], "people": n})
    return {"rows": rows, "sirens": sirens, "missed_by_phones": len(missed)}
