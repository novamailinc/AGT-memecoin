#!/usr/bin/env python3
"""Venue / pool selection for the paper-only Solana memecoin bot
(2026-11-21, advanced build).

Pure classification, stdlib only. Given a candidate pool and
whatever provenance the caller has for it — the migration pool
from the migration event / recorder / chain fallback, the
canonical basis stamped by grad_select.CanonicalPoolCheck, the
dex label, and the mint's bonding-curve address — answer which
venue this pool belongs to and whether the pool is VERIFIED.

"Verified" here means a verified VENUE: the pool matches its
provenance AND the venue is a known one. Both halves are
necessary:
  * the pool must match the migration pool (migration_pool ==
    pool, non-empty) or carry canonical_basis "migration_event"
    with a matching pool — the stream case, where the pool
    itself comes from the migration event (migration_pool may
    be absent; when it is present it must still match, a
    mismatch beats the basis);
  * the venue must be identifiable from the dex label or the
    curve address — an unrecognised dex leaves the venue
    "unknown" and verified False even for a matched pool.

Fail-closed: an empty pool, or a pool that matches neither the
migration pool nor the canonical basis, returns venue "unknown",
verified False, reason "unverified_pool" — regardless of what
the dex label claims. An unverified pool is never priced, never
entered, and never reported as a venue. This module never
fetches anything: the migration tx is fetched by the caller's
entry path (grad_select fetches it for fresh stream candidates)
and the canonical pool by _canonical_pool_from_chain for
backfill candidates; this module only classifies what it is
handed.

Canonical basis values (grad_select.CanonicalPoolCheck):
"migration_event" (stream), "recorder_migration_match" /
"chain_migration_match" (backfill, pool confirmed against the
migration tx), "recorder_migration_mismatch" /
"chain_migration_mismatch", "chain_unresolvable",
"unverifiable". NOTE: only "migration_event" verifies on the
basis alone. A "recorder_migration_match" /
"chain_migration_match" basis verifies ONLY when the caller
also passes the matching migration_pool — the match is the
evidence, the basis string is not. A backfill caller that has
the basis but not the pool should pass migration_pool through
from the recorder / chain verdict, or the pool stays
unverified (fail-closed, safe direction).

Dex labels are normalised (lowercased, non-alphanumerics
stripped) so "Pump.fun", "pumpfun", "PumpSwap", "bonding-curve"
and "Raydium" all match. The bonding-curve address check (pool
== curve_address) identifies the pre-graduation venue even
when the dex label is absent or wrong — the curve PDA is
derived from the mint, so the pool IS the curve.

Contract:
  * Pure, stdlib only, no I/O, no network.
  * Every public function never raises.
  * Fail-closed on any bad input: non-dict, empty pool, or no
    matching provenance -> {"venue": "unknown", "verified":
    False, "reason": "unverified_pool"}.

Paper only. Nothing here touches real money, keys, or wallets.
"""

# Canonical-basis values that verify a pool on their own (with a
# non-empty pool and no contradicting migration_pool). Only the
# stream basis qualifies — see the module docstring.
_BASIS_VERIFIES = ("migration_event",)


def _str(v):
    """str(v) stripped, or "" — never raises."""
    try:
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        return str(v).strip()
    except Exception:
        return ""


def _norm_dex(v):
    """Dex label normalised for matching: lowercased, only
    alphanumerics kept. "Pump.fun" -> "pumpfun",
    "bonding-curve" -> "bondingcurve". Never raises."""
    try:
        return "".join(ch for ch in _str(v).lower()
                       if ch.isalnum())
    except Exception:
        return ""


def classify_venue(pool_data):
    """Classify one pool's venue and verification.

    ``pool_data`` is {"pool", "dex", "migration_pool",
    "canonical_basis", "curve_address"} (all optional except
    "pool"). Returns {"venue", "verified", "reason"} with venue
    in pumpfun_curve | pumpswap | raydium | unknown. Never
    raises."""
    try:
        d = pool_data if isinstance(pool_data, dict) else {}
        pool = _str(d.get("pool"))
        if not pool:
            return {"venue": "unknown", "verified": False,
                    "reason": "unverified_pool"}
        migration_pool = _str(d.get("migration_pool"))
        basis = _str(d.get("canonical_basis"))
        curve_address = _str(d.get("curve_address"))

        # Fail-closed gate: the pool must match its provenance.
        pool_matches_migration = bool(migration_pool) and \
            migration_pool == pool
        basis_verifies = basis in _BASIS_VERIFIES and \
            (not migration_pool or migration_pool == pool)
        if not pool_matches_migration and not basis_verifies:
            return {"venue": "unknown", "verified": False,
                    "reason": "unverified_pool"}
        how = "migration_match" if pool_matches_migration \
            else "migration_event"

        # Venue: the curve address wins over the dex label (the
        # pool IS the bonding curve), then the dex label.
        if curve_address and pool == curve_address:
            return {"venue": "pumpfun_curve", "verified": True,
                    "reason": how}
        dex = _norm_dex(d.get("dex"))
        if dex in ("pumpfun", "bondingcurve"):
            return {"venue": "pumpfun_curve", "verified": True,
                    "reason": how}
        if dex == "pumpswap":
            return {"venue": "pumpswap", "verified": True,
                    "reason": how}
        if dex == "raydium":
            return {"venue": "raydium", "verified": True,
                    "reason": how}
        return {"venue": "unknown", "verified": False,
                "reason": "unknown_venue"}
    except Exception:
        return {"venue": "unknown", "verified": False,
                "reason": "unverified_pool"}
