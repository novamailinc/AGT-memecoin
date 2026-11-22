#!/usr/bin/env python3
"""Limit-order exit model + cover decision (2026-11-21).

Covers limit_exits.py (LimitModel rung/trailing/stop/time-stop
fills, idempotence, to_dict/from_dict round-trip, compare() on a
fast-dump fixture, peak_replay_mult) and cover.py (each cover
rule, HOLD for a healthy open position, never-raises on a
malformed position).

No network, no live ledgers. Plain script — run directly with
the venv python, not pytest.
"""
import importlib.util
import os
import sys

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import limit_exits as LE  # noqa: E402
from daemon.launch_scanner import cover as CV  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def grad_model(notional=25.0, entry_ts=1000000.0):
    return LE.LimitModel(1.0, legs=LE.LEGS_GRAD,
                         hard_stop_mult=0.5, time_stop_s=86400.0,
                         entry_ts=entry_ts,
                         entry_notional_usd=notional)


# ---------------- leg A fills at exactly 2x ----------------------
m = grad_model()
check("A: no fill below target (1.99x)",
      m.note_quote(1.99, now=1000001.0) == [])
fills = m.note_quote(2.0, now=1000002.0)
check("A: one fill at exactly 2x",
      len(fills) == 1 and fills[0]["leg"] == "a")
check("A: fill price is exactly the rung (2.0)",
      fills[0]["fill_price"] == 2.0 and fills[0]["fill_mult"] == 2.0)
check("A: fill usd is 50% x $25 x 2x = $25.00",
      fills[0]["usd"] == 25.0)
check("A: realized after leg A is $25.00",
      m.realized() == 25.0 and m.realized_mult() == 1.0)

# ---------------- leg B fills at exactly 3x ----------------------
fills = m.note_quote(3.0, now=1000003.0)
check("B: one fill at exactly 3x",
      len(fills) == 1 and fills[0]["leg"] == "b")
check("B: fill price is exactly the rung (3.0)",
      fills[0]["fill_price"] == 3.0 and fills[0]["fill_mult"] == 3.0)
check("B: fill usd is 25% x $25 x 3x = $18.75",
      fills[0]["usd"] == 18.75)
check("B: realized after A+B is $43.75",
      m.realized() == 43.75)

# ---------------- trailing leg at peak*(1-0.30) ------------------
# Price runs to 4x (banking A at 2x and B at 3x on the way), then
# falls: no trail fill at 2.81x, fill at exactly 2.80x.
m = grad_model()
m.note_quote(2.0, now=1000001.0)
m.note_quote(3.0, now=1000002.0)
m.note_quote(4.0, now=1000003.0)
check("trail: no fill at 2.81x (above peak*0.70 = 2.80x)",
      m.note_quote(2.81, now=1000004.0) == [])
fills = m.note_quote(2.80, now=1000005.0)
check("trail: one fill at exactly peak*(1-0.30) = 2.80x",
      len(fills) == 1 and fills[0]["leg"] == "c"
      and fills[0]["fill_price"] == 2.8
      and fills[0]["fill_mult"] == 2.8)
check("trail: fill usd is 25% x $25 x 2.8x = $17.50",
      fills[0]["usd"] == 17.5)
check("trail: model done once all three legs filled",
      m.is_done() and m.realized() == 61.25)
# Trailing leg alone (gradual fall, no rung banks): peak 1.0,
# falls to 0.70x -> leg C fills at 0.70x, A/B still open.
m = grad_model()
m.note_quote(1.0, now=1000001.0)
fills = m.note_quote(0.70, now=1000002.0)
check("trail: gradual fall fills leg C at 0.70x (peak was 1.0x)",
      len(fills) == 1 and fills[0]["leg"] == "c"
      and fills[0]["fill_price"] == 0.7)
check("trail: A/B still open after a trail fill",
      not m.is_done() and len(m.fills) == 1)

# ---------------- hard stop fills at 0.5*0.95 = 0.475x -----------
# A gradual fall to 0.51x is ABOVE the stop, so only the trailing
# leg fires — at 0.70x, its trail from the entry peak (the live
# leg-C peak starts at the entry price, same rule). The stop then
# takes only the remainder at 0.475x.
m = grad_model()
fills = m.note_quote(0.51, now=1000001.0)
check("stop: 0.51x is above the stop — only leg C trails at 0.70x",
      len(fills) == 1 and fills[0]["leg"] == "c"
      and fills[0]["fill_price"] == 0.7)
fills = m.note_quote(0.50, now=1000002.0)
check("stop: remaining legs A+B fill at the stop",
      len(fills) == 2
      and sorted(f["leg"] for f in fills) == ["a", "b"])
check("stop: fill price is stop*0.95 = 0.475x, not the quote",
      all(f["fill_price"] == 0.475 and f["fill_mult"] == 0.475
          for f in fills))
check("stop: realized is 0.25x0.70 + 0.75x0.475 = 0.53125x ($13.28125)",
      m.realized() == 13.28125 and m.realized_mult() == 0.53125)
# A one-quote gap straight below the stop also fills at 0.475x
# (no trail fill is credited — the price never traded near it).
m = grad_model()
fills = m.note_quote(0.20, now=1000001.0)
check("stop: gap dump fills everything at 0.475x (no 0.70x trail)",
      len(fills) == 3
      and all(f["fill_price"] == 0.475 for f in fills))
# Stop after leg A banked: only the remainder fills at 0.475x.
m = grad_model()
m.note_quote(2.0, now=1000001.0)
fills = m.note_quote(0.40, now=1000002.0)
check("stop: banked leg A keeps $25, remainder fills at 0.475x",
      sorted(f["leg"] for f in fills) == ["b", "c"]
      and m.realized() == round(25.0 + 0.5 * 25 * 0.475, 6))

# ---------------- time stop fills at the expiry quote ------------
m = grad_model()
check("time: no fill before expiry",
      m.note_quote(1.20, now=1000000.0 + 86399.0) == [])
fills = m.note_quote(1.20, now=1000000.0 + 86400.0)
check("time: remainder fills at the expiry quote (1.20x)",
      len(fills) == 3
      and all(f["fill_price"] == 1.2 and f["fill_mult"] == 1.2
              for f in fills))
check("time: realized is $25 x 1.20 = $30.00",
      m.realized() == 30.0)
# Time stop after leg A banked: only the remainder at the quote.
m = grad_model()
m.note_quote(2.0, now=1000001.0)
fills = m.note_quote(1.50, now=1000000.0 + 86400.0)
check("time: banked leg A keeps $25, remainder at 1.50x quote",
      sorted(f["leg"] for f in fills) == ["b", "c"]
      and m.realized() == round(25.0 + 0.5 * 25 * 1.5, 6))

# ---------------- idempotence ------------------------------------
m = grad_model()
m.note_quote(2.0, now=1000001.0)
check("idem: second quote at the same price fills nothing",
      m.note_quote(2.0, now=1000002.0) == [])
check("idem: higher quote below next rung fills nothing",
      m.note_quote(2.5, now=1000003.0) == [])
check("idem: still exactly one fill recorded",
      len(m.fills) == 1)
m.note_quote(0.40, now=1000004.0)
check("idem: stop after A fills B+C once, re-quote fills nothing",
      m.note_quote(0.40, now=1000005.0) == []
      and len(m.fills) == 3)

# ---------------- to_dict / from_dict round-trip -----------------
m = grad_model()
m.note_quote(2.0, now=1000001.0)
m.note_quote(4.0, now=1000002.0)  # peak 4.0, B banked at 3.0
d = m.to_dict()
m2 = LE.LimitModel.from_dict(d)
check("round-trip: fills survive",
      len(m2.fills) == 2
      and [f["leg"] for f in m2.fills] == ["a", "b"])
check("round-trip: peak survives (4.0)",
      m2.peak_price == 4.0)
check("round-trip: entry price / legs / stop survive",
      m2.entry_price == 1.0
      and m2.legs["a"]["target"] == 2.0
      and m2.hard_stop_mult == 0.5
      and m2.time_stop_s == 86400.0)
check("round-trip: to_dict is JSON-serializable",
      __import__("json").dumps(m2.to_dict()) is not None)
fills = m2.note_quote(2.80, now=1000003.0)
check("round-trip: restored model still trails (fill at 2.80x)",
      len(fills) == 1 and fills[0]["leg"] == "c"
      and fills[0]["fill_price"] == 2.8)
check("round-trip: from_dict of garbage never raises, never fills",
      LE.LimitModel.from_dict({}).note_quote(2.0) == []
      and LE.LimitModel.from_dict(None).note_quote(2.0) == []
      and LE.LimitModel.from_dict("junk").note_quote(2.0) == [])
check("never raises: garbage quotes are no-ops",
      grad_model().note_quote("junk") == []
      and grad_model().note_quote(None) == []
      and grad_model().note_quote(-1.0) == [])
check("never raises: garbage constructor is a dead model",
      LE.LimitModel(None, legs="junk").note_quote(2.0) == [])

# ---------------- peak_replay_mult --------------------------------
check("peak replay: peak 2.5x banks A only (1.0 + 0.5*0.26)",
      LE.peak_replay_mult(2.5, 0.26, LE.LEGS_GRAD) == 1.13)
check("peak replay: peak 3.5x banks A+B (1.75 + 0.25*0.26)",
      LE.peak_replay_mult(3.5, 0.26, LE.LEGS_GRAD) == 1.815)
check("peak replay: peak below A replays at realized_mult",
      LE.peak_replay_mult(1.2, 0.26, LE.LEGS_GRAD) == 0.26)
check("peak replay: tranche ladder banks A at 1.5x",
      LE.peak_replay_mult(1.6, 0.40, LE.LEGS_TRANCHE)
      == round(0.25 * 1.5 + 0.75 * 0.40, 6))
check("peak replay: bad inputs return None, never raise",
      LE.peak_replay_mult(None, 0.26) is None
      and LE.peak_replay_mult(2.0, None) is None)

# ---------------- compare() on a fast-dump fixture ---------------
# Fixture close: $25 in, poll exit banked 0.26x (the exit-study
# median stop close), pnl -$18.50. The limit model, fed the same
# dump, fills the stop at 0.475x: pnl -$13.125, delta +$5.375.
close = {"mint": "DumpMint11111111111111111111111111111111pump",
         "entry_notional_usd": 25.0, "realized_usd": 6.50,
         "realized_mult": 0.26, "pnl_usd": -18.50,
         "closed_reason": "hard_stop_-50pct"}
m = grad_model()
m.note_quote(1.0, now=1000001.0)
m.note_quote(0.20, now=1000002.0)  # gap dump through the stop
res = LE.compare(close, m)
check("compare: actual pnl is -$18.50",
      res["actual_pnl"] == -18.5)
check("compare: model pnl is -$13.125 (0.475x stop fill)",
      res["model_pnl"] == -13.125)
check("compare: delta is +$5.375 (model beats the poll exit)",
      res["delta"] == 5.375)
res2 = LE.compare(close, m.to_dict())
check("compare: model dict form agrees with the live model",
      res2 == res)
check("compare: bad inputs yield zeros, never raise",
      LE.compare({}, None) == {"actual_pnl": 0.0,
                               "model_pnl": 0.0, "delta": 0.0}
      and LE.compare(None, "junk")["delta"] == 0.0)

# ---------------- cover_decision ---------------------------------
NOW = 2000000.0


def pos(entry_ts=NOW - 600, peak=1.0, a_done=False, b_done=False,
        c_done=False, target_a=2.0):
    return {"mint": "CoverMint1111111111111111111111111111111pump",
            "entry_price": 1.0, "entry_ts": entry_ts,
            "peak_price": peak,
            "legs": {"a": {"frac": 0.5, "target": target_a,
                           "done": a_done},
                     "b": {"frac": 0.25, "target": 3.0,
                           "done": b_done},
                     "c": {"frac": 0.25, "trail": 0.30,
                           "peak": peak, "done": c_done}}}


r = CV.cover_decision(pos(), 2.0, NOW)
check("cover (a): mult 2.0x, leg A open -> TAKE_PROFIT leg_a_rung",
      r["action"] == "TAKE_PROFIT" and r["reason"] == "leg_a_rung")
r = CV.cover_decision(pos(target_a=1.5), 1.5, NOW)
check("cover (a): tranche leg A target 1.5x fires at 1.5x",
      r["action"] == "TAKE_PROFIT" and r["reason"] == "leg_a_rung")
r = CV.cover_decision(pos(a_done=True), 2.5, NOW)
check("cover (a): leg A already banked -> not leg_a_rung",
      r["reason"] != "leg_a_rung")

r = CV.cover_decision(pos(entry_ts=NOW - 300), 0.60, NOW)
check("cover (b): 0.60x at 5 min, nothing banked -> CUT fast_loser",
      r["action"] == "CUT_COVER" and r["reason"] == "fast_loser")
r = CV.cover_decision(pos(entry_ts=NOW - 300, a_done=True), 0.60, NOW)
check("cover (b): leg banked -> not fast_loser",
      r["reason"] != "fast_loser")
r = CV.cover_decision(pos(entry_ts=NOW - 3600), 0.60, NOW)
check("cover (b): 0.60x at 60 min -> not fast_loser (window over)",
      r["reason"] != "fast_loser")

r = CV.cover_decision(pos(entry_ts=NOW - 3600, peak=3.0,
                          a_done=True), 2.40, NOW)
check("cover (c): peak 3.0x, now 2.40x (< 2.55x) -> trailing_protect",
      r["action"] == "TAKE_PROFIT"
      and r["reason"] == "trailing_protect")
r = CV.cover_decision(pos(entry_ts=NOW - 3600, peak=3.0,
                          a_done=True), 2.60, NOW)
check("cover (c): 2.60x still above 2.55x trigger -> HOLD",
      r["action"] == "HOLD")

r = CV.cover_decision(pos(entry_ts=NOW - 300), 0.68, NOW)
check("cover (d): 0.68x at 5 min -> CUT shadow_stop_zone",
      r["action"] == "CUT_COVER"
      and r["reason"] == "shadow_stop_zone")
r = CV.cover_decision(pos(entry_ts=NOW - 3600), 0.68, NOW)
check("cover (d): 0.68x at 60 min -> not shadow zone (window over)",
      r["reason"] != "shadow_stop_zone")

r = CV.cover_decision(pos(entry_ts=NOW - 3600, peak=1.3), 1.20, NOW)
check("cover (e): healthy open 1.20x -> HOLD",
      r["action"] == "HOLD")
check("cover (e): HOLD reason names the closest trigger",
      r["reason"] == "hold_below_leg_a_rung"
      and r["evidence"]["nearest_trigger"] == "leg_a_rung"
      and r["evidence"]["distance_mult"] == 0.8)
check("cover (e): HOLD evidence carries mult/peak/age",
      r["evidence"]["mult"] == 1.2
      and r["evidence"]["peak_mult"] == 1.3
      and r["evidence"]["age_s"] == 3600.0)

r = CV.cover_decision({}, 1.0, NOW)
check("cover malformed: empty dict -> HOLD, never raises",
      r["action"] == "HOLD")
r = CV.cover_decision(None, None, None)
check("cover malformed: None position/price -> HOLD, never raises",
      r["action"] == "HOLD")
r = CV.cover_decision({"entry_price": "junk"}, "junk", "junk")
check("cover malformed: junk types -> HOLD, never raises",
      r["action"] == "HOLD")
r = CV.cover_decision(pos(entry_ts=0), 2.0, NOW)
check("cover: unknown entry time still fires leg_a_rung (no age)",
      r["action"] == "TAKE_PROFIT" and r["reason"] == "leg_a_rung")
r = CV.cover_decision(pos(entry_ts=0), 0.60, NOW)
check("cover: unknown age fails closed (no fast_loser)",
      r["reason"] != "fast_loser")

print("")
if fails:
    print("FAILURES: %d — %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("ALL LIMIT-EXITS TESTS PASSED")
