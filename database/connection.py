"""SQLite connection management and transaction helpers."""
from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from config import settings

log = logging.getLogger(__name__)

PathLike = Union[str, Path]


def connect(db_path: Optional[PathLike] = None) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced.

    The connection runs in autocommit mode (``isolation_level=None``) so that
    transactions are controlled explicitly by :func:`transaction`.
    """
    target = str(db_path if db_path is not None else settings.DB_PATH)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def session(db_path: Optional[PathLike] = None) -> Iterator[sqlite3.Connection]:
    """Connection that is always closed (short-lived use from the UI)."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing block. Nested use becomes a SAVEPOINT.

    Any exception rolls the block back and is re-raised (never swallowed).
    """
    if conn.in_transaction:
        name = "sp_" + uuid.uuid4().hex[:12]
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            log.exception("Transaction rolled back")
            raise
        else:
            conn.execute("COMMIT")
