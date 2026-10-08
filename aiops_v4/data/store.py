"""Optional evidence snapshots: atomic creation and strictly read-only querying."""

import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .discovery import SOURCE_PREFIXES
from .profile import profile_dataset
from .reader import utc_time


_SCHEMA = """
PRAGMA user_version = 1;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE records (
    record_id TEXT PRIMARY KEY,
    batch TEXT NOT NULL,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    record_index INTEGER NOT NULL,
    timestamp TEXT,
    node_id TEXT,
    payload TEXT NOT NULL
);
"""


def ingest_dataset(root: Path, batch: str, database: Path,
                   max_rows_per_file: int | None = None, *, progress=None) -> dict:
    """Create a new immutable snapshot, never replacing an existing database."""
    database = Path(database)
    if database.exists():
        raise FileExistsError(f"database already exists: {database}")
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".v4-evidence-", suffix=".sqlite", dir=database.parent)
    os.close(descriptor)
    temporary = Path(filename)
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript(_SCHEMA)
        pending = []

        def flush():
            connection.executemany("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)", pending)
            pending.clear()

        def save(record):
            pending.append((record.record_id, record.batch, record.source, record.source_file,
                            record.record_index, record.timestamp, record.node_id,
                            json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))))
            if len(pending) >= 1000:
                flush()

        report = profile_dataset(root, batch, max_rows_per_file, on_record=save, progress=progress)
        if report["errors"]:
            raise ValueError(f"scan errors prevent publishing evidence database: {report['errors']}")
        flush()
        connection.execute("CREATE INDEX records_lookup ON records(batch, source, node_id, timestamp)")
        connection.execute("CREATE INDEX records_time ON records(batch, timestamp)")
        connection.execute("INSERT INTO metadata VALUES ('profile', ?)",
                           (json.dumps(report, ensure_ascii=False, allow_nan=False),))
        connection.commit()
        connection.close()
        connection = None
        # link() atomically fails if another run has already claimed this destination.
        os.link(temporary, database)
        return report
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


def _read_connection(database: Path) -> sqlite3.Connection:
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"evidence database not found: {database}")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
        connection.close()
        raise ValueError("unsupported evidence database schema")
    return connection


def read_store_profile(database: Path) -> dict:
    """Read scope, quality and source metadata before treating a snapshot as evidence."""
    connection = _read_connection(database)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='profile'").fetchone()
        if row is None:
            raise ValueError("evidence database has no profile metadata")
        return json.loads(row[0])
    finally:
        connection.close()


def query_records(database: Path, batch: str, *, source: str | None = None,
                  node_id: str | None = None, start: str | None = None, end: str | None = None,
                  record_id: str | None = None, limit: int = 100) -> list[dict]:
    """Return original evidence. Time range is [start, end), in UTC."""
    if not isinstance(batch, str) or not batch.strip():
        raise ValueError("batch must be non-empty")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if source is not None and source not in SOURCE_PREFIXES:
        raise ValueError(f"unknown source: {source}")
    start = utc_time(start, require_timezone=True)[0] if start is not None else None
    end = utc_time(end, require_timezone=True)[0] if end is not None else None
    if start is not None and end is not None and start >= end:
        raise ValueError("end must be after start")
    clauses, params = ["batch = ?"], [batch]
    for column, value in [("source", source), ("node_id", node_id), ("record_id", record_id)]:
        if value is not None:
            clauses.append(column + " = ?")
            params.append(value)
    for operator, timestamp in [(">=", start), ("<", end)]:
        if timestamp is not None:
            clauses.append("timestamp " + operator + " ?")
            params.append(timestamp)
    params.append(limit)
    query = ("SELECT payload FROM records WHERE " + " AND ".join(clauses) +
             " ORDER BY timestamp IS NULL, timestamp, source_file, record_index LIMIT ?")
    connection = _read_connection(database)
    try:
        return [json.loads(row[0]) for row in connection.execute(query, params)]
    finally:
        connection.close()
