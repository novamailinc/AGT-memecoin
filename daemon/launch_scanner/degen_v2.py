#!/usr/bin/env python3
"""Degen v2: evidence-based memecoin picker.

Design principles (from 2026-10-14 deep research):
- Abandon sub-minute sniper game (can't beat 2s-decay edge)
- Target 2-10 min window: survived sniper dump, showing real buying
- Filters ranked by evidence strength, not guesswork
- Fast exits: 68% of tokens die on day 1, runners "usually don't come"
- Honest costs: $1.25 all-in per $1 shot
- Kill switch integration: halt on negative expectancy

Scoring (0-100):
- Telegram link in metadata: +25 (8.94x graduation lift, RED-PUMP)
- Holder count 100+: +20 (<100 = 94.9% high-risk, MemeTrans)
- Top-10 holders <30%: +15 (RugCheck threshold)
- Buy pressure (buy/sell >1.5): +15
- Holder velocity (5+ new/min): +10
- FDV in sweet spot ($30k-$300k): +10
- Deployer not serial rugger: +5 (filters 4x losers)

Penalties:
- No Telegram: -15
- Top-10 >40%: -20 (hard block)
- Age <2 min: -10 (still in sniper zone)
- Age >10 min: -10 (missed the window)

Bar: 60/100 (higher than v1's 50 — selectivity is the edge)
"""
from typing import Dict, List, Optional
import time

# Cost model: $1 shot = $1.25 all-in
COST_PER_SHOT = 1.25  # USD
SHOT_SIZE = 1.00       # USD

# Exit policy (from research: tight trails beat wide, never hold >1h)
EXITS = {
    "trail_pct": 12.0,       # 12% trailing stop
    "max_hold_min": 45,      # 45 min max hold
    "hard_stop_pct": -30.0,  # -30% hard stop
    "take_profit_mult": 2.0, # sell half at 2x
}

# Filter thresholds
FILTERS = {
    "min_holders": 100,
    "max_top10_pct": 30.0,
    "hard_block_top10_pct": 40.0,
    "min_buy_sell_ratio": 1.5,
    "min_holder_velocity": 5,  # new holders/min
    "fdv_min": 30_000,
    "fdv_max": 300_000,
    "min_liq": 8_000,
    "age_min_sec": 120,   # 2 min (past sniper zone)
    "age_max_sec": 600,   # 10 min (in the window)
    "score_bar": 60,
}

def score_token(t: Dict) -> Dict:
    """Score a token 0-100. Returns {score, reasons, pass}."""
    score = 0  # start from 0, earn points
    reasons = []

    # Telegram link (+25)
    if t.get("has_telegram"):
        score += 25
        reasons.append("+25 telegram")
    else:
        reasons.append("no telegram (+0)")

    # Holders: velocity + acceleration (2026-10-15 research #2).
    # ~64% stronger predictor than tx counts. Accelerating holders =
    # organic demand; flat holders + high txns = bump-bot wash (vetoed).
    if t.get("wash_flag"):
        return {"score": 0,
                "reasons": ["WASH VETO: txns up, holders flat (bump-bot)"],
                "pass": False}
    vel = t.get("holder_velocity", 0) or 0
    accel = t.get("holder_accel", 0) or 0
    if vel >= 5 and accel > 0:
        score += 20
        reasons.append(f"+20 holders accelerating ({vel:.0f}/min, accel+)")
    elif vel >= 5:
        score += 12
        reasons.append(f"+12 holder velocity {vel:.0f}/min")
    elif vel >= 2:
        score += 5
        reasons.append(f"+5 holders {vel:.0f}/min")
    else:
        # Fall back to static holder count when velocity unavailable
        holders = t.get("holders", 0)
        if holders >= FILTERS["min_holders"]:
            score += 20
            reasons.append(f"+20 holders={holders}")
        elif holders >= 50:
            score += 5
            reasons.append(f"+5 holders={holders}")
        else:
            reasons.append(f"low holders={holders} (+0)")

    # Top-10 concentration (+15)
    # Unknown holder data must NOT hard-block: a placeholder value here
    # once blocked every token (score 0 forever). Block only on real data.
    top10 = t.get("top10_pct")
    if top10 is not None and top10 > FILTERS["hard_block_top10_pct"]:
        return {"score": 0, "reasons": [f"HARD BLOCK top10={top10:.1f}%"],
                "pass": False}
    if top10 is not None and top10 <= FILTERS["max_top10_pct"]:
        score += 15
        reasons.append(f"+15 top10={top10:.1f}%")
    elif top10 is None:
        reasons.append("top10 unknown (+0)")
    else:
        reasons.append(f"top10={top10:.1f}% (+0)")

    # Buy pressure (+15)
    bs = t.get("buy_sell_ratio", 0)
    if bs >= FILTERS["min_buy_sell_ratio"]:
        score += 15
        reasons.append(f"+15 b/s={bs:.2f}")

    # Holder velocity (+10)
    hv = t.get("holder_velocity", 0)
    if hv >= FILTERS["min_holder_velocity"]:
        score += 10
        reasons.append(f"+10 vel={hv}/min")

    # FDV sweet spot (+10)
    fdv = t.get("fdv", 0)
    if FILTERS["fdv_min"] <= fdv <= FILTERS["fdv_max"]:
        score += 10
        reasons.append(f"+10 fdv=${fdv:,.0f}")

    # Deployer (+5 / -30): 2026-10-15 research #1 — strongest pre-launch
    # signal. Repeat-rug deployers get near-veto; proven get bonus.
    if t.get("deployer_rugger"):
        score -= 30
        reasons.append("-30 serial rugger/factory")
    elif t.get("deployer_clean"):
        ds = t.get("deployer_score", 0)
        if ds >= 25:
            score += 10
            reasons.append(f"+10 proven deployer ({ds})")
        else:
            score += 5
            reasons.append("+5 deployer ok")

    # Age window (penalty only)
    age = t.get("age_sec", 0)
    if age < FILTERS["age_min_sec"]:
        score -= 10
        reasons.append(f"-10 too young ({age:.0f}s)")
    elif age > FILTERS["age_max_sec"]:
        score -= 10
        reasons.append(f"-10 too old ({age:.0f}s)")

    # Liquidity floor (hard block)
    if t.get("liquidity", 0) < FILTERS["min_liq"]:
        return {"score": 0, "reasons": reasons + ["HARD BLOCK low liq"],
                "pass": False}

    score = max(0, min(100, score))
    return {
        "score": round(score, 1),
        "reasons": reasons,
        "pass": score >= FILTERS["score_bar"],
    }

def breakeven_win_rate(avg_win_mult: float) -> float:
    """Win rate needed to break even at $1.25 cost per $1 shot."""
    # Profit per win = (mult - 1) * 1.0, loss per loss = 1.25
    # breakeven: w*(mult-1) = (1-w)*1.25 → w = 1.25 / (mult - 1 + 1.25)
    return 1.25 / (avg_win_mult - 1 + 1.25)

def expected_value(win_rate: float, avg_win_mult: float) -> float:
    """Expected $ per shot."""
    return win_rate * (avg_win_mult - 1) * 1.0 - (1 - win_rate) * 1.25
