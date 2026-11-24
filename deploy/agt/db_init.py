"""Database init + migrations for the AGT deployment.

Phase 13 (AGT packaging skeleton). Creates the local SQLite database
fresh on first deploy and applies numbered migrations idempotently.

Sources of truth:
  * storage/schemas/schema.sql — the repo's local schema (risk_state,
    kill_switch, event_queue, audit_log, journal, event_journal,
    execution_idempotency, ...). Applied verbatim when the file is
    present in the deployed repo.
  * deploy/agt/migrations/*.sql — AGT-specific additions, applied in
    filename order after the base schema. Each file runs inside one
    transaction; applied filenames are recorded in agt_meta so a
    re-run is a no-op.

Growth policy (see storage_budget.md): the live database grows
unboundedly if left alone (event_journal is append-only). The init
sets WAL mode with an auto-checkpoint so the WAL file cannot grow
without bound, and the scheduler's backup loop is the checkpoint
that keeps it small. Pruning old events is a finalize-pass decision
(it deletes history the verdict chain may need) — nothing is pruned
here.

Usage:
  db_init.py [--home DIR] [--check] [--db PATH]
  --check exits 0 when the database exists and every expected table
  is present, 1 otherwise; it creates nothing.
"""

from __future__ import annotations

import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "migrations")
BASE_SCHEMA = os.path.join(REPO, "storage", "schemas", "schema.sql")

# Tables the base schema must provide (checked by --check and by tests).
EXPECTED_TABLES = (
    "risk_state", "kill_switch", "event_queue", "audit_log", "journal",
    "event_journal", "execution_idempotency",
)


def home_dir():
    return os.environ.get("FOREX_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".forex-agent")


def db_path(home=None):
    return os.path.join(home or home_dir(), "storage", "local.db")


def _applied(conn):
    try:
        rows = conn.execute(
            "SELECT name FROM agt_meta WHERE kind = 'migration'"
        ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def _record(conn, name):
    conn.execute(
        "INSERT OR IGNORE INTO agt_meta (kind, name, applied_ts) "
        "VALUES ('migration', ?, datetime('now'))", (name,))


def _apply_sql(conn, sql, label):
    conn.executescript(sql)


def init_db(path=None, home=None):
    """Create the database if needed and apply all migrations.

    Returns {"db": path, "applied": [names applied this run],
             "already_applied": [names]}. Idempotent: a second call
    applies nothing. Raises on a genuinely broken database (never
    silently reseeds — a corrupt DB is an operator decision, see
    backup_recovery.md).
    """
    path = path or db_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path)
    try:
        # WAL with auto-checkpoint: keeps the -wal file bounded.
        # (The VM's 8.7 GB WAL is what happens when nothing ever
        # checkpoints it — see storage_budget.md.)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA synchronous=NORMAL")
        applied_now = []
        already = sorted(_applied(conn))
        if fresh or "001_agt_meta" not in already:
            with open(os.path.join(MIGRATIONS_DIR,
                                   "001_agt_meta.sql")) as fh:
                _apply_sql(conn, fh.read(), "001_agt_meta")
            _record(conn, "001_agt_meta")
            applied_now.append("001_agt_meta")
            already = sorted(_applied(conn))
        if os.path.exists(BASE_SCHEMA):
            with open(BASE_SCHEMA) as fh:
                base_sql = fh.read()
            # CREATE TABLE IF NOT EXISTS: safe to re-apply.
            _apply_sql(conn, base_sql, "base schema")
            _record(conn, "base_schema")
            if "base_schema" not in applied_now:
                applied_now.append("base_schema")
        for name in sorted(os.listdir(MIGRATIONS_DIR)):
            if not name.endswith(".sql") or name == "001_agt_meta.sql":
                continue
            if name in already:
                continue
            with open(os.path.join(MIGRATIONS_DIR, name)) as fh:
                _apply_sql(conn, fh.read(), name)
            _record(conn, name)
            applied_now.append(name)
        conn.commit()
        return {"db": path, "applied": applied_now,
                "already_applied": [a for a in already
                                    if a not in applied_now],
                "fresh": fresh}
    finally:
        conn.close()


def check_db(path=None, home=None):
    """True when the DB exists and every expected table is present."""
    path = path or db_path(home)
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True,
                               timeout=5)
        try:
            have = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            return all(t in have for t in EXPECTED_TABLES)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    home = home_dir()
    path = None
    check_only = False
    i = 0
    while i < len(argv):
        if argv[i] == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 2
        elif argv[i] == "--db" and i + 1 < len(argv):
            path = argv[i + 1]
            i += 2
        elif argv[i] == "--check":
            check_only = True
            i += 1
        else:
            print("usage: db_init.py [--home DIR] [--db PATH] [--check]")
            return 2
    if check_only:
        ok = check_db(path, home)
        print("db check %s: %s" % ("OK" if ok else "MISSING",
                                   path or db_path(home)))
        return 0 if ok else 1
    result = init_db(path, home)
    print("db init: %(db)s fresh=%(fresh)s applied=%(applied)s "
          "already=%(already_applied)s" % result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
