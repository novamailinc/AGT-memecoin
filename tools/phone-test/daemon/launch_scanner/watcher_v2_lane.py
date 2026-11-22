#!/usr/bin/env python3
"""Watcher v2 paper lane (2026-10-24, user-approved).

The SAME smart-watcher convergence-fire trading arm as
watcher_lane.py, loaded as an ISOLATED module instance, with ONE
change: the first ladder rung banks half at 1.5x instead of 2x.

Diagnosis behind the change (2026-10-24): v1's 77 closes show 56
coins peaking between 1x and 2x and then hitting the -50% hard
stop; only 5 completed the ladder. The 2x first rung was too far
for what these entries actually do. v2 tests whether banking half
at 1.5x turns those stalled rises into partial wins.

Everything else is identical to v1: $1 paper shots, $100 paper
bankroll, fail-closed safety gate, $25k liquidity floor,
dedupe-ever, 25% @3x, final 25% on a 30% trailing stop, -50% hard
stop, 24h time stop, F2. Own ledger (watcher_v2_ledger.json), own
killswitch lane ('watcher_v2'), fresh 100-close verdict. v1 is
untouched and still runs to its own verdict.

Paper only. NO Telegram cards — smart-watcher already sends them.
Nothing here touches real money or keys.
"""

import importlib.util
import os
import time

_HOME = os.path.dirname(os.path.abspath(__file__))
_WATCHER_PY = os.path.join(_HOME, "watcher_lane.py")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "watcher_v2_ledger.json")
LANE = "watcher_v2"
LEG_A_TARGET = 1.5  # THE v2 change: bank half at 1.5x, not 2x


def _preregistration():
    """v2 verdict terms, stamped into the v2 ledger header at
    creation so they cannot be quietly changed later."""
    return {
        "lane": LANE,
        "sample_closes": _eng.VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'watcher_v2'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only. Reporting: raw AND ex-top-1 expectancy."
                 % _eng.VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": _eng.NOTIONAL_USD,
            "max_open": _eng.MAX_OPEN,
            "daily_entry_cap": None,  # none — bankroll-bound by design
            "candidate_source": "smart-watcher convergence fires on "
                                "the signal bus (source "
                                "'smart-watcher', >= 2 independent "
                                "wallet groups), consumed "
                                "event-driven (~1s bus watch)",
            "safety_gate": "fail-closed (safety.check_token)",
            "runner_rule": {"min_liq_usd": _eng.RUNNER_MIN_LIQ_USD,
                            "fail_closed_on_unknown_tape": True},
            "veto": "not applied (task terms; the fail-closed "
                    "safety gate is this lane's vetting)",
            "fire_audit": "every fire journaled with its outcome; "
                          "transient blocks retried for %ds"
                          % int(_eng.FIRE_TTL_S),
            "cards": "NONE (smart-watcher already sends the card)",
            "ladder": "50%@1.5x / 25%@3x / 25% trail 30% / "
                      "-50% stop / 24h",
            "f2": "active (shared f2.check choke point)",
            "v1_divergence": "first ladder rung ONLY: 1.5x instead "
                             "of v1's 2x; all other terms identical",
        },
        "registered_ts": time.time(),
    }


def _load_engine():
    """Load watcher_lane.py as a fresh, isolated module and override
    its lane policy for v2. The v1 module instance is never
    touched."""
    spec = importlib.util.spec_from_file_location(
        "watcher_v2_engine", _WATCHER_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = LEDGER_PATH
    eng.LANE = LANE
    eng.LEG_A_TARGET = LEG_A_TARGET
    eng._preregistration = _preregistration
    # Leg-A reason honesty: v1's exit code stamps "leg_a_2x"; on v2
    # that bank happens at 1.5x, so rewrite the journaled reason.
    _orig_close_leg = eng._close_leg

    def _close_leg_v2(pos, leg_key, exit_price, reason):
        if leg_key == "a" and reason == "leg_a_2x":
            reason = "leg_a_1.5x"
        return _orig_close_leg(pos, leg_key, exit_price, reason)

    eng._close_leg = _close_leg_v2
    return eng


_eng = _load_engine()


def register_trial():
    """Register the v2 lane in the trial registry (idempotent).
    Never raises."""
    try:
        if _eng._trials is None:
            return None
        pre = _preregistration()
        return _eng._trials.register(
            LANE, "lane", pre["terms"],
            note="watcher v2 lane (2026-10-24); verdict: %s"
                 % pre["rule"])
    except Exception:
        return None


# Trader API surface, re-exported from the isolated engine instance.
ensure_ledger = _eng.ensure_ledger
process_new_fires = _eng.process_new_fires
poll_exits = _eng.poll_exits
account_state = _eng.account_state
VERDICT_SAMPLE_CLOSES = _eng.VERDICT_SAMPLE_CLOSES
NOTIONAL_USD = _eng.NOTIONAL_USD
MAX_OPEN = _eng.MAX_OPEN
