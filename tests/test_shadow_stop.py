#!/usr/bin/env python3
"""Shadow-stop tests (2026-11-20, faster-exits item 1c).

Covers daemon/launch_scanner/shadow_stop.py: the -30%-within-15-min
rule (pure, no network), first-crossing journaling, the leg-aware
shadow-vs-live comparison, and — per live lane — the engine
integration: a fake price path through the lane's real poll_exits
journals the shadow crossing WITHOUT changing the live exit, and
the close record carries it.

Lanes proven here: grad_select / grad_raw / tranche /
tranche_protect (all four run grad.py's poll_exits as isolated
module instances — verified by code file, not assumed) and
momentum (momentum_lane.py's own poll_exits).

No network (every price is a fake price_fn), no live ledger
(temp ledgers only), no Helius. Run directly:
  python3 tests/test_shadow_stop.py
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import shadow_stop as SS
from daemon.launch_scanner import grad as G
from daemon.launch_scanner import tranche as TR
from daemon.launch_scanner import tranche_protect as TP
from daemon.launch_scanner import momentum_lane as M

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


NOW = 1795000000.0
ENTRY = 0.001  # fake entry price; multiples are what matter


# ---------------- would_fire: the rule itself ----------------
check("rule: 0.70x inside the window fires",
      SS.would_fire(ENTRY, 0.70 * ENTRY, 60))
check("rule: 0.69x fires", SS.would_fire(ENTRY, 0.69 * ENTRY, 60))
check("rule: 0.71x does not fire",
      not SS.would_fire(ENTRY, 0.71 * ENTRY, 60))
check("rule: 0.50x after 15 min does NOT fire (window over)",
      not SS.would_fire(ENTRY, 0.50 * ENTRY, 900))
check("rule: 0.50x at 899s fires",
      SS.would_fire(ENTRY, 0.50 * ENTRY, 899))
check("rule: future-dated age never fires",
      not SS.would_fire(ENTRY, 0.50 * ENTRY, -5))
check("rule: zero/unknown price never fires",
      not SS.would_fire(ENTRY, 0, 60)
      and not SS.would_fire(0, 0.5 * ENTRY, 60))
check("rule: custom stop mult honored",
      SS.would_fire(ENTRY, 0.79 * ENTRY, 60, stop_mult=0.80)
      and not SS.would_fire(ENTRY, 0.81 * ENTRY, 60, stop_mult=0.80))

# ---------------- note_quote: first crossing only ----------------
pos = {"entry_price": ENTRY, "entry_ts": NOW - 60}
check("note: no crossing above the stop",
      SS.note_quote(pos, 0.9 * ENTRY, NOW) is None
      and "shadow_stop" not in pos)
rec = SS.note_quote(pos, 0.65 * ENTRY, NOW + 10)
check("note: first crossing journaled (price/mult/ts/age)",
      rec is not None and abs(rec["mult"] - 0.65) < 1e-9
      and rec["price"] == 0.65 * ENTRY and rec["age_s"] == 70
      and pos["shadow_stop"] is rec)
rec2 = SS.note_quote(pos, 0.20 * ENTRY, NOW + 20)
check("note: later (deeper) eval never overwrites the first",
      rec2 is rec and rec["mult"] == 0.65)
old = {"entry_price": ENTRY, "entry_ts": NOW - 3600}
check("note: old position never journals",
      SS.note_quote(old, 0.2 * ENTRY, NOW) is None)
check("note: bad position/price never raises",
      SS.note_quote(None, 0.5 * ENTRY, NOW) is None
      and SS.note_quote({}, 0.5 * ENTRY, NOW) is None)

# ---------------- first_crossing: journal replay ----------------
journal = [{"ts": NOW + 10, "px": 0.95 * ENTRY},
           {"ts": NOW + 30, "px": 0.68 * ENTRY},
           {"ts": NOW + 50, "px": 0.40 * ENTRY}]
cross = SS.first_crossing(ENTRY, NOW, journal)
check("replay: first crossing found at 0.68x, age 30s",
      cross is not None and abs(cross["mult"] - 0.68) < 1e-9
      and cross["age_s"] == 30)
check("replay: unsorted journal tolerated",
      SS.first_crossing(ENTRY, NOW, list(reversed(journal))) == cross)
check("replay: no crossing above the stop",
      SS.first_crossing(ENTRY, NOW,
                        [{"ts": NOW + 10, "px": 0.9 * ENTRY}]) is None)
check("replay: crossing after the window ignored",
      SS.first_crossing(ENTRY, NOW,
                        [{"ts": NOW + 1000, "px": 0.5 * ENTRY}]) is None)
check("replay: journal starting after the window is no coverage, "
      "not a crossing",
      SS.first_crossing(ENTRY, NOW,
                        [{"ts": NOW + 1000, "px": 0.9 * ENTRY}]) is None)

# ---------------- shadow_outcome: leg-aware comparison ----------------


def _close(legs, realized_usd, realized_mult, journal=None,
           entry_ts=NOW, shadow=None):
    c = {"entry_price": ENTRY, "entry_ts": entry_ts,
         "realized_usd": realized_usd, "realized_mult": realized_mult,
         "closed_reason": "hard_stop_-50pct", "legs": legs,
         "quote_journal": journal or []}
    if shadow is not None:
        c["shadow_stop"] = shadow
    return c


def _leg(exit_mult, usd, done=True):
    return {"done": done, "exit_price": exit_mult * ENTRY,
            "realized_usd": usd, "exit_reason": "hard_stop_-50pct"}


# Leg A banked at 2x ($25 on a $25 shot: 50% x 2x), legs B/C
# stopped at 0.10x. Live realized $25 + $2.50 + $2.50 = $30.
# Shadow at 0.65x: B/C re-price to 0.65/0.10 of their live dollars.
banked = _close(
    {"a": _leg(2.0, 25.0), "b": _leg(0.10, 2.50), "c": _leg(0.10, 2.50)},
    30.0, 1.20,
    journal=[{"ts": NOW + 30, "px": 0.65 * ENTRY}])
out = SS.shadow_outcome(banked)
check("outcome: banked leg keeps live dollars, unbanked re-priced",
      out["covered"] and out["crossed"]
      and abs(out["shadow_usd"] - (25.0 + 2.50 * 6.5 * 2)) < 0.01
      and out["saved_usd"] > 0)
# Same close, but live realized $5 (no bank, stopped at 0.20x):
# shadow at 0.65x saves money.
loser = _close(
    {"a": _leg(0.20, 2.50), "b": _leg(0.20, 1.25),
     "c": _leg(0.20, 1.25)},
    5.0, 0.20, journal=[{"ts": NOW + 30, "px": 0.65 * ENTRY}])
out = SS.shadow_outcome(loser)
check("outcome: shadow saves on a crash close (0.65x vs 0.20x)",
      out["crossed"] and abs(out["shadow_usd"] - 16.25) < 0.01
      and abs(out["saved_usd"] - 11.25) < 0.01)
# Winner that dipped to 0.65x early then closed at 1.5x: shadow
# would have cut it — counted as a cut, honestly.
winner = _close(
    {"a": _leg(2.0, 25.0), "b": _leg(3.0, 18.75),
     "c": _leg(1.5, 9.375)},
    53.125, 2.125, journal=[{"ts": NOW + 30, "px": 0.65 * ENTRY}])
out = SS.shadow_outcome(winner)
check("outcome: shadow cuts a winner that later recovered",
      out["crossed"] and out["saved_usd"] < 0)
# Journal starting after the window: uncovered, never guessed.
late = _close({"a": _leg(0.2, 5.0)}, 5.0, 0.20,
              journal=[{"ts": NOW + 3600, "px": 0.2 * ENTRY}])
out = SS.shadow_outcome(late)
check("outcome: late journal is uncovered, no invented crossing",
      not out["covered"] and not out["crossed"]
      and out["saved_usd"] is None)
# Live shadow_stop field is authoritative over the journal.
fielded = _close({"a": _leg(0.2, 5.0)}, 5.0, 0.20, journal=[],
                 shadow={"ts": NOW + 45, "price": 0.68 * ENTRY,
                         "mult": 0.68, "age_s": 45, "stop_mult": 0.70})
out = SS.shadow_outcome(fielded)
check("outcome: live shadow_stop field is used when present",
      out["covered"] and out["crossed"]
      and abs(out["shadow_usd"] - 5.0 * 0.68 / 0.20) < 0.01)

# ---------------- engine integration: fake price paths ----------------


class _FakeKS:
    def __init__(self):
        self.recorded = []

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def is_halted(self, lane):
        return False

    def gate(self, lane):
        return not self.is_halted(lane)

    def halt_lane(self, lane, reason=""):
        return {"lane": lane, "action": "HALTED"}


def _grad_position(mint, entry_ts):
    return {"mint": mint, "symbol": "TKN", "chain": "solana",
            "source": "graduation", "entry_price": ENTRY,
            "entry_ts": entry_ts, "peak_price": ENTRY,
            "tokens": 25000.0, "realized_usd": 0.0,
            "legs": {"a": {"done": False, "frac": 0.50,
                           "target": 2.0, "exit_price": 0,
                           "realized_usd": 0, "exit_reason": ""},
                     "b": {"done": False, "frac": 0.25,
                           "target": 3.0, "exit_price": 0,
                           "realized_usd": 0, "exit_reason": ""},
                     "c": {"done": False, "frac": 0.25,
                           # Real positions carry leg-c "peak" (the
                           # trail reference, set to the fill at
                           # entry — grad.py line ~932); omitting it
                           # raises KeyError in the trail check and
                           # the per-position handler skips the write.
                           "peak": ENTRY,
                           "trail": 0.30, "exit_price": 0,
                           "realized_usd": 0, "exit_reason": ""}},
            "entry_snapshot": {"liquidity": 15000.0},
            "entry_features": {}}


def _run_grad_engine(eng, mint):
    """Open one young fake position, poll at 0.65x then 0.40x.
    Returns (pos_after_first_poll, close_record_or_None)."""
    tmp = tempfile.mkdtemp(prefix="shadow-grad-")
    eng.LEDGER_PATH = os.path.join(tmp, "ledger.json")
    eng._ks = _FakeKS()
    eng._jup = None
    eng._reserves = None
    eng._lessons = None
    eng.ensure_ledger()
    with eng._ledger_lock():
        d = eng._read()
        d["positions"][mint] = _grad_position(mint, time.time() - 60)
        eng._write(d)
    closes1 = eng.poll_exits(lambda c, m, s: 0.65 * ENTRY)
    pos1 = eng._read()["positions"].get(mint)
    closes2 = eng.poll_exits(lambda c, m, s: 0.40 * ENTRY)
    d = eng._read()
    rec = (d["closes"] or [{}])[-1] if closes2 else None
    return closes1, pos1, closes2, rec


for lane, eng in [("grad_select", G),
                  ("tranche", TR.make_engine()),
                  ("tranche_protect", TP.make_engine())]:
    # grad_raw shares grad.py with grad_select; the grad engine
    # instance above IS its code path (proven by code file in
    # test_exit_cadence.py) — the tranche engines are separate
    # instances with their own lane overrides, so they get their
    # own run each.
    mint = "SHDW%s pump" % lane[:4]
    mint = "SHDW%spump" % lane[:4].replace("_", "")
    c1, pos1, c2, rec = _run_grad_engine(eng, mint)
    check("engine %s: 0.65x eval does NOT exit (live stop is -50%%, "
          "shadow never gates)" % lane,
          c1 == [] and pos1 is not None)
    check("engine %s: shadow crossing journaled live at 0.65x" % lane,
          isinstance(pos1.get("shadow_stop"), dict)
          and abs(pos1["shadow_stop"]["mult"] - 0.65) < 1e-9
          and pos1["shadow_stop"]["age_s"] < 900)
    check("engine %s: 0.40x eval DOES close on the live -50%% stop"
          % lane,
          len(c2) == 1
          and rec.get("closed_reason") == "hard_stop_-50pct")
    check("engine %s: close record carries the shadow crossing"
          % lane,
          isinstance(rec.get("shadow_stop"), dict)
          and abs(rec["shadow_stop"]["mult"] - 0.65) < 1e-9)


def _run_momentum():
    tmp = tempfile.mkdtemp(prefix="shadow-mom-")
    M.LEDGER_PATH = os.path.join(tmp, "momentum_ledger.json")
    M._ks = _FakeKS()
    M.ensure_ledger()
    mint = "SHDWmom11111111111111111111111111111111111"
    pos = _grad_position(mint, time.time() - 60)
    pos["entry_snapshot"] = {"liquidity": 100000.0}
    with M._ledger_lock():
        d = M._read()
        d["positions"][mint] = pos
        M._write(d)
    c1 = M.poll_exits(
        price_fn=lambda c, m, s: 0.65 * ENTRY if m == mint else ENTRY)
    pos1 = M._read()["positions"].get(mint)
    c2 = M.poll_exits(
        price_fn=lambda c, m, s: 0.40 * ENTRY if m == mint else ENTRY)
    d = M._read()
    rec = (d["closes"] or [{}])[-1] if c2 else None
    return c1, pos1, c2, rec


c1, pos1, c2, rec = _run_momentum()
check("engine momentum: 0.65x eval does NOT exit (shadow never "
      "gates)", c1 == [] and pos1 is not None)
check("engine momentum: shadow crossing journaled live at 0.65x",
      isinstance(pos1.get("shadow_stop"), dict)
      and abs(pos1["shadow_stop"]["mult"] - 0.65) < 1e-9)
check("engine momentum: 0.40x eval DOES close on the live -50% stop",
      len(c2) == 1 and rec.get("closed_reason") == "hard_stop_-50pct")
check("engine momentum: close record carries the shadow crossing",
      isinstance(rec.get("shadow_stop"), dict)
      and abs(rec["shadow_stop"]["mult"] - 0.65) < 1e-9)

# grad_raw: same code as grad_select's engine (grad.py) — assert
# by code file + name rather than re-running the identical path
# (the shared-code proof also lives in test_exit_cadence.py).
from daemon.launch_scanner import grad_raw as RAW
_raw_eng = RAW.make_engine()
check("engine grad_raw: poll_exits IS grad.py's code (same file, "
      "same function, own module instance)",
      _raw_eng.poll_exits.__code__.co_filename ==
      G.poll_exits.__code__.co_filename
      and _raw_eng.poll_exits.__code__.co_name ==
      G.poll_exits.__code__.co_name
      and _raw_eng.LANE == "grad_raw")

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("all shadow-stop tests passed")
