"""SQLite persistence for the change-ringing API.

Three tables:
    methods   - one row per method *version* (same name => version increments)
    analyses  - one row per analysis report, linked to a method version
    touches   - one row per spliced-touch report, spanning method versions
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS methods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    stage      INTEGER NOT NULL,
    notation   TEXT NOT NULL,
    start_row  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS analyses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    method_id  INTEGER NOT NULL REFERENCES methods(id),
    overrides  TEXT NOT NULL DEFAULT '[]',
    max_rows   INTEGER,
    status     TEXT NOT NULL,
    report     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS touches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage      INTEGER NOT NULL,
    segments   TEXT NOT NULL,
    max_rows   INTEGER,
    status     TEXT NOT NULL,
    report     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe wrapper around a sqlite3 connection."""

    def __init__(self, path="ringing.db"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- methods
    def create_method(self, name, stage, notation, start_row):
        """Store a new version of a method; returns the full method record."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM methods WHERE name = ?",
                (name,)).fetchone()
            version = row["v"]
            cur = self._conn.execute(
                "INSERT INTO methods (name, stage, notation, start_row, version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, stage, notation, start_row, version, _now()))
            return self.get_method(cur.lastrowid)

    def get_method(self, method_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM methods WHERE id = ?", (method_id,)).fetchone()
        return dict(row) if row else None

    def list_methods(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM methods ORDER BY name, version").fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- analyses
    def create_analysis(self, method_id, overrides, max_rows, status, report):
        """Store an analysis report; returns the full analysis record."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO analyses (method_id, overrides, max_rows, status, report, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (method_id, json.dumps(overrides or []), max_rows, status,
                 json.dumps(report), _now()))
            return self.get_analysis(cur.lastrowid)

    def get_analysis(self, analysis_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        return self._decode_analysis(row) if row else None

    def list_analyses(self, method_id=None):
        with self._lock:
            if method_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM analyses ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM analyses WHERE method_id = ? ORDER BY id",
                    (method_id,)).fetchall()
        return [self._decode_analysis(r) for r in rows]

    @staticmethod
    def _decode_analysis(row):
        rec = dict(row)
        rec["overrides"] = json.loads(rec["overrides"])
        rec["report"] = json.loads(rec["report"])
        return rec

    # ---------------------------------------------------------------- touches
    def create_touch(self, stage, segments, max_rows, status, report):
        """Store a spliced-touch report; returns the full touch record."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO touches (stage, segments, max_rows, status, report,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stage, json.dumps(segments), max_rows, status,
                 json.dumps(report), _now()))
            return self.get_touch(cur.lastrowid)

    def get_touch(self, touch_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM touches WHERE id = ?", (touch_id,)).fetchone()
        return self._decode_touch(row) if row else None

    def list_touches(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM touches ORDER BY id").fetchall()
        return [self._decode_touch(r) for r in rows]

    @staticmethod
    def _decode_touch(row):
        rec = dict(row)
        rec["segments"] = json.loads(rec["segments"])
        rec["report"] = json.loads(rec["report"])
        return rec
