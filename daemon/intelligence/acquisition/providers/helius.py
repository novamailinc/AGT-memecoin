"""Helius provider adapter (Phases 3-4).

Live leg TODAY. READ-ONLY use of the existing transport in
``daemon/launch_scanner/reserves.py`` — this adapter adds the
(result, kind, elapsed) contract around it and NOTHING else: no
re-implementation of Helius pacing, no second pacer, no key handling
here. If the reserves module cannot be imported, the adapter reports
unconfigured rather than failing.

KNOWN LIMIT (Phase 2, unchanged): ``reserves.rpc`` returns None on
every failure class without distinguishing them, so this adapter
cannot tell a Helius 429 from a timeout — kind "error" is honest
here; the live path's own logs carry the per-endpoint detail.
Fixing that means editing the live transport, which this program
does not touch.

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
            out = reserves.rpc(method, params, timeout=timeout)
        except Exception:
            logger.exception("helius adapter: reserves.rpc raised (%s)",
                             method)
            return _result(None, "error", time.monotonic() - start,
                           self.name)
        elapsed = time.monotonic() - start
        if out is None:
            # reserves.rpc returns None on every failure class without
            # distinguishing them; kind "error" is honest here — the
            # live path's own logs carry the per-endpoint detail.
            return _result(None, "error", elapsed, self.name,
                           note="reserves.rpc returned None")
        return _result(out, "ok", elapsed, self.name)
