#!/usr/bin/env python3
"""Tamper-evident verdict ledger (2026-10-23): append-only, hash-chained
JSONL record of lane closes and pre-registered verdicts.

Observability ONLY. This module never makes, gates, or alters a
trading decision; every caller hook is fail-soft and this module's
own public functions never raise into a trading path (append()
returns False on any failure, verify()/readers return safe values).

Storage:
  ``$FOREX_AGENT_HOME/verdict_ledger/events.jsonl`` — HOME is read
  at call time (recorder.py pattern), so tests isolate with a temp
  FOREX_AGENT_HOME. Callers may also pass an explicit ``path`` (the
  grad/ceo/mirror hooks derive it from their own ledger's directory,
  which equals HOME in production and the temp dir in tests), or a
  test may reassign the module-level ``LEDGER_PATH`` override.

Records:
  {seq, ts, lane, kind, payload, prev_hash, hash}
  seq starts at 1 and increments by 1. Genesis prev_hash is the
  literal string "GENESIS". hash = sha256 hex of
  (prev_hash + canonical_json(record sans hash)), where canonical
  JSON is sorted keys + tight separators (trials.py pattern), so
  payload key order never changes the hash.

NO ROTATION — deliberate choice: the file grows by closes and
verdicts/checkpoints only. Closes are low-volume (a handful per lane
per day at most; the busiest lane in the system's history closed
~60 positions in six days), so the chain stays tiny for years and
history is NEVER rotated away. If a cap is ever needed, the cap
point must first be anchored with a kind="checkpoint" record
carrying the last hash before any archiving — that work is not
needed now and is not done here.

Concurrency: append holds a module threading lock AND an exclusive
flock on ``events.jsonl.lock`` across the read-tail + append, so
separate daemon processes (grad, grad_raw, ceo, mirror-exits) can
never fork the chain or duplicate a seq. Writes are single-line
appends (no tmp+rename — renaming would break other processes'
open appends and is unnecessary for an append-only log).
"""
import fcntl
import hashlib
import json
import logging
import os
import threading
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.verdict_ledger")

GENESIS = "GENESIS"

# Test override: when set (not None), it wins over the env-derived
# path. Production code never sets it.
LEDGER_PATH = None

_lock = threading.Lock()


# ---- paths (FOREX_AGENT_HOME read at call time) ---------------------------

def _home():
    return os.environ.get("FOREX_AGENT_HOME",
                          "/home/hatch/.forex-agent")


def ledger_dir():
    return os.path.join(_home(), "verdict_ledger")


def ledger_path(path=None):
    """Resolve the chain file: explicit path > module override >
    $FOREX_AGENT_HOME/verdict_ledger/events.jsonl."""
    if path:
        return str(path)
    if LEDGER_PATH:
        return str(LEDGER_PATH)
    return os.path.join(ledger_dir(), "events.jsonl")


def _canonical(obj):
    """Canonical JSON: sorted keys, tight separators. Key order in
    the source never changes the output (trials.canonical_terms_json
    pattern)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


def _record_hash(prev_hash, rec_without_hash):
    return hashlib.sha256(
        (str(prev_hash) + _canonical(rec_without_hash)).encode("utf-8")
    ).hexdigest()


class _file_lock:
    """Exclusive flock on a lock file next to the chain file."""

    def __init__(self, chain_path):
        self._lock_path = chain_path + ".lock"
        self._lf = None

    def __enter__(self):
        parent = os.path.dirname(self._lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lf = open(self._lock_path, "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
        finally:
            if self._lf is not None:
                self._lf.close()
        return False


def _read_lines(chain_path):
    """All non-empty lines of the chain file. [] when missing.
    Never raises."""
    try:
        with open(chain_path) as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except Exception:
        return []


def _tail(chain_path):
    """(last_seq, last_hash) of the chain, or (0, GENESIS) when the
    chain is empty/missing/unparseable at the tail. A corrupt tail
    line is treated as no tail only when NOTHING parses; when an
    earlier record parses, the last parseable record wins — the
    corruption itself is verify()'s job to report, append must not
    silently extend a broken chain from genesis. (In practice the
    tail is the last line; scanning backwards is cheap at this
    volume.) Never raises."""
    lines = _read_lines(chain_path)
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
            if isinstance(rec, dict) and rec.get("hash"):
                return int(rec.get("seq") or 0), str(rec["hash"])
        except Exception:
            continue
    return 0, GENESIS


# ---- public API -------------------------------------------------------------

def append(lane, kind, payload, ts=None, path=None):
    """Append one hash-chained record. Returns True when written,
    False when dropped (any error). NEVER raises — journaling must
    never break trading."""
    try:
        chain_path = ledger_path(path)
        rec_ts = time.time() if ts is None else float(ts)
        body_payload = payload if isinstance(payload, dict) else {}
        with _lock:
            with _file_lock(chain_path):
                last_seq, prev_hash = _tail(chain_path)
                rec = {
                    "seq": last_seq + 1,
                    "ts": rec_ts,
                    "lane": str(lane or ""),
                    "kind": str(kind or ""),
                    "payload": body_payload,
                    "prev_hash": prev_hash,
                }
                rec["hash"] = _record_hash(prev_hash, rec)
                line = _canonical(rec) + "\n"
                parent = os.path.dirname(chain_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(chain_path, "a") as f:
                    f.write(line)
        # Durable-memory linkage (2026-11-23, section 4.1): a lane
        # close is a resolved signal outcome; mirror it into the
        # persistent launch archive so the bot's long-term memory
        # holds outcomes, not just launches. Observability only:
        # same never-raises discipline as the chain itself, and a
        # mirror failure never affects the True result above.
        if str(kind or "") == "close":
            try:
                try:
                    from daemon.launch_scanner import dossiers as _doss
                except Exception:
                    import dossiers as _doss  # type: ignore
                _doss.record_outcome(
                    body_payload.get("mint"), str(lane or ""),
                    ts=rec_ts,
                    pnl_usd=body_payload.get("pnl_usd"),
                    realized_mult=body_payload.get("realized_mult"),
                    closed_reason=body_payload.get("closed_reason"))
            except Exception:
                pass
        return True
    except Exception as e:
        try:
            logger.warning("verdict_ledger append failed: %s", e)
        except Exception:
            pass
        return False


def read_records(path=None):
    """All parsed records in file order (malformed lines skipped).
    Never raises."""
    out = []
    for ln in _read_lines(ledger_path(path)):
        try:
            rec = json.loads(ln)
            if isinstance(rec, dict):
                out.append(rec)
        except Exception:
            continue
    return out


def head_hash(path=None):
    """Hash of the last record, or GENESIS for an empty chain.
    Never raises."""
    try:
        _seq, h = _tail(ledger_path(path))
        return h
    except Exception:
        return GENESIS


def verify(path=None):
    """Recompute the whole chain.

    Returns (ok, n_records, first_bad_seq_or_None): ok is True iff
    every record's prev_hash links to its predecessor (GENESIS for
    seq 1), every seq is exactly its 1-based position, and every
    hash recomputes. n_records counts the record lines read (a
    malformed line counts and is itself the first bad record, at
    its 1-based position). An empty/missing chain verifies as
    (True, 0, None). Never raises."""
    try:
        chain_path = ledger_path(path)
        lines = _read_lines(chain_path)
        prev_hash = GENESIS
        for idx, ln in enumerate(lines):
            expected_seq = idx + 1
            try:
                rec = json.loads(ln)
            except Exception:
                return False, len(lines), expected_seq
            if not isinstance(rec, dict):
                return False, len(lines), expected_seq
            seq = rec.get("seq")
            if seq != expected_seq:
                bad = seq if isinstance(seq, int) else expected_seq
                return False, len(lines), bad
            if str(rec.get("prev_hash") or "") != prev_hash:
                return False, len(lines), expected_seq
            without = {k: v for k, v in rec.items() if k != "hash"}
            if _record_hash(prev_hash, without) != rec.get("hash"):
                return False, len(lines), expected_seq
            prev_hash = str(rec["hash"])
        return True, len(lines), None
    except Exception:
        return False, 0, None


def _main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Verify the tamper-evident verdict ledger chain.")
    p.add_argument("--path", default=None,
                   help="chain file (default: $FOREX_AGENT_HOME/"
                        "verdict_ledger/events.jsonl)")
    args = p.parse_args(argv)
    chain_path = ledger_path(args.path)
    ok, n, bad = verify(chain_path)
    print("path: %s" % chain_path)
    print("records: %d" % n)
    print("head_hash: %s" % head_hash(chain_path))
    if ok:
        print("status: OK")
        return 0
    print("status: BROKEN at seq %s" % bad)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
