"""Public (keyless) provider adapter (Phases 3-4).

TERTIARY FALLBACK ONLY — never a primary. Keyless Solana
Foundation endpoint, no account, no SLA. Repo budget: 1 call/2s
fleet-wide (enforced by the scheduler, not here).

Facts (capability audit 2026-11-22): the ``getTransaction`` 403
claim was DISPROVED by a live probe (HTTP 200, 0.698s, single
sample — not a load guarantee). Recent-only history; no published
archive depth. 429 / 403 (IP block) possible.
"""

from __future__ import annotations

from .base import PUBLIC_URL, PUBLIC_WSS_URL, _HttpAdapter


class PublicAdapter(_HttpAdapter):
    name = "public"
    account = "public"
    endpoint = PUBLIC_URL

    def configured(self):
        return True

    def _url(self):
        return PUBLIC_URL

    def wss_url(self):
        return PUBLIC_WSS_URL
