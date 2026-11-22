"""Alchemy routine acquisition workload (Phase 5, spec section 4).

Alchemy is the primary routine/live acquisition provider in the
validated end-state allocation. This module is the ONLY entry point
for routine acquisition traffic: every call goes through the central
router (``provider_router.ProviderRouter.call``) — this module makes
NO routing decision of its own, builds no second routing path, and
never calls an adapter or ``rpc_client.rpc`` directly.

HTTP only. Ordinary RPC goes over HTTPS; this module never opens a
WebSocket (WSS subscriptions live in ``websocket_manager.
AlchemySubscriptions``). The router owns scheduling (via the shared
quota scheduler), response caching, usage tracking, circuit
breaking, and failover, so every call below honours them.

Shadow phase honesty: while the router is constructed with
``helius_enabled=True`` (the live leg today), the router puts
Helius first for P0-P2 work and Alchemy-first is the END-STATE
order, not the shadow order. This module never overrides that:
``route_preview()`` reports exactly what the router decides, and
the tests demonstrate Alchemy-first with ``helius_enabled=False``
(the end state). Nothing here re-enables, disables, or reorders a
provider.

Routine methods covered (spec section 4A):
- account lookups: getAccountInfo, getMultipleAccounts,
  getBalance, getTokenAccountBalance, getTokenAccountsByOwner
- transaction retrieval: getTransaction, getSignaturesForAddress
- slot/block reads: getSlot, getBlock, getLatestBlockhash,
  getSignatureStatuses
- getTokenLargestAccounts: PROVISIONAL — support and CU cost are
  UNVERIFIED on Alchemy (``provider_capabilities.
  UNVERIFIED_METHODS``). It is callable through the router but
  every result is flagged ``provisional=True`` and
  ``verify_methods()`` reports it as provisional, never supported.

Batching: getMultipleAccounts is chunked at 100 accounts per call
(the Solana per-request limit) and getSignatureStatuses at 256
signatures per call (a client-side batching cap); each chunk is
one router call, so scheduler tokens, usage, and caching apply
per chunk, never per account.

Caching: getTransaction results are cached per signature in this
workload (TTL 300s, ok results only) and never re-fetched while
the entry is fresh — a second request for the same signature
returns the cached result without a router call. The router's own
response cache also applies (getTransaction TTL 30s); this
per-signature cache is the "never re-fetched per signature"
guarantee for callers that ask repeatedly. getSignaturesForAddress
is NOT cached (range read, live by definition).

No secrets: no key material is read, logged, or stored here. No
live file is imported (in particular never the launch_scanner
monolith); only the acquisition package.
"""

from __future__ import annotations

import time

from . import provider_capabilities as caps
from .provider_router import (
    ROUTINE_ACCOUNT,
    ROUTINE_TX,
    ProviderRouter,
    candidate_order,
    classify_workload,
)
from .request_scheduler import PRIORITY_CARD, PRIORITY_CRITICAL
from .rpc_client import method_cost
from daemon import solana_ids as _solana_ids

# ---------------------------------------------------------------------------
# Method inventory (spec section 4A)
# ---------------------------------------------------------------------------
ACCOUNT_METHODS = (
    "getAccountInfo",
    "getMultipleAccounts",
    "getBalance",
    "getTokenAccountBalance",
    "getTokenAccountsByOwner",
)
TX_METHODS = (
    "getTransaction",
    "getSignaturesForAddress",
)
SLOT_BLOCK_METHODS = (
    "getSlot",
    "getBlock",
    "getLatestBlockhash",
    "getSignatureStatuses",
)
ROUTINE_METHODS = ACCOUNT_METHODS + TX_METHODS + SLOT_BLOCK_METHODS

# Methods whose Alchemy support/cost is UNVERIFIED — callable, but
# every result is flagged provisional (see module docstring).
PROVISIONAL_METHODS = ("getTokenLargestAccounts",)

# Client-side batching caps (per router call).
MAX_ACCOUNTS_PER_CALL = 100
MAX_SIGNATURES_PER_STATUS_CALL = 256

# Per-signature getTransaction cache TTL (seconds).
TX_CACHE_TTL_S = 300.0

# Canonical program ID lives in daemon/solana_ids.py — never
# redefine the literal here (Phase 2).
TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class AlchemyWorkload:
    """Routine acquisition entry points, Alchemy-first via the router.

    ``router`` may be injected (tests inject a router over a fake
    transport); otherwise one is built with ``helius_enabled``
    (default True = shadow phase, Helius still the live leg).
    ``home`` is passed through to the router (quota state + usage
    store location).
    """

    def __init__(self, router=None, home=None, helius_enabled=True,
                 transport=None, now=None):
        if router is not None:
            self.router = router
        else:
            self.router = ProviderRouter(
                home=home, helius_enabled=helius_enabled,
                transport=transport)
        self._now = now
        self._tx_cache = {}  # signature -> (expires_ts, RpcResult)

    def _time(self):
        return self._now() if self._now else time.time()

    # -- routing visibility (read-only views of the router's rules) ---
    def route_preview(self, method, priority=PRIORITY_CARD, hints=None):
        """What the router would do for this method — its decision,
        not ours. ``primary`` is Alchemy in the end state
        (``helius_enabled=False``); in the shadow phase it is the
        live Helius leg for P0-P2 work."""
        return self.router.route(method, priority=priority, hints=hints)

    def verify_methods(self):
        """Every routine method in section 4A verified against the
        capability config: supported, provisional, or refused.

        Returns [{"method", "status", "alchemy_cost_cu",
        "router_primary_end_state", "note"}]. ``status`` is
        "supported" (confirmed in provider_capabilities),
        "provisional" (UNVERIFIED — currently
        getTokenLargestAccounts and getTokenAccountsByOwner, whose
        Alchemy CU cost is unverified), "refused" (free-tier
        refusal), or "unroutable" (the router cannot classify the
        method at all — no method is unroutable today; the
        getTokenAccountsByOwner gap found by Phase 5 was fixed by
        the router-owner addition of 2026-11-22).
        This is a config read + a read of the router's own pure
        candidate-order function — it makes no routing decision and
        performs no network I/O.
        """
        out = []
        for method in ROUTINE_METHODS + PROVISIONAL_METHODS:
            refusals = caps.FREE_TIER_REFUSALS.get("alchemy", set())
            unverified = caps.UNVERIFIED_METHODS.get("alchemy", set())
            workload, _prio, err = classify_workload(method)
            primary = None
            if err is None and workload is not None:
                order = candidate_order(workload, method, {},
                                        helius_enabled=False)
                primary = order[0] if order else None
            if err is not None:
                # The router cannot classify this method (it is not
                # in METHOD_WORKLOADS), so router.call() refuses it
                # fail-closed. This is a ROUTER gap, not a workload
                # gap: this module must not build a second routing
                # path around it. Flagged here for the router owner.
                status, note = ("unroutable",
                                "router cannot classify %s (%s) — "
                                "call is refused fail-closed; needs a "
                                "router-owner addition" % (method, err))
            elif method in refusals:
                status, note = "refused", "free-tier refusal (alchemy)"
            elif method in unverified:
                status, note = ("provisional",
                                "support/cost UNVERIFIED on Alchemy — "
                                "result is provisional, do not treat as "
                                "confirmed")
            else:
                status, note = "supported", None
            out.append({"method": method, "status": status,
                        "alchemy_cost_cu": method_cost("alchemy", method),
                        "router_primary_end_state": primary,
                        "note": note})
        return out

    # -- account lookups ------------------------------------------------
    def get_account_info(self, pubkey, commitment="confirmed",
                         encoding="jsonParsed",
                         priority=PRIORITY_CARD, hints=None):
        return self.router.call(
            "getAccountInfo", [pubkey, {"encoding": encoding,
                                        "commitment": commitment}],
            priority=priority, hints=hints)

    def get_multiple_accounts(self, pubkeys, commitment="confirmed",
                              encoding="jsonParsed",
                              priority=PRIORITY_CARD, hints=None):
        """Chunked at MAX_ACCOUNTS_PER_CALL per router call. Returns
        a list of RpcResults, one per chunk, in request order."""
        results = []
        for chunk in _chunks(list(pubkeys), MAX_ACCOUNTS_PER_CALL):
            results.append(self.router.call(
                "getMultipleAccounts",
                [chunk, {"encoding": encoding,
                         "commitment": commitment}],
                priority=priority, hints=hints))
        return results

    def get_balance(self, pubkey, commitment="confirmed",
                    priority=PRIORITY_CARD, hints=None):
        return self.router.call(
            "getBalance", [pubkey, {"commitment": commitment}],
            priority=priority, hints=hints)

    def get_token_account_balance(self, token_account,
                                  commitment="confirmed",
                                  priority=PRIORITY_CARD, hints=None):
        return self.router.call(
            "getTokenAccountBalance",
            [token_account, {"commitment": commitment}],
            priority=priority, hints=hints)

    def get_token_accounts_by_owner(self, owner, mint=None,
                                    program_id=TOKEN_PROGRAM_ID,
                                    commitment="confirmed",
                                    encoding="jsonParsed",
                                    priority=PRIORITY_CARD, hints=None):
        """PROVISIONAL: Alchemy CU cost is UNVERIFIED (router-owner
        addition 2026-11-22 put the method in the router's
        METHOD_WORKLOADS as ROUTINE_ACCOUNT, closing the Phase 5
        gap). Callable through the router; the returned result is
        provisional, do not treat it as confirmed. See
        verify_methods()."""
        if mint is not None:
            filt = {"mint": mint}
        else:
            filt = {"programId": program_id}
        return self.router.call(
            "getTokenAccountsByOwner",
            [owner, filt, {"encoding": encoding,
                           "commitment": commitment}],
            priority=priority, hints=hints)

    def get_token_largest_accounts(self, mint, commitment="confirmed",
                                   priority=PRIORITY_CARD, hints=None):
        """PROVISIONAL: Alchemy support and CU cost are UNVERIFIED.
        Callable through the router, but the returned result always
        carries ``provisional=True`` and a note — callers must not
        treat it as a confirmed capability."""
        result = self.router.call(
            "getTokenLargestAccounts", [mint, {"commitment": commitment}],
            priority=priority, hints=hints)
        result = dict(result)
        result["provisional"] = True
        note = ("getTokenLargestAccounts support on Alchemy is "
                "UNVERIFIED — result is provisional")
        result["note"] = ((result.get("note") + "; ") if result.get("note")
                          else "") + note
        return result

    # -- transaction retrieval ------------------------------------------
    def get_transaction(self, signature, commitment="confirmed",
                        encoding="jsonParsed",
                        max_supported_transaction_version=0,
                        priority=PRIORITY_CARD, hints=None):
        """Per-signature cached: an ok result is never re-fetched
        while the cache entry is fresh (TX_CACHE_TTL_S). A cache hit
        returns a copy of the stored result with ``cached=True`` and
        makes no router call.

        A null result (the signature is not indexed YET) is never
        cached (2026-11-22 audit fix): ``{"result": null}`` classifies
        as kind "ok" in the adapter, and caching it would serve the
        stale null for the whole TTL — the Alchemy null-transaction
        caching defect. The tx can land seconds later, so the next
        poll must reach the provider again."""
        now = self._time()
        entry = self._tx_cache.get(signature)
        if entry is not None:
            expires, stored = entry
            if now <= expires:
                hit = dict(stored)
                hit["cached"] = True
                return hit
            del self._tx_cache[signature]
        result = self.router.call(
            "getTransaction",
            [signature, {"encoding": encoding,
                         "commitment": commitment,
                         "maxSupportedTransactionVersion":
                             max_supported_transaction_version}],
            priority=priority, hints=hints)
        if result.get("kind") == "ok" \
                and result.get("result") is not None:
            self._tx_cache[signature] = (now + TX_CACHE_TTL_S,
                                          dict(result))
            if len(self._tx_cache) > 10000:
                oldest = min(self._tx_cache,
                             key=lambda k: self._tx_cache[k][0])
                del self._tx_cache[oldest]
        return result

    def get_signatures_for_address(self, address, limit=1000,
                                   before=None, until=None,
                                   commitment="confirmed",
                                   priority=PRIORITY_CARD, hints=None):
        """One range read (NOT cached — live by definition)."""
        opts = {"limit": limit, "commitment": commitment}
        if before is not None:
            opts["before"] = before
        if until is not None:
            opts["until"] = until
        return self.router.call(
            "getSignaturesForAddress", [address, opts],
            priority=priority, hints=hints)

    def signatures_for_address_all(self, address, limit=1000,
                                   max_pages=10,
                                   priority=PRIORITY_CARD, hints=None):
        """Bounded paging over getSignaturesForAddress using the
        ``before`` cursor. Returns (signatures, pages_fetched).
        Stops on a short page, an empty page, or a non-ok result."""
        sigs = []
        before = None
        pages = 0
        for _ in range(max_pages):
            result = self.get_signatures_for_address(
                address, limit=limit, before=before, priority=priority,
                hints=hints)
            pages += 1
            if result.get("kind") != "ok":
                break
            rows = result.get("result") or []
            for row in rows:
                sig = row.get("signature") if isinstance(row, dict) \
                    else row
                if sig is not None:
                    sigs.append(sig)
            if len(rows) < limit:
                break
            before = sigs[-1] if sigs else None
            if before is None:
                break
        return sigs, pages

    # -- slot / block reads ----------------------------------------------
    def get_slot(self, commitment="confirmed",
                 priority=PRIORITY_CRITICAL, hints=None):
        return self.router.call(
            "getSlot", [], priority=priority, hints=hints)

    def get_block(self, slot, encoding="jsonParsed",
                  max_supported_transaction_version=0,
                  priority=PRIORITY_CRITICAL, hints=None):
        return self.router.call(
            "getBlock",
            [slot, {"encoding": encoding,
                    "maxSupportedTransactionVersion":
                        max_supported_transaction_version}],
            priority=priority, hints=hints)

    def get_latest_blockhash(self, commitment="confirmed",
                             priority=PRIORITY_CRITICAL, hints=None):
        return self.router.call(
            "getLatestBlockhash", [{"commitment": commitment}],
            priority=priority, hints=hints)

    def get_signature_statuses(self, signatures,
                               search_transaction_history=False,
                               priority=PRIORITY_CRITICAL, hints=None):
        """Chunked at MAX_SIGNATURES_PER_STATUS_CALL per router call.
        Returns a list of RpcResults, one per chunk."""
        results = []
        for chunk in _chunks(list(signatures),
                             MAX_SIGNATURES_PER_STATUS_CALL):
            results.append(self.router.call(
                "getSignatureStatuses",
                [chunk, {"searchTransactionHistory":
                             search_transaction_history}],
                priority=priority, hints=hints))
        return results

    # -- surfacing ---------------------------------------------------------
    def usage_summary(self):
        """Router usage summary (per-account, recorded by the router
        on every call)."""
        return self.router.usage_summary()

    def health_snapshot(self):
        return self.router.health_snapshot()
