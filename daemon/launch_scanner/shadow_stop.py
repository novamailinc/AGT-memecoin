"""Shadow tighter early stop (2026-11-20, faster-exits item 1c).

SHADOW ONLY — this module never closes, exits, or gates any
position. It answers one question per eval: "would a tighter stop
have fired here?" A tighter stop (-30% within the first 15 min
after entry) is recorded, with price / multiple / timestamp, into
the position's ``shadow_stop`` field the first time it would have
fired. Live exit decisions never read this field.

Why a shadow first: stop closes on the grad-family lanes realize
far below the -50% stop's promise (last 500 closes per lane:
grad_select stop-close median 0.29x, 206/297 below 0.6x; grad_raw
0.25x, 225/309; tranche 0.50x; tranche_protect 0.45x) because the
price crashes from above the stop to ~0.06x inside a single
~33s eval gap. A tighter stop might catch the fall earlier — or
it might cut winners that later recover. The ledger cannot settle
that from closes alone (closes keep only the last 10 journal
entries), so the shadow journals the counterfactual live and
scripts/shadow-stop-report compares it against live outcomes.

Two entry points:
- note_quote(pos, px, now): called from a lane's _poll_exits at
  the per-eval quote-journal site. Pure journaling; never raises.
- first_crossing(entry_price, entry_ts, quote_journal, ...): the
  same rule recomputed from a persisted quote journal (used by the
  report script for closes recorded before the live journaling
  existed, and for lanes whose engine does not call note_quote).
- shadow_outcome(close): leg-aware shadow-vs-live comparison for
  one close record (see docstring). Used by the report script.

All functions are pure / side-effect-free except note_quote,
which writes only pos["shadow_stop"]. No network, no ledger
writes, no Helius.
"""

import time

# The shadow stop: -30% ...
SHADOW_STOP_MULT = 0.70
# ... within the first 15 minutes after entry.
SHADOW_WINDOW_S = 900.0
# A leg whose exit multiple is at least this is treated as banked
# at its target (2x / 3x) rather than stopped out: it banked on the
# way up, before any later fall through the shadow stop.
BANKED_MULT = 1.9


def would_fire(entry_price, px, age_s,
               stop_mult=SHADOW_STOP_MULT, window_s=SHADOW_WINDOW_S):
    """True if a live price px at age_s would trip the shadow stop.

    Fires only inside the window and only at or below the stop
    multiple. Invalid inputs (no entry price, no price, unknown
    age) never fire — a shadow must not invent exits.
    """
    try:
        entry = float(entry_price)
        price = float(px)
        age = float(age_s)
    except Exception:
        return False
    if entry <= 0 or price <= 0:
        return False
    if age < 0 or age >= float(window_s):
        return False
    return (price / entry) <= float(stop_mult)


def note_quote(pos, px, now=None, stop_mult=SHADOW_STOP_MULT,
               window_s=SHADOW_WINDOW_S):
    """Journal the first shadow-stop crossing on a live position.

    Call once per exit eval with the eval's quoted price. If the
    shadow stop would have fired and no crossing is recorded yet,
    sets pos["shadow_stop"] = {"ts", "price", "mult", "age_s",
    "stop_mult"} and returns it. Otherwise returns the existing
    record (or None). First crossing only — later evals never
    overwrite it. Never raises; a bad position/price is a no-op.
    """
    now = time.time() if now is None else now
    try:
        if not isinstance(pos, dict):
            return None
        if pos.get("shadow_stop"):
            return pos["shadow_stop"]
        entry = float(pos.get("entry_price") or 0)
        entry_ts = float(pos.get("entry_ts") or 0)
        price = float(px)
        if entry <= 0 or entry_ts <= 0 or price <= 0:
            return None
        age = float(now) - entry_ts
        if not would_fire(entry, price, age, stop_mult, window_s):
            return None
        rec = {"ts": float(now), "price": price,
               "mult": price / entry, "age_s": age,
               "stop_mult": float(stop_mult)}
        pos["shadow_stop"] = rec
        return rec
    except Exception:
        return None


def first_crossing(entry_price, entry_ts, quote_journal,
                   stop_mult=SHADOW_STOP_MULT,
                   window_s=SHADOW_WINDOW_S):
    """First shadow-stop crossing recomputed from a quote journal.

    quote_journal is a list of {"ts", "px"} evals (any order is
    tolerated — sorted by ts). Returns the crossing record in the
    same shape as note_quote, or None. A journal that starts after
    the window (closes keep only the last 10 entries) simply yields
    None — callers must treat that as "no coverage", not as "no
    crossing". Never raises.
    """
    try:
        entry = float(entry_price)
        ets = float(entry_ts)
        if entry <= 0 or ets <= 0:
            return None
        entries = []
        for q in quote_journal or []:
            try:
                entries.append((float(q.get("ts")), float(q.get("px"))))
            except Exception:
                continue
        entries.sort()
        for ts, px in entries:
            age = ts - ets
            if age < 0:
                continue
            if age >= float(window_s):
                break
            if px > 0 and (px / entry) <= float(stop_mult):
                return {"ts": ts, "price": px, "mult": px / entry,
                        "age_s": age, "stop_mult": float(stop_mult)}
        return None
    except Exception:
        return None


def shadow_outcome(close, stop_mult=SHADOW_STOP_MULT,
                   window_s=SHADOW_WINDOW_S):
    """Leg-aware shadow-vs-live comparison for one close record.

    Returns a dict:
      {"covered": bool,           # journal/shadow field covers the window
       "crossed": bool,           # shadow stop would have fired
       "shadow": {...}|None,      # crossing record (authoritative field
                                  # preferred, journal recomputed otherwise)
       "shadow_usd": float|None,  # what the close would have realized
       "live_usd": float,         # what it actually realized
       "saved_usd": float|None}   # shadow - live (>0: shadow saved money)

    Leg-aware: legs that banked at their target (exit multiple >=
    BANKED_MULT — leg A at 2x, leg B at 3x) keep their live
    realized_usd; they banked on the way up, before any fall. Only
    unbanked legs are re-priced at the shadow exit price, scaled
    from their live exit (realized_usd * shadow_px / leg_exit_px).
    TWAP closes (legs == {}) and closes without leg prices fall
    back to a whole-position scaling: live_usd * shadow_mult /
    realized_mult. Fills/fees are NOT re-modeled — both sides use
    the live close's realized dollars as the base, so the
    comparison is gross of the same fill treatment. A close whose
    journal does not cover the early window and has no shadow_stop
    field is "covered": False and yields no saved_usd — never a
    guessed crossing.
    """
    try:
        live_usd = float(close.get("realized_usd") or 0)
    except Exception:
        live_usd = 0.0
    out = {"covered": False, "crossed": False, "shadow": None,
           "shadow_usd": None, "live_usd": live_usd, "saved_usd": None}
    try:
        entry = float(close.get("entry_price") or 0)
        ets = float(close.get("entry_ts") or 0)
        if entry <= 0 or ets <= 0:
            return out
        shadow = None
        if isinstance(close.get("shadow_stop"), dict):
            shadow = dict(close["shadow_stop"])
        else:
            journal = close.get("quote_journal") or []
            # Coverage: the journal must start inside the window
            # (closes keep only the last 10 evals; a journal whose
            # first entry is already past the window cannot rule a
            # crossing in or out).
            first_ts = None
            for q in journal:
                try:
                    ts = float(q.get("ts"))
                except Exception:
                    continue
                first_ts = ts if first_ts is None else min(first_ts, ts)
            if first_ts is not None and (first_ts - ets) <= 120.0:
                out["covered"] = True
                shadow = first_crossing(entry, ets, journal,
                                        stop_mult, window_s)
            else:
                return out
        if shadow is not None:
            out["covered"] = True
            # The recorded crossing must itself be inside the window.
            if float(shadow.get("age_s") or 0) >= float(window_s):
                return out
        else:
            # Authoritative field present but empty means "evaluated
            # live, never crossed" only when the position lived past
            # the window; the report treats a missing field + covered
            # journal as the same. Here shadow is None from the
            # journal path: no crossing observed in coverage.
            if not out["covered"]:
                return out
        if shadow is None:
            return out
        out["shadow"] = shadow
        out["crossed"] = True
        shadow_px = float(shadow.get("price") or 0)
        if shadow_px <= 0:
            return out
        legs = close.get("legs") or {}
        if legs:
            total = 0.0
            usable = False
            for leg in legs.values():
                if not isinstance(leg, dict):
                    continue
                leg_usd = float(leg.get("realized_usd") or 0)
                leg_px = float(leg.get("exit_price") or 0)
                if leg_px > 0 and (leg_px / entry) >= BANKED_MULT:
                    total += leg_usd  # banked: keep live dollars
                    usable = True
                elif leg_px > 0 and leg_usd > 0:
                    total += leg_usd * shadow_px / leg_px
                    usable = True
                elif leg.get("done"):
                    # Done leg with no usable price: keep live dollars
                    # rather than inventing a shadow value.
                    total += leg_usd
                    usable = True
            if usable:
                out["shadow_usd"] = total
                out["saved_usd"] = total - live_usd
                return out
        # Whole-position fallback (no legs / TWAP / unusable legs).
        rm = float(close.get("realized_mult") or 0)
        if rm > 0:
            shadow_usd = live_usd * float(shadow.get("mult") or 0) / rm
            out["shadow_usd"] = shadow_usd
            out["saved_usd"] = shadow_usd - live_usd
        return out
    except Exception:
        return out
