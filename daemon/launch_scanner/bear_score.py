#!/usr/bin/env python3
"""Bear scorer / devil's advocate at entry (2026-11-17).

A lightweight, rule-based, no-LLM, no-RPC second opinion on every
evaluated candidate. It runs on the SAME features already fetched
by the entry evaluation (grad_select.evaluate_candidate) and
returns a bear_score on a 0-100 scale (higher = more bearish)
plus per-feature contributions, so the score is explainable.

Scored features (weights sum to 100) and their bands. Bands are
stated from the literature / the lane's own stored closes; the
calibration script (scripts/bear-calibrate) re-measures them on
real stored closes before any gate is enabled:

  deployer_launches_24h (25): 0 launches -> +25, 1-2 -> 0,
      >=3 -> +25. The reflector's own finding (2026-11-17):
      deployer 0 launches is -$894.32 on 242 closes (28.1%
      wins); 1-2 launches is +$8,079.35 on 258 closes (72.9%
      wins). A first-time deployer is the bearish case here,
      not a serial one (>=3 is already filter-rejected).
  ticker_others_24h / copycat (20): 0 -> 0, 1-2 -> +10, >=3 -> +20.
  ttg_s / curve speed (15): <=60s -> 0, 60-600s -> +7, >600s -> +15.
  curve_trades (10): <=100 -> 0, 101-250 -> +5, >250 -> +10.
  grad_age_s / entry speed (15): <=30s -> 0, >30s -> +15.
      Stream candidates only; backfill candidates carry no
      grad_age_s and score unknown on this feature.
  liquidity_usd (10): >=20000 -> 0, 5000-20000 -> +5, <5000 -> +10.
      Liquidity <= 0 is UNKNOWN, never thin: a 0 quote-liquidity
      is this codebase's "no quote" marker, not a measured pool.
  top10_pct / top-10 concentration (5): <=20% -> 0, 20-40% -> +3,
      >40% -> +5. NOT available at evaluate time in the grad
      lanes (it comes from the RugCheck safety report, which
      these lanes do not run) and NOT present in stored closes,
      so it is unknown on every live evaluation and uncalibrated
      until a caller that has it passes it in.

Unknown handling (the point of this module): a missing feature
contributes NOTHING, is listed in ``unknown``, and is absent
from ``contributions``. A low score with unknowns is NOT a clean
bill of health — consumers must read ``unknown`` (and
``max_known``, the most points the known features could have
contributed) alongside the score. No placeholder 0 is ever fed
into a band: ``None``, non-numeric, and (for liquidity) <= 0 all
mean unknown.

JOURNAL-ONLY in v1: score_features() never gates anything. The
evaluation path journals the score on every candidate; a gate
is enabled only if scripts/bear-calibrate shows the score
clearly separating winners from losers on stored closes, and
then only as its own distinct reject reason (``bear_score``).

Pure Python, stdlib only. Every public function never raises.
No network, no disk, no state. Milliseconds: pure arithmetic.
"""

# Feature weights; sum is 100 (the score scale).
WEIGHTS = {
    "deployer": 25,
    "copycat": 20,
    "curve_speed": 15,
    "curve_trades": 10,
    "entry_speed": 15,
    "liquidity": 10,
    "top10": 5,
}

SCALE_MAX = 100

# Band edges, shared with the tests.
DEPLOYER_BEAR_N = 0        # 0 launches in 24h is the bearish band
COPYCAT_MID = 1            # 1-2 others: +10; >=3: +20
TTG_MID_S = 60.0
TTG_HIGH_S = 600.0
CURVE_TRADES_MID = 100
CURVE_TRADES_HIGH = 250
ENTRY_FAST_S = 30.0        # Rule A bar; mirrors ENTRY_MAX_AGE_S
LIQ_MID_USD = 5000.0
LIQ_HIGH_USD = 20000.0
TOP10_MID_PCT = 20.0
TOP10_HIGH_PCT = 40.0

# Canonical input keys for score_features(). score_close() in the
# calibration script flattens a ledger close into these keys.
FEATURE_KEYS = (
    "deployer_launches_24h",
    "ticker_others_24h",
    "ttg_s",
    "curve_trades",
    "grad_age_s",
    "liquidity_usd",
    "top10_pct",
)


def _num(v):
    """float(v) or None — never raises, never coerces junk.
    Booleans are not numbers here."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _band_points(value, edges, points):
    """points[i] for the band value falls in; None when unknown.
    edges are ascending upper bounds; points has len(edges)+1."""
    if value is None:
        return None
    for edge, pts in zip(edges, points):
        if value <= edge:
            return pts
    return points[-1]


def score_features(features):
    """Score one candidate's already-fetched features.

    ``features`` is a dict keyed by FEATURE_KEYS (extra keys are
    ignored). Returns::

        {"bear_score": int 0-100,
         "contributions": {feature: points} (known features only),
         "unknown": [feature, ...] (missing / unmeasurable),
         "known": int, "max_known": int,
         "version": 1}

    ``max_known`` is the most points the KNOWN features could
    have contributed — read it with the score: a bear_score of 10
    with max_known 15 is a strong all-clear; the same 10 with
    max_known 95 is a shrug. Never raises."""
    try:
        feats = features if isinstance(features, dict) else {}
        contributions = {}
        unknown = []

        # deployer: 0 launches -> full weight (the losing band);
        # 1-2 -> 0 (the winning band); >=3 -> full weight.
        dep = _num(feats.get("deployer_launches_24h"))
        if dep is None:
            unknown.append("deployer")
        else:
            contributions["deployer"] = WEIGHTS["deployer"] \
                if dep == 0 or dep >= 3 else 0

        # copycat: 0 -> 0; 1-2 -> +10; >=3 -> +20.
        cop = _num(feats.get("ticker_others_24h"))
        if cop is None:
            unknown.append("copycat")
        else:
            contributions["copycat"] = _band_points(
                cop, (0.0, 2.0),
                (0, WEIGHTS["copycat"] // 2, WEIGHTS["copycat"]))

        # curve speed (ttg): <=60s -> 0; 60-600s -> +7; >600s -> +15.
        ttg = _num(feats.get("ttg_s"))
        if ttg is None:
            unknown.append("curve_speed")
        else:
            contributions["curve_speed"] = _band_points(
                ttg, (TTG_MID_S, TTG_HIGH_S),
                (0, WEIGHTS["curve_speed"] // 2,
                 WEIGHTS["curve_speed"]))

        # curve trades: <=100 -> 0; 101-250 -> +5; >250 -> +10.
        tr = _num(feats.get("curve_trades"))
        if tr is None:
            unknown.append("curve_trades")
        else:
            contributions["curve_trades"] = _band_points(
                tr, (float(CURVE_TRADES_MID),
                     float(CURVE_TRADES_HIGH)),
                (0, WEIGHTS["curve_trades"] // 2,
                 WEIGHTS["curve_trades"]))

        # entry speed: <=30s -> 0; >30s -> +15. Missing (backfill)
        # is unknown, never fast.
        age = _num(feats.get("grad_age_s"))
        if age is None:
            unknown.append("entry_speed")
        else:
            contributions["entry_speed"] = 0 \
                if age <= ENTRY_FAST_S else WEIGHTS["entry_speed"]

        # liquidity: <= 0 is UNKNOWN (the "no quote" marker), not
        # thin. >=20000 -> 0; 5000-20000 -> +5; <5000 -> +10.
        liq = _num(feats.get("liquidity_usd"))
        if liq is None or liq <= 0:
            unknown.append("liquidity")
        else:
            contributions["liquidity"] = _band_points(
                liq, (LIQ_MID_USD, LIQ_HIGH_USD),
                (WEIGHTS["liquidity"], WEIGHTS["liquidity"] // 2, 0))

        # top-10 concentration: unknown at evaluate time in the
        # grad lanes (no safety report is fetched there).
        top = _num(feats.get("top10_pct"))
        if top is None:
            unknown.append("top10")
        else:
            contributions["top10"] = _band_points(
                top, (TOP10_MID_PCT, TOP10_HIGH_PCT),
                (0, 3, WEIGHTS["top10"]))

        total = sum(contributions.values())
        max_known = sum(WEIGHTS[f] for f in contributions)
        return {
            "bear_score": int(total),
            "contributions": contributions,
            "unknown": unknown,
            "known": len(contributions),
            "max_known": int(max_known),
            "version": 1,
        }
    except Exception:
        return {"bear_score": 0, "contributions": {},
                "unknown": list(FEATURE_KEYS), "known": 0,
                "max_known": 0, "version": 1}


def score_close(close_rec):
    """Score one stored ledger close the way the live scorer
    would, from its entry_features.

    Returns (full, eval_only): ``full`` scores every feature the
    close record carries (incl. top-level mcap/liquidity, which
    the live evaluator does NOT have); ``eval_only`` scores
    exactly the features available at evaluate_candidate time
    (deployer, copycat, curve speed/trades, grad age) — the
    score a gate would actually be decided on. top10 is unknown
    in both: stored closes never carry it. Never raises."""
    try:
        rec = close_rec if isinstance(close_rec, dict) else {}
        ef = rec.get("entry_features") or {}
        sel = None
        for key in ("select", "tranche", "tranche_protect"):
            if isinstance(ef.get(key), dict):
                sel = ef[key]
                break
        sel = sel or {}
        base = {
            "deployer_launches_24h": _num(
                sel.get("deployer_launches_24h")),
            "ticker_others_24h": _num(sel.get("ticker_others_24h")),
            "ttg_s": _num(sel.get("ttg_s")),
            "curve_trades": _num(sel.get("curve_trades")),
            "grad_age_s": _num(sel.get("grad_age_s")),
        }
        if base["grad_age_s"] is None:
            base["grad_age_s"] = _num(ef.get("grad_age_s"))
        eval_only = score_features(dict(base))
        full = dict(base)
        full["liquidity_usd"] = _num(ef.get("liquidity"))
        full["top10_pct"] = None
        return score_features(full), eval_only
    except Exception:
        empty = score_features({})
        return empty, empty
