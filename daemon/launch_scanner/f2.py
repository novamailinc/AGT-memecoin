"""F2 — sustained-unquotability exit for ARMED positions (2026-10-23,
user-approved decision; exit-leakage audit fix F2).

The defect (audit root cause #2): in every ladder engine's
_poll_exits, a quote of 0 means "skip this cycle" — no exit is
evaluated. A dying pool is precisely the one that stops returning
quotes, so an armed position (peak >= 1.5x, leg A banked) sat
indefinitely until the first post-rug dust print appeared, and THAT
print became the exit (ledger holds closes at 0.0037x / 0.01x /
0.00x). The only escape was the 24h backstop, which finalizes at 0.

F2: when an ARMED position has had NO usable quote — across the
aggregator (DexScreener/GT, or the rt_sync tape built from them) AND
the direct pool-reserve price (reserves.py / the armed watcher's
pushed reserve price) — for a SUSTAINED period, finalize the
remaining legs at the LAST REAL OBSERVED PRICE (the last entry of
the position's per-eval quote journal, P1/F3) with the standard
honest-exit haircut, stamped as an F2 close.

The user's three safeguards, enforced here (single choke point —
the engines only call check() on a cycle where every pricing
source they have is dark, i.e. px <= 0 after all fallbacks):

1. ARMED ONLY. The position's recorded peak (tracked peak, leg-C
   peak, or realized leg exit prices — recorded prices only, never
   fabricated) must be >= 1.5x entry AND leg A must be banked
   (legs["a"]["done"]). A position that never armed keeps the
   pre-F2 behavior exactly, including the 24h unquotable backstop.
2. SUSTAINED SILENCE ONLY. The last real quote in the journal must
   be at least F2_SUSTAINED_S old at the moment of a dark cycle.
   F2_SUSTAINED_S = 300s: TapeHealth's STALE marker is 60s, but
   that is a tape-health signal, not an exit-grade silence —
   DexScreener republishes quiet pools irregularly and gaps of
   60-120s occur on live pools; the exit backstops poll every
   45-120s, so 300s means roughly 3-6 consecutive dark evaluations
   across every driver, never a one-cycle gap. It is still ~288x
   faster than the 24h status quo it replaces for armed positions.
3. STAMPED. check() returns the stamp dict the engines write into
   the close record (rec["f2"]) and the verdict-ledger close
   payload: last_real_price, last_quote_ts, unquotable_seconds.
   The close reason is F2_REASON in every leg + the record.

Dust guard: if the last real price is itself < 10% of entry, it is
not a price worth exiting at — check() refuses and the position
keeps waiting (existing paths). (Through normal evaluation a
sub-0.5x print fires the hard stop in the same cycle, so an open
position's last journal price is in practice > 0.5x entry; this
guard is belt-and-braces for journal anomalies.)

Upside guard (2026-10-24): symmetric to the dust guard — if the
last real price is > 50x entry (F2_MAX_LAST_MULT, the audit's
ledger-sanity bound), it is a bad print until proven otherwise.
The 2026-10-24 phantom banked +$16M off exactly such a row
(last_real_price 1.027e-4 vs a 4.73e-12 fiction entry = 21.7M-x).
check() flag-and-refuses: logs the refusal and returns None, so
the position keeps waiting (the 24h backstop still governs) and
the fiction is never banked. Real multi-hundred-x tails happen
over hours on a LIVE tape, where the ladder legs bank normally;
F2 only ever fires on a dead tape, where a >50x "last price" is
stale data, not a moon.

Chains: solana/base/ethereum only. Hyperliquid is excluded — it
has a single pricing source (HL marks), so "dark across both
sources" can never be established, and HL spot listings do not
rug-to-unquotable the way graduation pools do; HL positions keep
the 24h backstop. On base/ethereum there is no reserves module
(Solana-only), but quote_price itself consults two independent
aggregators (DexScreener, then GeckoTerminal), so a dark cycle
there means both aggregators failed to price the mint.

Pure logic, no I/O, never raises out of check().
"""

import logging

logger = logging.getLogger("forex_agent.daemon.launch_scanner.f2")

F2_REASON = "f2_unquotable_last_price"
F2_SUSTAINED_S = 300.0     # sustained-silence threshold (see header)
F2_PEAK_MULT = 1.5         # armed: recorded peak >= 1.5x entry
F2_MIN_LAST_MULT = 0.10    # dust guard: last real price >= 10% of entry
F2_MAX_LAST_MULT = 50.0    # upside guard (2026-10-24): last real price
                           # > 50x entry on a dead tape is a bad print
                           # until proven otherwise — the bot-self-audit
                           # ledger-sanity bound (mult > 50). Real tails
                           # (BPCATE peaked 485x) do it over hours on a
                           # LIVE tape; on an unquotable tape the "last
                           # real price" is stale by construction, so a
                           # >50x multiple is refused, never banked.
F2_CHAINS = ("solana", "base", "ethereum")  # hyperliquid excluded


def recorded_peak(pos):
    """Highest RECORDED price for the position: tracked running
    peak, leg C's tracked peak, or any banked leg's exit price.
    0.0 when nothing was ever recorded. Mirrors the candidate set
    the engines' _peak_fields uses — recorded prices only."""
    best = 0.0
    try:
        tracked = pos.get("peak_price")
        if tracked:
            best = max(best, float(tracked))
        legs = pos.get("legs") or {}
        lc = legs.get("c") or {}
        if lc.get("peak"):
            best = max(best, float(lc["peak"]))
        for v in legs.values():
            if isinstance(v, dict) and v.get("done") \
                    and v.get("exit_price"):
                best = max(best, float(v["exit_price"]))
    except Exception:
        return 0.0
    return best


def is_armed(pos):
    """Safeguard 1: recorded peak >= 1.5x entry AND leg A banked."""
    try:
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            return False
        legs = pos.get("legs") or {}
        la = legs.get("a") or {}
        if not la.get("done"):
            return False
        return recorded_peak(pos) >= F2_PEAK_MULT * entry
    except Exception:
        return False


def is_candidate(pos):
    """Phase-2 pre-check (snapshot state): armed per the snapshot.
    Used by engines to decide whether a reserves cross-probe is
    warranted on an aggregator-dark cycle. The authoritative
    decision is check() on fresh phase-3 state."""
    return is_armed(pos)


def last_real_quote(pos):
    """(price, ts) of the most recent REAL observed quote, from the
    per-eval quote journal (entries are appended only on cycles
    where a source actually priced the position). (0.0, 0.0) when
    no quote was ever journaled."""
    try:
        jq = pos.get("quote_journal") or []
        for entry in reversed(jq):
            if not isinstance(entry, dict):
                continue
            px = float(entry.get("px") or 0)
            ts = float(entry.get("ts") or 0)
            if px > 0 and ts > 0:
                return px, ts
    except Exception:
        pass
    return 0.0, 0.0


def check(pos, now):
    """F2 decision for one position on a dark cycle (the caller has
    established px <= 0 across every pricing source this cycle).

    Returns the stamp dict when F2 must fire, else None:
      {"last_real_price": float, "last_quote_ts": float,
       "unquotable_seconds": float}
    Never raises."""
    try:
        chain = str(pos.get("chain") or "solana")
        if chain not in F2_CHAINS:
            return None
        if not is_armed(pos):
            return None
        entry = float(pos.get("entry_price") or 0)
        last_px, last_ts = last_real_quote(pos)
        if last_px <= 0 or last_ts <= 0:
            return None  # no real price on record — nothing honest
            # to exit at; existing paths (24h backstop) govern.
        unquotable_s = float(now) - last_ts
        if unquotable_s < F2_SUSTAINED_S:
            return None  # brief gap — safeguard 2
        if last_px < F2_MIN_LAST_MULT * entry:
            return None  # dust guard — last "real" price is dust;
            # keep waiting rather than stamping a fiction.
        if last_px > F2_MAX_LAST_MULT * entry:
            # Upside guard (2026-10-24): a >50x multiple on a dead
            # tape is a bad print, not a moon — the 2026-10-24
            # phantom banked +$16M off exactly such a row
            # (last_real_price 1.027e-4 vs 4.73e-12 entry). Flag
            # and refuse: the position keeps waiting (the 24h
            # backstop still governs); the fiction is never banked.
            logger.warning(
                "f2 upside refusal %s: last real price $%s = %.1fx "
                "entry (> %.0fx bound) — bad print, not banked",
                str(pos.get("mint") or "?")[:16], last_px,
                last_px / entry, F2_MAX_LAST_MULT)
            return None
        return {"last_real_price": last_px,
                "last_quote_ts": last_ts,
                "unquotable_seconds": round(unquotable_s, 1)}
    except Exception:
        return None
