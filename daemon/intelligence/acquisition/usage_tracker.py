"""Helius usage manager — runtime policy (Phases 3-4, §7).

Enforces the §7 usage rules on top of the ``provider_usage``
store:

- Per-account usage: every serviced request is recorded against
  the account that serviced it (``provider_usage.UsageStore``).
- Explicit budgets: from ``provider_capabilities`` (quotas as
  config). An account with no known quota amount is UNENFORCED —
  usage is recorded, no limit is invented.
- Warn before exhaustion: at ``WARN_PCT`` (80%) of the monthly
  budget the tracker emits ONE warning per month (``warned_at``)
  and reports it on every subsequent budget check until the month
  rolls over.
- Stop nonessential requests at threshold: at
  ``STOP_NONESSENTIAL_PCT`` (95%) NONESSENTIAL priorities
  (deployer + background — research, batch scans, dossiers) are
  refused with reason ``budget_stop_nonessential``. ESSENTIAL
  priorities (critical entry/exit + card safety) are NEVER stopped
  by this manager: silently dropping an entry-critical read would
  change trading behaviour, and exhausting an essential budget is
  an operator decision (disable the account, or accept the spend),
  not an automatic drop.
- Per-feature consumption: recorded per workload/feature in the
  store (``standard_rpc`` vs Helius ``parsed_streams`` /
  ``parsed_events`` / ``laserstream_wss``).
- Account-level disablement: a disabled account is never routed
  to (the router checks ``account_disabled``). Disablement is an
  operator action (``disable_account``) or a monthly-quota 429
  (``record_monthly_quota``) — never automatic on a throughput 429.
- Record which account serviced each request: the journal line in
  ``provider_usage`` carries account + provider + method +
  workload + cost + kind for every request.

Compliance (§7): NEVER rotate accounts or distribute requests to
evade rate limits, quotas, billing, or terms. This tracker enforces
the opposite direction: one account per provider, usage tracked
separately, and a rate-limited account is backed off / quieted /
failed over — never replaced by a fresh key.
"""

from __future__ import annotations

import logging
import time

from . import provider_capabilities as caps
from .provider_usage import (
    STOP_NONESSENTIAL_PCT,
    WARN_PCT,
    UsageStore,
    budget_for,
    daily_budget_for,
    feature_budget_for,
    feature_for,
)
from .request_scheduler import (
    PRIORITY_BACKGROUND,
    PRIORITY_CARD,
    PRIORITY_CRITICAL,
    PRIORITY_DEPLOYER,
)

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.usage_tracker")

# Priorities that may be stopped at the budget threshold. CRITICAL
# (entry/exit) and CARD (safety screening) are essential and are
# never stopped here.
NONESSENTIAL_PRIORITIES = (PRIORITY_DEPLOYER, PRIORITY_BACKGROUND)
ESSENTIAL_PRIORITIES = (PRIORITY_CRITICAL, PRIORITY_CARD)

# Emergency policy (Phase 3, explicit): when a budget is exhausted,
# NONESSENTIAL traffic stops and ESSENTIAL traffic CONTINUES — an
# entry/exit-critical read or a safety screen is never silently
# dropped by this manager. The over-budget essential spend is
# visible in budget_status() (pct > 1.0, essential_over_budget True)
# and is an OPERATOR decision: disable the account, accept the
# spend, or move the workload. Essential traffic past 100% never
# auto-disables an account and never silently degrades to a cheaper
# provider (a substitute would change the meaning of the read).
EMERGENCY_POLICY = (
    "budgets stop NONESSENTIAL traffic at 95% (monthly, daily, or "
    "per-feature). ESSENTIAL traffic (critical/card) is never "
    "stopped by this manager; over-budget essential spend is "
    "reported (pct > 1.0) for an operator decision — disable the "
    "account or accept the spend. No silent provider substitution.")


class UsageTracker:
    """Runtime usage policy around a UsageStore."""

    def __init__(self, home=None, store=None):
        self.home = home
        self.store = store or UsageStore(home=home)

    # -- recording ------------------------------------------------------
    def record(self, account, provider, method, workload, priority,
               helius_priority=None, cost=None, kind="ok", elapsed=0.0,
               cached=False, fallback=False, fallback_difference=None):
        """Record one serviced request; returns the updated account
        record (or None when the store was unavailable). Also checks
        the warn threshold and marks the warning once per month."""
        rec = self.store.record(
            account=account, provider=provider, method=method,
            workload=workload, priority=priority,
            helius_priority=helius_priority, cost=cost, kind=kind,
            elapsed=elapsed, cached=cached, fallback=fallback,
            fallback_difference=fallback_difference)
        if rec is not None:
            self._check_warn(account, rec)
        return rec

    def _check_warn(self, account, rec):
        """Emit the once-per-month exhaustion warning at WARN_PCT."""
        budget = budget_for(account)
        amount = budget["amount"]
        if amount is None or rec.get("warned_at") is not None:
            return None
        spent = rec.get("cost_spent", 0)
        if spent >= amount * budget["warn_pct"]:
            self.store.mark_warned(account)
            logger.warning(
                "provider usage: account %s at %.1f%% of monthly "
                "budget (%s/%s %s); nonessential traffic stops at "
                "%.0f%%", account, 100.0 * spent / amount, spent,
                amount, budget["unit"],
                100.0 * budget["stop_nonessential_pct"])
            return {"account": account, "spent": spent,
                    "amount": amount, "pct": spent / amount,
                    "unit": budget["unit"]}
        return None

    # -- budget checks --------------------------------------------------
    def budget_status(self, account):
        """{"spent", "amount", "unit", "pct", "enforced", "warn",
        "stop_nonessential", "disabled", "daily_*",
        "essential_over_budget"} for one account."""
        rec = self.store.get(account)
        budget = budget_for(account)
        amount = budget["amount"]
        spent = rec.get("cost_spent", 0)
        pct = (spent / amount) if amount else None
        daily_amount = daily_budget_for(account)
        daily_spent = rec.get("daily_cost_spent", 0)
        daily_pct = ((daily_spent / daily_amount)
                     if daily_amount else None)
        return {
            "account": account,
            "spent": spent,
            "amount": amount,
            "unit": budget["unit"],
            "pct": (round(pct, 4) if pct is not None else None),
            "enforced": budget["enforced"],
            "warn": bool(rec.get("warned_at") is not None),
            "warn_threshold_pct": budget["warn_pct"],
            "stop_nonessential": bool(
                amount is not None
                and spent >= amount * budget["stop_nonessential_pct"]),
            "stop_nonessential_pct": budget["stop_nonessential_pct"],
            "daily_spent": daily_spent,
            "daily_amount": daily_amount,
            "daily_pct": (round(daily_pct, 4)
                          if daily_pct is not None else None),
            "daily_stop_nonessential": bool(
                daily_amount is not None and daily_spent >=
                daily_amount * budget["stop_nonessential_pct"]),
            "essential_over_budget": bool(
                amount is not None and spent > amount),
            "disabled": bool(rec.get("disabled")),
            "disabled_reason": rec.get("disabled_reason"),
        }

    def feature_status(self, account, feature):
        """Budget status for one feature: {"spent", "amount", "pct",
        "stop_nonessential", "enforced"}. Unset budget -> enforced
        False, pct None (recorded, never stopped)."""
        amount = feature_budget_for(account, feature)
        spent = self.per_feature(account).get(feature, {}).get("cost", 0)
        pct = (spent / amount) if amount else None
        return {
            "account": account, "feature": feature,
            "spent": spent, "amount": amount,
            "pct": (round(pct, 4) if pct is not None else None),
            "enforced": amount is not None,
            "stop_nonessential": bool(
                amount is not None and spent >=
                amount * STOP_NONESSENTIAL_PCT),
            "stop_nonessential_pct": STOP_NONESSENTIAL_PCT,
        }

    def nonessential_allowed(self, account, priority, workload=None,
                             provider=None):
        """False when a NONESSENTIAL priority must be stopped: the
        account is disabled, or its MONTHLY, DAILY, or per-FEATURE
        spend passed the stop threshold. Essential priorities are
        always allowed here (see module docstring + EMERGENCY_POLICY).
        Returns (allowed, reason_or_None). Reasons:
        account_disabled | budget_stop_nonessential |
        daily_budget_stop_nonessential |
        feature_budget_stop_nonessential:<feature>."""
        if self.account_disabled(account):
            return False, "account_disabled"
        if priority not in NONESSENTIAL_PRIORITIES:
            return True, None
        status = self.budget_status(account)
        if status["stop_nonessential"]:
            return False, "budget_stop_nonessential"
        if status["daily_stop_nonessential"]:
            return False, "daily_budget_stop_nonessential"
        if workload is not None:
            feature = feature_for(provider or "helius", workload)
            fstatus = self.feature_status(account, feature)
            if fstatus["stop_nonessential"]:
                return False, ("feature_budget_stop_nonessential:%s"
                               % feature)
        return True, None

    def warn_pending(self, account):
        """Warning dict when the account crossed WARN_PCT this month
        (whether or not the once-per-month log already fired), else
        None. Callers surface this in health/status output."""
        status = self.budget_status(account)
        if status["amount"] is None or status["pct"] is None:
            return None
        if status["pct"] >= status["warn_threshold_pct"]:
            return {"account": account, "pct": status["pct"],
                    "spent": status["spent"],
                    "amount": status["amount"],
                    "unit": status["unit"],
                    "stop_nonessential_pct":
                        status["stop_nonessential_pct"]}
        return None

    # -- disablement ----------------------------------------------------
    def account_disabled(self, account):
        """True when the account is disabled in the usage store OR
        disabled in provider_capabilities (``enabled: False``)."""
        rec = caps.get_account(account)
        if rec is not None and not rec.get("enabled", True):
            return True
        stored = self.store.get(account)
        return bool(stored.get("disabled"))

    def disable_account(self, account, reason):
        """Operator action: disable one account. The router will
        never route to it afterwards. Returns the updated record."""
        logger.warning("provider usage: disabling account %s (%s)",
                       account, reason)
        return self.store.set_disabled(account, True, reason=reason)

    def enable_account(self, account):
        """Operator action: re-enable one account."""
        return self.store.set_disabled(account, False)

    def record_monthly_quota(self, account):
        """A monthly-quota 429 was observed on this account: disable
        it for the workload until the allowance renews (operator
        re-enables). Throughput 429s NEVER disable an account."""
        return self.disable_account(
            account, "monthly-quota 429 observed; allowance renews "
                     "next month (operator re-enable)")

    # -- reporting ------------------------------------------------------
    def summary(self):
        """Per-account budget status for every known account."""
        return self.store.summary()

    def per_feature(self, account):
        """Per-feature consumption for one account (calls + cost)."""
        return self.store.get(account).get("by_feature", {})
