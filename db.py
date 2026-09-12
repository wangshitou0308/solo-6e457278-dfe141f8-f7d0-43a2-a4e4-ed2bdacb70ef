"""SQLite persistence for the change-ringing API.

Seven tables:
    methods           - one row per method *version* (same name => version +1)
    analyses          - one row per analysis report, linked to a method version
    touches           - one row per spliced-touch report, spanning methods
    schemes           - one row per musicality scoring *scheme version*
    music_analyses    - one row per scored analysis/touch against a scheme version
    touch_searches    - one row per touch-search job: config plus the full report
    multipart_touches - one row per multi-part replay: the base touch (segments),
                        part count/cap plus the full replay report
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
CREATE TABLE IF NOT EXISTS schemes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    stage      INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    rules      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS music_analyses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    stage        INTEGER NOT NULL,
    subject_id   INTEGER NOT NULL,
    scheme_id    INTEGER NOT NULL REFERENCES schemes(id),
    scheme_version INTEGER NOT NULL,
    status       TEXT NOT NULL,
    partial      INTEGER NOT NULL,
    total_score  REAL NOT NULL,
    result       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS touch_searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage           INTEGER NOT NULL,
    config          TEXT NOT NULL,
    min_leads       INTEGER NOT NULL,
    max_leads       INTEGER NOT NULL,
    max_states      INTEGER NOT NULL,
    max_results     INTEGER NOT NULL,
    scheme_id       INTEGER,
    status          TEXT NOT NULL,
    truncated       INTEGER NOT NULL,
    result_count    INTEGER NOT NULL,
    report          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS multipart_touches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    touch_id    INTEGER REFERENCES touches(id),
    stage       INTEGER NOT NULL,
    segments    TEXT NOT NULL,
    parts       INTEGER NOT NULL,
    max_rows    INTEGER,
    status      TEXT NOT NULL,
    report      TEXT NOT NULL,
    created_at  TEXT NOT NULL
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

    # ---------------------------------------------------------------- schemes
    def create_scheme(self, name, stage, rules):
        """Store a new version of a scoring scheme; returns the full record.

        rules are normalized rule dicts (JSON-safe, bells as compact symbols).
        Same name => version increments independently of the rule contents.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM schemes"
                " WHERE name = ?", (name,)).fetchone()
            version = row["v"]
            cur = self._conn.execute(
                "INSERT INTO schemes (name, stage, version, rules, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, stage, version, json.dumps(rules), _now()))
            return self.get_scheme(cur.lastrowid)

    def get_scheme(self, scheme_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schemes WHERE id = ?", (scheme_id,)).fetchone()
        return self._decode_scheme(row) if row else None

    def list_schemes(self, stage=None):
        with self._lock:
            if stage is None:
                rows = self._conn.execute(
                    "SELECT * FROM schemes ORDER BY name, version").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM schemes WHERE stage = ? ORDER BY name, version",
                    (stage,)).fetchall()
        return [self._decode_scheme(r) for r in rows]

    @staticmethod
    def _decode_scheme(row):
        rec = dict(row)
        rec["rules"] = json.loads(rec["rules"])
        return rec

    # ----------------------------------------------------------- music scores
    def create_music_analysis(self, kind, stage, subject_id, scheme_id,
                              scheme_version, result):
        """Store a scored analysis/touch result; returns the full record."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO music_analyses (kind, stage, subject_id,"
                " scheme_id, scheme_version, status, partial, total_score,"
                " result, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, stage, subject_id, scheme_id, scheme_version,
                 result["status"], 1 if result["partial"] else 0,
                 result["total_score"], json.dumps(result), _now()))
            return self.get_music_analysis(cur.lastrowid)

    def get_music_analysis(self, music_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM music_analyses WHERE id = ?",
                (music_id,)).fetchone()
        return self._decode_music(row) if row else None

    def list_music_analyses(self, kind=None, subject_id=None, scheme_id=None,
                            stage=None):
        clauses, params = [], []
        for col, value in (("kind", kind), ("subject_id", subject_id),
                           ("scheme_id", scheme_id), ("stage", stage)):
            if value is not None:
                clauses.append(f"{col} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM music_analyses{where} ORDER BY id",
                params).fetchall()
        return [self._decode_music(r) for r in rows]

    @staticmethod
    def _decode_music(row):
        rec = dict(row)
        rec["partial"] = bool(rec["partial"])
        rec["result"] = json.loads(rec["result"])
        return rec

    # ----------------------------------------------------------- touch searches
    def create_touch_search(self, stage, config, min_leads, max_leads,
                            max_states, max_results, scheme_id, status,
                            truncated, result_count, report):
        """Store a touch-search job with its config and full report."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO touch_searches (stage, config, min_leads, max_leads,"
                " max_states, max_results, scheme_id, status, truncated,"
                " result_count, report, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (stage, json.dumps(config), min_leads, max_leads, max_states,
                 max_results, scheme_id, status, 1 if truncated else 0,
                 result_count, json.dumps(report), _now()))
            return self.get_touch_search(cur.lastrowid)

    def get_touch_search(self, search_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM touch_searches WHERE id = ?",
                (search_id,)).fetchone()
        return self._decode_search(row) if row else None

    def list_touch_searches(self, stage=None, scheme_id=None):
        clauses, params = [], []
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        if scheme_id is not None:
            clauses.append("scheme_id = ?")
            params.append(scheme_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM touch_searches{where} ORDER BY id",
                params).fetchall()
        return [self._decode_search(r) for r in rows]

    @staticmethod
    def _decode_search(row):
        rec = dict(row)
        rec["truncated"] = bool(rec["truncated"])
        rec["config"] = json.loads(rec["config"])
        rec["report"] = json.loads(rec["report"])
        return rec

    # ------------------------------------------------------ multi-part replays
    def create_multipart_touch(self, touch_id, stage, segments, parts, max_rows,
                                status, report):
        """Store a multi-part replay report; returns the full record."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO multipart_touches (touch_id, stage, segments, parts,"
                " max_rows, status, report, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (touch_id, stage, json.dumps(segments), parts, max_rows, status,
                 json.dumps(report), _now()))
            return self.get_multipart_touch(cur.lastrowid)

    def get_multipart_touch(self, replay_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM multipart_touches WHERE id = ?",
                (replay_id,)).fetchone()
        return self._decode_multipart(row) if row else None

    def list_multipart_touches(self, touch_id=None, stage=None):
        clauses, params = [], []
        if touch_id is not None:
            clauses.append("touch_id = ?")
            params.append(touch_id)
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM multipart_touches{where} ORDER BY id",
                params).fetchall()
        return [self._decode_multipart(r) for r in rows]

    @staticmethod
    def _decode_multipart(row):
        rec = dict(row)
        rec["segments"] = json.loads(rec["segments"])
        rec["report"] = json.loads(rec["report"])
        return rec
