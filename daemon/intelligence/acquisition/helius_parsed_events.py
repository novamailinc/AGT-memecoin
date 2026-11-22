"""Helius Parsed Events API client (Phase 7, spec §6).

REST, on demand — used SELECTIVELY, never as the default parser
for every transaction. The Parsed Events API (Helius docs, read
2026-11-22; GA on all plans including Free) decodes transactions
for 3,600+ programs from the same IDL catalog as Parsed Streams,
so stream and API consumers share the processing code (see
``helius_streams.normalize_parsed_transaction``).

Methods (``https://mainnet.helius-rpc.com/v1/parsed-events``,
auth = ``api-key`` query parameter, 10 credits per request):

* ``POST /transactions`` — parse one or more signatures
  (up to 100 per call).
* ``POST /transaction-history`` — parsed history for an address.
* ``POST /scan`` — search an address's history for transactions
  that call a program or touch a token mint; only parsed matches.
* ``GET /idl/{programId}`` — every IDL version the catalog has
  for a program, oldest first.
* ``GET /idl/{programId}/{slot}`` — the IDL in effect at a slot.

SELECTIVE USE ONLY. Every call must name a ``reason`` from the
allowed set — complex transaction investigation, a transaction
the own decoder cannot confidently interpret, high-value wallet
analysis, historical transaction interpretation, or disputed /
inconsistent event data. A call with reason "routine" (or no
reason) is REFUSED here, in code: the Parsed Events API must
never become the default parser for every transaction. Routine
parsing stays with the own decoders over standard RPC.

TRUST MODEL: same as the stream — a parsed result is
``parsed_unverified`` until ``verify_with_own_decoder`` (from
``helius_streams``) agrees with the own decoder on signature,
slot, fee, status and program ids. A disagreement is a CONFLICT,
flagged, never silently trusted.

KEYS: the API key is supplied by the caller (constructor arg or
``HELIUS_API_KEY`` env var) — this module never reads a key file.
The key travels only in the request URL's query parameter; it is
never logged, and URLs are never logged (only the host).
Transport is injectable (``_opener``) so no test ever touches the
network. Live verification is DEFERRED to AGT (no live call has
been made from this VM).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from .helius_streams import (
    VERIFICATION_PARSED_UNVERIFIED, normalize_parsed_transaction,
    verify_with_own_decoder)

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.helius_parsed_events")

PARSED_EVENTS_BASE = "https://mainnet.helius-rpc.com/v1/parsed-events"
PARSED_EVENTS_HOST = "mainnet.helius-rpc.com"

# 10 credits per request (capability config, helius_main.parsed_events).
CREDITS_PER_REQUEST = 10
MAX_SIGNATURES_PER_CALL = 100

# The ONLY legitimate reasons to call Parsed Events. Anything else
# — above all "routine" parsing of every transaction — is refused.
ALLOWED_REASONS = (
    "complex_investigation",      # complex tx investigation
    "decoder_unconfident",        # own decoder cannot interpret it
    "high_value_wallet",          # high-value wallet analysis
    "historical_interpretation",  # historical tx interpretation
    "disputed_event",             # disputed / inconsistent event data
)

SELECTIVE_USE_REFUSAL = (
    "Parsed Events is SELECTIVE-USE only (complex investigation, "
    "decoder-unconfident tx, high-value wallet, historical "
    "interpretation, disputed event). It is never the default "
    "parser for every transaction — routine parsing stays with the "
    "own decoders over standard RPC.")


def selective_use_check(reason):
    """(ok, note). A reason outside ALLOWED_REASONS is refused."""
    if reason in ALLOWED_REASONS:
        return True, None
    return False, SELECTIVE_USE_REFUSAL


class HeliusParsedEventsClient:
    """Parsed Events API REST client.

    ``api_key`` is the Helius API key (caller-supplied; env
    ``HELIUS_API_KEY`` is the fallback). ``_opener`` is an
    injectable ``urlopen``-shaped callable for tests.
    ``http_timeout`` is per request. Every method returns a dict:
    {"kind": "ok"|"refused"|"rate_limited"|"timeout"|"error"|
    "unconfigured", "result": ..., "credits": int|None,
    "elapsed": float, "note": str|None}.
    """

    def __init__(self, api_key=None, http_timeout=20, _opener=None,
                 usage_store=None):
        self._api_key = api_key or os.environ.get(
            "HELIUS_API_KEY", "").strip() or None
        self.http_timeout = http_timeout
        self._opener = _opener or urllib.request.urlopen
        self.calls = 0
        self.credits_spent = 0
        # Persistent usage accounting (Phase 3, D-07): when a
        # UsageStore is injected, every successful call (10 credits)
        # is recorded against helius_main / parsed_events, so the
        # spend survives a process restart. Without it the in-memory
        # counters below are the only record. Never raises.
        self._usage_store = usage_store

    def _record_usage(self, api_method, kind, cost=None, elapsed=0.0):
        store = self._usage_store
        if store is None:
            return None
        try:
            return store.record(
                account="helius_main", provider="helius",
                method="parsed_events/%s" % api_method,
                workload="complex_tx_parse", priority="card",
                helius_priority="P2", cost=cost, kind=kind,
                elapsed=elapsed)
        except Exception:
            logger.warning("parsed-events usage record failed",
                           exc_info=True)
            return None

    def persistent_stats(self):
        """Persistent counters from the UsageStore (None when no
        store is attached): calls + cost for the parsed_events
        feature this month, surviving restarts."""
        store = self._usage_store
        if store is None:
            return None
        rec = store.get("helius_main")
        feat = (rec.get("by_feature") or {}).get("parsed_events", {})
        return {"calls": feat.get("calls", 0),
                "cost": feat.get("cost", 0),
                "month": rec.get("month"),
                "account_calls": rec.get("calls", 0)}

    def configured(self):
        return bool(self._api_key)

    # -- transport ------------------------------------------------------
    def _request(self, method, path, body=None):
        """One HTTPS call. Returns (kind, parsed_body, elapsed,
        http_status). Never raises; never logs the URL."""
        if not self.configured():
            return "unconfigured", None, 0.0, None
        url = "%s%s?api-key=%s" % (PARSED_EVENTS_BASE, path,
                                   self._api_key)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"})
        start = time.monotonic()
        try:
            with self._opener(req, timeout=self.http_timeout) as resp:
                raw = resp.read()
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read() if hasattr(exc, "read") else b""
            except Exception:
                raw = b""
            status = exc.code
            try:
                parsed = json.loads(raw.decode("utf-8", "replace")) \
                    if raw else {}
            except ValueError:
                parsed = {}
            if status == 429:
                return "rate_limited", parsed, \
                    time.monotonic() - start, status
            return "error", parsed, time.monotonic() - start, status
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            if isinstance(exc, TimeoutError) \
                    or "timed out" in str(exc).lower():
                return "timeout", None, time.monotonic() - start, None
            return "error", None, time.monotonic() - start, None
        except Exception:
            return "error", None, time.monotonic() - start, None
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return "error", None, time.monotonic() - start, status
        return "ok", parsed, time.monotonic() - start, status

    def _result(self, kind, result, elapsed, note=None, credits=None):
        if kind == "ok":
            self.calls += 1
            self.credits_spent += CREDITS_PER_REQUEST
            credits = CREDITS_PER_REQUEST
        return {"kind": kind, "result": result, "elapsed": elapsed,
                "credits": credits, "note": note}

    def _ok(self, api_method, result, elapsed):
        """Success result + persistent usage record (D-07): one
        call, CREDITS_PER_REQUEST credits, against helius_main /
        parsed_events. The in-memory counters above stay the
        process-local mirror."""
        out = self._result("ok", result, elapsed)
        self._record_usage(api_method, "ok",
                           cost=CREDITS_PER_REQUEST, elapsed=elapsed)
        return out

    def _guard(self, reason):
        ok, note = selective_use_check(reason)
        if not ok:
            return self._result("refused", None, 0.0, note=note)
        if not self.configured():
            return self._result(
                "unconfigured", None, 0.0,
                note="no Helius API key supplied (caller arg or "
                     "HELIUS_API_KEY); live key is owned by the "
                     "deployment wiring")
        return None

    # -- API methods ----------------------------------------------------
    def parse_transactions(self, signatures, reason):
        """Parse 1–100 signatures. ``reason`` must be selective-use
        (see module docstring). Each returned entry is normalized
        with verification_status ``parsed_unverified``."""
        guard = self._guard(reason)
        if guard is not None:
            return guard
        if not signatures or not isinstance(signatures, (list, tuple)):
            return self._result("refused", None, 0.0,
                                note="signatures must be a non-empty list")
        if len(signatures) > MAX_SIGNATURES_PER_CALL:
            return self._result(
                "refused", None, 0.0,
                note="max %d signatures per call, got %d"
                     % (MAX_SIGNATURES_PER_CALL, len(signatures)))
        kind, body, elapsed, _status = self._request(
            "POST", "/transactions",
            {"transactions": list(signatures)})
        if kind != "ok":
            return self._result(kind, None, elapsed,
                                note="parsed-events request failed")
        entries = body if isinstance(body, list) else \
            (body.get("transactions") if isinstance(body, dict) else None)
        if entries is None:
            return self._result(
                "error", None, elapsed,
                note="unexpected parsed-events response shape")
        normalized = []
        for entry in entries:
            event = normalize_parsed_transaction(entry)
            if event is not None:
                event["source"] = "helius_parsed_events"
                normalized.append(event)
        return self._ok("transactions", normalized, elapsed)

    def transaction_history(self, address, reason, limit=None,
                            pagination_token=None):
        """Parsed transaction history for one address."""
        guard = self._guard(reason)
        if guard is not None:
            return guard
        if not isinstance(address, str) or not address:
            return self._result("refused", None, 0.0,
                                note="address must be a non-empty string")
        body = {"address": address}
        if limit is not None:
            body["limit"] = limit
        if pagination_token is not None:
            body["paginationToken"] = pagination_token
        kind, resp, elapsed, _status = self._request(
            "POST", "/transaction-history", body)
        if kind != "ok":
            return self._result(kind, None, elapsed,
                                note="parsed-events request failed")
        entries = resp.get("transactions") if isinstance(resp, dict) \
            else None
        if entries is None:
            return self._result(
                "error", None, elapsed,
                note="unexpected parsed-events response shape")
        normalized = []
        for entry in entries:
            event = normalize_parsed_transaction(entry)
            if event is not None:
                event["source"] = "helius_parsed_events"
                normalized.append(event)
        out = {"transactions": normalized,
               "paginationToken": resp.get("paginationToken")}
        return self._ok("transaction-history", out, elapsed)

    def scan_history(self, address, reason, program_id=None, mint=None,
                     limit=None):
        """Search an address's history for transactions calling a
        program or touching a mint. At least one of program_id /
        mint is required (server-side scan scope)."""
        guard = self._guard(reason)
        if guard is not None:
            return guard
        if not program_id and not mint:
            return self._result(
                "refused", None, 0.0,
                note="scan requires a program_id or a mint")
        body = {"address": address}
        if program_id:
            body["programId"] = program_id
        if mint:
            body["mint"] = mint
        if limit is not None:
            body["limit"] = limit
        kind, resp, elapsed, _status = self._request(
            "POST", "/scan", body)
        if kind != "ok":
            return self._result(kind, None, elapsed,
                                note="parsed-events request failed")
        entries = resp.get("transactions") if isinstance(resp, dict) \
            else None
        if entries is None:
            return self._result(
                "error", None, elapsed,
                note="unexpected parsed-events response shape")
        normalized = []
        for entry in entries:
            event = normalize_parsed_transaction(entry)
            if event is not None:
                event["source"] = "helius_parsed_events"
                normalized.append(event)
        out = {"transactions": normalized,
               "paginationToken": resp.get("paginationToken")}
        return self._ok("scan", out, elapsed)

    def list_idl_versions(self, program_id, reason):
        """Every IDL version the catalog has for a program."""
        guard = self._guard(reason)
        if guard is not None:
            return guard
        kind, body, elapsed, _status = self._request(
            "GET", "/idl/%s" % program_id)
        if kind != "ok":
            return self._result(kind, None, elapsed,
                                note="parsed-events request failed")
        return self._ok("idl", body, elapsed)

    def get_idl(self, program_id, slot, reason):
        """The IDL in effect for a program at a slot."""
        guard = self._guard(reason)
        if guard is not None:
            return guard
        kind, body, elapsed, _status = self._request(
            "GET", "/idl/%s/%s" % (program_id, slot))
        if kind != "ok":
            return self._result(kind, None, elapsed,
                                note="parsed-events request failed")
        return self._ok("idl", body, elapsed)

    # -- verification ---------------------------------------------------
    def verify(self, event, raw_tx, decoder=None):
        """Verify one parsed event against the own decoder. Thin
        wrapper over helius_streams.verify_with_own_decoder so API
        and stream consumers share the verdict."""
        return verify_with_own_decoder(event, raw_tx, decoder=decoder)

    def stats(self):
        return {"provider": "helius", "api": "parsed_events",
                "configured": self.configured(),
                "calls": self.calls,
                "credits_spent": self.credits_spent,
                "credits_per_request": CREDITS_PER_REQUEST,
                "persistent": self.persistent_stats()}
