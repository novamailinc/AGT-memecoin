"""Chainstack provider adapter (Phases 3-4).

END-STATE role (validated by the capability audit): recent-read
verification + event recovery within the retention window +
independent verification. NOT deep history — Chainstack Developer
Free has NO archive data (``getSignaturesForAddress`` is always
archive-billed and returns ``-32002`` on Developer; Growth $49/mo
is the first tier with archive). Deep-history retrieval on the free
tier is Alchemy's job.

Facts (capability audit 2026-11-22, from Chainstack docs):
- Developer Free: 3M RU/month, 5 RPS on Solana mainnet (not the
  advertised 25 RPS general figure).
- 1 RU standard / 2 RU archive-class; ``getSignaturesForAddress``
  always archive (2 RU) — effectively unavailable on Developer.
- ``getProgramAccounts`` paid-only; ``getTokenLargestAccounts``
  docs conflict (methods page lists it; limits page says 0 RPS /
  Dedicated Nodes only) — do not rely on it.
- Full node retains a 400M-shred ledger; no published slot/time
  depth — how far back "recent" reaches is UNVERIFIED and can only
  be measured live.
- Nodes auto-delete after 30 days of inactivity.

LIVE STATUS (corrected 2026-11-22 ~02:02 PST): live verification
from this VM is NOT possible through the surrogate helpers, and the
key may be wrong after all — do NOT declare it correct until a
path-segment test proves it. What changed: the query-param swap
was PROVEN working (Helius vault getHealth returned HTTP 200,
result ok), superseding the Nov 18 BLOCKED status for that
placement — which narrows the problem to the url_path_segment swap
(Alchemy + Chainstack), still UNPROVEN. The stored Chainstack value
DID change on re-entry and still 401s, so either the path-segment
swap is broken or the Chainstack node/key is inactive or wrong.
Next check is console-side (node active in the console) — not
another re-entry loop. Live verification is DEFERRED to the AGT
target. This adapter is verified here with mocked transport +
recorded fixtures only.
"""

from __future__ import annotations

from .base import (
    _HttpAdapter, chainstack_endpoint, chainstack_wss_url)


class ChainstackAdapter(_HttpAdapter):
    name = "chainstack"
    account = "chainstack"

    def configured(self):
        return chainstack_endpoint(home=self.home) is not None

    def _url(self):
        return chainstack_endpoint(home=self.home)

    def wss_url(self):
        return chainstack_wss_url(home=self.home)
