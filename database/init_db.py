"""Database initialisation with an explicit, versioned migration mechanism.

Usage:  python -m database.init_db [--db PATH]

Migrations are applied in order and recorded in ``schema_migrations``. Running
the script repeatedly never destroys data: already-applied versions are skipped.
To evolve the schema, append a new ``Migration`` to ``MIGRATIONS``.
"""
from __future__ import annotations  # modern type hints on older Python

import argparse  # command-line options
import logging  # log applied migrations
import sqlite3  # database type hints
from dataclasses import dataclass  # simple record class
from pathlib import Path  # file paths
from typing import Optional  # optional values

from config import settings  # folders and logging setup
from database.connection import PathLike, connect, transaction  # connection helpers

log = logging.getLogger(__name__)  # logger for this module
SCHEMA_FILE = Path(__file__).with_name("schema.sql")  # the base schema sits next to this file


@dataclass(frozen=True)
class Migration:  # one schema change
    version: int  # its number
    description: str  # what it does
    sql: str  # the SQL to run


def _v1_sql() -> str:  # version 1 is the whole base schema
    return SCHEMA_FILE.read_text(encoding="utf-8")  # read it from schema.sql


_V2_SQL = """
BEGIN;
ALTER TABLE agents ADD COLUMN alert_time_seconds REAL;
CREATE TABLE simulation_events_v2 (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    agent_id                INTEGER REFERENCES agents(agent_id),
    simulation_time_seconds REAL NOT NULL,
    event_type              TEXT NOT NULL CHECK (event_type IN (
        'AGENT_DEPARTED', 'EDGE_ENTERED', 'EDGE_EXITED', 'QUEUE_JOINED', 'QUEUE_LEFT', 'ROUTE_CHANGED',
        'ROAD_BLOCKED', 'AGENT_EVACUATED', 'AGENT_UNRESOLVED', 'AGENT_FAILED', 'ALERT_SENT', 'HAZARD_STRIKE')),
    node_id                 INTEGER REFERENCES network_nodes(node_id),
    edge_id                 INTEGER REFERENCES network_edges(edge_id),
    destination_id          INTEGER REFERENCES destinations(destination_id),
    wait_time_seconds       REAL,
    event_details           TEXT
);
INSERT INTO simulation_events_v2 SELECT * FROM simulation_events;
DROP TABLE simulation_events;
ALTER TABLE simulation_events_v2 RENAME TO simulation_events;
CREATE INDEX idx_events_run_type  ON simulation_events(run_id, event_type);
CREATE INDEX idx_events_run_agent ON simulation_events(run_id, agent_id);
CREATE INDEX idx_events_run_time  ON simulation_events(run_id, simulation_time_seconds);
CREATE INDEX idx_events_run_edge  ON simulation_events(run_id, edge_id);
COMMIT;
"""  # migration 2: new event types (the CHECK list cannot be altered, so the table is rebuilt) and the alert-time column

_V3_SQL = """
BEGIN;
ALTER TABLE agents ADD COLUMN alert_channel TEXT;
COMMIT;
"""  # migration 3: remember how each person was warned

MIGRATIONS = [  # all migrations in order
    Migration(1, "Initial PANIC ENGINE schema", ""),  # SQL loaded lazily from schema.sql
    Migration(2, "Hazard alerts: ALERT_SENT/HAZARD_STRIKE events, agents.alert_time_seconds", _V2_SQL),  # hazard events
    Migration(3, "Alert channels: agents.alert_channel (PHONE/SIREN/NEIGHBOUR/NONE)", _V3_SQL),  # alert channels
]

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""  # the table that records which migrations have run


def current_version(conn: sqlite3.Connection) -> int:  # which schema version is the database at?
    conn.executescript(_BOOTSTRAP)  # make sure the bookkeeping table exists
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()  # highest applied version, or 0
    return int(row["v"])  # as a number


def init_db(db_path: Optional[PathLike] = None) -> sqlite3.Connection:  # create or upgrade the database
    """Create/upgrade the database and return an open connection."""
    settings.ensure_directories()  # make sure the data folders exist
    conn = connect(db_path)  # open the database
    try:  # close the connection if anything fails
        version = current_version(conn)  # where the database is now
        for mig in MIGRATIONS:  # in order
            if mig.version <= version:  # already applied
                continue  # skip it
            sql = _v1_sql() if mig.version == 1 else mig.sql  # the SQL to run
            log.info("Applying migration %s: %s", mig.version, mig.description)  # record it in the log
            # executescript commits implicitly, so wrap each migration's
            # bookkeeping in its own transaction around a fresh statement batch.
            conn.executescript(sql)  # run the migration
            with transaction(conn):  # record it atomically
                conn.execute(  # insert the bookkeeping row
                    "INSERT INTO schema_migrations(version, description) VALUES (?, ?)",  # parameterised insert
                    (mig.version, mig.description),  # its values
                )
        return conn  # ready to use
    except Exception:  # anything went wrong
        conn.close()  # do not leak the connection
        log.exception("Database initialisation failed")  # log the traceback
        raise  # pass the error on


def main() -> None:  # command-line entry point
    settings.configure_logging()  # set up logging
    parser = argparse.ArgumentParser(description="Initialise the PANIC ENGINE SQLite database.")  # option parser
    parser.add_argument("--db", default=None, help="database path (default: %(default)s -> settings.DB_PATH)")  # optional database path
    args = parser.parse_args()  # read the options
    conn = init_db(args.db)  # create or upgrade
    try:  # always close
        print(f"Database ready at {args.db or settings.DB_PATH} (schema version {current_version(conn)})")  # report
    finally:  # cleanup
        conn.close()  # close the connection


if __name__ == "__main__":  # run only when executed as a script
    main()  # start
