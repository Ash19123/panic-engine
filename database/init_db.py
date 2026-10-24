"""Database initialisation with an explicit, versioned migration mechanism.

Usage:  python -m database.init_db [--db PATH]

Migrations are applied in order and recorded in ``schema_migrations``. Running
the script repeatedly never destroys data: already-applied versions are skipped.
To evolve the schema, append a new ``Migration`` to ``MIGRATIONS``.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import settings
from database.connection import PathLike, connect, transaction

log = logging.getLogger(__name__)
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    sql: str


def _v1_sql() -> str:
    return SCHEMA_FILE.read_text(encoding="utf-8")


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
"""

_V3_SQL = """
BEGIN;
ALTER TABLE agents ADD COLUMN alert_channel TEXT;
COMMIT;
"""

MIGRATIONS = [
    Migration(1, "Initial PANIC ENGINE schema", ""),  # SQL loaded lazily from schema.sql
    Migration(2, "Hazard alerts: ALERT_SENT/HAZARD_STRIKE events, agents.alert_time_seconds", _V2_SQL),
    Migration(3, "Alert channels: agents.alert_channel (PHONE/SIREN/NEIGHBOUR/NONE)", _V3_SQL),
]

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def current_version(conn: sqlite3.Connection) -> int:
    conn.executescript(_BOOTSTRAP)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    return int(row["v"])


def init_db(db_path: Optional[PathLike] = None) -> sqlite3.Connection:
    """Create/upgrade the database and return an open connection."""
    settings.ensure_directories()
    conn = connect(db_path)
    try:
        version = current_version(conn)
        for mig in MIGRATIONS:
            if mig.version <= version:
                continue
            sql = _v1_sql() if mig.version == 1 else mig.sql
            log.info("Applying migration %s: %s", mig.version, mig.description)
            # executescript commits implicitly, so wrap each migration's
            # bookkeeping in its own transaction around a fresh statement batch.
            conn.executescript(sql)
            with transaction(conn):
                conn.execute(
                    "INSERT INTO schema_migrations(version, description) VALUES (?, ?)",
                    (mig.version, mig.description),
                )
        return conn
    except Exception:
        conn.close()
        log.exception("Database initialisation failed")
        raise


def main() -> None:
    settings.configure_logging()
    parser = argparse.ArgumentParser(description="Initialise the PANIC ENGINE SQLite database.")
    parser.add_argument("--db", default=None, help="database path (default: %(default)s -> settings.DB_PATH)")
    args = parser.parse_args()
    conn = init_db(args.db)
    try:
        print(f"Database ready at {args.db or settings.DB_PATH} (schema version {current_version(conn)})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
