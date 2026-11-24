"""Provider management (Phases 3-4).

Owns the provider adapters, per-provider rolling stats, and the
health/usage wiring. SELECTION lives in ``provider_router.py`` —
this module delegates every routing decision to the router and
keeps NO hardcoded routing of its own (``failover_order`` below is
the router's function, re-exported for compatibility; the ORDER_*
constants are views of the router's rules, not a second copy).

Failover order per workload (shadow phase — Helius is live):
  default: helius -> alchemy -> public   (chainstack only for the
    recent-read subset; it never receives getSignaturesForAddress)
  safety screening: helius -> alchemy -> public
  recent reads (getSlot/getBalance/getAccountInfo/...): helius ->
    alchemy -> chainstack -> public
The scheduler paces every non-Helius attempt; Helius attempts go
through the existing reserves transport (which paces itself).

Nothing here sends a shadow call: shadow comparison is shadow.py's
job, and it calls providers directly so the live path is untouched.
"""

from __future__ import annotations

import logging
import time

from . import rpc_client as rpc_mod
from .provider_router import (
    ORDER_DEFAULT,
    ORDER_RECENT,
    failover_order,
)
from .provider_router import ProviderRouter
from .request_scheduler import (
    PRIORITY_BACKGROUND, PRIORITY_CARD, PRIORITY_CRITICAL,
    PRIORITY_DEPLOYER)

logger = logging.getLogger("forex_agent.daemon.intelligence.acquisition.provider_manager")

# Methods Chainstack can serve (recent-read subset only). Everything
# else on Chainstack is either archive (getSignaturesForAddress) or
# paid-only (getProgramAccounts) or conflicted (getTokenLargestAccounts).
# (Kept as a documented view of the capability rules; the router's
# capability matching in provider_capabilities.py is authoritative.)
CHAINSTACK_METHODS = {
    "getSlot", "getBalance", "getAccountInfo", "getMultipleAccounts",
    "getTokenAccountBalance", "getTransaction", "sendTransaction",
    "getSignatureStatuses", "getLatestBlockhash",
    "getRecentPrioritizationFees", "simulateTransaction", "getHealth",
}

__all__ = [
    "CHAINSTACK_METHODS",
    "ORDER_DEFAULT",
    "ORDER_RECENT",
    "failover_order",
    "ProviderStats",
    "ProviderManager",
    "PRIORITY_BACKGROUND",
    "PRIORITY_CARD",
    "PRIORITY_CRITICAL",
    "PRIORITY_DEPLOYER",
]


class ProviderStats:
    """Per-provider rolling stats: latency EMA, error rate, 429s."""

    def __init__(self):
        self.calls = 0
        self.errors = 0
        self.rate_limited = 0
        self.timeouts = 0
        self.latency_ema = None
        self.last_success = None
        self.last_error = None

    def record(self, result):
        self.calls += 1
        elapsed = result.get("elapsed", 0.0) or 0.0
        if self.latency_ema is None:
            self.latency_ema = elapsed
        else:
            self.latency_ema = 0.8 * self.latency_ema + 0.2 * elapsed
        kind = result.get("kind")
        if kind == "ok":
            self.last_success = time.time()
        else:
            self.errors += 1
            self.last_error = time.time()
            if kind == "rate_limited":
                self.rate_limited += 1
            elif kind == "timeout":
                self.timeouts += 1

    @property
    def error_rate(self):
        return (self.errors / self.calls) if self.calls else 0.0

    def as_dict(self):
        return {"calls": self.calls, "errors": self.errors,
                "rate_limited": self.rate_limited,
                "timeouts": self.timeouts,
                "error_rate": round(self.error_rate, 4),
                "latency_ema_s": (round(self.latency_ema, 3)
                                  if self.latency_ema is not None else None),
                "last_success": self.last_success,
                "last_error": self.last_error}


class ProviderManager:
    """Provider lifecycle + stats. All selection delegates to the
    central router (``provider_router.ProviderRouter``)."""

    def __init__(self, home=None, helius_enabled=True, transport=None,
                 router=None):
        self.home = home
        self.helius_enabled = helius_enabled
        self._transport = transport
        self.stats = {p: ProviderStats() for p in rpc_mod.PROVIDERS}
        self._adapters = {}
        self.router = router or ProviderRouter(
            home=home, helius_enabled=helius_enabled,
            transport=transport)

    def _adapter(self, provider):
        if provider not in self._adapters:
            self._adapters[provider] = rpc_mod.make_adapter(
                provider, home=self.home, transport=self._transport)
        return self._adapters[provider]

    def configured(self, provider):
        try:
            return self._adapter(provider).configured()
        except Exception:
            return False

    def select(self, method, priority=PRIORITY_CARD):
        """(primary, fallbacks) — usable providers for the method, in
        the router's order, skipping providers that cannot serve it
        (capability), are unconfigured, disabled, circuit-open, or
        budget-stopped for this priority. Empty list means no
        provider can serve the call right now (callers fail closed)."""
        primary, fallbacks = self.router.select(method, priority)
        order = ([primary] + fallbacks) if primary else []
        usable = []
        for provider in order:
            adapter = self._adapter(provider)
            if not adapter.supports(method):
                continue
            if not adapter.configured():
                continue
            account = rpc_mod.PROVIDER_ACCOUNTS.get(provider, provider)
            if self.router.usage.account_disabled(account):
                continue
            if not self.router.breaker.allow(provider):
                continue
            allowed, _reason = self.router.usage.nonessential_allowed(
                account, priority)
            if not allowed:
                continue
            usable.append(provider)
        return (usable[0], usable[1:]) if usable else (None, [])

    def call(self, method, params, timeout=12, priority=PRIORITY_CARD):
        """One logical call with routing + failover, via the router.
        Returns the first RpcResult with kind == "ok", else the last
        attempt's result (callers fail closed on non-ok). Per-provider
        stats are recorded from the router's attempts list."""
        result = self.router.call(method, params, timeout=timeout,
                                  priority=priority)
        for attempt in result.get("attempts", []):
            provider = attempt.get("provider")
            kind = attempt.get("kind")
            if provider not in self.stats or kind in (None, "skipped"):
                continue
            record_kind = "ok" if kind.startswith("ok") else kind
            self.stats[provider].record({
                "kind": record_kind,
                "elapsed": attempt.get("elapsed_s", 0.0) or 0.0})
        return result

    def stats_dict(self):
        return {p: s.as_dict() for p, s in self.stats.items()}
