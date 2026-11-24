"""Per-account provider usage store + budgets (Phases 3-4, §7).

The Helius usage manager (§7) data layer. Every request the router
serves is recorded here against the ACCOUNT that serviced it — never
against a provider family in aggregate, because accounts have
separate quotas and separate disablement:

- ``helius_main`` / ``helius_discovery`` / ``helius_vault`` are
  separate Helius key sources (capability audit §1). The two disk
  keys may belong to ONE Helius account — no second 1M-credit
  allowance is assumed. The router's Helius adapter records against
  ``helius_main``; the reserves transport rotates keys internally
  and per-key attribution is NOT visible from this layer (recorded
  as a caveat in the journal, never silently merged).
- ``alchemy`` (30M CU/month Free), ``chainstack`` (3M RU/month
  Developer), ``public`` (keyless, no metered quota).

Budgets come from ``provider_capabilities.py`` (quotas as config,
not assumptions). A budget with ``amount=None`` is UNENFORCED and
recorded as such — never assumed to be infinite, never assumed to
be zero.

Thresholds (module constants; override per account in
``BUDGET_OVERRIDES`` only with a recorded reason):
- ``WARN_PCT = 0.80`` — warn before exhaustion (once per month).
- ``STOP_NONESSENTIAL_PCT = 0.95`` stops nonessential requests.
- At known recorded monthly/daily/feature exhaustion, every priority stops.
  Threshold checks are not projected-cost or concurrent reservations.

Files (under FOREX_AGENT_HOME, atomic replace, flock-guarded):
- ``acquisition_provider_usage.json`` — per-account counters for the
  current calendar month (YYYY-MM). Counters reset when the month
  rolls over; the previous month's totals are kept in
  ``previous_month`` for one rollover (audit trail, not billing).
- ``acquisition_provider_usage.jsonl`` — one line per serviced
  request: ts, account, provider, method, workload, priority,
  helius_priority, cost, cost_unit, kind, elapsed_s, cached,
  fallback, fallback_difference. NEVER key material, NEVER URLs.

Compliance (§7): NEVER rotate accounts or distribute requests to
evade rate limits, quotas, billing, or terms. This store exists to
make that verifiable: per-account usage is tracked separately, an
account can be disabled individually, and the journal shows exactly
which account serviced each request. If an account is rate-limited,
the correct responses are backoff, quiet, and fallback — never a
new key.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time

from . import provider_capabilities as caps

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.provider_usage")

USAGE_FILE = "acquisition_provider_usage.json"
JOURNAL_FILE = "acquisition_provider_usage.jsonl"

WARN_PCT = 0.80
STOP_NONESSENTIAL_PCT = 0.95

# Per-account threshold overrides: {account: {"warn_pct": ...,
# "stop_nonessential_pct": ..., "daily_amount": ...}}.
# Empty by default; an override must carry a recorded reason.
BUDGET_OVERRIDES = {}

# Per-feature hard budgets (Phase 3): {(account, feature): amount}
# in the account's own unit, per calendar month. Empty by default —
# a feature budget must be set deliberately by the operator (there
# is no safe invented default: parsed-events at 10 credits/call and
# parsed-streams at unknown per-message cost cannot share one
# number). When set, nonessential traffic for that feature stops at
# STOP_NONESSENTIAL_PCT of the feature budget; every priority stops
# at recorded exhaustion (see usage_tracker.EMERGENCY_POLICY).
FEATURE_BUDGETS = {}


def set_feature_budget(account, feature, amount, reason=""):
    """Operator action: set a monthly hard budget for one feature.
    Returns the stored amount. Recorded in the module config (the
    operator's deployment config is the source of truth for budget
    numbers, as with BUDGET_OVERRIDES)."""
    FEATURE_BUDGETS[(account, feature)] = {
        "amount": amount, "reason": reason}
    return amount


def feature_budget_for(account, feature):
    """Monthly hard budget for one feature, or None when unset."""
    rec = FEATURE_BUDGETS.get((account, feature))
    return (rec or {}).get("amount")


def daily_budget_for(account):
    """Daily hard budget for one account, in its own unit.

    Operator-configured ONLY, via
    BUDGET_OVERRIDES[account]["daily_amount"]. The default is None
    (unenforced): there is no safe invented default — monthly/30
    would trip the daily stop on any legitimate single-day bulk
    spend (e.g. a backfill) and contradict the established policy
    that deployer traffic stays allowed at 80% of the monthly
    budget. When an operator sets a daily amount, the tracker stops
    nonessential traffic at 95% of it, exactly like the monthly
    stop; every priority stops at recorded exhaustion."""
    override = BUDGET_OVERRIDES.get(account, {})
    daily = override.get("daily_amount")
    if daily is not None:
        return daily
    return None

# Helius feature names for per-feature consumption (§7: "per-feature
# consumption"). Standard RPC reads are one feature; the specialised
# Helius products are tracked separately because they are the
# end-state Helius role and the expensive part of the account.
HELIUS_FEATURES = ("standard_rpc", "parsed_streams", "parsed_events",
                   "preprocessed_observation", "laserstream_wss")


def feature_for(provider, workload):
    """Feature name a workload consumes on a provider (for
    per-feature accounting). Non-Helius providers track the workload
    itself as the feature; Helius maps its specialised workloads."""
    if provider == "helius":
        if workload == "parsed_tx_stream":
            return "parsed_streams"
        if workload == "complex_tx_parse":
            return "parsed_events"
        if workload == "preprocessed_observation":
            return "preprocessed_observation"
        return "standard_rpc"
    return workload


def budget_for(account):
    """Budget for one account, from provider_capabilities.

    Returns {"account", "unit", "amount", "warn_at", "stop_at",
    "enforced", "note"}. ``enforced`` is False when the quota amount
    is None (UNVERIFIED / unmetered) — the tracker records usage
    but cannot enforce a limit it does not know."""
    rec = caps.get_account(account) or {}
    quota = rec.get("monthly_quota") or {}
    amount = quota.get("amount")
    unit = quota.get("unit")
    override = BUDGET_OVERRIDES.get(account, {})
    warn_pct = override.get("warn_pct", WARN_PCT)
    stop_pct = override.get("stop_nonessential_pct",
                            STOP_NONESSENTIAL_PCT)
    return {
        "account": account,
        "unit": unit,
        "amount": amount,
        "warn_at": (amount * warn_pct) if amount is not None else None,
        "stop_at": (amount * stop_pct) if amount is not None else None,
        "warn_pct": warn_pct,
        "stop_nonessential_pct": stop_pct,
        "enforced": amount is not None,
        "note": quota.get("note", ""),
    }


def _month_key(ts=None):
    return time.strftime("%Y-%m", time.gmtime(ts or time.time()))


def _default_account_record(account):
    rec = caps.get_account(account) or {}
    quota = rec.get("monthly_quota") or {}
    return {
        "account": account,
        "provider": rec.get("provider"),
        "unit": quota.get("unit"),
        "month": _month_key(),
        "calls": 0,
        "ok": 0,
        "errors": 0,
        "rate_limited": 0,
        "timeouts": 0,
        "cost_spent": 0,
        "day": time.strftime("%Y-%m-%d", time.gmtime()),
        "daily_calls": 0,
        "daily_cost_spent": 0,
        "cached_calls": 0,
        "by_feature": {},
        "by_method": {},
        "disabled": False,
        "disabled_reason": None,
        "warned_at": None,
        "first_seen_ts": None,
        "last_call_ts": None,
        "previous_month": None,
    }


class UsageStore:
    """Persistent per-account usage counters + request journal.

    All mutations go through a flock-guarded read-modify-write so
    several processes (fleet daemons) can record against one file
    without losing counts. A corrupt/empty file reseeds to empty
    counters (usage history is accounting, not financial state —
    the journal lines survive independently; a reseed is logged).
    """

    def __init__(self, home=None):
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self._path = os.path.join(self.home, USAGE_FILE)
        self._journal_path = os.path.join(self.home, JOURNAL_FILE)
        self._lock_path = os.path.join(self.home, USAGE_FILE + ".lock")

    # -- internals ------------------------------------------------------
    def _load(self, strict=False):
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("usage file is not an object")
            return data
        except FileNotFoundError:
            if strict and os.path.exists(self._journal_path):
                raise ValueError("usage counters absent with journal")
            return {}
        except (OSError, ValueError):
            if strict:
                raise
            return {}

    def _save(self, data):
        tmp = "%s.tmp.%d" % (self._path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)
        directory=os.open(os.path.dirname(self._path) or ".",os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)

    def _transact(self, fn, strict=False):
        """Read-modify-write under an exclusive flock. Returns
        fn(data)'s return value. On lock failure, degrades to an
        in-memory pass (the journal append below still records the
        request; the counters may lag — logged, never silent)."""
        try:
            lock_fh = open(self._lock_path, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                data = self._load(strict=strict)
                result = fn(data)
                self._save(data)
                if strict and self._load(strict=True) != data:
                    raise OSError("usage readback differs after save")
                return result
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
        except OSError:
            if strict:
                raise
            logger.warning("usage store lock unavailable; counters "
                           "not updated for this call")
            return None

    # -- recording ------------------------------------------------------
    def record(self, account, provider, method, workload, priority,
               helius_priority=None, cost=None, kind="ok", elapsed=0.0,
               cached=False, fallback=False, fallback_difference=None,
               ts=None, strict=False):
        """Record one serviced request. Returns the updated account
        record (or None when the store was unavailable). The journal
        line is appended even when the counters cannot be updated.

        ``cost`` is the per-call cost in the account's own unit
        (credits/CU/RU) or None when UNVERIFIED/uncached — an
        unknown cost is recorded as unknown (cost_spent unchanged),
        never assumed to be 0 or 1.
        """
        ts = ts if ts is not None else time.time()
        month = _month_key(ts)
        entry = {
            "ts": ts,
            "account": account,
            "provider": provider,
            "method": method,
            "workload": workload,
            "priority": priority,
            "helius_priority": helius_priority,
            "cost": cost,
            "kind": kind,
            "elapsed_s": round(elapsed, 4),
            "cached": bool(cached),
            "fallback": bool(fallback),
            "fallback_difference": fallback_difference,
        }
        if account == "helius_main":
            entry["account_note"] = (
                "reserves.rpc rotates Helius key sources internally; "
                "per-key attribution not visible from this layer")
        def _append_journal():
            with open(self._journal_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
                if strict:
                    fh.flush()
                    os.fsync(fh.fileno())
            if strict:
                directory=os.open(os.path.dirname(self._journal_path) or ".",os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)

        if not strict:
            try:_append_journal()
            except OSError:logger.warning("could not append usage journal")

        def _do(data):
            if strict:
                # Validate BEFORE appending: a fresh store has no counters
                # and no journal. Existing corruption/loss must never reseed.
                self.validate_admission_state(account)
                _append_journal()
            rec = data.get(account)
            if rec is None:
                rec = _default_account_record(account)
                data[account] = rec
            if rec.get("month") != month:
                prev = {k: rec.get(k) for k in (
                    "month", "calls", "ok", "errors", "rate_limited",
                    "timeouts", "cost_spent", "cached_calls")}
                rec["previous_month"] = prev
                for k in ("calls", "ok", "errors", "rate_limited",
                          "timeouts", "cost_spent", "cached_calls"):
                    rec[k] = 0
                rec["by_feature"] = {}
                rec["by_method"] = {}
                rec["warned_at"] = None
                rec["month"] = month
            # Daily rollover (Phase 3): daily counters reset on the
            # UTC calendar day; the monthly counters above are
            # untouched. Records written before Phase 3 have no
            # "day" key — they start a fresh day, never a crash.
            day = time.strftime("%Y-%m-%d", time.gmtime(ts))
            if rec.get("day") != day:
                rec["day"] = day
                rec["daily_calls"] = 0
                rec["daily_cost_spent"] = 0
            rec["calls"] += 1
            rec["daily_calls"] = rec.get("daily_calls", 0) + 1
            if kind == "ok":
                rec["ok"] += 1
            elif kind == "rate_limited":
                rec["rate_limited"] += 1
            elif kind == "timeout":
                rec["timeouts"] += 1
            else:
                rec["errors"] += 1
            if cached:
                rec["cached_calls"] += 1
            if cost is not None and not cached:
                rec["cost_spent"] += cost
                rec["daily_cost_spent"] = \
                    rec.get("daily_cost_spent", 0) + cost
            feature = feature_for(provider, workload)
            feat = rec["by_feature"].setdefault(
                feature, {"calls": 0, "cost": 0})
            feat["calls"] += 1
            if cost is not None and not cached:
                feat["cost"] += cost
            meth = rec["by_method"].setdefault(
                method, {"calls": 0, "cost": 0})
            meth["calls"] += 1
            if cost is not None and not cached:
                meth["cost"] += cost
            if rec.get("first_seen_ts") is None:
                rec["first_seen_ts"] = ts
            rec["last_call_ts"] = ts
            return dict(rec)

        return self._transact(_do, strict=strict)

    # -- reads ----------------------------------------------------------
    def validate_admission_state(self, account):
        """Strict integrity read for admission, separate from fail-soft reports.

        An absent brand-new store is allowed. Existing journal with lost
        counters, unreadable/corrupt counters or invalid numeric evidence is
        unavailable, never interpreted as a fresh zero budget. This does not
        make read/check/write atomic or reconstruct external billed spend.
        """
        import math
        try:
            with open(self._path) as fh:
                data=json.load(fh)
        except FileNotFoundError:
            if os.path.exists(self._journal_path):
                raise ValueError('usage counters missing with retained journal')
            return
        if not isinstance(data,dict):raise ValueError('usage root invalid')
        if account not in data:
            # Distinguish another account's legitimate history from loss of
            # this account. This legacy audit scan is bounded; beyond the
            # bound admission remains unavailable, never guessed empty.
            try:
                with open(self._journal_path) as journal:
                    for index,line in enumerate(journal):
                        if index>=10000:raise ValueError('usage admission journal audit limit')
                        entry=json.loads(line)
                        if not isinstance(entry,dict) or not isinstance(entry.get('account'),str) or not entry['account'].strip():raise ValueError('usage journal attribution unavailable')
                        if entry.get('account')==account:raise ValueError('usage account counters missing')
            except FileNotFoundError:pass
            return
        rec=data[account]
        if not isinstance(rec,dict):raise ValueError('usage account invalid')
        for key in ('cost_spent','daily_cost_spent','calls','daily_calls'):
            value=rec.get(key)
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:
                raise ValueError('usage numeric evidence invalid')
        if type(rec.get('disabled')) is not bool:raise ValueError('usage disabled evidence invalid')
        import datetime
        for key,fmt in (('month','%Y-%m'),('day','%Y-%m-%d')):
            if not isinstance(rec.get(key),str):raise ValueError('usage window invalid')
            if datetime.datetime.strptime(rec[key],fmt).strftime(fmt)!=rec[key]:raise ValueError('usage window invalid')
        features=rec.get('by_feature')
        if not isinstance(features,dict):raise ValueError('usage features invalid')
        for feature,row in features.items():
            if not isinstance(feature,str) or not isinstance(row,dict):raise ValueError('usage feature invalid')
            for key in ('cost','calls'):
                value=row.get(key)
                if type(value) not in (int,float) or not math.isfinite(value) or value<0:
                    raise ValueError('usage feature evidence invalid')
        if not math.isclose(sum(row['cost'] for row in features.values()),rec['cost_spent'],rel_tol=1e-12,abs_tol=1e-9):
            raise ValueError('usage feature cost conservation failed')
        if sum(row['calls'] for row in features.values())!=rec['calls']:
            raise ValueError('usage feature call conservation failed')

    def get(self, account):
        data = self._load()
        rec = data.get(account)
        if rec is None:
            return _default_account_record(account)
        if rec.get("month") != _month_key():
            # Stale month on read: report the stored record but with
            # the rollover visible (counters are for the old month).
            rec = dict(rec)
            rec["stale_month"] = True
        return rec

    def all(self):
        data = self._load()
        return {name: self.get(name) for name in data}

    def summary(self):
        """Per-account budget status: spent, amount, pct, enforced,
        warned, disabled. pct is None when the budget is unenforced."""
        out = {}
        for account in caps.ACCOUNTS:
            rec = self.get(account)
            budget = budget_for(account)
            amount = budget["amount"]
            spent = rec.get("cost_spent", 0)
            pct = (spent / amount) if amount else None
            out[account] = {
                "account": account,
                "provider": rec.get("provider"),
                "unit": budget["unit"],
                "month": rec.get("month"),
                "calls": rec.get("calls", 0),
                "ok": rec.get("ok", 0),
                "errors": rec.get("errors", 0),
                "rate_limited": rec.get("rate_limited", 0),
                "cost_spent": spent,
                "budget_amount": amount,
                "budget_pct": (round(pct, 4) if pct is not None else None),
                "budget_enforced": budget["enforced"],
                "daily_cost_spent": rec.get("daily_cost_spent", 0),
                "daily_budget": daily_budget_for(account),
                "daily_calls": rec.get("daily_calls", 0),
                "warned": rec.get("warned_at") is not None,
                "disabled": bool(rec.get("disabled")),
                "disabled_reason": rec.get("disabled_reason"),
                "by_feature": rec.get("by_feature", {}),
            }
        return out

    # -- disablement (operator action) ----------------------------------
    def set_disabled(self, account, disabled, reason=None):
        def _do(data):
            rec = data.get(account)
            if rec is None:
                rec = _default_account_record(account)
                data[account] = rec
            rec["disabled"] = bool(disabled)
            rec["disabled_reason"] = reason if disabled else None
            return dict(rec)
        return self._transact(_do)

    def mark_warned(self, account, ts=None):
        def _do(data):
            rec = data.get(account)
            if rec is None:
                rec = _default_account_record(account)
                data[account] = rec
            rec["warned_at"] = ts if ts is not None else time.time()
            return dict(rec)
        return self._transact(_do)
