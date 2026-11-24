#!/usr/bin/env python3
"""Cover decision (2026-11-21, exit-study follow-up).

JOURNAL-ONLY DECISION LAYER — this module never changes a live
leg, never exits a position, never gates an entry, never touches
a ledger, the network, or real money. It answers one question
per exit eval, from the position dict and the current price
alone: "what would the cover rules say here — hold, take profit
at a rung, or cut cover on a fast loser?" The caller journals
the returned dict (e.g. into pos["cover_decision"]); nothing
live reads it to trade.

Rules (checked in this order):
(a) mult >= leg A target and leg A not done -> TAKE_PROFIT
    "leg_a_rung" (the rung is here; bank it).
(b) mult <= 0.65 and age < 15 min and no leg banked ->
    CUT_COVER "fast_loser" (exit study: hard stops inside
    15 min win 10.4% and average -$13.64; cut early).
(c) peak_mult >= 2.0 and mult < peak_mult * 0.85 ->
    TAKE_PROFIT "trailing_protect" (gave back 15%+ from a
    2x+ peak; protect what is left of the run).
(d) mult <= 0.70 and age < 15 min -> CUT_COVER
    "shadow_stop_zone" (journal-only echo of the -30% shadow
    model — NOT a live stop; the -30% early stop stays a
    forward test until 100 closes, per the exit study).
(e) else HOLD, with the reason naming the closest trigger and
    its distance in multiples.

Leg A target comes from the position's own legs (2.0x on the
grad lanes, 1.5x on the tranche lanes); missing legs default
to the grad 2.0x target. Unknown age fails closed: rules (b)
and (d) cannot fire without a known entry time. A malformed
position (empty dict, missing prices) returns HOLD "hold_no_data"
— never an exception, never an action.

Pure + stdlib-only. No I/O.
"""

import time

FAST_LOSER_MULT = 0.65
SHADOW_ZONE_MULT = 0.70
FAST_WINDOW_S = 900.0          # 15 minutes
TRAIL_PROTECT_PEAK_MULT = 2.0
TRAIL_PROTECT_GIVEBACK = 0.85  # gave back 15% from the peak


def _num(x, default=None):
    try:
        v = float(x)
    except Exception:
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _legs(pos):
    try:
        legs = (pos or {}).get("legs")
        return legs if isinstance(legs, dict) else {}
    except Exception:
        return {}


def _leg_done(legs, key):
    try:
        leg = legs.get(key)
        return bool(isinstance(leg, dict) and leg.get("done"))
    except Exception:
        return False


def _any_banked(legs):
    try:
        return any(_leg_done(legs, k) for k in ("a", "b", "c"))
    except Exception:
        return False


def cover_decision(pos, px, now=None):
    """Journal-only cover decision for one position at one price.

    Returns {"action": "HOLD"|"TAKE_PROFIT"|"CUT_COVER",
             "reason": <stable key>,
             "evidence": {...}}. Never raises."""
    try:
        now = _num(now, time.time())
        if now is None:
            now = time.time()
        pos = pos if isinstance(pos, dict) else {}
        entry = _num(pos.get("entry_price"))
        price = _num(px)
        legs = _legs(pos)
        leg_a_done = _leg_done(legs, "a")
        any_banked = _any_banked(legs)
        # Leg A target from the position's own ladder (grad 2.0x,
        # tranche 1.5x); 2.0x default when the legs are absent.
        target_a = 2.0
        try:
            raw_a = legs.get("a")
            if isinstance(raw_a, dict):
                t = _num(raw_a.get("target"))
                if t is not None and t > 0:
                    target_a = t
        except Exception:
            pass
        # Peak: the tracked peak_price, else leg C's tracked peak,
        # else the current price (no peak history on record).
        peak = None
        for src in (pos.get("peak_price"),
                    (legs.get("c") or {}).get("peak")
                    if isinstance(legs.get("c"), dict) else None):
            v = _num(src)
            if v is not None and v > 0:
                peak = v if peak is None else max(peak, v)
        entry_ts = _num(pos.get("entry_ts"))
        age_s = (now - entry_ts) if entry_ts is not None \
            and entry_ts > 0 else None
        mult = (price / entry) if entry is not None and entry > 0 \
            and price is not None and price > 0 else None
        peak_mult = (peak / entry) if peak is not None \
            and entry is not None and entry > 0 else None
        evidence = {
            "mult": round(mult, 4) if mult is not None else None,
            "peak_mult": round(peak_mult, 4)
                         if peak_mult is not None else None,
            "age_s": round(age_s, 1) if age_s is not None else None,
            "leg_a_target": target_a,
            "leg_a_done": leg_a_done,
            "any_banked": any_banked,
        }
        if mult is None:
            evidence["nearest_trigger"] = None
            evidence["distance_mult"] = None
            return {"action": "HOLD", "reason": "hold_no_data",
                    "evidence": evidence}

        # (a) Leg A rung is here and unbanked.
        if mult >= target_a and not leg_a_done:
            evidence["trigger_mult"] = target_a
            return {"action": "TAKE_PROFIT",
                    "reason": "leg_a_rung", "evidence": evidence}
        young = age_s is not None and 0 <= age_s < FAST_WINDOW_S
        # (b) Fast loser: down 35%+ inside 15 min, nothing banked.
        if young and mult <= FAST_LOSER_MULT and not any_banked:
            evidence["trigger_mult"] = FAST_LOSER_MULT
            return {"action": "CUT_COVER",
                    "reason": "fast_loser", "evidence": evidence}
        # (c) Gave back 15%+ from a 2x+ peak.
        if peak_mult is not None \
                and peak_mult >= TRAIL_PROTECT_PEAK_MULT \
                and mult < peak_mult * TRAIL_PROTECT_GIVEBACK:
            evidence["trigger_mult"] = round(
                peak_mult * TRAIL_PROTECT_GIVEBACK, 4)
            return {"action": "TAKE_PROFIT",
                    "reason": "trailing_protect",
                    "evidence": evidence}
        # (d) Shadow-stop zone: down 30%+ inside 15 min (echo of
        # the shadow model only — NOT a live stop).
        if young and mult <= SHADOW_ZONE_MULT:
            evidence["trigger_mult"] = SHADOW_ZONE_MULT
            return {"action": "CUT_COVER",
                    "reason": "shadow_stop_zone",
                    "evidence": evidence}

        # (e) HOLD: name the closest trigger and its distance.
        cands = []  # (distance, trigger_key, reason_key)
        if not leg_a_done:
            cands.append((abs(target_a - mult), "leg_a_rung",
                          "hold_below_leg_a_rung"))
        if young and not any_banked:
            cands.append((abs(mult - FAST_LOSER_MULT),
                          "fast_loser", "hold_above_fast_loser"))
        if peak_mult is not None \
                and peak_mult >= TRAIL_PROTECT_PEAK_MULT:
            cands.append((abs(mult - peak_mult
                              * TRAIL_PROTECT_GIVEBACK),
                          "trailing_protect",
                          "hold_above_trailing_protect"))
        if young:
            cands.append((abs(mult - SHADOW_ZONE_MULT),
                          "shadow_stop_zone",
                          "hold_above_shadow_stop_zone"))
        if cands:
            cands.sort(key=lambda c: c[0])
            dist, trigger, reason = cands[0]
            evidence["nearest_trigger"] = trigger
            evidence["distance_mult"] = round(dist, 4)
            return {"action": "HOLD", "reason": reason,
                    "evidence": evidence}
        evidence["nearest_trigger"] = None
        evidence["distance_mult"] = None
        return {"action": "HOLD", "reason": "hold_no_data",
                "evidence": evidence}
    except Exception:
        try:
            return {"action": "HOLD", "reason": "hold_no_data",
                    "evidence": {"mult": None, "peak_mult": None,
                                 "age_s": None, "leg_a_target": 2.0,
                                 "leg_a_done": False,
                                 "any_banked": False,
                                 "nearest_trigger": None,
                                 "distance_mult": None}}
        except Exception:
            return {"action": "HOLD", "reason": "hold_no_data",
                    "evidence": {}}
