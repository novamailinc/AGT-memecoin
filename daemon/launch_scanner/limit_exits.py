#!/usr/bin/env python3
"""Limit-order exit model (2026-11-21, exit-study follow-up).

JOURNAL-ONLY MODEL — this module never exits a position, never
changes a live leg, never touches a ledger, the network, or real
money. It answers one question per quote: "what would the ladder
have banked if every rung had been a resting limit order instead
of a poll-and-market exit?" The parent wires it in as a journal
layer (note_quote per exit eval, to_dict riding on the position
into the close record via _finalize's verbatim entry_features
copy — same ride as the shadow_stop field); nothing live reads
its output to trade.

Why a limit model: the exit study (reports/exit-study-2026-11-21.md)
found the -50% stop banks a median 0.26x, not 0.50x — the price
keeps falling between the poll trigger and the fill. A resting
limit order at each rung fills AT the rung (or better); the model
is conservative and fills at exactly the rung price. The hard
stop is the exception: a stop is a market order once triggered,
so a fast dump may not fill at the stop — the model fills the
stop at stop * 0.95 (5% slippage haircut).

Fill rules, in order, per quote (see note_quote):
1. Rung fills: leg A at exactly its target, leg B at exactly its
   target, when the quote reaches them.
2. Hard stop: if the quote is at/below the stop, ALL remaining
   legs fill at stop * 0.95. This is checked BEFORE the trailing
   leg: a single quote that gaps straight through the trail
   trigger to below the stop never traded near the trail price,
   so no leg is credited with a trail fill it could not have had.
   A gradual fall still lets the trailing leg fire first (rule 3).
3. Trailing leg: fills at exactly peak * (1 - trail) when the
   quote falls to it.
4. Time stop: any remainder fills at the quote at expiry.

Each leg fills at most once (idempotent: a repeat quote at the
same price fills nothing new). All functions never raise; bad
inputs are no-ops returning empty results.

Pure + stdlib-only. No I/O.
"""

import time

# The two ladders in production (grad.py / tranche.py constants).
LEGS_GRAD = {"a": (0.50, 2.0), "b": (0.25, 3.0), "c": (0.25, 0.30)}
LEGS_TRANCHE = {"a": (0.25, 1.5), "b": (0.25, 3.0), "c": (0.50, 0.30)}
LADDERS = {"grad_select": LEGS_GRAD, "grad_raw": LEGS_GRAD,
           "grad": LEGS_GRAD,
           "tranche": LEGS_TRANCHE,
           "tranche_protect": LEGS_TRANCHE}

DEFAULT_LEGS = LEGS_GRAD
DEFAULT_HARD_STOP_MULT = 0.50
DEFAULT_TIME_STOP_S = 86400.0
# Hard-stop slippage haircut: a fast dump may not fill at the
# stop itself (exit study: median stop close banks 0.26x).
STOP_FILL_HAIRCUT = 0.95


def ladder_for_lane(lane):
    """Legs config for a lane name, or None when unknown.

    Never raises. Unknown lanes get None (the caller must not
    guess a ladder — the report skips such closes)."""
    try:
        legs = LADDERS.get(str(lane or ""))
        return dict(legs) if legs is not None else None
    except Exception:
        return None


def _num(x, default=None):
    try:
        v = float(x)
    except Exception:
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _norm_legs(legs):
    """Normalize a legs config to {"a": {"frac","target"},
    "b": {"frac","target"}, "c": {"frac","trail"}}.

    Accepts per-leg tuples (frac, target_or_trail), lists, or
    dicts {"frac":..,"target":..} / {"frac":..,"trail":..}.
    Any leg that is missing or unparseable falls back to the
    default grad ladder leg. Never raises."""
    out = {}
    defaults = {"a": {"frac": 0.50, "target": 2.0},
                "b": {"frac": 0.25, "target": 3.0},
                "c": {"frac": 0.25, "trail": 0.30}}
    src = legs if isinstance(legs, dict) else {}
    for key in ("a", "b", "c"):
        dflt = defaults[key]
        try:
            raw = src.get(key)
            frac = None
            val = None
            if isinstance(raw, dict):
                frac = _num(raw.get("frac"))
                val = _num(raw.get("target") if key in ("a", "b")
                           else raw.get("trail"))
            elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
                frac = _num(raw[0])
                val = _num(raw[1])
            if frac is None or not (0.0 < frac <= 1.0):
                frac = dflt["frac"]
            if val is None or val <= 0:
                val = dflt["target"] if key in ("a", "b") \
                    else dflt["trail"]
            if key in ("a", "b"):
                out[key] = {"frac": frac, "target": val}
            else:
                out[key] = {"frac": frac, "trail": val}
        except Exception:
            out[key] = dict(dflt)
    return out


def peak_replay_mult(peak_mult, realized_mult, legs=None):
    """Counterfactual realized multiple from a close's peak ONLY.

    Leg A banks at its target if peak_mult reached it, leg B the
    same; the unbanked remainder exits at the recorded
    realized_mult. This is a MODEL, not measured P&L: the close
    record keeps only the last 10 quote-journal entries, so the
    full price path is not recoverable — the peak is the only
    rung evidence that survives. It captures rung banks the live
    poll missed (exit study: 46% of hard-stop closes peaked
    >=1.5x); it does NOT capture the hard-stop slippage saving
    (a remainder that dumped to 0.26x replays at 0.26x here —
    that saving needs the quote path, see LimitModel/compare).
    Banked legs are priced gross of exit fees; the remainder
    carries the live close's fee treatment inside realized_mult.
    Returns None when the inputs are unusable. Never raises."""
    try:
        peak = _num(peak_mult)
        rm = _num(realized_mult)
        if peak is None or rm is None or peak <= 0 or rm < 0:
            return None
        cfg = _norm_legs(legs)
        banked_frac = 0.0
        banked = 0.0
        for key in ("a", "b"):
            if peak >= cfg[key]["target"]:
                banked_frac += cfg[key]["frac"]
                banked += cfg[key]["frac"] * cfg[key]["target"]
        remainder = max(0.0, 1.0 - banked_frac)
        return round(banked + remainder * rm, 6)
    except Exception:
        return None


class LimitModel:
    """Resting-limit-order replay of one position's ladder.

    Created at entry with (entry_price, legs, hard_stop_mult,
    time_stop_s). Feed every exit-eval quote to note_quote; it
    returns the fills that quote triggered (possibly []). The
    model is JSON-serializable (to_dict/from_dict) so it can ride
    on the position dict into the close record.
    """

    def __init__(self, entry_price, legs=None, hard_stop_mult=None,
                 time_stop_s=None, entry_ts=None,
                 entry_notional_usd=None):
        try:
            self.entry_price = _num(entry_price, 0.0) or 0.0
        except Exception:
            self.entry_price = 0.0
        self.legs = _norm_legs(legs)
        hs = _num(hard_stop_mult, DEFAULT_HARD_STOP_MULT)
        self.hard_stop_mult = hs if hs is not None \
            and 0.0 < hs < 1.0 else DEFAULT_HARD_STOP_MULT
        ts = _num(time_stop_s, DEFAULT_TIME_STOP_S)
        self.time_stop_s = ts if ts is not None and ts > 0 \
            else DEFAULT_TIME_STOP_S
        ets = _num(entry_ts)
        self.entry_ts = ets if ets is not None and ets > 0 else None
        notional = _num(entry_notional_usd)
        self.entry_notional_usd = notional if notional is not None \
            and notional > 0 else None
        self.peak_price = self.entry_price if self.entry_price > 0 \
            else 0.0
        self.fills = []          # list of fill dicts, in order
        self._done = set()       # leg keys already filled

    # -- helpers ---------------------------------------------------
    def _notional(self, notional=None):
        n = _num(notional)
        if n is not None and n > 0:
            return n
        if self.entry_notional_usd is not None:
            return self.entry_notional_usd
        return 1.0

    def _fill(self, leg, fill_price, ts):
        """Record one leg fill. fill_price is the exact fill price;
        fill_mult is measured against entry. usd is priced at the
        model's notional when known, else per $1 of notional
        (frac * fill_mult) so realized() stays well-defined."""
        frac = self.legs[leg]["frac"]
        mult = fill_price / self.entry_price \
            if self.entry_price > 0 else 0.0
        rec = {"leg": leg,
               "fill_price": fill_price,
               "fill_mult": round(mult, 6),
               "ts": float(ts),
               "frac": frac,
               "usd": round(frac * self._notional() * mult, 6)}
        self.fills.append(rec)
        self._done.add(leg)
        return dict(rec)

    # -- main entry point ------------------------------------------
    def note_quote(self, px, now=None):
        """Feed one exit-eval quote. Returns the NEW fills this
        quote triggered (list, possibly []). Never raises; a bad
        price/time or a dead model (entry_price <= 0) is a no-op.
        Idempotent: a filled leg never fills again."""
        try:
            price = _num(px)
            if price is None or price <= 0 or self.entry_price <= 0:
                return []
            now = _num(now, time.time())
            if now is None:
                now = time.time()
            if self.entry_ts is None:
                # First quote anchors the time stop when the
                # caller did not stamp entry_ts at construction.
                self.entry_ts = now
            if price > self.peak_price:
                self.peak_price = price
            entry = self.entry_price
            mult = price / entry
            new = []
            # 1. Rung fills at exactly the rung price.
            for leg in ("a", "b"):
                if leg not in self._done and \
                        mult >= self.legs[leg]["target"]:
                    rung = entry * self.legs[leg]["target"]
                    new.append(self._fill(leg, rung, now))
            remaining = [k for k in ("a", "b", "c")
                         if k not in self._done]
            if not remaining:
                return new
            # 2. Hard stop: everything left fills at stop * 0.95.
            # Checked before the trailing leg (see module docstring:
            # a one-quote gap below the stop never traded near the
            # trail price, so no trail fill is credited).
            if mult <= self.hard_stop_mult:
                stop_px = entry * self.hard_stop_mult \
                    * STOP_FILL_HAIRCUT
                for leg in list(remaining):
                    new.append(self._fill(leg, stop_px, now))
                return new
            # 3. Trailing leg at exactly peak * (1 - trail).
            if "c" in self._done:
                pass
            else:
                trail_px = self.peak_price * \
                    (1.0 - self.legs["c"]["trail"])
                if price <= trail_px:
                    new.append(self._fill("c", trail_px, now))
                    remaining = [k for k in ("a", "b", "c")
                                 if k not in self._done]
                    if not remaining:
                        return new
            # 4. Time stop: remainder fills at the quote at expiry.
            if self.entry_ts is not None and \
                    (now - self.entry_ts) >= self.time_stop_s:
                for leg in list(remaining):
                    new.append(self._fill(leg, price, now))
            return new
        except Exception:
            return []

    # -- accounting --------------------------------------------------
    def realized_mult(self):
        """Realized multiple on filled legs so far (unfilled legs
        contribute 0). Never raises."""
        try:
            return round(sum(f["frac"] * f["fill_mult"]
                             for f in self.fills), 6)
        except Exception:
            return 0.0

    def realized(self, entry_notional_usd=None):
        """Total realized USD given the entry notional and the
        fills so far. Notional precedence: the argument, then the
        model's stored notional, then $1. Never raises."""
        try:
            return round(self.realized_mult()
                         * self._notional(entry_notional_usd), 6)
        except Exception:
            return 0.0

    def is_done(self):
        """True when every leg has filled. Never raises."""
        try:
            return len(self._done) >= 3
        except Exception:
            return False

    # -- serialization ----------------------------------------------
    def to_dict(self):
        """JSON-serializable state. Never raises."""
        try:
            return {
                "version": 1,
                "entry_price": self.entry_price,
                "entry_ts": self.entry_ts,
                "entry_notional_usd": self.entry_notional_usd,
                "legs": {"a": (self.legs["a"]["frac"],
                               self.legs["a"]["target"]),
                         "b": (self.legs["b"]["frac"],
                               self.legs["b"]["target"]),
                         "c": (self.legs["c"]["frac"],
                               self.legs["c"]["trail"])},
                "hard_stop_mult": self.hard_stop_mult,
                "time_stop_s": self.time_stop_s,
                "peak_price": self.peak_price,
                "fills": [dict(f) for f in self.fills],
            }
        except Exception:
            return {"version": 1, "entry_price": 0.0, "fills": []}

    @classmethod
    def from_dict(cls, d):
        """Restore a model from to_dict output. Never raises: a
        malformed dict yields a dead model (entry_price 0) that
        simply never fills, never an exception."""
        try:
            if not isinstance(d, dict):
                return cls(0.0)
            m = cls(d.get("entry_price"),
                    legs=d.get("legs"),
                    hard_stop_mult=d.get("hard_stop_mult"),
                    time_stop_s=d.get("time_stop_s"),
                    entry_ts=d.get("entry_ts"),
                    entry_notional_usd=d.get("entry_notional_usd"))
            peak = _num(d.get("peak_price"))
            if peak is not None and peak > 0:
                m.peak_price = peak
            fills = d.get("fills") or []
            if isinstance(fills, list):
                for f in fills:
                    if not isinstance(f, dict):
                        continue
                    leg = str(f.get("leg") or "")
                    if leg not in ("a", "b", "c"):
                        continue
                    if leg in m._done:
                        continue
                    fp = _num(f.get("fill_price"))
                    if fp is None or fp <= 0:
                        continue
                    rec = {"leg": leg, "fill_price": fp,
                           "fill_mult": _num(f.get("fill_mult"),
                                            fp / m.entry_price
                                            if m.entry_price > 0
                                            else 0.0) or 0.0,
                           "ts": _num(f.get("ts"), 0.0) or 0.0,
                           "frac": m.legs[leg]["frac"],
                           "usd": _num(f.get("usd"), 0.0) or 0.0}
                    m.fills.append(rec)
                    m._done.add(leg)
            return m
        except Exception:
            try:
                return cls(0.0)
            except Exception:
                return None


def compare(actual_close, model):
    """Actual vs limit-model P&L for one close.

    actual_close: a close-record dict (pnl_usd preferred; else
    realized_usd / realized_mult against entry_notional_usd).
    model: a LimitModel or its to_dict() dict.
    Returns {"actual_pnl", "model_pnl", "delta"} in USD
    (delta = model - actual; positive means the limit model
    would have banked more). MODEL, not measured P&L. Never
    raises; unusable inputs yield zeros."""
    out = {"actual_pnl": 0.0, "model_pnl": 0.0, "delta": 0.0}
    try:
        close = actual_close if isinstance(actual_close, dict) else {}
        notional = _num(close.get("entry_notional_usd"))
        if notional is None or notional <= 0:
            notional = 25.0
        pnl = _num(close.get("pnl_usd"))
        if pnl is None:
            realized = _num(close.get("realized_usd"))
            if realized is not None:
                pnl = realized - notional
            else:
                rm = _num(close.get("realized_mult"))
                pnl = (rm * notional - notional) \
                    if rm is not None else 0.0
        m = model
        if isinstance(m, dict):
            m = LimitModel.from_dict(m)
        if not isinstance(m, LimitModel):
            return out
        model_pnl = m.realized(notional) - notional
        out = {"actual_pnl": round(pnl, 4),
               "model_pnl": round(model_pnl, 4),
               "delta": round(model_pnl - pnl, 4)}
        return out
    except Exception:
        return out
