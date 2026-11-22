"""Synthetic post-mortem cases for the funnel autopsy lab.

Names echo the *shapes* of two real misses discussed in the live bot's
history, but every number here is invented for this lab. No live data.
"""

# DACITY-like: a convergence signal fires and is blocked about a minute
# after launch while safety is still unknown. The token then clears the
# runner bar from ~5 to ~40 minutes, peaking near $1.6M mcap, and safety
# becomes known-ok -- but the convergence lane never retries, so the
# one early shot is the only shot it ever got.
DACITY_CASE = {
    "name": "DACITY-like",
    "timeline": [
        (0, 3_000, 18_000, True, "unknown"),
        (1, 8_000, 30_000, True, "unknown"),
        (5, 40_000, 120_000, True, "unknown"),
        (10, 75_000, 420_000, True, "known_ok"),
        (40, 110_000, 1_600_000, True, "known_ok"),
    ],
    "gate_log": [
        (1, "convergence", "blocked", "safety unknown at convergence"),
    ],
    "outcome": "runner",
}

# Doomsday-like: comfortably over the bar on every axis with safety
# known-ok, killed only by a home-grown ceiling on 24h gain.
DOOMSDAY_CASE = {
    "name": "Doomsday-like",
    "timeline": [
        (20, 96_000, 1_140_000, True, "known_ok"),
        (35, 98_000, 1_200_000, True, "known_ok"),
    ],
    "gate_log": [
        (20, "momentum", "blocked", "h24 gain 2540% exceeds 300% ceiling"),
    ],
    "outcome": "runner",
}

# Never cleared liquidity + mcap + momentum at the same time.
NEVER_MET_CASE = {
    "name": "dust-that-went-nowhere",
    "timeline": [
        (0, 2_000, 9_000, True, "unknown"),
        (10, 12_000, 44_000, True, "known_ok"),
        (30, 20_000, 60_000, False, "known_ok"),
    ],
    "gate_log": [],
    "outcome": "flat",
}

# Cleared the bar while rising, but safety stayed unknown throughout.
SAFETY_UNKNOWN_CASE = {
    "name": "fast-runner-slow-safety",
    "timeline": [
        (3, 30_000, 90_000, True, "unknown"),
        (9, 55_000, 300_000, True, "unknown"),
    ],
    "gate_log": [
        (3, "momentum", "blocked", "safety unknown"),
    ],
    "outcome": "runner",
}

# Cleared the bar, safety failed there, and it did in fact rug.
RUG_CASE = {
    "name": "correctly-blocked-rug",
    "timeline": [
        (4, 35_000, 150_000, True, "failed"),
        (12, 40_000, 200_000, True, "failed"),
    ],
    "gate_log": [
        (4, "momentum", "blocked", "safety report failed hard checks"),
    ],
    "outcome": "rug",
}

# Cleared the bar with safety ok and no blocking story: caught/other.
CAUGHT_CASE = {
    "name": "ordinary-caught-runner",
    "timeline": [
        (6, 50_000, 250_000, True, "known_ok"),
    ],
    "gate_log": [
        (6, "momentum", "carded", "all gates passed"),
    ],
    "outcome": "runner",
}

ALL_CASES = (
    DACITY_CASE,
    DOOMSDAY_CASE,
    NEVER_MET_CASE,
    SAFETY_UNKNOWN_CASE,
    RUG_CASE,
    CAUGHT_CASE,
)
