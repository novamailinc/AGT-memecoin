"""Central provider router (Phase 4).

THE single routing layer. ALL provider selection passes through
this module — no other module in the codebase makes a routing
decision. ``provider_manager`` delegates to it; the shadow harness
(``shadow.py``) addresses providers explicitly BY DESIGN (it compares
named providers — that is verification, not selection); probes and
benchmarks likewise address providers explicitly.

Routing is decided from three inputs, in this order:
1. WORKLOAD CLASSIFICATION — what kind of work is this?
   (``classify_workload``; the caller may declare a workload in
   ``hints`` — a declaration that contradicts the method is refused,
   never silently reinterpreted.)
2. CAPABILITY MATCHING — which providers CAN serve this method on
   their free tier? (``provider_capabilities.py``: free-tier
   refusals, UNVERIFIED methods. A provider that cannot perform the
   method is never sent the request.)
3. LIVE STATE — configured? disabled (usage/capabilities)? in
   scheduler backoff? circuit breaker open? over budget for
   nonessential traffic?

Routing rules (§8 + the validated end-state allocation):
- routine account lookup        -> Alchemy
- routine transaction retrieval -> Alchemy
- historical transaction range  -> Chainstack where its retention
  window covers it, else Alchemy (archive)
- missed event recovery         -> Chainstack (within window) else
  Alchemy
- independent verification      -> Chainstack (recent reads only)
- selected complex tx parsing   -> Helius (Parsed Events) ONLY;
  no fallback — a substitute would change the meaning
- high-priority parsed tx stream -> Helius (Parsed Streams /
  LaserStream WSS standard) ONLY
- preprocessed early observation -> REFUSED, always. Helius
  Preprocessed Transactions is PAID-gated on Free (paid plans
  only, 0.1 credits/message). Recorded as disabled; never enabled.

SHADOW PHASE: Helius is the live leg TODAY and stays live until
benchmarks prove the new allocation per workload. While
``helius_enabled`` is True, Helius leads the candidate order for
P0–P2 work and is the last resort for P3 work. When False, Helius
is absent from every order (end state).

Helius P0–P3 prioritization (§7):
- P0: preprocessed observations (REFUSED — paid-gated) + high-value
  time-sensitive monitoring (parsed stream, critical).
- P1: Parsed Streams for selected programs + important DEX
  transactions; entry/exit-critical routine reads.
- P2: complex interpretation + high-value wallet investigations +
  conflicting-decoder verification; card/deployer routine reads;
  historical reads Alchemy cannot cover alone.
- P3: historical investigation Chainstack can handle, routine
  metadata, ordinary balances, repeated cached requests — normally
  routed ELSEWHERE; Helius is only the last resort.

Fallback discipline: a fallback NEVER silently changes the meaning
or completeness of the requested data. When the serving provider is
not the primary candidate, the returned result carries
``fallback_difference`` naming exactly what differs (e.g. "public
tertiary fallback: recent-only history, no SLA" or "Chainstack
served instead of Helius: RECENT reads only"). A workload whose
fallback would change the meaning (complex parsing, parsed
streams, preprocessed) is REFUSED instead of substituted.

Also enforced here: priority scheduling (via the shared-quota
scheduler), request quotas, provider-specific rate limits, usage
accounting (via the usage tracker — every serviced request is
recorded against the account that serviced it), response caching
(idempotent reads only, per-provider, short TTLs), circuit
breakers (consecutive errors/timeouts; 429s are the scheduler's
job, not the breaker's), retry-via-failover (a retry goes to the
NEXT provider, never the same one), timeouts per workload, and
latency measurement (every attempt records elapsed into health +
usage + the returned attempts list).
"""

from __future__ import annotations

import json
import logging
import time

from . import rpc_client as rpc_mod
from .provider_health import HealthMonitor
from .request_scheduler import (
    PRIORITY_BACKGROUND,
    PRIORITY_CARD,
    PRIORITY_CRITICAL,
    PRIORITY_DEPLOYER,
    SharedQuota,
)
from .usage_tracker import UsageTracker

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.provider_router")

# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------
ROUTINE_ACCOUNT = "routine_account"
ROUTINE_TX = "routine_tx"
HISTORICAL_RANGE = "historical_range"
MISSED_EVENT_RECOVERY = "missed_event_recovery"
INDEPENDENT_VERIFICATION = "independent_verification"
COMPLEX_TX_PARSE = "complex_tx_parse"
PARSED_TX_STREAM = "parsed_tx_stream"
PREPROCESSED_OBSERVATION = "preprocessed_observation"

WORKLOADS = (
    ROUTINE_ACCOUNT, ROUTINE_TX, HISTORICAL_RANGE,
    MISSED_EVENT_RECOVERY, INDEPENDENT_VERIFICATION,
    COMPLEX_TX_PARSE, PARSED_TX_STREAM, PREPROCESSED_OBSERVATION,
)

# Stream/specialised workloads are not JSON-RPC methods.
SPECIALISED_WORKLOADS = (COMPLEX_TX_PARSE, PARSED_TX_STREAM,
                         PREPROCESSED_OBSERVATION)

# Method -> default workload (the caller may override with a
# hints["workload"] declaration; a declaration that contradicts the
# method is refused).
METHOD_WORKLOADS = {
    "getAccountInfo": ROUTINE_ACCOUNT,
    "getBalance": ROUTINE_ACCOUNT,
    "getMultipleAccounts": ROUTINE_ACCOUNT,
    "getTokenAccountBalance": ROUTINE_ACCOUNT,
    "getTokenAccountsByOwner": ROUTINE_ACCOUNT,
    "getTokenSupply": ROUTINE_ACCOUNT,
    "getTokenLargestAccounts": ROUTINE_ACCOUNT,
    "getProgramAccounts": ROUTINE_ACCOUNT,
    "getSlot": ROUTINE_ACCOUNT,
    "getHealth": ROUTINE_ACCOUNT,
    "getLatestBlockhash": ROUTINE_ACCOUNT,
    "getRecentPrioritizationFees": ROUTINE_ACCOUNT,
    "getSignatureStatuses": ROUTINE_ACCOUNT,
    "getTransaction": ROUTINE_TX,
    "getBlock": ROUTINE_TX,
    "sendTransaction": ROUTINE_TX,
    "simulateTransaction": ROUTINE_TX,
    "getSignaturesForAddress": HISTORICAL_RANGE,
}

# Per-workload default timeout (seconds) when the caller passes none.
WORKLOAD_TIMEOUTS = {
    ROUTINE_ACCOUNT: 12,
    ROUTINE_TX: 12,
    HISTORICAL_RANGE: 15,
    MISSED_EVENT_RECOVERY: 15,
    INDEPENDENT_VERIFICATION: 12,
    COMPLEX_TX_PARSE: 20,
    PARSED_TX_STREAM: 12,
    PREPROCESSED_OBSERVATION: 12,
}

# Routing modes (Phase 3). The mode is the ONE switch that decides
# which allocation the router enforces — there is no second copy of
# the rules anywhere else.
#   "shadow"    — Helius is the live leg and leads P0–P2 work (the
#                 mode this program runs in; Alchemy/Chainstack
#                 carry NO live workload).
#   "end_state" — Alchemy-first (routine reads, signature history,
#                 routine recovery), Chainstack for recent reads +
#                 independent verification, Helius ONLY for its
#                 specialised products (Parsed Streams / Parsed
#                 Events). Built and unit-tested in Phase 3; the
#                 shadow program owns any cutover, workload by
#                 workload, on evidence.
ROUTING_MODE_SHADOW = "shadow"
ROUTING_MODE_END_STATE = "end_state"
ROUTING_MODES = (ROUTING_MODE_SHADOW, ROUTING_MODE_END_STATE)

# Helius priorities.
P0 = "P0"
P1 = "P1"
P2 = "P2"
P3 = "P3"

# Response-cache TTLs (seconds) for idempotent reads. Only "ok"
# results are cached, per provider (providers may legitimately
# differ — e.g. Chainstack recent-only vs Alchemy archive — so a
# cached answer never crosses providers). getSlot / getHealth /
# send / simulate / getSignaturesForAddress / getProgramAccounts are
# NEVER cached (live, mutating, or range reads).
CACHE_TTL_S = {
    "getTransaction": 30.0,
    "getAccountInfo": 10.0,
    "getMultipleAccounts": 10.0,
    "getBlock": 60.0,
    "getTokenAccountBalance": 10.0,
    "getBalance": 5.0,
}

# Circuit breaker: consecutive error/timeout failures before the
# provider is skipped, and the cooldown before a half-open probe.
BREAKER_FAILURE_THRESHOLD = 5
BREAKER_COOLDOWN_S = 60.0

PREPROCESSED_REFUSAL = (
    "PAID-GATED: Helius Preprocessed Transactions "
    "(preprocessedSubscribe) is available on Helius paid plans only "
    "(0.1 credits/message) — NOT on Free. Recorded as disabled in "
    "the free-only program; never enabled without an explicit paid "
    "decision by the operator.")

COMPLEX_PARSE_REFUSAL = (
    "Helius Parsed Events unavailable (Helius disabled or "
    "unconfigured). No fallback: substituting a standard-RPC read "
    "would change the meaning of the requested parsed result.")

PARSED_STREAM_REFUSAL = (
    "Helius Parsed Streams / LaserStream unavailable (Helius "
    "disabled or unconfigured). No fallback: no other provider "
    "offers the parsed-stream product.")


# ---------------------------------------------------------------------------
# Workload classification + Helius priority
# ---------------------------------------------------------------------------
def classify_workload(method, params=None, hints=None,
                      priority=PRIORITY_CARD):
    """(workload, helius_priority, error).

    ``hints`` may carry: workload (explicit declaration),
    historical (bool — this read targets old data), recent (bool —
    caller asserts the target is inside Chainstack's retention
    window), target_slot / latest_slot (for the retention-window
    check), high_value (bool — high-value time-sensitive monitoring),
    wallet_investigation / conflicting (bool — P2 Helius work).

    Returns error (a string) instead of a workload when the method
    is unknown or the declared workload contradicts the method.
    """
    hints = hints or {}
    declared = hints.get("workload")
    if declared is not None and declared not in WORKLOADS:
        return None, None, "unknown workload %r" % (declared,)
    if declared in (PARSED_TX_STREAM, PREPROCESSED_OBSERVATION):
        # Stream / specialised workloads are not RPC methods; they
        # are routed via route_stream(). A call() with one is a
        # caller error.
        return None, None, (
            "workload %r is not an RPC workload; use route_stream()"
            % declared)
    base = METHOD_WORKLOADS.get(method)
    if base is None:
        return None, None, "unclassifiable method %r" % (method,)
    workload = base
    if declared is not None and declared != base:
        # The legitimate overrides: a routine tx read that is really
        # a historical-range / recovery / complex-parse / verification
        # read, a sigs read that is really a recovery / verification
        # read, an account read that is really a verification read.
        if base == ROUTINE_TX and declared in (
                HISTORICAL_RANGE, MISSED_EVENT_RECOVERY,
                COMPLEX_TX_PARSE, INDEPENDENT_VERIFICATION):
            workload = declared
        elif base == HISTORICAL_RANGE and declared in (
                MISSED_EVENT_RECOVERY, INDEPENDENT_VERIFICATION):
            workload = declared
        elif base == ROUTINE_ACCOUNT and declared in (
                INDEPENDENT_VERIFICATION,):
            workload = declared
        else:
            return None, None, (
                "declared workload %r contradicts method %s "
                "(default %s)" % (declared, method, base))
    if hints.get("historical") and base == ROUTINE_TX:
        workload = HISTORICAL_RANGE
    if hints.get("workload") in (MISSED_EVENT_RECOVERY,
                                 INDEPENDENT_VERIFICATION):
        workload = hints["workload"]
    return workload, helius_priority(workload, hints, priority), None


def helius_priority(workload, hints=None, priority=PRIORITY_CARD):
    """Helius P0–P3 priority for a workload (see module docstring).

    ``priority`` is the scheduler priority class; ``hints`` carries
    high_value / wallet_investigation / conflicting flags.
    """
    hints = hints or {}
    if workload == PREPROCESSED_OBSERVATION:
        return P0
    if workload == PARSED_TX_STREAM:
        return P0 if priority == PRIORITY_CRITICAL else P1
    if workload == COMPLEX_TX_PARSE:
        return P1 if priority == PRIORITY_CRITICAL else P2
    if hints.get("high_value") and priority == PRIORITY_CRITICAL:
        return P0
    if hints.get("wallet_investigation") or hints.get("conflicting"):
        return P2
    if workload in (HISTORICAL_RANGE, MISSED_EVENT_RECOVERY):
        # Chainstack handles the recent window (P3); only the
        # archive leg needs a scarce-provider fallback (P2).
        return P3 if hints.get("recent") else P2
    if workload == INDEPENDENT_VERIFICATION:
        return P2 if hints.get("conflicting") else P3
    if priority == PRIORITY_BACKGROUND:
        return P3
    if priority == PRIORITY_CRITICAL:
        return P1
    return P2


# ---------------------------------------------------------------------------
# Retention-window check (Chainstack recent window)
# ---------------------------------------------------------------------------
def chainstack_window_covers(hints, window_slots=None):
    """True when the caller has established that the target is inside
    Chainstack's retention window. Chainstack's published retention
    (400M-shred ledger) has NO published slot/time depth, so without
    an explicit ``recent`` assertion or a measured window + slot
    hints, coverage is NOT assumed — the router falls back to
    Alchemy (archive). ``window_slots`` is the operator-measured
    window (None = unknown)."""
    hints = hints or {}
    if hints.get("recent") is True:
        return True
    target = hints.get("target_slot")
    latest = hints.get("latest_slot")
    if target is None or latest is None or window_slots is None:
        return False
    try:
        return (int(latest) - int(target)) <= int(window_slots)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Candidate ordering (the routing rules — the ONLY place they live)
# ---------------------------------------------------------------------------
def _end_state_order(workload, method, hints, window_slots=None):
    """Candidate order with Helius absent (end state)."""
    covers = chainstack_window_covers(hints, window_slots)
    if workload == ROUTINE_ACCOUNT:
        return ["alchemy", "chainstack", "public"]
    if workload == ROUTINE_TX:
        order = ["alchemy"]
        if hints.get("recent"):
            order.append("chainstack")
        order.append("public")
        return order
    if workload in (HISTORICAL_RANGE, MISSED_EVENT_RECOVERY):
        # Chainstack first ONLY where its window covers the target
        # AND it can serve the method (getSignaturesForAddress is a
        # free-tier refusal there — capability filtering below drops
        # it; the order here is the preference, not a guarantee).
        if covers:
            return ["chainstack", "alchemy", "public"]
        return ["alchemy", "public"]
    if workload == INDEPENDENT_VERIFICATION:
        return ["chainstack", "alchemy", "public"]
    return []


def end_state_order(workload, method, hints=None, window_slots=None):
    """The Alchemy-first candidate order for one workload (the
    end-state allocation, built in Phase 3, NOT cut over).

    Routine account/tx reads, signature history and routine
    recovery go Alchemy-first; Chainstack serves recent reads and
    independent verification of known signatures (its Developer
    plan cannot serve getSignaturesForAddress / getProgramAccounts
    — capability filtering drops those, never retries them);
    Helius appears ONLY for its specialised products (Parsed
    Streams / Parsed Events), which return ["helius"] via
    candidate_order, never via this function. This is a VIEW of
    the routing rules for the shadow program's cutover decisions,
    not a second copy — _end_state_order() is the implementation.
    """
    return _end_state_order(workload, method, hints or {},
                            window_slots)


def candidate_order(workload, method, hints=None, helius_enabled=True,
                    helius_prio=P2, window_slots=None):
    """Ordered candidate providers for one workload.

    Shadow phase (helius_enabled): Helius leads for P0–P2 work and
    is the last resort for P3 work. Verification never includes
    Helius (it is the subject of verification, not the verifier).
    Specialised Helius-only workloads return ["helius"] or [] when
    Helius is disabled (refusal, not substitution).
    """
    hints = hints or {}
    if workload == PREPROCESSED_OBSERVATION:
        return []
    if workload in (COMPLEX_TX_PARSE, PARSED_TX_STREAM):
        return ["helius"] if helius_enabled else []
    order = _end_state_order(workload, method, hints, window_slots)
    if not helius_enabled:
        return order
    if workload == INDEPENDENT_VERIFICATION:
        return order
    if helius_prio in (P0, P1, P2):
        return ["helius"] + [p for p in order if p != "helius"]
    # P3: normally routed elsewhere; Helius is the last resort.
    return [p for p in order if p != "helius"] + ["helius"]


def failover_order(method, helius_enabled=True):
    """Default failover order for a method (no hints, CARD priority).

    Kept for compatibility with provider_manager and the Phase 2
    tests: getSignaturesForAddress excludes Chainstack (free-tier
    refusal), recent reads include it. Delegates to the router's
    own classification — this is a VIEW of the routing rules, not a
    second copy of them."""
    workload, prio, err = classify_workload(method)
    if err is not None:
        return (["helius", "alchemy", "public"] if helius_enabled
                else ["alchemy", "public"])
    return candidate_order(workload, method, {}, helius_enabled, prio)


# Backwards-compatible order constants (views, not decisions).
ORDER_DEFAULT = ["helius", "alchemy", "public"]
ORDER_RECENT = ["helius", "alchemy", "chainstack", "public"]


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """Per-provider breaker on consecutive error/timeout failures.

    429s do NOT trip the breaker (the scheduler owns 429 backoff);
    unsupported/unconfigured outcomes do not either. After the
    threshold the provider is skipped for the cooldown; then one
    half-open probe is allowed — success closes it, failure re-opens
    it for a full cooldown.
    """

    def __init__(self, failure_threshold=BREAKER_FAILURE_THRESHOLD,
                 cooldown_s=BREAKER_COOLDOWN_S, now=None):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._now = now
        self._state = {}  # provider -> {failures, opened_until, half_open}

    def _time(self):
        return self._now() if self._now else time.time()

    def _rec(self, provider):
        return self._state.setdefault(
            provider, {"failures": 0, "opened_until": None,
                       "half_open": False})

    def allow(self, provider):
        rec = self._rec(provider)
        until = rec["opened_until"]
        if until is None:
            return True
        now = self._time()
        if now < until:
            return False
        # Cooldown elapsed: one half-open probe.
        rec["opened_until"] = None
        rec["half_open"] = True
        return True

    def record(self, provider, result):
        rec = self._rec(provider)
        kind = result.get("kind")
        if kind == "ok":
            rec["failures"] = 0
            rec["opened_until"] = None
            rec["half_open"] = False
            return
        if kind not in ("error", "timeout"):
            return
        if rec["half_open"]:
            rec["half_open"] = False
            rec["failures"] = self.failure_threshold
            rec["opened_until"] = self._time() + self.cooldown_s
            return
        rec["failures"] += 1
        if rec["failures"] >= self.failure_threshold:
            rec["opened_until"] = self._time() + self.cooldown_s

    def status(self, provider):
        rec = self._rec(provider)
        until = rec["opened_until"]
        now = self._time()
        if until is not None and now < until:
            state = "open"
        elif rec["half_open"]:
            state = "half_open"
        else:
            state = "closed"
        return {"state": state, "consecutive_failures": rec["failures"],
                "opened_until": until,
                "cooldown_remaining_s": max(0.0, (until or 0) - now)
                if until else 0.0}


# ---------------------------------------------------------------------------
# Response cache (idempotent reads only, per provider)
# ---------------------------------------------------------------------------
class ResponseCache:
    """Short-TTL cache for idempotent reads, keyed per provider.

    Only kind == "ok" results are stored. A cache hit consumes no
    quota and is recorded as cached (cost None) in usage.

    A null payload is NEVER stored (2026-11-22 audit fix): a null
    result (e.g. getTransaction for a not-yet-indexed signature,
    getAccountInfo for a not-yet-created account) is a transient
    answer, not a fact. Caching it would serve the stale null to
    every caller until the TTL lapsed — the Alchemy null-tx caching
    defect. A re-fetch costs one quota unit; a poisoned lookup can
    cost a missed transaction.
    """

    def __init__(self, ttl_overrides=None, capacity=1000, now=None):
        self._ttl = dict(CACHE_TTL_S)
        if ttl_overrides:
            self._ttl.update(ttl_overrides)
        self.capacity = capacity
        self._now = now
        self._entries = {}  # key -> (expires_ts, RpcResult)
        self._order = []

    def _time(self):
        return self._now() if self._now else time.time()

    def _key(self, provider, method, params):
        return (provider, method,
                json.dumps(params, sort_keys=True, default=str))

    def get(self, provider, method, params):
        if method not in self._ttl:
            return None
        key = self._key(provider, method, params)
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, result = entry
        if self._time() > expires:
            del self._entries[key]
            return None
        return result

    def put(self, provider, method, params, result):
        if method not in self._ttl:
            return
        if result.get("kind") != "ok":
            return
        if result.get("result") is None:
            # Transient null — see the class docstring. Never cached.
            return
        key = self._key(provider, method, params)
        if key not in self._entries:
            self._order.append(key)
            while len(self._order) > self.capacity:
                old = self._order.pop(0)
                self._entries.pop(old, None)
        self._entries[key] = (self._time() + self._ttl[method], result)

    def __len__(self):
        return len(self._entries)


# ---------------------------------------------------------------------------
# Fallback-difference notes
# ---------------------------------------------------------------------------
def fallback_difference(primary, serving, method, workload):
    """Human-readable record of what differs when the serving
    provider is not the primary candidate. None when identical."""
    if serving == primary:
        return None
    if serving == "public":
        return ("served by public (tertiary fallback): no SLA, "
                "recent-only history, 1 call/2s fleet budget; "
                "completeness may differ from the primary provider")
    if primary == "chainstack" and serving == "alchemy":
        return ("Chainstack unavailable for this call; Alchemy served "
                "instead (full archive — a SUPERSET of Chainstack's "
                "recent window, no data lost)")
    if primary == "alchemy" and serving == "chainstack":
        return ("Chainstack served instead of Alchemy: RECENT reads "
                "only; below firstAvailable+5000 would be archive "
                "(-32002 on Developer) — completeness may differ")
    if primary == "helius" and serving == "alchemy":
        return ("Alchemy served instead of the live Helius leg "
                "(Helius unavailable/backoff); same standard-RPC data")
    if primary == "helius" and serving == "chainstack":
        return ("Chainstack served instead of Helius: RECENT reads "
                "only; below firstAvailable+5000 would be archive "
                "(-32002 on Developer) — completeness may differ")
    if primary == "alchemy" and serving == "helius":
        return ("live Helius leg served instead of Alchemy (shadow "
                "phase); same standard-RPC data")
    return ("served by %s instead of primary %s; provider "
            "capabilities differ — see provider_capabilities.py"
            % (serving, primary))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
class ProviderRouter:
    """The central routing layer. See the module docstring."""

    def __init__(self, home=None, helius_enabled=True, transport=None,
                 chainstack_recent_window_slots=None, cache=None,
                 usage_tracker=None, health=None, now=None,
                 mode=None):
        """mode="shadow" (default): Helius leads, the live leg.
        mode="end_state": Alchemy-first, Helius absent from ordinary
        routing (specialised products only). When mode is None (the
        default) it is DERIVED from ``helius_enabled`` — True ->
        shadow, False -> end_state — so the pre-Phase-3
        ``helius_enabled=False`` contract (used by the router,
        workload, and benchmark suites) keeps working unchanged.
        An explicit mode wins over helius_enabled; a mismatch is
        logged, never silently reinterpreted."""
        if mode is None:
            mode = (ROUTING_MODE_SHADOW if helius_enabled
                    else ROUTING_MODE_END_STATE)
        if mode not in ROUTING_MODES:
            raise ValueError("unknown routing mode %r" % (mode,))
        if (mode == ROUTING_MODE_END_STATE) != (not helius_enabled):
            logger.warning(
                "provider router: mode=%r disagrees with "
                "helius_enabled=%r; mode wins", mode, helius_enabled)
        self.mode = mode
        self.helius_enabled = (mode == ROUTING_MODE_SHADOW)
        self.home = home
        self._transport = transport
        self.chainstack_recent_window_slots = chainstack_recent_window_slots
        self.cache = cache if cache is not None else ResponseCache(now=now)
        self.usage = usage_tracker or UsageTracker(home=home)
        self.health = health or HealthMonitor(home=home)
        # Health counters survive a restart (Phase 3): a provider
        # that was down / quota-exhausted before the restart is
        # still down after it. A corrupt/missing file restores to
        # empty health; never raises.
        try:
            self.health.restore()
        except Exception:
            pass
        self.breaker = CircuitBreaker(now=now)
        self._now = now
        self._adapters = {}
        # Cheap configured flags only. Helius configured() imports
        # the live transport (daemon.launch_scanner.reserves — the
        # monolith); it is evaluated lazily in call(), which updates
        # the health flag there. Nothing here imports reserves.
        for provider in ("alchemy", "chainstack", "public"):
            try:
                self.health.set_configured(
                    provider,
                    rpc_mod.make_adapter(
                        provider, home=home,
                        transport=transport).configured())
            except Exception:
                self.health.set_configured(provider, False)

    def _time(self):
        return self._now() if self._now else time.time()

    def _adapter(self, provider):
        if provider not in self._adapters:
            self._adapters[provider] = rpc_mod.make_adapter(
                provider, home=self.home, transport=self._transport)
        return self._adapters[provider]

    def _account(self, provider):
        return rpc_mod.PROVIDER_ACCOUNTS.get(provider, provider)

    # -- classification / routing ---------------------------------------
    def classify(self, method, params=None, hints=None,
                 priority=PRIORITY_CARD):
        return classify_workload(method, params, hints, priority)

    def route(self, method, params=None, priority=PRIORITY_CARD,
              hints=None):
        """Routing decision for one logical call.

        Returns {"workload", "helius_priority", "primary",
        "fallbacks", "candidates", "refused", "refusal_reason"}.
        ``primary`` is None when the workload is refused (paid-gated
        or Helius-only with Helius disabled) or when no provider can
        serve the method at all.
        """
        hints = hints or {}
        # Specialised declarations are answered here (before method
        # classification) so call() reports the right refusal:
        # preprocessed is PAID-GATED (never enabled); a parsed stream
        # is not an RPC call at all.
        declared = hints.get("workload")
        if declared == PREPROCESSED_OBSERVATION:
            return {"mode": self.mode, "workload": declared, "helius_priority": P0,
                    "primary": None, "fallbacks": [], "candidates": [],
                    "refused": True,
                    "refusal_reason": PREPROCESSED_REFUSAL}
        if declared == PARSED_TX_STREAM:
            return {"mode": self.mode, "workload": declared, "helius_priority": None,
                    "primary": None, "fallbacks": [], "candidates": [],
                    "refused": True,
                    "refusal_reason": (
                        "workload %r is not an RPC workload; use "
                        "route_stream()" % declared)}
        workload, prio, err = classify_workload(method, params, hints,
                                                priority)
        if err is not None:
            return {"mode": self.mode, "workload": None, "helius_priority": None,
                    "primary": None, "fallbacks": [],
                    "candidates": [], "refused": True,
                    "refusal_reason": err}
        if workload == PREPROCESSED_OBSERVATION:
            return {"mode": self.mode, "workload": workload, "helius_priority": P0,
                    "primary": None, "fallbacks": [], "candidates": [],
                    "refused": True,
                    "refusal_reason": PREPROCESSED_REFUSAL}
        order = candidate_order(workload, method, hints,
                                self.helius_enabled, prio,
                                self.chainstack_recent_window_slots)
        # Capability + configured filtering happens in call(); route()
        # reports the preference order. A refused Helius-only
        # workload has an empty order here.
        if not order:
            if workload in (COMPLEX_TX_PARSE, PARSED_TX_STREAM):
                reason = (COMPLEX_PARSE_REFUSAL
                          if workload == COMPLEX_TX_PARSE
                          else PARSED_STREAM_REFUSAL)
            else:
                reason = ("no provider can serve %s on its free tier"
                          % method)
            return {"mode": self.mode, "workload": workload, "helius_priority": prio,
                    "primary": None, "fallbacks": [], "candidates": [],
                    "refused": True, "refusal_reason": reason}
        return {"mode": self.mode, "workload": workload, "helius_priority": prio,
                "primary": order[0], "fallbacks": order[1:],
                "candidates": order, "refused": False,
                "refusal_reason": None}

    def select(self, method, priority=PRIORITY_CARD, hints=None):
        """(primary, fallbacks) — the preference order for a call.
        Compatibility seam for provider_manager; the router owns the
        decision."""
        decision = self.route(method, priority=priority, hints=hints)
        return decision["primary"], decision["fallbacks"]

    def route_stream(self, workload, priority=PRIORITY_CARD, hints=None):
        """Routing decision for a specialised (non-RPC) workload.

        Returns {"workload", "helius_priority", "provider",
        "account", "stream", "refused", "refusal_reason"}.
        """
        hints = hints or {}
        if workload == PREPROCESSED_OBSERVATION:
            return {"workload": workload, "helius_priority": P0,
                    "provider": None, "account": None, "stream": None,
                    "refused": True,
                    "refusal_reason": PREPROCESSED_REFUSAL}
        if workload == PARSED_TX_STREAM:
            prio = helius_priority(workload, hints, priority)
            if not self.helius_enabled:
                return {"workload": workload, "helius_priority": prio,
                        "provider": None, "account": None,
                        "stream": None, "refused": True,
                        "refusal_reason": PARSED_STREAM_REFUSAL}
            return {"workload": workload, "helius_priority": prio,
                    "provider": "helius", "account": "helius_main",
                    "stream": "parsed_streams", "refused": False,
                    "refusal_reason": None}
        if workload == COMPLEX_TX_PARSE:
            prio = helius_priority(workload, hints, priority)
            if not self.helius_enabled:
                return {"workload": workload, "helius_priority": prio,
                        "provider": None, "account": None,
                        "stream": None, "refused": True,
                        "refusal_reason": COMPLEX_PARSE_REFUSAL}
            return {"workload": workload, "helius_priority": prio,
                    "provider": "helius", "account": "helius_main",
                    "stream": "parsed_events", "refused": False,
                    "refusal_reason": None}
        return {"workload": workload, "helius_priority": None,
                "provider": None, "account": None, "stream": None,
                "refused": True,
                "refusal_reason": "unknown stream workload %r" % workload}

    # -- the call path ----------------------------------------------------
    def call(self, method, params, timeout=None, priority=PRIORITY_CARD,
             hints=None, max_attempts=None):
        """One logical call with routing, scheduling, usage, health,
        circuit-breaking, caching, and failover.

        Returns the first RpcResult with kind == "ok", else the last
        attempt's result — annotated with routing metadata:
        workload, helius_priority, account, attempts (per-provider
        outcomes), fallback (bool), fallback_difference (str|None),
        cached (bool). Callers fail closed on non-ok.
        """
        hints = hints or {}
        decision = self.route(method, params, priority, hints)
        workload = decision["workload"]
        prio = decision["helius_priority"]
        if decision["refused"]:
            return rpc_mod._result(
                None, "unsupported", 0.0, "none",
                note=decision["refusal_reason"])
        if timeout is None:
            timeout = WORKLOAD_TIMEOUTS.get(workload, 12)
        candidates = decision["candidates"]
        if max_attempts is not None:
            candidates = candidates[:max_attempts]
        attempts = []
        last = None
        for provider in candidates:
            adapter = self._adapter(provider)
            account = self._account(provider)
            # Capability gate: never send a request to a provider
            # that cannot perform the method.
            if not adapter.supports(method):
                attempts.append({"provider": provider, "kind": "skipped",
                                 "elapsed_s": 0.0,
                                 "note": "capability refusal"})
                continue
            is_configured = adapter.configured()
            self.health.set_configured(provider, is_configured)
            if not is_configured:
                attempts.append({"provider": provider, "kind": "skipped",
                                 "elapsed_s": 0.0,
                                 "note": "unconfigured"})
                continue
            if self.usage.account_disabled(account):
                attempts.append({"provider": provider, "kind": "skipped",
                                 "elapsed_s": 0.0,
                                 "note": "account disabled"})
                continue
            if not self.breaker.allow(provider):
                attempts.append({"provider": provider, "kind": "skipped",
                                 "elapsed_s": 0.0,
                                 "note": "circuit breaker open"})
                continue
            allowed, budget_reason = self.usage.nonessential_allowed(
                account, priority, workload=workload, provider=provider)
            if not allowed:
                attempts.append({"provider": provider, "kind": "skipped",
                                 "elapsed_s": 0.0,
                                 "note": budget_reason})
                if budget_reason in (
                        "budget_stop_nonessential",
                        "daily_budget_stop_nonessential") or \
                        (budget_reason or "").startswith(
                            "feature_budget_stop_nonessential"):
                    stop_pct = self.usage.budget_status(account).get(
                        "stop_nonessential_pct") or 0.0
                    last = rpc_mod._result(
                        None, "rate_limited", 0.0, provider,
                        note="budget stop (%s): nonessential traffic "
                             "refused at %.0f%% of budget"
                             % (budget_reason, 100.0 * stop_pct))
                continue
            # Response cache (idempotent reads only; no quota spent).
            cached = self.cache.get(provider, method, params)
            if cached is not None:
                hit = rpc_mod.RpcResult(dict(cached))
                hit["cached"] = True
                hit["workload"] = workload
                hit["helius_priority"] = prio
                hit["account"] = account
                hit["attempts"] = attempts + [
                    {"provider": provider, "kind": "ok (cached)",
                     "elapsed_s": 0.0, "note": None}]
                hit["fallback"] = provider != decision["primary"]
                hit["fallback_difference"] = fallback_difference(
                    decision["primary"], provider, method, workload)
                self.usage.record(account, provider, method, workload,
                                  priority, helius_priority=prio,
                                  cost=None, kind="ok", elapsed=0.0,
                                  cached=True,
                                  fallback=hit["fallback"],
                                  fallback_difference=
                                  hit["fallback_difference"])
                return hit
            # Scheduler token for every non-Helius attempt; Helius
            # attempts go through the existing reserves transport
            # (which paces itself).
            if provider != "helius":
                quota = SharedQuota(provider, home=self.home)
                granted, _waited, reason = quota.acquire(
                    priority, cost=1.0)
                if not granted:
                    refused = rpc_mod._result(
                        None, "rate_limited", 0.0, provider,
                        note="scheduler refused token (%s)" % reason)
                    attempts.append({"provider": provider,
                                     "kind": "rate_limited",
                                     "elapsed_s": 0.0,
                                     "note": "scheduler %s" % reason})
                    self.health.record_rpc(refused)
                    self.usage.record(
                        account, provider, method, workload, priority,
                        helius_priority=prio, cost=None,
                        kind="rate_limited", elapsed=0.0)
                    last = refused
                    continue
            result = adapter.call(method, params, timeout=timeout)
            attempts.append({"provider": provider,
                             "kind": result["kind"],
                             "elapsed_s": round(result["elapsed"], 4),
                             "note": result.get("note")})
            self.health.record_rpc(result)
            breaker_was = self.breaker.status(provider)["state"]
            self.breaker.record(provider, result)
            # Health survives a restart (Phase 3): persist when the
            # breaker trips open or a monthly-quota 429 marks the
            # provider down. Persisting on every call would be a
            # disk write per RPC; state changes are the durable
            # events.
            breaker_now = self.breaker.status(provider)["state"]
            if breaker_now == "open" and breaker_was != "open":
                try:
                    self.health.persist()
                except Exception:
                    pass
            if result.get("rate_limit_kind") == "monthly_quota":
                try:
                    self.health.persist()
                except Exception:
                    pass
            cost = adapter.cost(method)
            # The journal records the fallback that actually served
            # (or attempted) this request — not just cache hits.
            # Before this fix only the cache-hit path passed
            # fallback/fallback_difference, so a failover serve
            # looked like a primary serve in the journal.
            self.usage.record(account, provider, method, workload,
                              priority, helius_priority=prio, cost=cost,
                              kind=result["kind"],
                              elapsed=result["elapsed"],
                              fallback=provider != decision["primary"],
                              fallback_difference=fallback_difference(
                                  decision["primary"], provider, method,
                                  workload))
            if provider != "helius":
                quota = SharedQuota(provider, home=self.home)
                if result["kind"] == "rate_limited":
                    quota.report_429(result.get("rate_limit_kind"),
                                     result.get("wait_hint_s"))
                    if result.get("rate_limit_kind") == "monthly_quota":
                        self.usage.record_monthly_quota(account)
                elif result["kind"] == "ok":
                    quota.report_success()
                    if cost:
                        quota.record_cost(
                            cu=cost if provider == "alchemy" else 0,
                            ru=cost if provider == "chainstack" else 0)
            if result["kind"] == "ok":
                self.cache.put(provider, method, params, result)
                result["workload"] = workload
                result["helius_priority"] = prio
                result["account"] = account
                result["attempts"] = attempts
                result["fallback"] = provider != decision["primary"]
                diff = fallback_difference(
                    decision["primary"], provider, method, workload)
                if adapter.unverified(method):
                    unverified_note = (
                        "%s support on %s is UNVERIFIED — result is "
                        "provisional" % (method, provider))
                    diff = ((diff + "; ") if diff else "") + unverified_note
                result["fallback_difference"] = diff
                result["cached"] = False
                return result
            last = result
        if last is None:
            skipped = [a for a in attempts if a["kind"] == "skipped"]
            note = "no provider could serve %s" % method
            if skipped:
                note += " (skipped: %s)" % ", ".join(
                    "%s:%s" % (a["provider"], a["note"]) for a in skipped)
            last = rpc_mod._result(None, "unconfigured", 0.0, "none",
                                   note=note)
        last["workload"] = workload
        last["helius_priority"] = prio
        last["account"] = None
        last["attempts"] = attempts
        last["fallback"] = False
        last["fallback_difference"] = None
        last["cached"] = False
        return last

    # -- surfacing --------------------------------------------------------
    def health_snapshot(self):
        return self.health.snapshot()

    def usage_summary(self):
        return self.usage.summary()

    def breaker_status(self, provider=None):
        if provider:
            return {provider: self.breaker.status(provider)}
        return {p: self.breaker.status(p) for p in rpc_mod.PROVIDERS}

    def record_wss_handshake_429(self, provider):
        self.health.record_wss_handshake_429(provider)

    def record_wss_handshake_ok(self, provider):
        self.health.record_wss_handshake_ok(provider)

    def wss_url(self, provider):
        """WebSocket endpoint for a provider, or None when this layer
        cannot build one (Helius: the live transport owns the keys)."""
        return rpc_mod.wss_url(provider, home=self.home)
