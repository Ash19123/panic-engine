"""Seed the database with the synthetic network and a demonstration scenario.

Usage:
    python -m database.seed_data                # network + demo scenario (skips what exists)
    python -m database.seed_data --run-demo     # also run the demo scenario with 2 strategies
    python -m database.seed_data --reset --yes  # DELETE the database file first (explicit consent)

Existing user data is never overwritten: seeding only inserts what is missing.
"""
from __future__ import annotations  # modern type hints on older Python

import argparse  # command-line options
import logging  # log messages
import sqlite3  # database type hints
from typing import Optional  # optional values

from config import settings  # paths and logging setup
from database import repositories as repo  # save and load helpers
from database.init_db import init_db  # create or migrate the database
from geo.synthetic_network import build_synthetic_network, default_origins_destinations  # the fake grid and its demo layout
from simulation.models import OriginSpec, RoadNetwork, ScenarioConfig  # scenario building blocks

log = logging.getLogger(__name__)  # logger for this module
DEMO_NAME = "Demo: synthetic grid evacuation"  # name of the demo scenario


def get_or_create_synthetic_network(conn: sqlite3.Connection) -> RoadNetwork:  # make sure the grid is stored
    return repo.save_network(conn, build_synthetic_network())  # saving is idempotent: same content, same row


def create_demo_scenario(conn: sqlite3.Connection, name: str = DEMO_NAME, strategy: str = "SHORTEST_PATH",
                         agents: int = 300, seed: int = 42, time_limit: float = 2400.0) -> int:  # build and save a demo scenario
    """Create a demo scenario on the synthetic network (origins/destinations are HYPOTHETICAL)."""
    local = build_synthetic_network()  # the grid with local ids
    saved = repo.save_network(conn, local)  # the same grid with database ids
    to_db = dict(zip(sorted(local.nodes), sorted(saved.nodes)))       # same content => same order
    origins, dests = default_origins_destinations(local)  # demo start points and safe stops
    for d in dests:  # every safe stop
        d.node_id = to_db[d.node_id]  # translate its node id to the database id
    cfg = ScenarioConfig(  # describe the scenario
        name=name, network_id=saved.network_id, routing_strategy=strategy, agent_count=agents, random_seed=seed,  # identity and size
        time_limit_seconds=time_limit, description="Synthetic demonstration scenario. Not real roads.",  # limit and note
        study_area="SYNTHETIC GRID (not a real place)", geographic_data_source="SYNTHETIC",  # clearly labelled as fake
        network_fingerprint=saved.fingerprint(),  # ties the scenario to this exact network
        origins=[OriginSpec(to_db[n], 1.0, label, "SYNTHETIC_DEMO") for n, label in origins],  # where people start
        destinations=dests)  # the safe stops
    # reuse existing hypothetical destination rows for this network instead of duplicating them
    existing = {d.name: d for d in repo.list_destinations(conn, saved.network_id)}  # safe stops already stored
    for d in cfg.destinations:  # every safe stop of this scenario
        if d.name in existing:  # already in the database
            d.destination_id = existing[d.name].destination_id  # reuse its row
    return repo.save_scenario(conn, cfg, saved)  # store the scenario and return its id


def seed(conn: sqlite3.Connection, run_demo: bool = False) -> Optional[int]:  # insert whatever is missing
    get_or_create_synthetic_network(conn)  # make sure the grid exists
    row = conn.execute("SELECT scenario_id FROM scenarios WHERE scenario_name = ?", (DEMO_NAME,)).fetchone()  # is the demo already there?
    sid = row["scenario_id"] if row else create_demo_scenario(conn)  # reuse it or create it
    if run_demo:  # also simulate it
        from simulation.experiments import run_paired_strategies  # imported here to avoid a circular import
        have = conn.execute("SELECT COUNT(*) FROM simulation_runs WHERE scenario_id = ? AND status='COMPLETED'",
                            (sid,)).fetchone()[0]  # how many finished runs exist already
        if have == 0:  # none yet
            run_paired_strategies(conn, sid, ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED"))  # run all three strategies
    return sid  # the demo scenario id


def main() -> None:  # command-line entry point
    settings.configure_logging()  # set up logging
    ap = argparse.ArgumentParser(description="Seed PANIC ENGINE with the synthetic network and a demo scenario.")  # option parser
    ap.add_argument("--run-demo", action="store_true", help="also simulate the demo scenario (real engine runs)")  # flag: run it
    ap.add_argument("--reset", action="store_true", help="delete the database file first (needs --yes)")  # flag: wipe the database
    ap.add_argument("--yes", action="store_true", help="confirm destructive --reset")  # flag: confirm the wipe
    ap.add_argument("--db", default=None)  # optional database path
    args = ap.parse_args()  # read the options
    path = args.db or settings.DB_PATH  # which database file
    if args.reset:  # the user asked to delete everything
        if not args.yes:  # not confirmed
            raise SystemExit("--reset deletes all saved experiments; re-run with --yes to confirm.")  # stop with a message
        from pathlib import Path  # only needed here
        for suffix in ("", "-wal", "-shm"):  # the database file and its WAL side files
            Path(str(path) + suffix).unlink(missing_ok=True)  # delete each if present
    conn = init_db(args.db)  # create or migrate the database
    try:  # always close the connection
        sid = seed(conn, args.run_demo)  # insert what is missing
        print(f"Seed complete. Demo scenario id = {sid}.")  # report
    finally:  # cleanup
        conn.close()  # close the connection


if __name__ == "__main__":  # run only when executed as a script
    main()  # start
