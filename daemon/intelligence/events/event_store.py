"""Persistent logical-event store (Phase 8, 2026-11-22).

Own SQLite DB file — NOT the 1.4 GB local.db — under the runtime
home (``$FOREX_AGENT_HOME/intelligence_events.db``,
default ``~/.forex-agent/intelligence_events.db``).

Discipline:
- WAL mode with a checkpoint policy (``wal_autocheckpoint`` +
  explicit ``checkpoint()`` on close and after prune).
- Single-writer: one connection per store instance, all writes
  behind a re-entrant lock.
- Retention/prune from day one: ``prune()`` removes events older
  than a block-time cutoff, NEVER conflicting events (they need
  human review) and NEVER events without a block time (their age
  cannot be established). Every prune is logged to ``prune_log``.
- Corruption is NEVER silently fixed: if the file exists but is
  not a valid SQLite database (or fails integrity check on open)
  ``open()`` raises ``CorruptStoreError`` and the file is left
  byte-for-byte untouched. A corrupt store file is preserved,
  never silently blanked — an operator restores it by hand.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.events.event_store")

SCHEMA_VERSION = 1
DEFAULT_DB_NAME = "intelligence_events.db"
DEFAULT_RETENTION_DAYS = 30
WAL_AUTOCHECKPOINT_PAGES = 1000


class CorruptStoreError(Exception):
    """The store file exists but is not a usable SQLite database.

    The file is preserved untouched; the caller must not delete,
    truncate or re-create it. Raise, do not recover silently.
    """


def default_db_path():
    home = os.environ.get("FOREX_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".forex-agent")
    return os.path.join(home, DEFAULT_DB_NAME)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    slot INTEGER,
    block_time INTEGER,
    commitment TEXT,
    lifecycle_status TEXT,
    event_type TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    decoder_version TEXT,
    providers_json TEXT NOT NULL DEFAULT '[]',
    observations_json TEXT NOT NULL DEFAULT '[]',
    raw_tx_ref_json TEXT,
    parsed_refs_json TEXT NOT NULL DEFAULT '[]',
    normalized_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    disagreements_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_signature ON events(signature);
CREATE INDEX IF NOT EXISTS idx_events_slot ON events(slot);
CREATE INDEX IF NOT EXISTS idx_events_block_time ON events(block_time);
CREATE INDEX IF NOT EXISTS idx_events_type_status
    ON events(event_type, verification_status);
CREATE TABLE IF NOT EXISTS failed_events (
    event_id TEXT PRIMARY KEY,
    signature TEXT,
    slot INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    stream_key TEXT PRIMARY KEY,
    last_slot INTEGER,
    last_signature TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS disagreements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    field TEXT NOT NULL,
    providers_json TEXT NOT NULL DEFAULT '[]',
    values_json TEXT NOT NULL DEFAULT '{}',
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disagreements_event
    ON disagreements(event_id);
CREATE TABLE IF NOT EXISTS prune_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pruned_at REAL NOT NULL,
    cutoff_block_time INTEGER NOT NULL,
    pruned_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_events (
    event_id TEXT PRIMARY KEY,
    applied_at REAL NOT NULL
);
"""


class EventStore(object):
    """Single-writer SQLite store for logical events."""

    def __init__(self, path=None, retention_days=DEFAULT_RETENTION_DAYS):
        self.path = path or default_db_path()
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._conn = None

    # -- lifecycle ---------------------------------------------------
    def open(self):
        """Open (creating parent dirs + schema). Raises
        CorruptStoreError when an existing file is not a valid
        SQLite database; the file is left untouched."""
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
                conn.execute(
                    "PRAGMA wal_autocheckpoint=%d"
                    % WAL_AUTOCHECKPOINT_PAGES)
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                # Integrity gate: a file that opens but is corrupt
                # must not be written over.
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
                    raise CorruptStoreError(
                        "event store file %s is corrupt (%s); file "
                        "preserved, not blanked" % (self.path, exc))
                raise
            self._conn = conn
            return self

    def close(self):
        with self._lock:
            if self._conn is None:
                return
            try:
                self.checkpoint()
            except Exception as exc:  # checkpoint failure must not
                logger.warning("checkpoint on close failed: %s", exc)
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    def checkpoint(self):
        """Checkpoint the WAL into the main file (TRUNCATE mode)."""
        with self._lock:
            self._require_open()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.commit()

    def _require_open(self):
        if self._conn is None:
            raise RuntimeError("EventStore is not open; call open() first")

    # -- events ------------------------------------------------------
    def upsert_event(self, event):
        """Insert or replace a logical event (idempotent by
        event_id). Disagreements are also written to the
        disagreements table for queryability."""
        with self._lock:
            self._require_open()
            data = event.to_dict()
            self._conn.execute(
                "INSERT OR REPLACE INTO events("
                "event_id, signature, slot, block_time, commitment,"
                "lifecycle_status, event_type, verification_status,"
                "processing_status, decoder_version, providers_json,"
                "observations_json, raw_tx_ref_json, parsed_refs_json,"
                "normalized_json, provenance_json, disagreements_json,"
                "created_at, updated_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (data["event_id"], data["signature"], data["slot"],
                 data["block_time"], data["commitment"],
                 data["lifecycle_status"], data["event_type"],
                 data["verification_status"], data["processing_status"],
                 data["decoder_version"],
                 json.dumps(data["providers"]),
                 json.dumps(data["observations"]),
                 json.dumps(data["raw_tx_ref"])
                 if data["raw_tx_ref"] else None,
                 json.dumps(data["parsed_refs"]),
                 json.dumps({"accounts": data["normalized_accounts"],
                             "sol_movement": data["sol_movement"],
                             "token_movement": data["token_movement"]}),
                 json.dumps(data["provenance"]),
                 json.dumps(data["disagreements"]),
                 data["created_at"], data["updated_at"]))
            self._conn.execute(
                "DELETE FROM disagreements WHERE event_id=?",
                (data["event_id"],))
            for disc in data["disagreements"]:
                self._conn.execute(
                    "INSERT INTO disagreements(event_id, field,"
                    "providers_json, values_json, recorded_at)"
                    " VALUES (?,?,?,?,?)",
                    (data["event_id"], disc.get("field"),
                     json.dumps(list(disc.get("values", {}).keys())),
                     json.dumps(disc.get("values", {})),
                     disc.get("recorded_at") or time.time()))
            self._conn.commit()
            return data["event_id"]

    def get_event(self, event_id):
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id=?",
                (event_id,)).fetchone()
            return self._row_to_event(row) if row else None

    def get_by_signature(self, signature):
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT * FROM events WHERE signature=? ORDER BY slot",
                (signature,)).fetchall()
            return [self._row_to_event(r) for r in rows]

    def event_exists(self, event_id):
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE event_id=?",
                (event_id,)).fetchone()
            return row is not None

    def delete_event(self, event_id):
        """Delete one event row (used only when a merge re-keys a
        preliminary event from its slot-less ID to its slotted
        ID). Never used to silently discard records."""
        with self._lock:
            self._require_open()
            self._conn.execute(
                "DELETE FROM events WHERE event_id=?", (event_id,))
            self._conn.execute(
                "DELETE FROM disagreements WHERE event_id=?",
                (event_id,))
            self._conn.commit()

    def count_events(self, verification_status=None, event_type=None):
        with self._lock:
            self._require_open()
            sql = "SELECT COUNT(*) FROM events"
            args = []
            clauses = []
            if verification_status:
                clauses.append("verification_status=?")
                args.append(verification_status)
            if event_type:
                clauses.append("event_type=?")
                args.append(event_type)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            return self._conn.execute(sql, args).fetchone()[0]

    def list_events(self, limit=100, verification_status=None):
        with self._lock:
            self._require_open()
            sql = "SELECT * FROM events"
            args = []
            if verification_status:
                sql += " WHERE verification_status=?"
                args.append(verification_status)
            sql += " ORDER BY block_time DESC, slot DESC LIMIT ?"
            args.append(limit)
            rows = self._conn.execute(sql, args).fetchall()
            return [self._row_to_event(r) for r in rows]

    def list_disagreements(self, event_id=None):
        with self._lock:
            self._require_open()
            if event_id:
                rows = self._conn.execute(
                    "SELECT event_id, field, providers_json,"
                    " values_json, recorded_at FROM disagreements"
                    " WHERE event_id=? ORDER BY recorded_at",
                    (event_id,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT event_id, field, providers_json,"
                    " values_json, recorded_at FROM disagreements"
                    " ORDER BY recorded_at").fetchall()
            return [{"event_id": r[0], "field": r[1],
                     "providers": json.loads(r[2]),
                     "values": json.loads(r[3]),
                     "recorded_at": r[4]} for r in rows]

    @staticmethod
    def _row_to_event(row):
        from .event_models import LogicalEvent
        columns = ["event_id", "signature", "slot", "block_time",
                   "commitment", "lifecycle_status", "event_type",
                   "verification_status", "processing_status",
                   "decoder_version", "providers_json",
                   "observations_json", "raw_tx_ref_json",
                   "parsed_refs_json", "normalized_json",
                   "provenance_json", "disagreements_json",
                   "created_at", "updated_at"]
        data = dict(zip(columns, row))
        normalized = json.loads(data["normalized_json"] or "{}")
        return LogicalEvent(
            signature=data["signature"], slot=data["slot"],
            block_time=data["block_time"],
            event_type=data["event_type"],
            commitment=data["commitment"],
            lifecycle_status=data["lifecycle_status"],
            verification_status=data["verification_status"],
            processing_status=data["processing_status"],
            decoder_version=data["decoder_version"],
            providers=json.loads(data["providers_json"] or "[]"),
            observations=json.loads(data["observations_json"] or "[]"),
            raw_tx_ref=json.loads(data["raw_tx_ref_json"])
            if data["raw_tx_ref_json"] else None,
            parsed_refs=json.loads(data["parsed_refs_json"] or "[]"),
            normalized_accounts=normalized.get("accounts") or {},
            sol_movement=normalized.get("sol_movement") or [],
            token_movement=normalized.get("token_movement") or [],
            provenance=json.loads(data["provenance_json"] or "{}"),
            disagreements=json.loads(data["disagreements_json"] or "[]"),
            event_id=data["event_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"])

    # -- retention ---------------------------------------------------
    def prune(self, cutoff_block_time=None):
        """Delete events older than ``cutoff_block_time`` (default:
        retention_days before now). NEVER deletes conflicting
        events or events without a block time. Returns the count
        pruned and logs the run to prune_log."""
        with self._lock:
            self._require_open()
            if cutoff_block_time is None:
                cutoff_block_time = int(time.time()) - int(
                    self.retention_days * 86400)
            cur = self._conn.execute(
                "DELETE FROM events WHERE block_time IS NOT NULL"
                " AND block_time < ?"
                " AND verification_status != 'conflicting'",
                (cutoff_block_time,))
            pruned = cur.rowcount
            self._conn.execute(
                "INSERT INTO prune_log(pruned_at, cutoff_block_time,"
                " pruned_count) VALUES (?,?,?)",
                (time.time(), cutoff_block_time, pruned))
            self._conn.commit()
            self.checkpoint()
            return pruned

    def prune_log_entries(self):
        with self._lock:
            self._require_open()
            rows = self._conn.execute(
                "SELECT pruned_at, cutoff_block_time, pruned_count"
                " FROM prune_log ORDER BY pruned_at").fetchall()
            return [{"pruned_at": r[0], "cutoff_block_time": r[1],
                     "pruned_count": r[2]} for r in rows]

    # -- checkpoints -------------------------------------------------
    def set_checkpoint(self, stream_key, last_slot, last_signature=None):
        with self._lock:
            self._require_open()
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints(stream_key,"
                " last_slot, last_signature, updated_at)"
                " VALUES (?,?,?,?)",
                (stream_key, last_slot, last_signature, time.time()))
            self._conn.commit()

    def get_checkpoint(self, stream_key):
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT last_slot, last_signature, updated_at"
                " FROM checkpoints WHERE stream_key=?",
                (stream_key,)).fetchone()
            if not row:
                return None
            return {"last_slot": row[0], "last_signature": row[1],
                    "updated_at": row[2]}

    # -- failed-event retry queue ------------------------------------
    def enqueue_failed(self, event_id, signature, slot, error):
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT attempts FROM failed_events WHERE event_id=?",
                (event_id,)).fetchone()
            attempts = (row[0] + 1) if row else 1
            # Backoff: 60s * 2**attempts, capped at 1h.
            delay = min(3600, 60 * (2 ** min(attempts, 6)))
            self._conn.execute(
                "INSERT OR REPLACE INTO failed_events(event_id,"
                " signature, slot, attempts, last_error,"
                " next_retry_at, status, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (event_id, signature, slot, attempts, str(error),
                 time.time() + delay, "pending", time.time()))
            self._conn.commit()
            return attempts

    def failed_due(self, now=None):
        with self._lock:
            self._require_open()
            now = time.time() if now is None else now
            rows = self._conn.execute(
                "SELECT event_id, signature, slot, attempts,"
                " last_error, next_retry_at FROM failed_events"
                " WHERE status='pending' AND next_retry_at <= ?"
                " ORDER BY next_retry_at",
                (now,)).fetchall()
            return [{"event_id": r[0], "signature": r[1], "slot": r[2],
                     "attempts": r[3], "last_error": r[4],
                     "next_retry_at": r[5]} for r in rows]

    def mark_failed_resolved(self, event_id):
        with self._lock:
            self._require_open()
            self._conn.execute(
                "UPDATE failed_events SET status='resolved',"
                " updated_at=? WHERE event_id=?",
                (time.time(), event_id))
            self._conn.commit()

    def failed_count(self, status="pending"):
        with self._lock:
            self._require_open()
            return self._conn.execute(
                "SELECT COUNT(*) FROM failed_events WHERE status=?",
                (status,)).fetchone()[0]

    # -- durable applied set (Phase 13 finalize) -------------------
    def mark_applied(self, event_id):
        """Record that event_id's market-state application is done.

        Durable across processor restarts: the in-memory guard in
        EventProcessor is per instance, so a restarted processor
        would otherwise re-apply an already-applied event
        (double-counting, e.g. trade_count += 1 twice for one
        trade). Idempotent (INSERT OR REPLACE)."""
        with self._lock:
            self._require_open()
            self._conn.execute(
                "INSERT OR REPLACE INTO applied_events(event_id,"
                " applied_at) VALUES (?,?)",
                (event_id, time.time()))
            self._conn.commit()

    def is_applied(self, event_id):
        """True when event_id is in the durable applied set."""
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT 1 FROM applied_events WHERE event_id=?",
                (event_id,)).fetchone()
            return row is not None
