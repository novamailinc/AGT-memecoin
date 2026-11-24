#!/usr/bin/env python3
"""X/Twitter signal feed — TEST-DATA-ONLY parser (Phase 3, 2026-11-17).

Pure parser: post dicts in, normalized candidates out (the sources.py
shape). NO network calls in this module, NO X API client, NO bearer
token, NO credential reads of any kind.

LIVE WIRING IS BLOCKED: nothing in this module may be connected to a
live X/Twitter source until the user supplies (a) the 5 trusted
accounts to follow and (b) his login through the secure entry. Until
then this parser runs only on fixture data via scripts/x-feed-paper-test.

Stdlib-only, never raises: a malformed post yields nothing for that
post (counted as malformed_skipped), never an exception.
"""
import re
from datetime import datetime, timezone

CHAIN = "solana"
SOURCE = "x-feed"

# Base58 alphabet (no 0, O, I, l). A Solana mint is 32-44 chars.
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# Boundary-aware: a 32-44-char base58 run that is NOT part of a longer
# base58 run (so a 45-char token is rejected outright, not truncated).
_MINT_RE = re.compile(r"(?<![%s])[%s]{32,44}(?![%s])"
                      % (_B58, _B58, _B58))
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,19})")


def _is_mint(s):
    """True for a plausible Solana mint address (base58, 32-44 chars)."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    return 32 <= len(s) <= 44 and all(c in _B58 for c in s)


def extract_mints(text):
    """All base58 32-44-char tokens in text, in order, deduped.

    A token is accepted only if the whole token is base58 of the right
    length — a too-short string or one containing 0/O/I/l never
    matches. Never raises."""
    try:
        if not isinstance(text, str):
            return []
        seen = set()
        out = []
        for m in _MINT_RE.findall(text):
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out
    except Exception:
        return []


def extract_cashtags(text):
    """$TICKER cashtags in text, upper-cased, in order, deduped.

    A cashtag starts with a letter ($123 is not a ticker). Never
    raises."""
    try:
        if not isinstance(text, str):
            return []
        seen = set()
        out = []
        for t in _CASHTAG_RE.findall(text):
            t = t.upper()
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out
    except Exception:
        return []


def parse_created_at(value):
    """ISO-8601 string or epoch number -> epoch seconds (float).

    0.0 when unknown or unparseable. Never raises."""
    try:
        if value is None:
            return 0.0
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, (int, float)):
            v = float(value)
            return v if v > 0 else 0.0
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return 0.0
            if s.replace(".", "", 1).replace("-", "", 1).isdigit():
                v = float(s)
                return v if v > 0 else 0.0
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        return 0.0
    except Exception:
        return 0.0


def _int(v):
    try:
        if isinstance(v, bool):
            return 0
        return int(v or 0)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def _candidate(mint, symbol, cashtags, post, created_ts):
    """One normalized candidate in the sources.py shape."""
    extra = {
        "author": str(post.get("author") or ""),
        "likes": _int(post.get("likes")),
        "reposts": _int(post.get("reposts")),
        "replies": _int(post.get("replies")),
        "cashtags": list(cashtags),
        "duplicates": 0,
    }
    return {
        "mint": mint,
        "symbol": symbol or "",
        "name": "",
        "chain": CHAIN,
        "source": SOURCE,
        "created_ts": created_ts,
        "usd_market_cap": 0.0,
        "extra": extra,
        "url": str(post.get("url") or ""),
    }


def parse_posts(posts):
    """Parse post dicts into normalized candidates.

    posts: iterable of dicts with `author`, `text`, `created_at`,
    `likes`/`reposts`/`replies`, `url`. Returns (candidates, stats):

    - one candidate per distinct mint found (a post naming two mints
      yields two candidates); a post with no mint but a $TICKER yields
      one cashtag-only candidate (mint None);
    - dedupe by mint across posts (first post wins; later duplicates
      are dropped and counted in the winner's extra["duplicates"]);
      cashtag-only candidates dedupe by cashtag the same way;
    - a malformed post (not a dict, or no usable text) yields nothing
      and is counted in stats["malformed_skipped"];
    - a post with neither a mint nor a cashtag yields nothing.

    stats: {"posts_in", "candidates", "with_mint", "cashtag_only",
    "duplicates_dropped", "malformed_skipped"}. Never raises.
    """
    candidates = []
    stats = {"posts_in": 0, "candidates": 0, "with_mint": 0,
             "cashtag_only": 0, "duplicates_dropped": 0,
             "malformed_skipped": 0}
    try:
        items = list(posts or [])
    except Exception:
        return [], stats
    seen_mints = {}
    seen_tags = {}
    for post in items:
        stats["posts_in"] += 1
        try:
            if not isinstance(post, dict):
                stats["malformed_skipped"] += 1
                continue
            text = post.get("text")
            if not isinstance(text, str) or not text.strip():
                stats["malformed_skipped"] += 1
                continue
            created_ts = parse_created_at(post.get("created_at"))
            mints = extract_mints(text)
            tags = extract_cashtags(text)
            if mints:
                for mint in mints:
                    if mint in seen_mints:
                        seen_mints[mint]["extra"]["duplicates"] += 1
                        stats["duplicates_dropped"] += 1
                        continue
                    cand = _candidate(mint, tags[0] if tags else "",
                                      tags, post, created_ts)
                    seen_mints[mint] = cand
                    candidates.append(cand)
                    stats["with_mint"] += 1
            elif tags:
                tag = tags[0]
                if tag in seen_tags:
                    seen_tags[tag]["extra"]["duplicates"] += 1
                    stats["duplicates_dropped"] += 1
                    continue
                cand = _candidate(None, tag, tags, post, created_ts)
                seen_tags[tag] = cand
                candidates.append(cand)
                stats["cashtag_only"] += 1
            # else: no mint, no cashtag — noise, yields nothing.
        except Exception:
            stats["malformed_skipped"] += 1
            continue
    stats["candidates"] = len(candidates)
    return candidates, stats
