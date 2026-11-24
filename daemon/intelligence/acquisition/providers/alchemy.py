"""Alchemy provider adapter (Phases 3-4).

END-STATE primary routine/live acquisition (initial allocation, to
be validated by benchmarks) + deep-history retrieval on Free (full
archive back to genesis — the evidence-based correction: Chainstack
Developer has no archive data).

Facts (capability audit 2026-11-22, from Alchemy's own docs):
- Free: 30M CU/month, 300 CU/s account-level (10s rolling window,
  ~15 req/s; "25 RPS" is a secondary claim, not official).
- Per-method CU: account reads 10; getSlot / getProgramAccounts /
  getMultipleAccounts / send / simulate 20; getTransaction / getBlock /
  getSignaturesForAddress 40. getTokenLargestAccounts CU UNVERIFIED.
- WSS: all 6 core subscriptions on a different host from HTTPS.

LIVE STATUS (corrected 2026-11-22 ~02:02 PST): live verification
from this VM is NOT possible through the surrogate helpers, and the
keys may be wrong after all — do NOT declare them correct until a
path-segment test proves it. What changed: the query-param swap
was PROVEN working (Helius vault getHealth returned HTTP 200,
result ok), superseding the Nov 18 BLOCKED status for that
placement — which narrows the problem to the url_path_segment swap
(Alchemy + Chainstack), still UNPROVEN. Alchemy's surrogate never
changed (ad0ba3 both times), so its stored value likely never
changed; Chainstack's value DID change and still 401s, so either
the path-segment swap is broken or the Chainstack node/key is
inactive or wrong. Next checks are console-side (Chainstack node
active; Alchemy key from a Solana-enabled app, just the key) —
not another re-entry loop. Live verification is DEFERRED to the
AGT target, where the key will be a real env/file value. This
adapter is verified here with mocked transport + recorded fixtures
only.
"""

from __future__ import annotations

from .base import _HttpAdapter, alchemy_url, alchemy_wss_url


class AlchemyAdapter(_HttpAdapter):
    name = "alchemy"
    account = "alchemy"

    def configured(self):
        return alchemy_url(home=self.home) is not None

    def _url(self):
        return alchemy_url(home=self.home)

    def wss_url(self):
        return alchemy_wss_url(home=self.home)
