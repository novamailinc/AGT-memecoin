#!/usr/bin/env python3
"""Trial registry (P6, 2026-10-23): every lane / config experiment on
record, with its pre-registered terms, so verdicts are judged against
what was promised BEFORE the sample ran — never re-narrated after.

One JSON file, ~/.forex-agent/trial_registry.json: a list of trial
records

    {trial_id, lane, kind ("lane" | "config_change"), terms (dict),
     terms_hash (sha256 of the canonical JSON of terms),
     started_ts, stopped_ts (null while active), closes (int),
     expectancy (float | null),
     status ("active" | "halted" | "verdict_halt" | "survived" |
             "retired"), note}

Registration is idempotent: an ACTIVE trial with the same lane +
terms_hash is returned unchanged instead of duplicated, so daemons
and backfills can call register() on every boot safely. Changing the
terms (a new config) hashes differently and starts a NEW trial — the
old one stays on record. That is the point: the registry is the
count of everything that was ever tried.

VERDICT-TOOLING NOTE (how consumers must read this registry):
when judging whether a lane/config "worked", verdict tooling must
count ALL trials in the family — every trial ever registered for
that lane (and, for config changes, every sibling trial on the same
lane) — not just the surviving/active one. A lane that looks good
after three retired siblings failed is a multiple-comparisons
artifact, not a clean single-test result: the family's total closes
and the number of trials tried belong in the significance judgement
(e.g. a Bonferroni-style correction, or at minimum reporting
"trial k of N in this family" beside any expectancy figure). Never
judge a trial's expectancy in isolation from its family's history
in this registry.

Storage follows the grad.py ledger pattern: every read-modify-write
holds an exclusive flock on a lock file next to the registry, and
writes are atomic tmp+rename. REGISTRY_PATH is module-level and the
lock path is derived from it at call time, so tests can isolate the
registry by reassigning REGISTRY_PATH. Reads never raise: a missing
or corrupt registry reads as [].
"""
import hashlib
import json
import logging
import os
import time
import uuid

logger = logging.getLogger("forex_agent.daemon.launch_scanner.trials")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
REGISTRY_PATH = os.path.join(HOME, "trial_registry.json")

KINDS = ("lane", "config_change")
STATUSES = ("active", "halted", "verdict_halt", "survived", "retired")
TERMINAL_STATUSES = tuple(s for s in STATUSES if s != "active")


# ---- canonical terms + hashing -------------------------------------------

def canonical_terms_json(terms):
    """Canonical JSON for a terms dict: sorted keys, tight separators.
    Key order in the source dict never changes the output."""
    return json.dumps(terms or {}, sort_keys=True,
                      separators=(",", ":"), default=str)


def terms_hash(terms):
    """sha256 hex of the canonical JSON of terms."""
    return hashlib.sha256(
        canonical_terms_json(terms).encode("utf-8")).hexdigest()


def _normalized_terms(terms):
    """Terms as they will be stored: round-tripped through the
    canonical JSON so the stored form always hashes to terms_hash."""
    try:
        return json.loads(canonical_terms_json(terms))
    except Exception:
        return dict(terms or {})


# ---- storage (grad.py ledger pattern: flock + atomic write) ---------------

import fcntl


def _lock_path():
    return REGISTRY_PATH + ".lock"


class _registry_lock:
    """Exclusive inter-process lock spanning a read-modify-write."""

    def __enter__(self):
        parent = os.path.dirname(REGISTRY_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lf = open(_lock_path(), "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
        finally:
            self._lf.close()
        return False


def _read_registry():
    """Bare registry read. Hold _registry_lock if you will modify.
    Never raises: missing/corrupt/non-list reads as []."""
    try:
        with open(REGISTRY_PATH) as f:
            d = json.load(f)
        if isinstance(d, list):
            return [r for r in d if isinstance(r, dict)]
    except Exception:
        pass
    return []


def _write_registry(records):
    """Bare registry write (atomic tmp+rename). Hold _registry_lock."""
    try:
        tmp = REGISTRY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(records, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, REGISTRY_PATH)
        return True
    except Exception:
        return False


def _load():
    """Locked single read (for read-only callers). Never raises."""
    try:
        with _registry_lock():
            return _read_registry()
    except Exception:
        # Even a lock failure must not take readers down.
        return _read_registry()


# ---- public API -------------------------------------------------------------

def register(lane, kind, terms, note="", started_ts=None):
    """Register a trial. Idempotent: if an ACTIVE trial already exists
    for (lane, terms_hash), it is returned unchanged. Otherwise a new
    active record is appended. Returns the trial dict. Never raises:
    on a storage failure the would-be record is returned unstored."""
    lane = str(lane or "")
    kind = str(kind or "lane")
    if kind not in KINDS:
        kind = "lane"
    terms = _normalized_terms(terms)
    th = terms_hash(terms)
    rec = {
        "trial_id": "%s-%s" % (lane or "trial", uuid.uuid4().hex[:12]),
        "lane": lane,
        "kind": kind,
        "terms": terms,
        "terms_hash": th,
        "started_ts": float(started_ts) if started_ts else time.time(),
        "stopped_ts": None,
        "closes": 0,
        "expectancy": None,
        "status": "active",
        "note": str(note or ""),
    }
    try:
        with _registry_lock():
            records = _read_registry()
            for r in records:
                if r.get("lane") == lane and \
                        r.get("terms_hash") == th and \
                        r.get("status") == "active":
                    return dict(r)
            records.append(rec)
            _write_registry(records)
    except Exception as e:
        logger.warning("trials register failed: %s", e)
    return dict(rec)


def close_trial(trial_id, status, closes, expectancy):
    """Close a trial: stamp stopped_ts, final closes + expectancy,
    and a terminal status (halted | verdict_halt | survived |
    retired). Returns the updated record, or None if not found.
    Never raises."""
    if status not in TERMINAL_STATUSES:
        status = "retired"
    try:
        with _registry_lock():
            records = _read_registry()
            for r in records:
                if r.get("trial_id") == trial_id:
                    r["status"] = status
                    r["stopped_ts"] = time.time()
                    r["closes"] = int(closes or 0)
                    r["expectancy"] = (None if expectancy is None
                                        else round(float(expectancy), 6))
                    _write_registry(records)
                    return dict(r)
    except Exception as e:
        logger.warning("trials close_trial failed: %s", e)
    return None


def update_stats(trial_id, closes=None, expectancy=None):
    """Refresh the running counters of a trial WITHOUT changing its
    status (used by backfills/syncs for still-active trials).
    Returns the updated record, or None if not found. Never raises."""
    try:
        with _registry_lock():
            records = _read_registry()
            for r in records:
                if r.get("trial_id") == trial_id:
                    if closes is not None:
                        r["closes"] = int(closes)
                    if expectancy is not None:
                        r["expectancy"] = round(float(expectancy), 6)
                    _write_registry(records)
                    return dict(r)
    except Exception as e:
        logger.warning("trials update_stats failed: %s", e)
    return None


def all_trials():
    """Every trial on record, oldest first. Never raises."""
    return _load()


def active_trials():
    """Trials with status 'active'. Never raises."""
    return [r for r in _load() if r.get("status") == "active"]


def trials_for(lane):
    """All trials (any status) for one lane. Never raises."""
    return [r for r in _load() if r.get("lane") == str(lane)]


# ---- backfill from live ledgers (READ-ONLY on the ledgers) -----------------

# lane -> ledger filename under the agent home.
BACKFILL_LEDGERS = {
    "grad": "grad_ledger.json",
    "grad_raw": "grad_raw_ledger.json",
    "ceo": "ceo_ledger.json",
    "degen_mirror": "degen_mirror_ledger.json",
    "momentum": "momentum_ledger.json",
    "watcher": "watcher_ledger.json",
}

# Used only when NO killswitch state file exists at all.
_KNOWN_HALTED_FALLBACK = {"ceo", "degen_mirror"}


def _halted_lanes(base_dir):
    """Halted-lane set from killswitch state. Checks both the legacy
    flat path (killswitch_state.json) and the current path
    (killswitch/state.json). Falls back to the known-halted set when
    no state file exists."""
    for cand in (os.path.join(base_dir, "killswitch_state.json"),
                 os.path.join(base_dir, "killswitch", "state.json")):
        try:
            with open(cand) as f:
                d = json.load(f)
            lanes = d.get("lanes") or {}
            return {ln for ln, s in lanes.items()
                    if isinstance(s, dict) and s.get("halted")}
        except Exception:
            continue
    return set(_KNOWN_HALTED_FALLBACK)


def _ledger_expectancy(closes):
    """Mean pnl over closes (pnl_usd, falling back to pnl), or None
    when no close carries a numeric pnl."""
    vals = []
    for c in closes or []:
        if not isinstance(c, dict):
            continue
        v = c.get("pnl_usd")
        if not isinstance(v, (int, float)):
            v = c.get("pnl")
        if isinstance(v, (int, float)):
            vals.append(float(v))
    if not vals:
        return None
    return sum(vals) / len(vals)


def backfill_from_ledgers(base_dir=None):
    """Register one lane trial per known live ledger found under
    base_dir (default: the agent HOME). Terms come from the ledger's
    preregistered_verdict.terms when present, else a minimal
    backfill marker dict. Closes/expectancy are computed from the
    ledger's closes; status is 'halted' when the killswitch state
    says the lane is halted, a ledger verdict (when present) maps to
    verdict_halt/survived, otherwise 'active'. Ledgers are only ever
    READ. Returns the list of trial records. Never raises."""
    base_dir = base_dir or HOME
    halted = _halted_lanes(base_dir)
    out = []
    for lane, fname in BACKFILL_LEDGERS.items():
        path = os.path.join(base_dir, fname)
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        prereg = d.get("preregistered_verdict") or {}
        if isinstance(prereg.get("terms"), dict) and prereg["terms"]:
            terms = dict(prereg["terms"])
            note = "backfilled from %s (pre-registered terms)" % fname
        else:
            terms = {"source": "backfill",
                     "note": "terms not pre-registered in ledger"}
            note = "backfilled from %s" % fname
        closes = d.get("closes") or []
        n_closes = len(closes) if isinstance(closes, list) else 0
        exp = _ledger_expectancy(closes if isinstance(closes, list)
                                 else [])
        started = None
        for k in ("created_ts",):
            if isinstance(d.get(k), (int, float)):
                started = float(d[k])
        if started is None and isinstance(prereg.get("registered_ts"),
                                          (int, float)):
            started = float(prereg["registered_ts"])
        if started is None and isinstance(closes, list):
            ts_vals = [float(c[k]) for c in closes
                       if isinstance(c, dict)
                       for k in ("entry_ts", "close_ts")
                       if isinstance(c.get(k), (int, float))]
            if ts_vals:
                started = min(ts_vals)
        # Backfill is safely re-runnable: a TERMINAL trial already on
        # record with these exact terms is refreshed in place, never
        # duplicated (register()'s idempotency covers active trials).
        th = terms_hash(_normalized_terms(terms))
        terminal = None
        for r in trials_for(lane):
            if r.get("terms_hash") == th and \
                    r.get("status") != "active":
                terminal = r
                break
        if terminal is not None:
            updated = update_stats(terminal["trial_id"],
                                   closes=n_closes, expectancy=exp)
            out.append(updated or terminal)
            continue
        trial = register(lane, "lane", terms, note=note,
                         started_ts=started)
        verdict = d.get("verdict")
        if isinstance(verdict, dict) and verdict:
            action = str(verdict.get("action") or "")
            status = "verdict_halt" if "HALT" in action else "survived"
            closed = close_trial(trial["trial_id"], status,
                                 n_closes, exp)
            out.append(closed or trial)
        elif lane in halted:
            closed = close_trial(trial["trial_id"], "halted",
                                 n_closes, exp)
            out.append(closed or trial)
        else:
            updated = update_stats(trial["trial_id"], closes=n_closes,
                                   expectancy=exp)
            out.append(updated or trial)
    return out


# ---- CLI ---------------------------------------------------------------------

def _main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Trial registry: list/register/close/backfill.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="all trials")
    sub.add_parser("active", help="active trials only")
    pf = sub.add_parser("for-lane", help="trials for one lane")
    pf.add_argument("lane")
    pr = sub.add_parser("register", help="register a trial")
    pr.add_argument("lane")
    pr.add_argument("--kind", default="lane", choices=KINDS)
    pr.add_argument("--terms", default="{}",
                    help="terms as a JSON object string")
    pr.add_argument("--note", default="")
    pc = sub.add_parser("close", help="close a trial")
    pc.add_argument("trial_id")
    pc.add_argument("status", choices=TERMINAL_STATUSES)
    pc.add_argument("--closes", type=int, default=0)
    pc.add_argument("--expectancy", type=float, default=None)
    sub.add_parser("backfill", help="backfill from live ledgers")
    args = p.parse_args(argv)

    if args.cmd == "active":
        rows = active_trials()
    elif args.cmd == "for-lane":
        rows = trials_for(args.lane)
    elif args.cmd == "register":
        try:
            terms = json.loads(args.terms)
        except Exception:
            terms = {}
        rows = [register(args.lane, args.kind, terms, note=args.note)]
    elif args.cmd == "close":
        rows = [close_trial(args.trial_id, args.status, args.closes,
                            args.expectancy)]
    elif args.cmd == "backfill":
        rows = backfill_from_ledgers()
    else:
        rows = all_trials()
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
