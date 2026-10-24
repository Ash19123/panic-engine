"""Seed the database with the synthetic network and a demonstration scenario.

Usage:
    python -m database.seed_data                # network + demo scenario (skips what exists)
    python -m database.seed_data --run-demo     # also run the demo scenario with 2 strategies
    python -m database.seed_data --reset --yes  # DELETE the database file first (explicit consent)

Existing user data is never overwritten: seeding only inserts what is missing.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from typing import Optional

from config import settings
from database import repositories as repo
from database.init_db import init_db
from geo.synthetic_network import build_synthetic_network, default_origins_destinations
from simulation.models import OriginSpec, RoadNetwork, ScenarioConfig

log = logging.getLogger(__name__)
DEMO_NAME = "Demo: synthetic grid evacuation"


def get_or_create_synthetic_network(conn: sqlite3.Connection) -> RoadNetwork:
    return repo.save_network(conn, build_synthetic_network())


def create_demo_scenario(conn: sqlite3.Connection, name: str = DEMO_NAME, strategy: str = "SHORTEST_PATH",
                         agents: int = 300, seed: int = 42, time_limit: float = 2400.0) -> int:
    """Create a demo scenario on the synthetic network (origins/destinations are HYPOTHETICAL)."""
    local = build_synthetic_network()
    saved = repo.save_network(conn, local)
    to_db = dict(zip(sorted(local.nodes), sorted(saved.nodes)))       # same content => same order
    origins, dests = default_origins_destinations(local)
    for d in dests:
        d.node_id = to_db[d.node_id]
    cfg = ScenarioConfig(
        name=name, network_id=saved.network_id, routing_strategy=strategy, agent_count=agents, random_seed=seed,
        time_limit_seconds=time_limit, description="Synthetic demonstration scenario. Not real roads.",
        study_area="SYNTHETIC GRID (not a real place)", geographic_data_source="SYNTHETIC",
        network_fingerprint=saved.fingerprint(),
        origins=[OriginSpec(to_db[n], 1.0, label, "SYNTHETIC_DEMO") for n, label in origins],
        destinations=dests)
    # reuse existing hypothetical destination rows for this network instead of duplicating them
    existing = {d.name: d for d in repo.list_destinations(conn, saved.network_id)}
    for d in cfg.destinations:
        if d.name in existing:
            d.destination_id = existing[d.name].destination_id
    return repo.save_scenario(conn, cfg, saved)


def seed(conn: sqlite3.Connection, run_demo: bool = False) -> Optional[int]:
    get_or_create_synthetic_network(conn)
    row = conn.execute("SELECT scenario_id FROM scenarios WHERE scenario_name = ?", (DEMO_NAME,)).fetchone()
    sid = row["scenario_id"] if row else create_demo_scenario(conn)
    if run_demo:
        from simulation.experiments import run_paired_strategies
        have = conn.execute("SELECT COUNT(*) FROM simulation_runs WHERE scenario_id = ? AND status='COMPLETED'",
                            (sid,)).fetchone()[0]
        if have == 0:
            run_paired_strategies(conn, sid, ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED"))
    return sid


def main() -> None:
    settings.configure_logging()
    ap = argparse.ArgumentParser(description="Seed PANIC ENGINE with the synthetic network and a demo scenario.")
    ap.add_argument("--run-demo", action="store_true", help="also simulate the demo scenario (real engine runs)")
    ap.add_argument("--reset", action="store_true", help="delete the database file first (needs --yes)")
    ap.add_argument("--yes", action="store_true", help="confirm destructive --reset")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    path = args.db or settings.DB_PATH
    if args.reset:
        if not args.yes:
            raise SystemExit("--reset deletes all saved experiments; re-run with --yes to confirm.")
        from pathlib import Path
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    conn = init_db(args.db)
    try:
        sid = seed(conn, args.run_demo)
        print(f"Seed complete. Demo scenario id = {sid}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
