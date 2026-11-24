#!/usr/bin/env python3
"""Spark: ignition detector for the Degen v2 lane.

Rationale (2026-10-15 pre-pump research):
- No published evidence supports PREDICTING which memecoin will 100x
  before it moves. The documented edge is REACTING fast to ignition.
- Degen v2 scans every 5 min with a 2-10 min age window. Coins ignite
  between scans. Spark watches the 30-180s window for ignition
  fingerprints and feeds survivors to Degen v2 as a bonus signal.
- The #1 documented detectable signal is holder concentration +
  bundle detection (MemeTrans: bundled accounts hold 36.5% of supply;
  early-buyer concentration gaps of 17-19pp mark dump-prone tokens).
  Spark uses this as a VETO (reject coordinated dumps), not a predictor.
- "Smart money inflow" does NOT cause pumps (166k-launch cohort study:
  placebo wallets show larger flow lift than real cohorts). Spark does
  not chase smart-money labels.

What Spark is: a fast first-look that notices ignition earlier and
rejects coordinated dumps sooner. It is NOT a runner predictor.

Kill criteria: 50 spark-tagged closes. If spark-tagged picks do not beat
the degen_v2 baseline expectancy, Spark is removed.

Free-data signals used (all via GeckoTerminal/DexScreener/RugCheck):
- Holder velocity: holders/min in the ignition window
- Buy pressure: buy/sell ratio from pool transactions
- Volume acceleration: minute-over-minute txn growth
- Bundle veto: top-10 holder concentration from RugCheck
"""
import json
import os
import time

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
SPARK_STATE_FILE = os.path.join(HOME, "spark_signals.json")

# Ignition window: 30s - 3min. Younger = still in sniper chaos,
# older = Degen v2's normal 2-10min window takes over.
SPARK_AGE_MIN_SEC = 30
SPARK_AGE_MAX_SEC = 180

# Signal is valid for 8 min (covers the Degen v2 2-10min scoring window)
SPARK_TTL_SEC = 8 * 60

# Bonus added to Degen v2 score when Spark fires
SPARK_BONUS = 15

# Ignition thresholds (tuned for selectivity: the base rate is brutal,
# ~1% graduation, so false positives must be starved)
MIN_HOLDER_VELOCITY = 8.0    # holders/min in ignition window
MIN_BUY_SELL_RATIO = 2.0    # buys/sells
MIN_TXN_ACCEL = 1.5         # txn growth factor minute-over-minute

# Bundle veto: coordinated early buying = dump setup, not a runner.
# (MemeTrans: top-10 concentration +24pp for high-risk tokens)
VETO_TOP10_PCT = 40.0


def _load_state():
    try:
        with open(SPARK_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(SPARK_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def record_spark(mint, symbol, reasons, metrics):
    """Record a Spark ignition signal. TTL'd so stale signals expire."""
    state = _load_state()
    # Prune expired
    now = time.time()
    state = {m: s for m, s in state.items()
             if now - s.get("ts", 0) < SPARK_TTL_SEC}
    state[mint] = {
        "ts": now,
        "symbol": symbol,
        "reasons": reasons,
        "metrics": metrics,
    }
    _save_state(state)


def get_spark(mint):
    """Return active Spark signal for a mint, or None."""
    state = _load_state()
    s = state.get(mint)
    if not s:
        return None
    if time.time() - s.get("ts", 0) > SPARK_TTL_SEC:
        return None
    return s


def clear_spark(mint):
    """Remove a Spark signal (e.g., after it fires into a card)."""
    state = _load_state()
    state.pop(mint, None)
    _save_state(state)


def bundle_veto(top10_pct):
    """Veto coordinated dumps. Returns (vetoed: bool, reason: str).

    High early concentration = one operator across many wallets =
    dump setup, not organic ignition. (MemeTrans, n=41,470)
    """
    if top10_pct is None:
        return False, "concentration unknown, no veto"
    if top10_pct > VETO_TOP10_PCT:
        return True, f"bundle veto: top10={top10_pct:.1f}% > {VETO_TOP10_PCT}%"
    return False, ""


def detect_ignition(token):
    """Check ignition fingerprints on a young token.

    token dict needs: age_sec, holder_velocity, holder_accel,
    buy_sell_ratio, txn_accel (optional), top10_pct (optional).

    Returns (ignited: bool, reasons: list, metrics: dict).
    All thresholds must pass (AND logic) — selectivity is the edge.
    """
    reasons = []
    metrics = {}
    age = token.get("age_sec", 0)

    # 1. Must be in the ignition window
    if not (SPARK_AGE_MIN_SEC <= age <= SPARK_AGE_MAX_SEC):
        return False, [], {}
    reasons.append(f"age {age:.0f}s in ignition window")

    # 2. Holder velocity: the core ignition signal.
    #    Organic ignition = holders arriving fast AND accelerating.
    vel = token.get("holder_velocity", 0) or 0
    accel = token.get("holder_accel", 0) or 0
    metrics["holder_velocity"] = vel
    metrics["holder_accel"] = accel
    if vel < MIN_HOLDER_VELOCITY:
        return False, [], {}
    reasons.append(f"holder velocity {vel:.1f}/min >= {MIN_HOLDER_VELOCITY}")

    # 3. Buy pressure: ignition needs real buying, not just holders.
    bs = token.get("buy_sell_ratio", 0) or 0
    metrics["buy_sell_ratio"] = bs
    if bs < MIN_BUY_SELL_RATIO:
        return False, [], {}
    reasons.append(f"buy/sell {bs:.2f} >= {MIN_BUY_SELL_RATIO}")

    # 4. Acceleration: flat velocity + high txns = bump-bot wash.
    #    (holder_tracker wash detection is the stronger version;
    #    this is the cheap pre-check)
    if accel <= 0:
        # Allow if velocity is very high (strong ignition overrides)
        if vel < MIN_HOLDER_VELOCITY * 2:
            return False, [], {}
        reasons.append(f"velocity very high ({vel:.1f}/min), accel flat ok")
    else:
        reasons.append(f"holders accelerating (+{accel:.1f}/min²)")

    # 5. Wash flag veto (from holder_tracker, when available)
    if token.get("wash_flag"):
        return False, ["wash veto: bump-bot signature"], {}

    # 6. Bundle veto: coordinated early buying = dump, not ignition
    vetoed, veto_reason = bundle_veto(token.get("top10_pct"))
    if vetoed:
        return False, [veto_reason], {}
    if veto_reason:
        reasons.append(veto_reason)

    return True, reasons, metrics


def spark_stats():
    """Count active spark signals (for monitoring)."""
    state = _load_state()
    now = time.time()
    active = [m for m, s in state.items()
              if now - s.get("ts", 0) < SPARK_TTL_SEC]
    return {"active_signals": len(active)}
