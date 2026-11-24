#!/usr/bin/env python3
"""Conviction scoring for the paper-only grad lanes (advanced build,
piece 1, 2026-11-21).

Promotes the advisory decision memory (lessons.py) to a
per-candidate conviction score that DECIDES: candidates below the
bar do not enter; borderline candidates go to a REVIEW sentinel
queue instead of entering; the conviction rides into every close
so it can be judged on real paper closes.

Calibration (exit study 2026-11-21, pooled grad family, 2,000
closes; lessons log bands):
  * age <=30s wins 71.0% (+$35,728, n=1,218); >30s wins 21.5%
    (-$5,960, n=782). Age is the strongest single signal.
  * mcap <$50k wins 57.9% (+$30,855, n=1,418); $50-250k is dead
    weight (+$19, n=411); $1M+ wins 30.3% (-$1,846, n=145).
  * deployer 1-2 launches wins 72.9% (+$8,079, n=258); 0 launches
    wins 28.1% (-$894, n=242) — the lessons log.
  * fast entries (age <=30s) +$7,974/281 (69.0%) vs slow
    -$789/219 (28.3%) — the lessons log.
  * bear_score >=40 is confounded with Rule A (old coins score
    bearish); it only ever subtracts, never blocks alone.

Components (points):
  grad_age_s:            0-5s +2, 5-15s +2, 15-30s 0, >30s -10
                         (BLOCK), unknown -10 (BLOCK, fail-closed)
  mcap:                  <$50k +2, $50k-250k 0, $250k-1M 0, >=$1M -3,
                         unknown 0 (never farm)
  deployer_launches_24h: 1-2 +2, 0 -1, >=3 -10 (BLOCK),
                         unknown 0
  ttg_s:                 <=60s +1, 60-600s 0, >600s -1, unknown 0
  curve_trades:          <=100 +1, 101-250 0, >250 -1, unknown 0
  ticker_others_24h:     0 +1, 1-2 0, >=3 -10 (BLOCK), unknown 0
  bear_score:            >=60 -2, 40-59 -1, <40 0, unknown 0
  liquidity:             >=$250k -1 (already-run), else 0,
                         unknown 0

Tiers:
  BLOCK    any -10 component, or total <= -3
  SKIP     total -2..-1
  WEAK     total 0..+2   -> REVIEW sentinel queue
  STANDARD total +3..+5
  STRONG   total >= +6

Gate: BLOCK/SKIP reject (conviction_block / conviction_skip),
WEAK reviews (conviction_review), STANDARD/STRONG pass
(conviction_standard / conviction_strong). The gate NEVER
weakens the existing filters or Rule A — it runs only AFTER
they pass, so it can only remove entries, never add one.

Pure Python, stdlib only. Every public function never raises.
Paper only. Nothing here touches real money, keys, or wallets.
"""

# Blocking component score: a single hard disqualifier.
_BLOCK = -10

# Tier edges on the total score.
STRONG_MIN = 6
STANDARD_MIN = 3
WEAK_MIN = 0
SKIP_MIN = -2

# mcap edges (USD) from the exit study.
MCAP_FRESH_MAX = 50_000.0
MCAP_DEAD_MAX = 250_000.0
MCAP_RUN_MAX = 1_000_000.0
# liquidity edge (USD): >=$250k is the already-run band.
LIQ_RUN_MIN = 250_000.0


def _num(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _bear_int(bear):
    """bear_score may be a dict {"bear_score": int} (the pipeline
    stamps the full dict) or a bare int. Returns int or None."""
    if isinstance(bear, dict):
        return _num(bear.get("bear_score"))
    return _num(bear)


def _mcap(values):
    for key in ("mcap", "mcap_usd", "fdv_usd"):
        v = _num((values or {}).get(key))
        if v is not None:
            return v
    return None


def _liquidity(values):
    for key in ("liquidity", "reserve_usd", "liq_usd"):
        v = _num((values or {}).get(key))
        if v is not None:
            return v
    return None


def score_candidate(values):
    """Score one candidate's measured values.

    Returns {"score": int, "tier": str, "components": {name:
    points}, "reasons": [stable keys]}. Never raises. Unknown
    age is a BLOCK (fail-closed); every other unknown is scored
    0 so it never farms a band."""
    values = values if isinstance(values, dict) else {}
    components = {}
    reasons = []

    # --- grad age (Rule A already passed; this is defence in
    # depth: a >30s or unknown age here must never enter) ---
    age = _num(values.get("grad_age_s"))
    if age is None or age < -5.0:
        components["grad_age_s"] = _BLOCK
        reasons.append("unknown_grad_age")
    elif age > 30.0:
        components["grad_age_s"] = _BLOCK
        reasons.append("slow_entry")
    elif age <= 15.0:
        components["grad_age_s"] = 2
    else:
        components["grad_age_s"] = 0

    # --- mcap (exit study: <$50k is the profit centre) ---
    # A 0 or negative mcap is UNKNOWN (no measurement), never
    # fresh: a 0 placeholder must not farm the fresh band.
    mcap = _mcap(values)
    if mcap is None or mcap <= 0:
        components["mcap"] = 0
    elif mcap < MCAP_FRESH_MAX:
        components["mcap"] = 2
    elif mcap >= MCAP_RUN_MAX:
        components["mcap"] = -3
        reasons.append("high_mcap")
    else:
        components["mcap"] = 0

    # --- deployer (lessons log: 1-2 launches wins 72.9%) ---
    dep = _num(values.get("deployer_launches_24h"))
    if dep is None:
        components["deployer"] = 0
    elif dep >= 3:
        components["deployer"] = _BLOCK
        reasons.append("serial_deployer")
    elif dep >= 1:
        components["deployer"] = 2
    else:
        components["deployer"] = -1
        reasons.append("new_deployer")

    # --- curve speed (ttg) ---
    ttg = _num(values.get("ttg_s"))
    if ttg is None:
        components["ttg_s"] = 0
    elif ttg <= 60:
        components["ttg_s"] = 1
    elif ttg > 600:
        components["ttg_s"] = -1
        reasons.append("slow_curve")
    else:
        components["ttg_s"] = 0

    # --- curve trades ---
    trades = _num(values.get("curve_trades"))
    if trades is None:
        components["curve_trades"] = 0
    elif trades <= 100:
        components["curve_trades"] = 1
    elif trades > 250:
        components["curve_trades"] = -1
        reasons.append("many_trades_curve")
    else:
        components["curve_trades"] = 0

    # --- copycat tickers ---
    copy = _num(values.get("ticker_others_24h"))
    if copy is None:
        components["copycat"] = 0
    elif copy >= 3:
        components["copycat"] = _BLOCK
        reasons.append("copycat_ticker")
    elif copy == 0:
        components["copycat"] = 1
    else:
        components["copycat"] = 0

    # --- bear score (subtracts only, never blocks alone) ---
    bear = _bear_int(values.get("bear_score"))
    if bear is None:
        components["bear_score"] = 0
    elif bear >= 60:
        components["bear_score"] = -2
        reasons.append("bear_high")
    elif bear >= 40:
        components["bear_score"] = -1
        reasons.append("bear_mid")
    else:
        components["bear_score"] = 0

    # --- liquidity (already-run band) ---
    # A 0 or negative liquidity is UNKNOWN, never a measurement.
    liq = _liquidity(values)
    if liq is None or liq <= 0:
        components["liquidity"] = 0
    elif liq >= LIQ_RUN_MIN:
        components["liquidity"] = -1
        reasons.append("high_liquidity")
    else:
        components["liquidity"] = 0

    total = sum(components.values())
    if _BLOCK in components.values() or total <= -3:
        tier = "BLOCK"
    elif total >= STRONG_MIN:
        tier = "STRONG"
    elif total >= STANDARD_MIN:
        tier = "STANDARD"
    elif total >= WEAK_MIN:
        tier = "WEAK"
    else:
        tier = "SKIP"
    return {"score": total, "tier": tier,
            "components": components, "reasons": reasons}


def gate(values):
    """Conviction gate for one candidate's measured values.

    Returns {"verdict": "pass"|"review"|"reject", "reason":
    <stable key>, "conviction": <score_candidate result>}.
    Never raises. Runs only after the existing filters + Rule A
    pass, so it can only remove entries, never add one."""
    try:
        conv = score_candidate(values)
    except Exception:
        return {"verdict": "reject", "reason": "conviction_block",
                "conviction": {"score": _BLOCK, "tier": "BLOCK",
                               "components": {}, "reasons": [
                                   "conviction_error"]}}
    tier = conv["tier"]
    if tier == "BLOCK":
        return {"verdict": "reject", "reason": "conviction_block",
                "conviction": conv}
    if tier == "SKIP":
        return {"verdict": "reject", "reason": "conviction_skip",
                "conviction": conv}
    if tier == "WEAK":
        return {"verdict": "review", "reason": "conviction_review",
                "conviction": conv}
    if tier == "STRONG":
        return {"verdict": "pass", "reason": "conviction_strong",
                "conviction": conv}
    return {"verdict": "pass", "reason": "conviction_standard",
            "conviction": conv}
