"""SQLite connection management and transaction helpers."""
from __future__ import annotations  # modern type hints on older Python

import logging  # log rollbacks
import sqlite3  # the database driver
import uuid  # unique savepoint names
from contextlib import contextmanager  # turns a generator into a "with" block
from pathlib import Path  # file paths
from typing import Iterator, Optional, Union  # type hints

from config import settings  # database path setting

log = logging.getLogger(__name__)  # logger for this module

PathLike = Union[str, Path]  # a path given as text or Path


def connect(db_path: Optional[PathLike] = None) -> sqlite3.Connection:  # open the database
    """Open a connection with foreign keys enforced.

    The connection runs in autocommit mode (``isolation_level=None``) so that
    transactions are controlled explicitly by :func:`transaction`.
    """
    target = str(db_path if db_path is not None else settings.DB_PATH)  # which file to open
    if target != ":memory:":  # a real file, not an in-memory test database
        Path(target).parent.mkdir(parents=True, exist_ok=True)  # make sure its folder exists
    conn = sqlite3.connect(target, timeout=30, isolation_level=None)  # wait up to 30 s for locks; autocommit mode
    conn.row_factory = sqlite3.Row  # rows can be read by column name
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite ignores foreign keys unless asked
    if target != ":memory:":  # these settings only make sense for files
        conn.execute("PRAGMA journal_mode = WAL")  # readers do not block the writer
        conn.execute("PRAGMA synchronous = NORMAL")  # faster commits, still safe with WAL
    return conn  # the ready connection


@contextmanager
def session(db_path: Optional[PathLike] = None) -> Iterator[sqlite3.Connection]:  # "with session() as conn:"
    """Connection that is always closed (short-lived use from the UI)."""
    conn = connect(db_path)  # open
    try:  # run the caller's block
        yield conn  # hand the connection over
    finally:  # always, even after an error
        conn.close()  # close it


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:  # "with transaction(conn):"
    """All-or-nothing block. Nested use becomes a SAVEPOINT.

    Any exception rolls the block back and is re-raised (never swallowed).
    """
    if conn.in_transaction:  # already inside a transaction: nest
        name = "sp_" + uuid.uuid4().hex[:12]  # unique savepoint name
        conn.execute(f"SAVEPOINT {name}")  # mark a point to return to
        try:  # run the caller's block
            yield conn  # hand the connection over
        except BaseException:  # any failure
            conn.execute(f"ROLLBACK TO {name}")  # undo back to the savepoint
            conn.execute(f"RELEASE {name}")  # forget the savepoint
            raise  # pass the error on
        else:  # no failure
            conn.execute(f"RELEASE {name}")  # keep the work, drop the savepoint
    else:  # outermost transaction
        conn.execute("BEGIN IMMEDIATE")  # take the write lock now
        try:  # run the caller's block
            yield conn  # hand the connection over
        except BaseException:  # any failure
            conn.execute("ROLLBACK")  # undo everything
            log.exception("Transaction rolled back")  # record why
            raise  # pass the error on
        else:  # no failure
            conn.execute("COMMIT")  # make the changes permanent
