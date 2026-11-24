#!/usr/bin/env python3
"""Jupiter Price API v3 client for EXIT pricing (2026-11-17).

Free, no key, batchable: GET https://api.jup.ag/price/v3?ids=<mint,...>
returns {"<mint>": {"usdPrice": <float>, ...}, ...} per mint.

Why this exists: exit pricing for graduated tokens used to ride the
Helius-backed paths (direct pool reserves via reserves.py, exempt
from the SharedGuard but still burning the spent Helius quota, and
the armed watcher's Helius WSS push). With the quota spent
("max usage reached") those legs fail, so exits go dark. Jupiter
v3 prices graduated (Raydium/PumpSwap-listed) tokens without any
Helius call, and it is verified live from this VM (HTTP 200,
SOL ~$73.8, USDC ~$0.9996).

Contract:
  * stdlib urllib only, no new dependency; urllib honours the
    egress proxy from the environment, like every other fetcher
    in this codebase.
  * Batch: one GET covers up to MAX_BATCH mints; callers pass the
    whole open-position set and this module chunks it.
  * Short TTL cache (CACHE_TTL_S) keyed by mint: exit polls run
    every ~5s, so most polls are served from cache with zero HTTP.
  * Fail-OPEN to the caller's existing path on ANY error (timeout,
    non-200, malformed JSON, mint missing from the response): every
    public function never raises and a missing mint is simply
    absent from the returned dict, so the caller falls through to
    DexScreener / Helius reserves exactly as before.
  * Graduated tokens ONLY: a pre-graduation bonding-curve token is
    not on Jupiter, so v3 returns no entry for it and the caller
    keeps its Helius curve path. This module never invents a price
    (0.0 / absent means "unknown", never a fabricated quote).
  * The v2 price URL is DEAD (404) — v3 only, never v2.

Paper only. Nothing here touches real money, keys, or wallets.
"""

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.jup_price")

PRICE_V3_URL = "https://api.jup.ag/price/v3"
CACHE_TTL_S = 12.0
MAX_BATCH = 50
TIMEOUT_S = 6.0
SOURCE = "jup-v3"

_cache = {}  # mint -> (price_usd, fetched_ts)


def parse_prices(payload):
    """{mint: usdPrice>0} from a v3 response body. Pure; never raises.

    Unknown / malformed / zero / negative entries are dropped, never
    coerced into a price: a dropped mint falls back to the caller's
    existing source.
    """
    out = {}
    if not isinstance(payload, dict):
        return out
    for mint, row in payload.items():
        if not isinstance(mint, str) or not mint:
            continue
        if not isinstance(row, dict):
            continue
        try:
            px = float(row.get("usdPrice") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            out[mint] = px
    return out


def _fetch_batch(mints):
    """One v3 GET for up to MAX_BATCH mints. {} on any failure.

    Never raises. Only the host is logged, never the URL (the URL
    carries mint ids only, no credentials, but there is nothing to
    gain from logging it).
    """
    try:
        url = PRICE_V3_URL + "?ids=" + ",".join(mints)
        req = urllib.request.Request(
            url, headers={"User-Agent": "forex-agent/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            if r.status != 200:
                logger.warning("jup price v3: HTTP %s", r.status)
                return {}
            return parse_prices(json.loads(r.read().decode()))
    except Exception as e:
        logger.warning("jup price v3 fetch failed: %s", type(e).__name__)
        return {}


def get_prices_observed(mints, now=None):
    """{mint: (usdPrice, observed_ts)} for the requested mints.
    Never raises.

    Same cache/fetch behavior as get_prices, but each price carries
    its ACTUAL observation time: the cache's fetched_ts for a cache
    hit, the fetch time for a fresh read. A caller that stamps
    freshness (account marks) must use this so a cached price can
    never masquerade as just-observed.
    """
    now = time.time() if now is None else now
    out = {}
    try:
        wanted = []
        for m in mints or []:
            if isinstance(m, str) and m and m not in wanted:
                wanted.append(m)
        missing = []
        for m in wanted:
            hit = _cache.get(m)
            if hit is not None and now - hit[1] <= CACHE_TTL_S \
                    and hit[0] > 0:
                out[m] = (hit[0], hit[1])
            else:
                missing.append(m)
        for i in range(0, len(missing), MAX_BATCH):
            fresh = _fetch_batch(missing[i:i + MAX_BATCH])
            for m, px in fresh.items():
                _cache[m] = (px, now)
                out[m] = (px, now)
    except Exception:
        pass
    return out


def get_prices(mints, now=None):
    """{mint: usdPrice} for the requested mints. Never raises.

    Fresh cache entries (<= CACHE_TTL_S old) are served without HTTP;
    only stale/missing mints are fetched, in MAX_BATCH chunks. A mint
    the API has no entry for (e.g. a pre-graduation curve token) is
    simply absent — the caller falls back to its existing source.
    """
    return {m: px for m, (px, _ts)
            in get_prices_observed(mints, now=now).items()}


def get_price(mint, now=None):
    """Single-mint price in USD; 0.0 when unknown. Never raises."""
    try:
        return float(get_prices([mint], now=now).get(mint) or 0)
    except Exception:
        return 0.0


def clear_cache():
    """Empty the price cache (tests; never needed in production)."""
    _cache.clear()
