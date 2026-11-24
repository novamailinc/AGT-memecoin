"""Decision journal (Phase 11, 2026-11-22).

Own SQLite store for fusion decision records. SHADOW MODE ONLY:
this journal is never a live ledger -- no entry/exit decision,
lane setting, stake, or killswitch state is ever read from or
written to it by a live path.

Each record stores the full input snapshot alongside the decision,
so any record is replayable: ``replay(decision_id, engine)``
re-runs the fusion engine on the stored input and reports whether
the recomputed decision, score, and reason match the stored one.
A mismatch means the engine changed since the record was written,
and must be investigated, never silently overwritten.

Schema (single table, WAL, integrity-checked on open -- same
pattern as the Phase 8 EventStore; corrupt files raise and are
left untouched, never blanked).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    lane TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    score REAL NOT NULL,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_mint
    ON decisions (mint, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_decision
    ON decisions (decision, created_at);
"""


class CorruptJournalError(Exception):
    """Raised when an existing journal file is not a valid DB."""


def default_db_path():
    home = os.environ.get("FOREX_AGENT_HOME") \
        or os.path.expanduser("~/.forex-agent")
    return os.path.join(home, "intelligence",
                        "fusion_decisions.db")


class DecisionJournal(object):
    """Single-writer SQLite journal for fusion decision records."""

    def __init__(self, path=None, retention_days=None):
        self.path = path or default_db_path()
        self.retention_days = (DEFAULT_RETENTION_DAYS
                               if retention_days is None
                               else retention_days)
        self._lock = threading.RLock()
        self._conn = None

    def open(self):
        with self._lock:
            if self._conn is not None:
                return self
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            existed = os.path.exists(self.path)
            try:
                conn = sqlite3.connect(self.path, timeout=30.0,
                                        check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA)
                row = conn.execute("PRAGMA quick_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    raise sqlite3.DatabaseError(
                        "quick_check failed: %s" % (row,))
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES "
                    "('schema_version', ?)", (str(SCHEMA_VERSION),))
                conn.commit()
            except sqlite3.DatabaseError as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                if existed:
                    raise CorruptJournalError(
                        "decision journal file %s is corrupt (%s); "
                        "file preserved, not blanked"
                        % (self.path, exc))
                raise
            self._conn = conn
            return self

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _require_open(self):
        if self._conn is None:
            raise RuntimeError("DecisionJournal is not open")

    def record(self, decision_record):
        """Insert (or replace, same deterministic id) one record."""
        self._require_open()
        payload = json.dumps(decision_record, sort_keys=True,
                             default=str)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO decisions "
                "(decision_id, mint, lane, decision, reason, score, "
                "created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_record["decision_id"],
                 decision_record["mint"],
                 decision_record["lane"],
                 decision_record["decision"],
                 decision_record["reason"],
                 float(decision_record["score"]),
                 float(decision_record.get("created_at")
                       or time.time()),
                 payload))
            self._conn.commit()
        return decision_record["decision_id"]

    def get(self, decision_id):
        self._require_open()
        row = self._conn.execute(
            "SELECT payload FROM decisions WHERE decision_id = ?",
            (decision_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def list_by_mint(self, mint, limit=100):
        self._require_open()
        rows = self._conn.execute(
            "SELECT payload FROM decisions WHERE mint = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (mint, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count(self, decision=None):
        self._require_open()
        if decision is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM decisions").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE decision = ?",
                (decision,)).fetchone()
        return int(row[0])

    def replay(self, decision_id, engine):
        """Re-run the engine on the stored input snapshot.

        Returns {"matches": bool, "stored": {...}, "recomputed":
        {...}, "mismatches": [...]}. ``matches`` is True only when
        decision, score, and reason all agree. A False result is
        evidence the engine changed -- investigate, never overwrite.
        """
        stored = self.get(decision_id)
        if stored is None:
            return {"matches": False, "stored": None,
                    "recomputed": None,
                    "mismatches": ["decision_id_not_found"]}
        snapshot = stored.get("input") or {}
        recomputed = engine.fuse(
            stored["mint"], lane=stored["lane"],
            signals=snapshot.get("signals") or [],
            events=snapshot.get("events") or [],
            conflicts=snapshot.get("conflicts") or [],
            restrictions=snapshot.get("restrictions") or {},
            cost_check=snapshot.get("cost_check") or {})
        mismatches = []
        for field in ("decision", "score", "reason"):
            if stored.get(field) != recomputed.get(field):
                mismatches.append(field)
        return {"matches": not mismatches,
                "stored": {"decision": stored.get("decision"),
                           "score": stored.get("score"),
                           "reason": stored.get("reason")},
                "recomputed": {"decision": recomputed.get("decision"),
                               "score": recomputed.get("score"),
                               "reason": recomputed.get("reason")},
                "mismatches": mismatches}

    def prune(self, cutoff_created_at=None):
        """Delete records older than the retention window."""
        self._require_open()
        if cutoff_created_at is None:
            cutoff_created_at = (time.time()
                                 - self.retention_days * 86400.0)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM decisions WHERE created_at < ?",
                (cutoff_created_at,))
            self._conn.commit()
            return cur.rowcount
