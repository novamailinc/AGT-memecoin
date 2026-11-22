"""Voided-closes list for the AGT status dashboard (Phase 13 finalize).

A voided close is a ledger record whose exit price was bad — a
phantom win that must never appear in any total. The live ledgers
are NEVER edited to "fix" a balance: the void lives in the
checked-in file ``voided_closes.json`` next to this module, and the
dashboard applies it at READ time, showing raw vs void-excluded
totals side by side, labelled.

Matching: (lane, mint), and close_ts when the void entry carries
one (a mint that legitimately re-enters later must not be voided
by an old entry — grad lanes dedupe-ever, but the ts guard costs
nothing). A close matches when its mint + lane match and either
the entry has no close_ts or the timestamps agree to within 1s.

Live10 funding: the Live10 ledger carries per-lane balances, not
per-close records, so a funded phantom cannot be recomputed from
it. Each void entry therefore records ``funded_pnl_usd``
explicitly — the funded amount the phantom contributed (paper pnl
x the Live10 scale in force at the time; 0.0 when the lane was
unfunded). The dashboard subtracts it from that lane's Live10
balance/net and decrements its funded close count by one.

Stdlib only. No I/O at import time; the file is read on demand.
"""

from __future__ import annotations

import json
import os

DEFAULT_VOID_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "voided_closes.json")


def load_voided(path=None):
    """Load the void list. Returns (entries, error).

    entries: list of void dicts. error: None on success, else a
    human-readable reason (missing/unparsable file). A load error
    is NEVER silent: callers must report it and treat the list as
    empty only with the error shown — a silently dropped void list
    would re-inflate the totals.
    """
    path = path or DEFAULT_VOID_FILE
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], "void file absent: %s" % path
    except (OSError, ValueError) as exc:
        return [], "void file unreadable %s: %s" % (path, exc)
    entries = data.get("voided") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return [], "void file has no 'voided' list: %s" % path
    return entries, None


def void_index(entries):
    """Index void entries by (lane, mint) for O(1) lookup."""
    index = {}
    for entry in entries:
        mint = entry.get("mint")
        lane = entry.get("lane")
        if mint and lane:
            index.setdefault((lane, mint), []).append(entry)
    return index


def find_void(index, lane, mint, close_ts=None):
    """The void entry matching (lane, mint, close_ts), or None."""
    if not mint:
        return None
    for entry in index.get((lane, mint), []):
        entry_ts = entry.get("close_ts")
        if entry_ts is None or close_ts is None:
            return entry
        try:
            if abs(float(entry_ts) - float(close_ts)) <= 1.0:
                return entry
        except (TypeError, ValueError):
            return entry
    return None


def is_voided(index, lane, mint, close_ts=None):
    return find_void(index, lane, mint, close_ts) is not None


def filter_closes(index, lane, closes):
    """Split closes into (kept, voided) by the void list."""
    kept = []
    voided = []
    for close in closes or []:
        entry = find_void(index, lane, close.get("mint"),
                          close.get("close_ts"))
        if entry is None:
            kept.append(close)
        else:
            voided.append((close, entry))
    return kept, voided


def entry_has_close(entry, closes):
    """True when `closes` contains a close matching the void
    entry (same mint, close_ts within 1s when both carry one)."""
    mint = entry.get("mint")
    entry_ts = entry.get("close_ts")
    for close in closes or []:
        if close.get("mint") != mint:
            continue
        close_ts = close.get("close_ts")
        if entry_ts is None or close_ts is None:
            return True
        try:
            if abs(float(entry_ts) - float(close_ts)) <= 1.0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def find_void_by_pnl_ts(entries, lane, pnl, ts, pnl_tol=0.01,
                        ts_tol=5.0):
    """Match a void entry to a killswitch window trade, which
    carries no mint — only (ts, pnl). The killswitch records a
    close ~1s after the close_ts, with the pnl to the cent, so a
    (lane, pnl within $0.01, ts within 5s) match is
    essentially collision-proof: it would need another close in
    the same lane, within 5 seconds, with the same pnl to the
    cent. Returns the void entry or None."""
    for entry in entries:
        if entry.get("lane") != lane:
            continue
        paper = entry.get("paper_pnl_usd")
        entry_ts = entry.get("close_ts")
        if paper is None or entry_ts is None:
            continue
        try:
            if abs(float(paper) - float(pnl)) > pnl_tol:
                continue
            if abs(float(entry_ts) - float(ts)) > ts_tol:
                continue
        except (TypeError, ValueError):
            continue
        return entry
    return None


def funded_void_for_lane(entries, lane, closes=None):
    """Total funded phantom USD + voided funded-close count for a
    Live10 lane — counting ONLY void entries whose close is
    actually present in that lane's stored closes.

    The check matters: the void list is global (checked in with
    the deploy), but a home whose ledgers never contained the
    phantom close must NOT have its Live10 balance adjusted for
    it. When `closes` is None (the lane ledger could not be
    read), nothing is adjusted — the raw total stands and the
    caller must say so. Only entries flagged ``funded: true``
    count: a lane that was unfunded at the time (e.g. grad_raw
    below the $10 floor) contributed no funded phantom, and its
    funded close count must not move. (Paper PnL voids are
    handled per close above; the Live10 ledger has no per-close
    records, so the funded amounts come from the void entries
    themselves.)"""
    total = 0.0
    count = 0
    if closes is None:
        return 0.0, 0
    for entry in entries:
        if entry.get("lane") != lane:
            continue
        if not entry.get("funded"):
            continue
        if not entry_has_close(entry, closes):
            continue
        funded = entry.get("funded_pnl_usd")
        if funded is None:
            continue
        try:
            total += float(funded)
        except (TypeError, ValueError):
            continue
        count += 1
    return round(total, 2), count
