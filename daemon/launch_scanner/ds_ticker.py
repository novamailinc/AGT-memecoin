#!/usr/bin/env python3
"""DexScreener ticker search client for the copycat tally (2026-11-17).

Free, no key: GET https://api.dexscreener.com/latest/dex/search?q=<ticker>
returns {"pairs": [...]} with chainId, baseToken {address, symbol},
fdv, and pairCreatedAt (ms epoch). Verified live from this VM
(HTTP 200, solana pairs, FDV present).

Why this exists: the copycat filter (grad_select Filter 4) rejects a
candidate when >= 3 OTHER coins with the same ticker launched in the
trailing 24h, failing closed on unknown. Its source, the
saturation.py tally, is fed by the pump.fun frontend API, which
returns 403 (Cloudflare) since 2026-11-06 — the tally is stale and
every candidate reaching the filter fails closed as unknown_copycat.
This module is the ALTERNATIVE source: resolve a ticker to its
highest-FDV Solana mint and count same-ticker Solana launches in
the trailing 24h, straight from DexScreener search, with no Helius
call and no pump.fun call. grad_select prefers the exact
saturation/recorder census while it is fresh and consults this
module only when that tally is stale (or the saturation module is
unavailable).

Contract:
  * Transport is curl-first, urllib fallback (no new dependency):
    the VM's urllib-through-proxy path intermittently truncates
    large responses — the same quirk saturation.py documents —
    so curl goes first exactly as saturation._fetch_page does.
  * Solana only: chainId == "solana". Exact symbol match,
    case-insensitive ("BONK" matches "bonk", never "BONK2").
  * Resolution: highest-FDV matching mint. A tie at the top
    between different mints (including an all-zero-FDV tie) is
    AMBIGUOUS -> None, never a guess.
  * Counting: trailing-24h window (now-24h, now], same half-open
    convention as saturation.rolling_count. ``others`` counts
    matching pairs EXCLUDING the candidate's own mint when it is
    among the matches; when the candidate is not indexed yet,
    every match is an other. The reject bar (>= 3 others) is
    applied by the caller, never here.
  * FAIL-CLOSED: every public function never raises. A fetch
    failure (timeout, non-200, malformed JSON), a blank symbol,
    or zero usable matching pairs returns None — the caller
    treats None as UNKNOWN and rejects as unknown_copycat. A
    count of 0 others is only ever returned when DexScreener
    positively shows the ticker (at least one matching pair) and
    no other in-window launch exists. Never 0 from an error.
  * Dedupe cache: same ticker searched repeatedly inside
    CACHE_TTL_S returns the cached pair list with zero HTTP.
    Failed lookups are cached briefly (FAIL_TTL_S) so a dark
    endpoint is not hammered.
  * Rate guard: at most one outbound search per MIN_INTERVAL_S
    and at most HOURLY_CAP searches per rolling hour. A call
    that hits the guard returns None (UNKNOWN -> caller fails
    closed) instead of queuing or sleeping — the entry path must
    never block on this module.
  * Honest limit: the search endpoint returns a bounded result
    set, ranked by relevance, not by recency. The count is exact
    over the pairs returned; a reject (>= 3 others seen) is
    conclusive, while a pass below the bar assumes the search
    surface covers the ticker's recent pairs. The saturation
    census stays the preferred source for exactly this reason.

Paper only. Nothing here touches real money, keys, or wallets.
"""

import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.ds_ticker")

SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
TIMEOUT_S = 5.0

CACHE_TTL_S = 120.0       # positive cache: same ticker, 2 min
FAIL_TTL_S = 60.0         # negative cache: dark endpoint, 1 min
MIN_INTERVAL_S = 1.0      # at most one outbound search per second
HOURLY_CAP = 600          # at most 600 outbound searches per rolling hour
WINDOW_S = 86400          # trailing-24h copycat window

_cache = {}               # norm symbol -> (fetched_ts, [pairs] or None)
_last_fetch_ts = 0.0
_hour_stamps = []         # fetch timestamps inside the rolling hour


def norm_symbol(symbol):
    """Canonical ticker: upper, no leading $, trimmed. Same rule as
    saturation._norm so the two sources always agree."""
    return str(symbol or "").strip().upper().lstrip("$").strip()


def parse_search(payload):
    """Solana pairs from a search response body. Pure; never raises.

    Returns [{mint, symbol, fdv, created_ms}] — one entry per
    solana pair with a usable mint. ``fdv`` is 0.0 when absent;
    ``created_ms`` is None when pairCreatedAt is absent/invalid
    (such a pair can resolve a ticker but never counts into a
    24h window).
    """
    out = []
    try:
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not isinstance(pairs, list):
            return out
        for p in pairs:
            if not isinstance(p, dict):
                continue
            if p.get("chainId") != "solana":
                continue
            base = p.get("baseToken") or {}
            mint = str(base.get("address") or "") if isinstance(
                base, dict) else ""
            if not mint:
                continue
            try:
                fdv = float(p.get("fdv") or 0)
            except (TypeError, ValueError):
                fdv = 0.0
            if fdv < 0:
                fdv = 0.0
            try:
                created_ms = int(p.get("pairCreatedAt"))
            except (TypeError, ValueError):
                created_ms = None
            out.append({
                "mint": mint,
                "symbol": str(base.get("symbol") or ""),
                "fdv": fdv,
                "created_ms": created_ms,
            })
    except Exception:
        return out
    return out


def matching_pairs(payload, symbol):
    """Exact-symbol Solana matches, highest FDV first. Pure.

    Case-insensitive exact match on baseToken.symbol; pairs whose
    symbol is missing/blank never match. Sorted by fdv desc.
    """
    sym = norm_symbol(symbol)
    if not sym:
        return []
    try:
        matches = [p for p in parse_search(payload)
                   if norm_symbol(p.get("symbol")) == sym]
    except Exception:
        return []
    matches.sort(key=lambda p: p["fdv"], reverse=True)
    return matches


def _throttle(now):
    """Rate guard: True when an outbound search may proceed now.

    Records the slot on success. False when inside
    MIN_INTERVAL_S of the last fetch or at HOURLY_CAP fetches in
    the rolling hour — the caller then returns UNKNOWN instead
    of fetching. Pure bookkeeping on module state; never raises.
    """
    global _last_fetch_ts
    try:
        _hour_stamps[:] = [t for t in _hour_stamps
                           if now - t < 3600.0]
        if len(_hour_stamps) >= HOURLY_CAP:
            return False
        if now - _last_fetch_ts < MIN_INTERVAL_S:
            return False
        _last_fetch_ts = now
        _hour_stamps.append(now)
        return True
    except Exception:
        return False


def _fetch_search(symbol):
    """One DexScreener search GET. Parsed JSON dict, or None on any
    failure (timeout, non-200, malformed JSON). Never raises. Only
    the host/status is logged, never the query URL.

    Transport mirrors saturation.py: curl first, urllib fallback.
    The VM's urllib-through-proxy path intermittently truncates
    large responses (IncompleteRead — the same quirk sources.py
    documents); a BONK search returns a large body, so urllib
    alone fails it live while curl succeeds.
    """
    try:
        url = SEARCH_URL + "?q=" + urllib.parse.quote(symbol, safe="")
        raw = None
        curl = shutil.which("curl")
        if curl:
            try:
                out = subprocess.run(
                    [curl, "-sS", "--compressed", "--max-time",
                     str(TIMEOUT_S), "-H", "User-Agent: forex-agent/1.0",
                     url], capture_output=True,
                    timeout=TIMEOUT_S + 3)
                if out.returncode == 0 and out.stdout:
                    raw = out.stdout
            except Exception:
                raw = None
        if raw is None:
            req = urllib.request.Request(
                url, headers={"User-Agent": "forex-agent/1.0"})
            with urllib.request.urlopen(req,
                                        timeout=TIMEOUT_S) as r:
                if r.status != 200:
                    logger.warning("dexscreener search: HTTP %s",
                                   r.status)
                    return None
                raw = r.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("dexscreener search failed: %s",
                       type(e).__name__)
        return None


def search_ticker(symbol, now=None):
    """Matching Solana pairs for a ticker, highest FDV first.

    Returns the list (possibly empty only when the fetch succeeded
    but DexScreener returned no usable pairs — callers treat an
    empty list as UNKNOWN), or None when the lookup itself failed
    (fetch error, rate guard, blank symbol). Cache + rate guard
    applied; never raises.
    """
    now = time.time() if now is None else now
    sym = norm_symbol(symbol)
    if not sym:
        return None
    try:
        hit = _cache.get(sym)
        if hit is not None:
            fetched_ts, pairs = hit
            ttl = CACHE_TTL_S if pairs else FAIL_TTL_S
            if now - fetched_ts < ttl:
                return pairs
        if not _throttle(now):
            logger.warning("dexscreener search throttled: %s", sym)
            return None
        payload = _fetch_search(sym)
        if payload is None:
            _cache[sym] = (now, None)
            return None
        pairs = matching_pairs(payload, sym)
        _cache[sym] = (now, pairs)
        return pairs
    except Exception:
        return None


def resolve_mint(symbol, now=None):
    """Highest-FDV Solana mint for a ticker, or None (UNKNOWN).

    None when the lookup failed, when DexScreener shows no
    matching pair, or when the top-FDV spot is tied between
    different mints (ambiguous — never a guess). Never raises.
    """
    try:
        pairs = search_ticker(symbol, now=now)
        if not pairs:
            return None
        top = pairs[0]
        if len(pairs) > 1 and pairs[1]["fdv"] == top["fdv"] \
                and pairs[1]["mint"] != top["mint"]:
            return None  # ambiguous top: two mints share max FDV
        return top["mint"]
    except Exception:
        return None


def count_24h(symbol, now=None, self_mint=None):
    """Same-ticker Solana launches in the trailing 24h.

    Returns {"tally": int, "others": int, "top_mint": str|None,
    "self_seen": bool, "pairs_seen": int} or None when UNKNOWN
    (fetch failed, rate guard, blank symbol, or DexScreener shows
    no matching pair at all — the ticker may simply not be
    indexed yet, which is unknown, not zero). ``tally`` is the
    in-window matching-pair count; ``others`` excludes the
    candidate's own mint when it is among the matches; ``top_mint``
    is the highest-FDV matching mint over ALL ages (None when the
    top is ambiguous); ``pairs_seen`` is the total matching-pair
    count over all ages. Never raises.
    """
    now = time.time() if now is None else now
    try:
        pairs = search_ticker(symbol, now=now)
        if not pairs:
            return None
        now_ms = int(now * 1000)
        cutoff_ms = now_ms - int(WINDOW_S * 1000)
        in_window = [p for p in pairs
                     if p["created_ms"] is not None
                     and cutoff_ms < p["created_ms"] <= now_ms]
        self_seen = bool(self_mint) and any(
            p["mint"] == self_mint for p in in_window)
        tally = len(in_window)
        others = tally - (1 if self_seen else 0)
        top_mint = None
        if len(pairs) == 1:
            top_mint = pairs[0]["mint"]
        elif pairs[1]["fdv"] != pairs[0]["fdv"] \
                or pairs[1]["mint"] == pairs[0]["mint"]:
            top_mint = pairs[0]["mint"]
        return {"tally": tally, "others": others,
                "top_mint": top_mint, "self_seen": self_seen,
                "pairs_seen": len(pairs)}
    except Exception:
        return None


def clear_cache():
    """Drop the dedupe cache and rate-guard state (tests)."""
    global _last_fetch_ts
    _cache.clear()
    _hour_stamps[:] = []
    _last_fetch_ts = 0.0
