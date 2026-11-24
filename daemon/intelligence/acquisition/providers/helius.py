"""Helius provider adapter (Phases 3-4).

Live leg TODAY. READ-ONLY use of the existing transport in
``daemon/launch_scanner/reserves.py`` — this adapter adds the
(result, kind, elapsed) contract around it and NOTHING else: no
re-implementation of Helius pacing, no second pacer, no key handling
here. If the reserves module cannot be imported, the adapter reports
unconfigured rather than failing.

NULL VS ERROR (2026-11-22, resolved): the transport now exposes
``reserves.rpc_typed`` alongside ``reserves.rpc``, so a JSON-null
answer (e.g. getTransaction for a not-yet-indexed signature) is
reported as kind "ok" with a null payload instead of being
conflated with an outage. Finer error classes (429 vs timeout)
remain collapsed into kind "error" — the live path's own logs
carry the per-endpoint detail.

Usage attribution: the reserves transport rotates across the
operator's Helius key sources internally (main disk key, discovery
disk key, vault surrogate when available); this layer cannot see
which key served a given call. Usage is therefore recorded against
the ``helius_main`` account with a rotation caveat in the usage
journal — per-key attribution is NOT visible from here, and no
second allowance is assumed (the two disk keys may belong to one
account; see the capability audit §1).
"""

from __future__ import annotations

import logging
import time

from .base import BaseProviderAdapter, _result

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.providers.helius")


class HeliusAdapter(BaseProviderAdapter):
    """Live leg, delegating to the existing reserves transport."""

    name = "helius"
    account = "helius_main"

    def configured(self):
        try:
            from daemon.launch_scanner import reserves
            return bool(reserves.rpc_url_pairs())
        except Exception:
            return False

    def wss_url(self):
        # The live transport owns the Helius keys; this layer cannot
        # build a WSS URL without handling key material. armed_watch
        # owns the Helius WSS URL. Return None (do not fabricate).
        return None

    def call(self, method, params, timeout=12):
        start = time.monotonic()
        try:
            from daemon.launch_scanner import reserves
        except Exception:
            return _result(None, "unconfigured", time.monotonic() - start,
                           self.name, note="reserves module unavailable")
        try:
            out, rkind = reserves.rpc_typed(method, params,
                                            timeout=timeout)
        except Exception:
            logger.exception("helius adapter: reserves.rpc_typed "
                             "raised (%s)", method)
            return _result(None, "error", time.monotonic() - start,
                           self.name)
        elapsed = time.monotonic() - start
        if rkind == "ok":
            return _result(out, "ok", elapsed, self.name)
        if rkind == "null":
            # The provider answered JSON null (e.g. getTransaction
            # for a not-yet-indexed signature): an ANSWER, not an
            # outage. Report kind "ok" with a null payload, the
            # same contract the Alchemy leg has always had; the
            # router never caches null payloads and callers
            # re-poll (2026-11-22 null-vs-error fix).
            return _result(None, "ok", elapsed, self.name,
                           note="reserves.rpc answered null")
        return _result(None, "error", elapsed, self.name,
                       note="reserves.rpc failed")
