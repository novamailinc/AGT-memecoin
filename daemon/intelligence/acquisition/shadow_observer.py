"""Router shadow observer (Phase 3, Master Directive §19 Phase 3).

SHADOW MODE ONLY. The central router makes the routing decision for
each live request, the decision + the provider that actually served
it are logged, and the live path executes EXACTLY as before. Nothing
here sends a request, retries one, or changes a result.

How it works: the live transport (``daemon/launch_scanner/reserves.
rpc``) calls ``observe_live_call()`` after every logical call with
the method, the provider that answered (``last_rpc_source()``:
"helius" / "public-rpc" / None on failure), and the outcome. This
module asks the router for its PURE decision (``route()`` —
classification + candidate order, no transport, no network) and
records one JSONL line:

    {"ts", "method", "workload", "helius_priority", "mode",
     "primary", "candidates", "served", "kind", "elapsed_s",
     "agreement", "fallback_difference"}

Agreement classes:
- "exact" — the router's primary served the call.
- "fallback_consistent" — the serving provider was in the router's
  candidate order but not primary (e.g. live fell back to public
  while the router's primary was Helius: the router's order
  contains public as a fallback, so the live behaviour is
  consistent with the router's plan).
- "divergent" — the serving provider was NOT in the router's
  candidate order at all (a real disagreement; needs a cause).
- "no_serve" — nothing served the call (live failure); the router
  cannot be judged against a failure, so these are counted
  separately and never folded into the agreement rate.
- "refused" — the router refused the workload (paid-gated /
  unclassifiable); the live path should not have served it either.

The journal is append-only under FOREX_AGENT_HOME
(``acquisition_router_shadow.jsonl``); no key material is ever
written. ``summarize()`` reads the journal and reports the rates
— it is the evidence for the Phase 3 report, computed from the
file, not from any process's memory.

Fail-safety: the observer NEVER raises into the live path. The
router is constructed lazily on first observation; if construction
or a decision fails, the observer disables itself for the life of
the process (counted in ``errors``) and the live path continues
untouched. Journal-append failures are likewise swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.shadow_observer")

JOURNAL_FILE = "acquisition_router_shadow.jsonl"

# Live source names (reserves.last_rpc_source()) -> router provider.
SERVED_MAP = {"alchemy": "alchemy", "helius": "helius",
              "public-rpc": "public", "public": "public"}


def served_to_provider(served):
    """Map a live source string to a router provider name (None
    when nothing served the call)."""
    if served is None:
        return None
    return SERVED_MAP.get(served, served)


def agreement_class(decision, served_provider, kind, method=None):
    """Agreement between the router's decision and live behaviour.

    Returns (agreement, fallback_difference_or_None)."""
    if decision.get("refused"):
        return "refused", decision.get("refusal_reason")
    if served_provider is None:
        return "no_serve", None
    primary = decision.get("primary")
    candidates = decision.get("candidates") or []
    if served_provider == primary:
        return "exact", None
    if served_provider in candidates:
        from .provider_router import fallback_difference
        return ("fallback_consistent",
                fallback_difference(primary, served_provider,
                                    method,
                                    decision.get("workload")))
    return ("divergent",
            "live served by %s, which is not in the router's "
            "candidate order %s" % (served_provider, candidates))


class ShadowObserver:
    """Records router decisions against live outcomes."""

    def __init__(self, home=None, router=None):
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self._journal_path = os.path.join(self.home, JOURNAL_FILE)
        self._router = router
        self._router_failed = False
        self._lock = threading.Lock()
        self.observed = 0
        self.errors = 0

    def _get_router(self):
        if self._router is not None:
            return self._router
        if self._router_failed:
            return None
        try:
            from .provider_router import ProviderRouter
            self._router = ProviderRouter(home=self.home)
            return self._router
        except Exception:
            self._router_failed = True
            self.errors += 1
            logger.warning("shadow observer: router construction "
                           "failed; shadow disabled for this process",
                           exc_info=True)
            return None

    def observe(self, method, params=None, served=None, kind="ok",
                elapsed=0.0, hints=None, priority="card"):
        """Record one live call against the router's decision.
        Never raises. Returns the journal entry dict, or None when
        the observation could not be recorded."""
        router = self._get_router()
        if router is None:
            return None
        try:
            decision = router.route(method, params, priority=priority,
                                    hints=hints)
        except Exception:
            self.errors += 1
            logger.warning("shadow observer: route() failed for %s",
                           method, exc_info=True)
            return None
        served_provider = served_to_provider(served)
        agreement, diff = agreement_class(decision, served_provider,
                                          kind, method=method)
        entry = {
            "ts": time.time(),
            "method": method,
            "workload": decision.get("workload"),
            "helius_priority": decision.get("helius_priority"),
            "mode": decision.get("mode"),
            "primary": decision.get("primary"),
            "candidates": decision.get("candidates"),
            "served": served_provider,
            "kind": kind,
            "elapsed_s": round(elapsed, 4),
            "agreement": agreement,
            "fallback_difference": diff,
            "refused": bool(decision.get("refused")),
            "refusal_reason": decision.get("refusal_reason"),
        }
        try:
            line = json.dumps(entry) + "\n"
            with self._lock:
                with open(self._journal_path, "a") as fh:
                    fh.write(line)
                self.observed += 1
        except OSError:
            self.errors += 1
            logger.warning("shadow observer: journal append failed",
                           exc_info=True)
            return None
        return entry

    def summarize(self, since_ts=None):
        """Aggregate the journal file: counts per agreement class
        and per method, plus the agreement rate (exact +
        fallback_consistent over exact + fallback_consistent +
        divergent; no_serve / refused are reported separately and
        never folded into the rate). Reads the FILE, so it covers
        every process that observed, not just this one."""
        counts = {"exact": 0, "fallback_consistent": 0,
                  "divergent": 0, "no_serve": 0, "refused": 0}
        by_method = {}
        divergences = []
        total = 0
        try:
            with open(self._journal_path) as fh:
                lines = fh.readlines()
        except OSError:
            lines = []
        first_ts = None
        last_ts = None
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            ts = entry.get("ts")
            if since_ts is not None and ts is not None \
                    and ts < since_ts:
                continue
            total += 1
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            ag = entry.get("agreement")
            if ag in counts:
                counts[ag] += 1
            meth = entry.get("method") or "?"
            slot = by_method.setdefault(
                meth, {"exact": 0, "fallback_consistent": 0,
                       "divergent": 0, "no_serve": 0, "refused": 0,
                       "total": 0})
            if ag in slot:
                slot[ag] += 1
            slot["total"] += 1
            if ag == "divergent":
                divergences.append(entry)
        judged = (counts["exact"] + counts["fallback_consistent"]
                  + counts["divergent"])
        agree = counts["exact"] + counts["fallback_consistent"]
        rate = (agree / judged) if judged else None
        return {"journal": self._journal_path,
                "total": total, "first_ts": first_ts,
                "last_ts": last_ts,
                "window_s": ((last_ts - first_ts)
                             if first_ts and last_ts else None),
                "counts": counts,
                "agreement_rate": rate,
                "exact_rate": ((counts["exact"] / judged)
                               if judged else None),
                "by_method": by_method,
                "divergences": divergences}


# Process-wide singleton for the live transport hook. The hook in
# reserves.py calls observe_live_call(); it never constructs the
# observer itself.
_OBSERVER = None
_OBSERVER_LOCK = threading.Lock()


def get_observer(home=None):
    global _OBSERVER
    with _OBSERVER_LOCK:
        if _OBSERVER is None:
            _OBSERVER = ShadowObserver(home=home)
        return _OBSERVER


def observe_live_call(method, params=None, served=None, kind="ok",
                      elapsed=0.0, hints=None, priority="card",
                      home=None):
    """Module-level hook for the live transport. Never raises.
    Disabled when ROUTER_SHADOW_OFF=1 (test runs set this so suite
    traffic never pollutes the live shadow journal)."""
    if os.environ.get("ROUTER_SHADOW_OFF") == "1":
        return None
    try:
        return get_observer(home=home).observe(
            method, params, served=served, kind=kind, elapsed=elapsed,
            hints=hints, priority=priority)
    except Exception:
        return None


def summarize_shadow(home=None, since_ts=None):
    """Aggregate the shadow journal (see ShadowObserver.summarize).
    Never raises."""
    try:
        return ShadowObserver(home=home).summarize(since_ts=since_ts)
    except Exception:
        return {"total": 0, "counts": {}, "agreement_rate": None,
                "by_method": {}, "divergences": []}
