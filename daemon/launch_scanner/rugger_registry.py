#!/usr/bin/env python3
"""Serial-rugger registry (2026-10-23): deployers with repeated
dead/rugged launches, on the record.

MEASUREMENT / DISPLAY ONLY. Nothing in this module gates, vetoes,
sizes, or blocks any trade, card, or entry. The existing veto.py
_check_deployer keeps its own repeat-rug logic and behavior —
this registry is a parallel measurement store that READS (never
writes) the same evidence files, and adds mint-level joins veto
does not keep.

DEFINITION (explicit; this is the whole contract): a deployer is
FLAGGED when >= 3 (FLAG_THRESHOLD) of its launches carry recorded
dead/rugged evidence. Evidence is counted ONLY where it is actually
present in the source data — never inferred, never invented:

  1. launch_scanner/deployer_history.json counters (deployers.py):
     the graded outcome counters `rugs` and `dead`, incremented
     only by record_outcome() with outcome "rug"/"dead".
     counter_dead = rugs + dead.
  2. Mint-level dead_mints: grad_ledger.json + grad_raw_ledger.json
     `dead_mints` (mints the grad lanes finally skipped) joined to a
     deployer through live dossiers (dossiers/<mint>.json ->
     deployer). Entries whose reason starts with "window expired"
     are EXCLUDED — that reason means "never entered, retry window
     passed", not dead/rugged. All other reasons present in the
     data (overwhelmingly "safety: rugcheck danger: ...") count.
  3. deployer_tracker.json launch entries (when the file exists)
     carrying an explicit truthy `rugged` / `dead` / `rug` flag.
  4. memory/launch_archive.jsonl (dossiers.py durable launch
     memory): the SAME mint->deployer join and launch census as
     the live dossiers, hardened against the 24h dossier TTL.
     Launch records join dead_mints to their deployer and count
     toward the launch census; launch_rollup rows contribute
     their aggregated per-year launch counts. Outcome records
     are NOT read here: simulated closes are not dead/rug
     evidence. The flag rule and thresholds are unchanged.

The reported `dead` is the CONSERVATIVE lower bound
max(counter_dead, distinct mint-level dead) so the two pipelines
(counter-graded closes vs skipped mints) can never double-count
one launch into a flag. `launches` is likewise the max of the
recorded counters (history `launches_seen`, dossier-distinct
mints, tracker launch count). Per-source components are stored
on every record so the number can always be audited back to its
evidence. `examples` lists up to 5 mints with mint-level dead
evidence (empty when the flag rests on counters alone — counters
keep no mint ids, and none are fabricated).

Registry: one JSON file, ~/.forex-agent/rugger_registry.json ::

    {"version": 1, "updated_ts": float,
     "deployers": {deployer: {"launches": int, "dead": int,
                              "dead_counter": int,
                              "dead_mints": int,
                              "flagged": bool,
                              "examples": [mint, ...],
                              "sources": [str, ...]}}}

Storage follows the trials.py pattern (HOME from
FOREX_AGENT_HOME, module-level REGISTRY_PATH, flock + atomic
write). Reads never raise; a missing/corrupt registry reads as
empty and lookup() returns the zero record.
"""
import json
import logging
import os
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.rugger_registry")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
REGISTRY_PATH = os.path.join(HOME, "rugger_registry.json")

FLAG_THRESHOLD = 3
_MAX_EXAMPLES = 5
# dead_mints reasons that are NOT dead/rugged evidence.
_NON_EVIDENCE_REASON_PREFIXES = ("window expired",)

import fcntl


def _home():
    """Evidence files live beside the registry (agent home), so
    reassigning REGISTRY_PATH in tests isolates all reads."""
    return os.path.dirname(REGISTRY_PATH) or HOME


def _lock_path():
    return REGISTRY_PATH + ".lock"


class _registry_lock:
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


def _blank():
    return {"version": 1, "updated_ts": 0.0, "deployers": {}}


def _read_registry():
    try:
        with open(REGISTRY_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("deployers"), dict):
            out = _blank()
            out.update(d)
            return out
    except Exception:
        pass
    return _blank()


def load_registry():
    """The full registry dict. Never raises."""
    try:
        with _registry_lock():
            return _read_registry()
    except Exception:
        return _read_registry()


def _load_json(path, default):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


# ---- evidence collection (READ-ONLY on every source file) -------------------

def _evidence(home):
    """Aggregate all evidence under `home` into
    {deployer: {"launches": int, "dead_counter": int,
                "dead_mint_set": set, "sources": set}}.
    Pure file reading; never raises."""
    agg = {}

    def slot(dep):
        return agg.setdefault(dep, {"launches": 0, "dead_counter": 0,
                                    "dead_mint_set": set(),
                                    "sources": set()})

    # 1. deployer_history counters + tracker flags.
    hist = _load_json(os.path.join(
        home, "launch_scanner", "deployer_history.json"), {})
    for dep, e in hist.items():
        if not dep or not isinstance(e, dict):
            continue
        s = slot(str(dep))
        s["launches"] = max(s["launches"],
                            int(e.get("launches_seen") or 0))
        dead_c = int(e.get("rugs") or 0) + int(e.get("dead") or 0)
        if dead_c:
            s["dead_counter"] = max(s["dead_counter"], dead_c)
            s["sources"].add("deployer_history")
        elif int(e.get("launches_seen") or 0):
            s["sources"].add("deployer_history")

    tracker = _load_json(os.path.join(
        home, "launch_scanner", "deployer_tracker.json"), {})
    for dep, e in ((tracker.get("deployers") or {}).items()
                   if isinstance(tracker, dict) else []):
        if not dep or not isinstance(e, dict):
            continue
        s = slot(str(dep))
        launches = e.get("launches") or []
        if isinstance(launches, list):
            s["launches"] = max(s["launches"], len(launches),
                                int(e.get("total_launches") or 0))
            for l in launches:
                if isinstance(l, dict) and (
                        l.get("rugged") or l.get("dead") or l.get("rug")):
                    m = str(l.get("mint") or "")
                    if m:
                        s["dead_mint_set"].add(m)
                        s["sources"].add("deployer_tracker")
        else:
            s["launches"] = max(s["launches"],
                                int(e.get("total_launches") or 0))

    # 2. dead_mints from both grad ledgers.
    dead_mints = {}      # mint -> reason (first ledger wins)
    for fn in ("grad_ledger.json", "grad_raw_ledger.json"):
        led = _load_json(os.path.join(home, fn), {})
        dm = led.get("dead_mints") if isinstance(led, dict) else None
        if isinstance(dm, dict):
            for mint, meta in dm.items():
                reason = ""
                if isinstance(meta, dict):
                    reason = str(meta.get("reason") or "")
                if reason.startswith(_NON_EVIDENCE_REASON_PREFIXES):
                    continue
                dead_mints.setdefault(str(mint), reason)

    # Dossiers: mint -> deployer join + per-deployer launch census.
    ddir = os.path.join(home, "dossiers")
    try:
        fns = [f for f in os.listdir(ddir) if f.endswith(".json")]
    except Exception:
        fns = []
    dossier_mints = {}   # deployer -> set of mints
    for fn in fns:
        d = _load_json(os.path.join(ddir, fn), {})
        if not isinstance(d, dict):
            continue
        dep = str(d.get("deployer") or "")
        mint = str(d.get("mint") or fn[:-5])
        if not dep:
            continue
        dossier_mints.setdefault(dep, set()).add(mint)
        if mint in dead_mints:
            s = slot(dep)
            s["dead_mint_set"].add(mint)
            s["sources"].add("dead_mints+dossiers")
    for dep, mints in dossier_mints.items():
        s = slot(dep)
        s["launches"] = max(s["launches"], len(mints))
        s["sources"].add("dossiers")

    # 4. Durable launch archive: the same joins as the live
    # dossiers above, still valid after those dossiers expire.
    # Read-only; a missing/corrupt archive contributes nothing
    # (evidence is counted only where it is present).
    try:
        archive_mints = {}   # deployer -> set of mints
        rollup_launches = {}  # deployer -> summed rollup count
        with open(os.path.join(
                home, "memory", "launch_archive.jsonl")) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict) or rec.get("v") != 1:
                    continue
                dep = str(rec.get("deployer") or "")
                if not dep:
                    continue
                rtype = rec.get("record_type", "launch")
                if rtype == "launch":
                    mint = str(rec.get("mint") or "")
                    if not mint:
                        continue
                    archive_mints.setdefault(dep, set()).add(mint)
                    if mint in dead_mints:
                        s = slot(dep)
                        s["dead_mint_set"].add(mint)
                        s["sources"].add("dead_mints+launch_archive")
                elif rtype == "launch_rollup":
                    try:
                        n = int(rec.get("launches") or 0)
                    except Exception:
                        n = 0
                    if n > 0:
                        rollup_launches[dep] = \
                            rollup_launches.get(dep, 0) + n
        for dep, mints in archive_mints.items():
            s = slot(dep)
            s["launches"] = max(s["launches"], len(mints))
            s["sources"].add("launch_archive")
        for dep, n in rollup_launches.items():
            s = slot(dep)
            s["launches"] = max(s["launches"], n)
            s["sources"].add("launch_archive")
    except Exception:
        pass
    return agg


def build_records(home=None):
    """Evidence -> registry records (pure aggregation + the flag
    rule; no file writes). Never raises."""
    try:
        home = home or _home()
        agg = _evidence(home)
        out = {}
        for dep, s in agg.items():
            mint_dead = len(s["dead_mint_set"])
            dead = max(s["dead_counter"], mint_dead)
            out[dep] = {
                "launches": int(s["launches"]),
                "dead": int(dead),
                "dead_counter": int(s["dead_counter"]),
                "dead_mints": int(mint_dead),
                "flagged": bool(dead >= FLAG_THRESHOLD),
                "examples": sorted(s["dead_mint_set"])[:_MAX_EXAMPLES],
                "sources": sorted(s["sources"]),
            }
        return out
    except Exception as e:
        logger.warning("rugger build_records failed: %s", e)
        return {}


def refresh(home=None):
    """Rebuild the registry from live evidence and persist it.
    Returns stats(). Never raises."""
    try:
        records = build_records(home=home)
        reg = {"version": 1, "updated_ts": time.time(),
               "deployers": records}
        try:
            with _registry_lock():
                tmp = REGISTRY_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(reg, f)
                os.replace(tmp, REGISTRY_PATH)
        except Exception as e:
            logger.warning("rugger registry write failed: %s", e)
    except Exception as e:
        logger.warning("rugger refresh failed: %s", e)
    return stats()


# ---- query API (display only) ------------------------------------------------

_ZERO = {"launches": 0, "dead": 0, "flagged": False, "examples": []}


def lookup(deployer):
    """{launches, dead, flagged, examples} for one deployer; the
    zero record for an unknown deployer. Never raises."""
    try:
        dep = str(deployer or "").strip()
        if not dep:
            return dict(_ZERO)
        rec = (load_registry().get("deployers") or {}).get(dep)
        if not isinstance(rec, dict):
            return dict(_ZERO)
        return {"launches": int(rec.get("launches") or 0),
                "dead": int(rec.get("dead") or 0),
                "flagged": bool(rec.get("flagged")),
                "examples": list(rec.get("examples") or [])[:_MAX_EXAMPLES]}
    except Exception:
        return dict(_ZERO)


def stats():
    """Registry headline numbers. Never raises."""
    try:
        deps = load_registry().get("deployers") or {}
        return {"deployers": len(deps),
                "flagged": sum(1 for r in deps.values()
                               if isinstance(r, dict) and r.get("flagged"))}
    except Exception:
        return {"deployers": 0, "flagged": 0}
