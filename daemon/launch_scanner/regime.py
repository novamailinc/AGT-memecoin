#!/usr/bin/env python3
"""Regime-aware paper stake scaling + sit-out rule (advanced build,
piece 2 and the sit-out piece, 2026-11-21).

The paper lanes size by MARKET REGIME instead of a flat $25:
cold regimes trade half size and half capacity, hot regimes trade
1.5x size, normal regimes trade the base stake. Sitting out is a
DECISION, journaled, not silence: when the regime is cold AND the
recent tape is bleeding, the lane stops entering (exits still run)
until the evidence recovers.

Calibration (exit study 2026-11-21, pooled grad family, 2,000
closes: 51.6% win, mean +$14.88/close):
  * HOT bar: expectancy >= +$15/close over the last 50 (above the
    all-time mean) AND win rate >= 55% (above the 51.6% baseline)
    AND the last 20 closes net positive. A genuinely hot regime.
  * COLD bar: expectancy < $0 over the last 50 (losing) OR the
    last 20 closes net < -$50 (recently bleeding) OR win rate
    < 40% over the last 50 (well below baseline).
  * Below 20 closes the regime is NORMAL ("insufficient_data"):
    a lane trades at its base stake until it has evidence, the
    same n>=20 floor the killswitch uses (min_trades).
  * SIT-OUT: regime COLD AND (the last 5 closes all lost OR the
    last 10 closes net < -$100). A cold regime alone halves size;
    a cold regime PLUS a bleeding tape stops entries. The sit-out
    is BOUNDED: it lasts at most SIT_OUT_MAX_SECONDS (12h) from
    its start, then lifts even if the evidence has not recovered —
    with no entries possible while sitting out, no new closes can
    land, so an unbounded sit-out could never lift on its own (a
    latched gate, fixed 2026-11-22). A re-trigger needs FRESH
    evidence: only closes that landed after the sit-out started
    count toward the next trigger. It is an entry pause, NOT a
    killswitch halt (halting rules and the human-only re-enable
    are untouched).

PAPER ONLY: the stakes scaled here are paper stakes. Live10 is
never touched — the demo mirror normalizes scaled paper P&L back
to the $25 baseline so the demo stays exactly $10/trade (see
scripts/live10-demo-mirror). Killswitch terms are untouched.

Pure Python, stdlib only. Every public function never raises.
"""

# Regime windows (closes, most recent first in the input order —
# the function sorts by close_ts, so callers may pass any order).
PRIMARY_N = 50
RECENT_N = 20
MIN_N = 20

# HOT bar (see calibration above).
HOT_EXPECTANCY_MIN = 15.0
HOT_WIN_RATE_MIN = 0.55
# COLD bar.
COLD_RECENT_NET_MAX = -50.0
COLD_WIN_RATE_MAX = 0.40
# Sit-out bar (on top of COLD).
SIT_OUT_STREAK = 5
SIT_OUT_LAST10_NET_MAX = -100.0
# Maximum sit-out duration: a sit-out that runs this long lifts
# even if the evidence has not recovered (see sit_out()). The
# bound is what keeps the gate from latching forever — while
# sitting out the lane cannot enter, so no new closes can land
# and the evidence that triggered the sit-out can never change.
SIT_OUT_MAX_SECONDS = 12 * 3600

# Stake multipliers by regime.
MULTIPLIERS = {"COLD": 0.5, "NORMAL": 1.0, "HOT": 1.5}
# Minimum paper stake (avoid dust when a small base is halved).
MIN_STAKE_USD = 0.50
# Cold-regime capacity floor (a lane never drops below 5 opens).
MIN_MAX_OPEN = 5


def _pnls(closes):
    """Sorted (oldest-first) pnl list from close records."""
    try:
        recs = [c for c in (closes or []) if isinstance(c, dict)]
        recs.sort(key=lambda c: float(c.get("close_ts") or 0))
        return [float(c.get("pnl_usd") or 0) for c in recs]
    except Exception:
        return []


def _loss_streak(pnls):
    """Trailing consecutive closes with pnl <= 0 (a 0 is not a
    win). 0 when the most recent close won."""
    streak = 0
    for pnl in reversed(pnls):
        if pnl <= 0:
            streak += 1
        else:
            break
    return streak


def assess(closes, now=None):
    """Assess the market regime from a lane's closes.

    Returns {"regime": "COLD"|"NORMAL"|"HOT", "expectancy":
    float, "win_rate": float, "n": int, "recent_n": int,
    "recent_net": float, "recent_expectancy": float,
    "recent_win_rate": float, "loss_streak": int, "reason": str}.
    Never raises. Below MIN_N closes the regime is NORMAL with
    reason "insufficient_data"."""
    try:
        pnls = _pnls(closes)
        n = len(pnls)
        primary = pnls[-PRIMARY_N:]
        recent = pnls[-RECENT_N:]
        if n < MIN_N:
            return {"regime": "NORMAL", "expectancy": 0.0,
                    "win_rate": 0.0, "n": n, "recent_n": len(recent),
                    "recent_net": sum(recent),
                    "recent_expectancy": 0.0,
                    "recent_win_rate": 0.0,
                    "loss_streak": _loss_streak(pnls),
                    "reason": "insufficient_data"}
        expectancy = sum(primary) / len(primary)
        win_rate = (sum(1 for p in primary if p > 0)
                    / len(primary)) if primary else 0.0
        recent_net = sum(recent)
        recent_expectancy = (recent_net / len(recent)) if recent else 0.0
        recent_win_rate = (sum(1 for p in recent if p > 0)
                           / len(recent)) if recent else 0.0
        streak = _loss_streak(pnls)
        if (expectancy >= HOT_EXPECTANCY_MIN
                and win_rate >= HOT_WIN_RATE_MIN
                and recent_net > 0):
            regime, reason = "HOT", "hot_regime"
        elif (expectancy < 0 or recent_net < COLD_RECENT_NET_MAX
                or win_rate < COLD_WIN_RATE_MAX):
            regime, reason = "COLD", "cold_regime"
        else:
            regime, reason = "NORMAL", "normal_regime"
        return {"regime": regime, "expectancy": round(expectancy, 4),
                "win_rate": round(win_rate, 4), "n": n,
                "recent_n": len(recent),
                "recent_net": round(recent_net, 4),
                "recent_expectancy": round(recent_expectancy, 4),
                "recent_win_rate": round(recent_win_rate, 4),
                "loss_streak": streak, "reason": reason}
    except Exception:
        return {"regime": "NORMAL", "expectancy": 0.0,
                "win_rate": 0.0, "n": 0, "recent_n": 0,
                "recent_net": 0.0, "recent_expectancy": 0.0,
                "recent_win_rate": 0.0, "loss_streak": 0,
                "reason": "assess_error"}


def stake_multiplier(regime):
    """Stake multiplier for a regime. Unknown regimes trade at
    the base stake (fail-safe: never 0x, never 2x on bad input)."""
    try:
        return float(MULTIPLIERS.get(str(regime or "").upper(),
                                     1.0))
    except Exception:
        return 1.0


def effective_stake(base_usd, regime):
    """Paper stake for a regime: base x multiplier, floored at
    MIN_STAKE_USD so a small base never becomes dust. For the
    $25 lanes: COLD $12.50, NORMAL $25.00, HOT $37.50."""
    try:
        base = float(base_usd or 0)
        if base <= 0:
            return 0.0
        return max(MIN_STAKE_USD,
                   round(base * stake_multiplier(regime), 2))
    except Exception:
        return float(base_usd or 0)


def effective_max_open(base_max_open, regime):
    """Open-position capacity for a regime: COLD halves it
    (floored at MIN_MAX_OPEN), NORMAL/HOT keep it. A hot regime
    never adds capacity — it sizes up, it does not spray wider."""
    try:
        base = int(base_max_open or 0)
        if str(regime or "").upper() == "COLD":
            return max(MIN_MAX_OPEN, base // 2)
        return base
    except Exception:
        return int(base_max_open or 0)


def sit_out(closes, now=None, sit_out_started_ts=None,
            fresh_after_ts=None):
    """Should the lane stop entering? Returns {"sit_out": bool,
    "reason": str, "regime": str, "evidence": {...}}.

    Sits out ONLY when the regime is COLD AND the recent tape is
    bleeding (last SIT_OUT_STREAK closes all lost, or the last 10
    closes net below SIT_OUT_LAST10_NET_MAX). A cold regime alone
    halves size (it does not sit out); a hot/normal regime never
    sits out. Never raises.

    BOUNDED (fix 2026-11-22): when sit_out_started_ts is given
    and now is at least SIT_OUT_MAX_SECONDS past it, the gate
    lifts ("sit_out_expired") even though the evidence is
    unchanged — no new closes can land while sitting out, so
    waiting for the evidence to "recover" would latch the gate
    forever. fresh_after_ts restricts the evidence to closes
    that landed after that timestamp, so a re-trigger needs
    FRESH closes: the stale closes that caused an expired
    sit-out cannot re-engage it. Both new parameters are
    optional; callers that pass neither get the original
    behaviour."""
    try:
        if fresh_after_ts is not None:
            try:
                cutoff = float(fresh_after_ts)
                closes = [c for c in (closes or [])
                          if isinstance(c, dict)
                          and float(c.get("close_ts") or 0) > cutoff]
            except Exception:
                pass
        reg = assess(closes, now=now)
        pnls = _pnls(closes)
        last10 = pnls[-10:]
        last10_net = sum(last10)
        evidence = {
            "regime": reg["regime"],
            "expectancy": reg["expectancy"],
            "win_rate": reg["win_rate"],
            "n": reg["n"],
            "recent_net": reg["recent_net"],
            "loss_streak": reg["loss_streak"],
            "last10_net": round(last10_net, 4),
            "last5_all_losses": bool(
                len(pnls) >= SIT_OUT_STREAK
                and all(p <= 0 for p in pnls[-SIT_OUT_STREAK:])),
        }
        if sit_out_started_ts is not None and now is not None:
            try:
                age = float(now) - float(sit_out_started_ts)
            except Exception:
                age = 0.0
            if age >= SIT_OUT_MAX_SECONDS:
                evidence["sit_out_age_s"] = round(age, 1)
                return {"sit_out": False, "reason": "sit_out_expired",
                        "regime": reg["regime"], "evidence": evidence}
        if reg["regime"] != "COLD":
            return {"sit_out": False, "reason": "regime_not_cold",
                    "regime": reg["regime"], "evidence": evidence}
        if evidence["last5_all_losses"]:
            return {"sit_out": True, "reason": "loss_streak",
                    "regime": "COLD", "evidence": evidence}
        if last10_net < SIT_OUT_LAST10_NET_MAX:
            return {"sit_out": True, "reason": "recent_bleed",
                    "regime": "COLD", "evidence": evidence}
        return {"sit_out": False, "reason": "cold_but_not_bleeding",
                "regime": "COLD", "evidence": evidence}
    except Exception:
        return {"sit_out": False, "reason": "sit_out_error",
                "regime": "NORMAL", "evidence": {}}
