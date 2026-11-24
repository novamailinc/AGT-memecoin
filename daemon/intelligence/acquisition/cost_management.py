"""Cost management layer (Phase 9, spec §12).

Wraps — never rewrites — ``provider_usage`` (the §7 usage store)
and ``usage_tracker`` (the §7 usage manager). Goal: maximise useful
intelligence per unit of provider cost, NOT maximise API usage.

What lives here:

- Provider-specific request counters, aggregated per provider
  family from the per-account store (accounts have separate quotas;
  this layer never merges them for enforcement, only aggregates
  them for reporting).
- Per-provider cost accounting in the provider's OWN unit — Helius
  credits, Alchemy compute units (CU), Chainstack request units
  (RU). Every cost figure traces to ``METHOD_COSTS`` in
  ``provider_capabilities`` (single source). NO credit-conversion
  rate is invented anywhere: provider credits are NOT
  interchangeable, so costs are never summed across providers.
- WebSocket bandwidth tracking WHERE MEASURABLE: bytes are recorded
  only when the transport reports them; otherwise the event is
  recorded as ``unmeasured`` — never invented.
- Request caching: accounted for via the ROUTER's cache
  (``provider_router.ResponseCache``). This module builds NO second
  cache; a cache hit is recorded in the journal as ``cached`` and
  its avoided cost is counted as saved cost.
- Event deduplication accounting: deduplicated requests counted as
  saved cost (``record_dedup``).
- Incremental retrieval accounting (``record_incremental``):
  saved cost = full-retrieval cost minus incremental cost.
- Filtered-subscription accounting (``record_filtered_subscription``):
  events filtered out before delivery are counted; saved bandwidth
  only where bytes were measured.
- Batch-request accounting (``record_batch``): saved cost = N x
  per-call cost minus the batch cost.
- Priority-based processing: nonessential work is shed FIRST,
  essential work also stops at recorded exhaustion. Below exhaustion,
  warn 80% / stop nonessential 95% ordering is unchanged.
- AUTOMATIC SUPPRESSION of low-value requests (see
  ``LOW_VALUE_DEFINITION``). ``pre_call_check`` is the pre-call
  gate workload modules call before issuing nonessential requests;
  the Phase 11 integration wires it into the lanes. It does NOT
  edit the router.

Budgets come from ``provider_capabilities``; a budget with
``amount=None`` is UNENFORCED and recorded as such — never assumed
infinite, never assumed zero.

Sidecar file (under FOREX_AGENT_HOME, atomic replace,
flock-guarded, same pattern as ``UsageStore``):
- ``acquisition_cost_accounting.json`` — WS bytes, dedup/batch/
  incremental/filtered-subscription savings, and a bounded log of
  suppressed pre-call checks. The usage store + journal remain the
  authoritative record of serviced requests; this file only holds
  accounting the journal cannot express (bytes, avoided cost).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time

from . import provider_capabilities as caps
from .provider_usage import (
    JOURNAL_FILE,
    UsageStore,
    budget_for,
)
from .request_scheduler import (
    PRIORITY_BACKGROUND,
    PRIORITY_CARD,
    PRIORITY_CRITICAL,
    PRIORITY_DEPLOYER,
)
from .usage_tracker import (
    ESSENTIAL_PRIORITIES,
    NONESSENTIAL_PRIORITIES,
    UsageTracker,
)

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.cost_management")

ACCOUNTING_FILE = "acquisition_cost_accounting.json"

# METHOD_COSTS tuple index per provider family. Public has no metered
# cost (index 3 is None for every method); its "cost" is unmetered.
COST_INDEX = {"helius": 0, "alchemy": 1, "chainstack": 2, "public": 3}

# Cost unit per provider family (from the capability records' quota
# units; public is unmetered). Never summed across providers.
COST_UNIT = {"helius": "credits", "alchemy": "compute units",
             "chainstack": "request units", "public": None}

# ---------------------------------------------------------------------------
# Low-value definition (spec §12, explicit — this is the suppression
# rule the whole layer enforces):
#
# - LOW-VALUE = P3/background-priority work: research, batch scans,
#   dossiers, backfill refresh, and any other work that is neither
#   entry/exit-critical nor card safety. At >= 80% of ANY enforced
#   provider budget, background work is suppressed (first on the
#   account that crossed 80%, then globally — a strained fleet sheds
#   background everywhere, because background on a healthy account
#   still competes for the same shared scheduler/transport).
# - At >= 95% of a provider's budget, ALL nonessential work
#   (deployer + background) on that account is stopped (the §7
#   tracker policy, reused here, not re-implemented).
# - Essential work is not low-value, but priority is not spending
#   approval. Every priority stops at known recorded exhaustion.
# ---------------------------------------------------------------------------
LOW_VALUE_DEFINITION = (
    "Low-value requests are P3/background-priority work (research, "
    "batch scans, dossiers, backfill refresh) and, at the 95% stop "
    "threshold, ALL nonessential work (deployer + background). "
    "Essential work — critical entry/exit and card safety — is never "
    "low-value, but is refused at known recorded exhaustion. Background is suppressed at "
    "80% of any enforced provider budget; all nonessential work is "
    "stopped at 95% of the serving account's budget."
)

# Suppression reasons (stable strings; surfaced in reports/logs).
REASON_ACCOUNT_DISABLED = "account_disabled"
REASON_BUDGET_STOP = "budget_stop_nonessential"
REASON_WARN_BACKGROUND = "budget_warn_suppress_background"
REASON_GLOBAL_PRESSURE = "global_budget_pressure_background"

# Bound on the suppression audit log (newest entries kept).
MAX_SUPPRESSION_LOG = 500


def method_cost(provider, method):
    """Per-call cost of ``method`` on ``provider`` in that provider's
    own unit (Helius credits / Alchemy CU / Chainstack RU), from
    ``METHOD_COSTS``. Returns None when the method is unknown or the
    cost is UNVERIFIED for that provider — never assume 1, never
    assume 0."""
    costs = caps.method_cost(provider, method)
    if costs is None:
        return None
    return costs[COST_INDEX[provider]]


def cost_unit(provider):
    """Cost unit for a provider family, or None (public: unmetered)."""
    return COST_UNIT.get(provider)


def provider_of(account):
    """Provider family for an account name (caps record), or None."""
    rec = caps.get_account(account) or {}
    return rec.get("provider")


def _default_accounting():
    return {
        "ws": {},
        "dedup": {},
        "batch": {},
        "incremental": {},
        "filtered_subscriptions": {},
        "suppressions": [],
    }


def _default_sidecar_record():
    return {"events": 0, "bytes_measured": 0,
            "bytes_unmeasured_events": 0, "requests": 0,
            "saved_cost": 0, "saved_unknown": 0,
            "events_received": 0, "events_filtered": 0,
            "batch_calls": 0, "batch_items": 0,
            "full_cost": 0, "incremental_cost": 0}


class CostManager:
    """Cost-management layer wrapping UsageTracker + UsageStore."""

    def __init__(self, home=None, tracker=None):
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self.tracker = tracker or UsageTracker(home=self.home)
        self.store = self.tracker.store
        self._path = os.path.join(self.home, ACCOUNTING_FILE)
        self._lock_path = os.path.join(self.home,
                                       ACCOUNTING_FILE + ".lock")

    # -- sidecar --------------------------------------------------------
    def _load_accounting(self):
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("accounting file is not an object")
            return data
        except (OSError, ValueError):
            return _default_accounting()

    def _save_accounting(self, data):
        tmp = "%s.tmp.%d" % (self._path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    def _transact_accounting(self, fn):
        """Read-modify-write under an exclusive flock."""
        try:
            lock_fh = open(self._lock_path, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                data = self._load_accounting()
                result = fn(data)
                self._save_accounting(data)
                return result
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
        except OSError:
            logger.warning("cost accounting lock unavailable; "
                           "accounting not updated for this call")
            return None

    def accounting(self):
        """Current sidecar accounting (WS bytes, dedup/batch/
        incremental/filtered savings, suppression log)."""
        return self._load_accounting()

    # -- per-provider counters ------------------------------------------
    def provider_counters(self):
        """Per-provider-family aggregates (requests/ok/errors/cost in
        the provider's own unit/cached) summed across that family's
        accounts. Enforcement NEVER uses these aggregates — budgets
        are per account; this is reporting only."""
        summary = self.tracker.summary()
        out = {}
        for account, rec in summary.items():
            provider = rec.get("provider") or provider_of(account)
            if provider is None:
                continue
            agg = out.setdefault(provider, {
                "provider": provider, "unit": cost_unit(provider),
                "accounts": [], "calls": 0, "ok": 0, "errors": 0,
                "rate_limited": 0, "cost_spent": 0,
                "budget_enforced": False, "budget_amount": None,
            })
            agg["accounts"].append(account)
            agg["calls"] += rec.get("calls", 0)
            agg["ok"] += rec.get("ok", 0)
            agg["errors"] += rec.get("errors", 0)
            agg["rate_limited"] += rec.get("rate_limited", 0)
            agg["cost_spent"] += rec.get("cost_spent", 0)
            if rec.get("budget_enforced"):
                agg["budget_enforced"] = True
                amt = rec.get("budget_amount")
                agg["budget_amount"] = (
                    (agg["budget_amount"] or 0) + amt
                    if amt is not None else agg["budget_amount"])
        return out

    # -- cost estimates (METHOD_COSTS only) ------------------------------
    def cost_estimate(self, provider, method, calls=1):
        """Estimated cost of ``calls`` x ``method`` on ``provider``.

        Returns {"provider", "method", "calls", "cost", "unit",
        "verified"} — ``cost`` is None and ``verified`` False when
        the method cost is UNVERIFIED for that provider. No rate is
        invented: the only source is METHOD_COSTS."""
        unit_cost = method_cost(provider, method)
        return {
            "provider": provider,
            "method": method,
            "calls": calls,
            "cost": (unit_cost * calls) if unit_cost is not None else None,
            "unit": cost_unit(provider),
            "verified": unit_cost is not None,
        }

    # -- suppression (pre-call check) ------------------------------------
    def pre_call_check(self, account, priority, workload=None):
        """Gate workload modules call BEFORE issuing a request.

        Call convention (Phase 11 wires this into the lanes):
            allowed, reason = cost.pre_call_check(account, priority)
            if not allowed:
                # do not issue the request; journal the suppression
                # (already logged here) and fall back / skip.
                ...

        Returns (allowed, reason_or_None). Ordering (nonessential
        shed before essential; essential stops at recorded exhaustion):
        1. Disabled account -> refuse everything (operator decision).
        2. Known exhausted monthly/daily/feature budget -> refuse every priority.
        3. Account at/above the 95% stop threshold -> refuse all
           nonessential (tracker policy, reused).
        4. Background priority at/above the account's 80% warn
           threshold -> refuse background only.
        5. Background priority while ANY enforced account is at/above
           80% -> refuse background only (global pressure).
        6. Otherwise allowed.
        Refusals are logged to the suppression audit log.
        """
        if self.tracker.account_disabled(account):
            self._log_suppression(account, priority, workload,
                                  REASON_ACCOUNT_DISABLED)
            return False, REASON_ACCOUNT_DISABLED
        allowed, reason = self.tracker.nonessential_allowed(
            account, priority, workload=workload)
        if not allowed:
            # Tracker refuses nonessential at 95% (or disabled,
            # already handled above).
            self._log_suppression(account, priority, workload, reason)
            return False, reason
        if priority in ESSENTIAL_PRIORITIES:
            return True, None
        if priority == PRIORITY_BACKGROUND:
            status = self.tracker.budget_status(account)
            pct = status.get("pct")
            if pct is not None and pct >= status["warn_threshold_pct"]:
                self._log_suppression(account, priority, workload,
                                      REASON_WARN_BACKGROUND)
                return False, REASON_WARN_BACKGROUND
            strained = self._strained_accounts()
            if strained:
                self._log_suppression(account, priority, workload,
                                      REASON_GLOBAL_PRESSURE)
                return False, REASON_GLOBAL_PRESSURE
        return True, None

    def _strained_accounts(self):
        """Enforced accounts currently at/above their warn threshold."""
        strained = []
        for account in caps.ACCOUNTS:
            status = self.tracker.budget_status(account)
            if status.get("amount") is None or status.get("pct") is None:
                continue
            if status["pct"] >= status["warn_threshold_pct"]:
                strained.append(account)
        return strained

    def _log_suppression(self, account, priority, workload, reason):
        def _do(data):
            log = data.setdefault("suppressions", [])
            log.append({"ts": time.time(), "account": account,
                        "priority": priority, "workload": workload,
                        "reason": reason})
            del log[:-MAX_SUPPRESSION_LOG]
            return None
        self._transact_accounting(_do)

    def suppression_status(self, account=None):
        """What is suppressed right now, per account (or one account):
        warn/stop state, whether background / all nonessential is
        suppressed, and whether essential requests are refused at recorded exhaustion."""
        accounts = [account] if account else list(caps.ACCOUNTS)
        strained = self._strained_accounts()
        out = {}
        for name in accounts:
            status = self.tracker.budget_status(name)
            pct = status.get("pct")
            warn = pct is not None and pct >= status["warn_threshold_pct"]
            stop = bool(status.get("stop_nonessential"))
            out[name] = {
                "account": name,
                "provider": provider_of(name),
                "pct": pct,
                "warn_active": warn,
                "stop_nonessential_active": stop,
                "background_suppressed": bool(
                    self.tracker.account_disabled(name) or stop or warn
                    or bool(strained)),
                "nonessential_suppressed": bool(
                    self.tracker.account_disabled(name) or stop),
                "essential_suppressed": not self.tracker.nonessential_allowed(name, PRIORITY_CRITICAL)[0],
                "disabled": bool(status.get("disabled")),
                "global_pressure": bool(strained),
                "strained_accounts": list(strained),
            }
        return out[account] if account else out

    # -- alerts (warn 80% / stop 95%, existing policy) ------------------
    def alerts(self):
        """Active usage alerts, one dict per account with something
        to say: warn pending (>= 80%), stop active (>= 95%), or
        disabled. Empty list = nothing to alert on."""
        out = []
        for account in caps.ACCOUNTS:
            status = self.tracker.budget_status(account)
            warn = self.tracker.warn_pending(account)
            entry = {"account": account,
                     "provider": provider_of(account),
                     "spent": status["spent"],
                     "amount": status["amount"],
                     "unit": status["unit"],
                     "pct": status["pct"],
                     "enforced": status["enforced"],
                     "warn": warn is not None,
                     "stop_nonessential": bool(
                         status.get("stop_nonessential")),
                     "disabled": bool(status.get("disabled")),
                     "disabled_reason": status.get("disabled_reason")}
            if (entry["warn"] or entry["stop_nonessential"]
                    or entry["disabled"]):
                out.append(entry)
        return out

    # -- WebSocket bandwidth ---------------------------------------------
    def record_ws(self, account, provider, bytes_received=None,
                  events=1, subscription=None):
        """Record WebSocket traffic. ``bytes_received`` is recorded
        ONLY when the transport reports it; pass None when it does
        not and the events are counted as ``unmeasured`` — a byte
        count is never invented. Returns the updated sidecar record
        for the account."""
        def _do(data):
            ws = data.setdefault("ws", {})
            rec = ws.setdefault(account, _default_sidecar_record())
            rec["events"] += events
            if bytes_received is None:
                rec["bytes_unmeasured_events"] += events
            else:
                rec["bytes_measured"] += bytes_received
            if subscription is not None:
                rec.setdefault("subscriptions", {}).setdefault(
                    subscription, 0)
                rec["subscriptions"][subscription] += events
            return dict(rec)
        return self._transact_accounting(_do)

    # -- deduplication accounting -----------------------------------------
    def record_dedup(self, account, provider, method, workload,
                     count=1):
        """Count ``count`` deduplicated requests (identical request
        already in flight / already answered — no provider call
        made). Saved cost = per-call METHOD_COSTS cost x count, or
        unknown when the method cost is UNVERIFIED. Returns the
        updated sidecar record for the account."""
        unit_cost = method_cost(provider, method)
        saved = unit_cost * count if unit_cost is not None else None

        def _do(data):
            dd = data.setdefault("dedup", {})
            rec = dd.setdefault(account, _default_sidecar_record())
            rec["requests"] += count
            if saved is None:
                rec["saved_unknown"] += count
            else:
                rec["saved_cost"] += saved
            return dict(rec)
        return self._transact_accounting(_do)

    # -- batch-request accounting ------------------------------------------
    def record_batch(self, account, provider, method, workload,
                     batch_size, batch_cost):
        """Account one batch request carrying ``batch_size`` items at
        ``batch_cost`` (provider units, as billed). Saved cost =
        batch_size x per-call METHOD_COSTS cost minus batch_cost, or
        unknown when the method cost is UNVERIFIED."""
        unit_cost = method_cost(provider, method)
        saved = (unit_cost * batch_size - batch_cost
                 if unit_cost is not None else None)

        def _do(data):
            bt = data.setdefault("batch", {})
            rec = bt.setdefault(account, _default_sidecar_record())
            rec["batch_calls"] += 1
            rec["batch_items"] += batch_size
            if saved is None:
                rec["saved_unknown"] += batch_size
            else:
                rec["saved_cost"] += saved
            return dict(rec)
        return self._transact_accounting(_do)

    # -- incremental-retrieval accounting ----------------------------------
    def record_incremental(self, account, provider, method, workload,
                           full_cost, incremental_cost):
        """Account one incremental retrieval: the full retrieval
        would have cost ``full_cost`` (provider units, METHOD_COSTS
        or measured); the incremental fetch cost
        ``incremental_cost``. Saved cost = full - incremental
        (floored at 0 — an incremental fetch that costs more than
        the full one saves nothing and is recorded as such)."""
        saved = max(0, full_cost - incremental_cost)

        def _do(data):
            inc = data.setdefault("incremental", {})
            rec = inc.setdefault(account, _default_sidecar_record())
            rec["requests"] += 1
            rec["full_cost"] += full_cost
            rec["incremental_cost"] += incremental_cost
            rec["saved_cost"] += saved
            return dict(rec)
        return self._transact_accounting(_do)

    # -- filtered-subscription accounting -----------------------------------
    def record_filtered_subscription(self, account, provider,
                                     subscription, events_received,
                                     events_filtered, bytes_received=None):
        """Account one filtered subscription window: ``events_received``
        arrived, ``events_filtered`` were dropped by the subscription
        filter before delivery. Saved bandwidth is counted only when
        ``bytes_received`` is reported; otherwise the filtered events
        are counted with bytes unmeasured."""
        def _do(data):
            fs = data.setdefault("filtered_subscriptions", {})
            rec = fs.setdefault(account, _default_sidecar_record())
            rec["events_received"] += events_received
            rec["events_filtered"] += events_filtered
            if bytes_received is None:
                rec["bytes_unmeasured_events"] += events_filtered
            else:
                rec["bytes_measured"] += bytes_received
            if subscription is not None:
                rec.setdefault("subscriptions", {}).setdefault(
                    subscription, 0)
                rec["subscriptions"][subscription] += events_received
            return dict(rec)
        return self._transact_accounting(_do)

    # -- cache savings (router's cache, from the journal) -----------------
    def cache_savings(self, home=None):
        """Saved cost from router cache hits, read from the request
        journal: a cached line spent 0 and avoided one call at the
        METHOD_COSTS cost of its method. Returns per-account
        {"cached_requests", "saved_cost", "saved_unknown"} — saved
        is unknown for methods whose cost is UNVERIFIED on that
        provider. The cache itself lives in the router; this only
        accounts for it."""
        journal = os.path.join(
            home or self.home, JOURNAL_FILE)
        out = {}
        try:
            with open(journal) as fh:
                lines = fh
                for raw in lines:
                    if not raw.strip():
                        continue
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if not entry.get("cached"):
                        continue
                    account = entry.get("account")
                    rec = out.setdefault(account, {
                        "cached_requests": 0, "saved_cost": 0,
                        "saved_unknown": 0})
                    rec["cached_requests"] += 1
                    provider = entry.get("provider")
                    cost = method_cost(provider, entry.get("method"))
                    if cost is None:
                        rec["saved_unknown"] += 1
                    else:
                        rec["saved_cost"] += cost
        except OSError:
            pass
        return out

    # -- savings roll-up ---------------------------------------------------
    def total_saved(self):
        """All accounted savings, per account, in that account's own
        unit (never summed across providers): cache + dedup + batch
        + incremental. WS bytes are reported separately (bytes are
        not provider cost units)."""
        acct = self.accounting()
        cache = self.cache_savings()
        accounts = set(cache) | set(acct.get("dedup", {})) \
            | set(acct.get("batch", {})) | set(acct.get("incremental", {}))
        out = {}
        for name in sorted(accounts):
            saved = 0
            unknown = 0
            for section in ("dedup", "batch", "incremental"):
                rec = acct.get(section, {}).get(name)
                if rec:
                    saved += rec.get("saved_cost", 0)
                    unknown += rec.get("saved_unknown", 0)
            c = cache.get(name)
            if c:
                saved += c.get("saved_cost", 0)
                unknown += c.get("saved_unknown", 0)
            out[name] = {"account": name,
                         "provider": provider_of(name),
                         "unit": cost_unit(provider_of(name) or ""),
                         "saved_cost": saved,
                         "saved_unknown_requests": unknown}
        return out
