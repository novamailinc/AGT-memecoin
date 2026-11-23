#!/usr/bin/env python3
"""Watcher paper lane + realtime heartbeat tests (2026-10-23).

Covers watcher_lane.py: killswitch registration, pre-registered
terms, convergence-fire intake off the signal bus (event-driven:
a published fire is decided in the same process_new_fires cycle),
the anti-miss fire audit (opened / blocked / pending / dropped),
fail-closed safety, the $25k fresh-tape liquidity floor, the 25%
cost gate, dedupe-ever, transient retry + stale drop, F2 wiring,
the user's ladder, the pre-registered 100-close verdict (raw +
ex-top-1) halting the lane, and verdict-ledger chaining. Plus
heartbeat.py: merged-doc beats at the 2s cadence preserving the
watchdog's sections. No network: every feed is stubbed. Plain
script — run directly, not via pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import watcher_lane as W
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import trials as T
from daemon.launch_scanner import signal_bus as BUS
from daemon.launch_scanner import heartbeat as HB
from daemon.launch_scanner import f2 as F2

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()

# ---------------- killswitch registration ----------------
check("killswitch: watcher registered (window/min 100, halt)",
      "watcher" in KS.CFG and KS.CFG["watcher"]["window"] == 100
      and KS.CFG["watcher"]["min_trades"] == 100
      and KS.CFG["watcher"]["halt"] is True)
check("watcher: verdict sample constant is 100",
      W.VERDICT_SAMPLE_CLOSES == 100)
check("watcher: MAX_OPEN is 50 (bankroll binds first)",
      W.MAX_OPEN == 50)
check("watcher: $1 shots on a $100 bankroll",
      W.NOTIONAL_USD == 1.0 and W.BANKROLL_START == 100.0)

_ks_tmp = tempfile.mkdtemp(prefix="watcher-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")

_tmp = tempfile.mkdtemp(prefix="watcher-ledger-")
W.LEDGER_PATH = os.path.join(_tmp, "watcher_ledger.json")
led = W.ensure_ledger()
pre = led["preregistered_verdict"]
check("preregistration: lane watcher, sample 100",
      pre["lane"] == "watcher" and pre["sample_closes"] == 100)
check("preregistration: killswitch cfg stamped {100,100,halt}",
      pre["killswitch_cfg"] == {"window": 100, "min_trades": 100,
                                "halt": True})
check("preregistration: terms carry fail-closed safety, $25k floor, "
      "no daily cap, ex-top-1 reporting",
      "fail-closed" in pre["terms"]["safety_gate"]
      and pre["terms"]["runner_rule"]["min_liq_usd"] == 25000.0
      and pre["terms"]["daily_entry_cap"] is None
      and "ex-top-1" in pre["rule"])

# ---------------- trial registration ----------------
_reg_tmp = tempfile.mkdtemp(prefix="watcher-trials-")
T.REGISTRY_PATH = os.path.join(_reg_tmp, "trial_registry.json")
if "watcher" in T.BACKFILL_LEDGERS:
    check("trials: watcher ledger in BACKFILL_LEDGERS",
          T.BACKFILL_LEDGERS["watcher"] == "watcher_ledger.json")
rec = W.register_trial()
check("trials: register_trial returns an active watcher trial",
      rec is not None and rec["lane"] == "watcher"
      and rec["status"] == "active")
W.register_trial()
check("trials: registration is idempotent",
      len(T.trials_for("watcher")) == 1)

# ---------------- stubs (no network) ----------------
GOOD_TAPE = {"dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0,
             "chg_m5": 5.0, "price_usd": 0.001,
             "tape_source": "test"}
TAPES = {}
QUOTES = {}
SAFETY = {}


def _tape(chain, mint):
    t = TAPES.get(mint, dict(GOOD_TAPE))
    return dict(t) if isinstance(t, dict) else t


W._fresh_tape = _tape
W.quote_price = lambda chain, mint, spot_index=None: \
    QUOTES.get(mint, 0.001)


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return SAFETY.get(mint, {"passed": True, "score": 80,
                                 "flags": [], "fails": [],
                                 "unknown": False})


W._safety = _SafetyStub()

_bus_tmp = tempfile.mkdtemp(prefix="watcher-bus-")
BUS.BUS_PATH = os.path.join(_bus_tmp, "signal_bus.jsonl")


def fire(mint, symbol="WATCH", groups=3, wallets=None, ts=None,
         source="smart-watcher"):
    ev = {"smart_wallets": groups,
          "wallets": wallets or ["Wal%d" % i for i in range(groups)],
          "wallet_groups": ["g%d" % i for i in range(groups)],
          "safety": "ok",
          "components": {"convergence_groups": groups,
                         "sightings": groups,
                         "sol_spent_total": 4.2,
                         "safety_score": 80}}
    sig = BUS.make_signal("solana", source, mint, symbol=symbol,
                          tier="converged", mcap=500000.0,
                          liquidity=100000.0, evidence=ev)
    if ts is not None:
        sig["ts"] = ts
    return sig


def M(i):
    return "WatchMint%037d" % i


# ---------------- convergence intake: fire -> entry, same cycle --------
F1 = M(1)
sig1 = fire(F1)
check("bus: publish lands", BUS.publish(sig1))
res = W.process_new_fires(now=sig1["ts"] + 0.5)
check("intake: fire decided in the same cycle it landed",
      res["fires"] == 1 and res["opened"] == 1)
d = W._read()
pos = d["positions"][F1]
check("ledger: 1 open, bankroll $99 after the $1 shot",
      len(d["positions"]) == 1 and abs(d["bankroll"] - 99.0) < 1e-6)
check("entry: user ladder legs present (50/25/25, 2x/3x/trail30)",
      pos["legs"]["a"]["frac"] == 0.50
      and pos["legs"]["b"]["frac"] == 0.25
      and pos["legs"]["c"]["frac"] == 0.25
      and pos["legs"]["a"]["target"] == 2.0
      and pos["legs"]["b"]["target"] == 3.0
      and pos["legs"]["c"]["trail"] == 0.30)
ef = pos["entry_features"]
check("entry_features: convergence journaled (groups, wallets, lag)",
      ef["source"] == "smart-watcher-convergence"
      and ef["convergence_groups"] == 3
      and len(ef["wallets"]) == 3
      and ef["detection_lag_s"] is not None
      and ef["detection_lag_s"] < 2.0
      and ef["mcap"] == 500000.0 and ef["liquidity"] == 100000.0)
aud = [a for a in d["fire_audit"] if a["mint"] == F1]
check("fire audit: the fire is journaled as opened",
      len(aud) == 1 and aud[0]["outcome"] == "opened")

# dedupe-ever: a second fire for the same mint never re-enters
sig1b = fire(F1, ts=sig1["ts"] + 60)
BUS.publish(sig1b)
res = W.process_new_fires(now=sig1b["ts"] + 0.5)
d = W._read()
check("dedupe: same mint never opens twice",
      res["opened"] == 0 and len(d["positions"]) == 1)

# a fire that predates the lane is history, not a signal
F_OLD = M(2)
old_sig = fire(F_OLD, ts=led["created_ts"] - 100)
BUS.publish(old_sig)
res = W.process_new_fires(now=time.time())
check("intake: pre-lane fires are ignored (not counted, not traded)",
      res["fires"] == 0 and F_OLD not in W._read()["positions"])

# no proven convergence -> not a fire for this lane
F_NC = M(3)
BUS.publish(fire(F_NC, groups=1))
res = W.process_new_fires(now=time.time())
check("convergence: a single-wallet record is not a fire",
      res["fires"] == 0 and F_NC not in W._read()["positions"])
opened, why, final = W.consider_fire(fire(F_NC, groups=1), time.time())
check("convergence: consider_fire refuses <2 groups, final",
      not opened and final and "convergence" in why)

# a different source's signal is not ours
F_OS = M(4)
BUS.publish(fire(F_OS, source="degen-feed"))
res = W.process_new_fires(now=time.time())
check("intake: other sources' signals are not traded",
      res["fires"] == 0 and F_OS not in W._read()["positions"])

# ---------------- liquidity floor, fail-closed ----------------
F5 = M(5)
TAPES[F5] = dict(GOOD_TAPE, usd_liquidity=10000.0)
opened, why, final = W.consider_fire(fire(F5), time.time())
check("liq floor: liq $10k < $25k blocks (transient)",
      not opened and not final and "liq" in why)
F6 = M(6)
TAPES[F6] = None
opened, why, final = W.consider_fire(fire(F6), time.time())
check("liq floor: unknown tape fails closed (no entry)",
      not opened and "no live tape" in why)

# ---------------- safety gate fail-closed ----------------
F7 = M(7)
SAFETY[F7] = {"passed": False, "score": 10, "flags": [],
              "fails": ["honeypot"], "unknown": False}
opened, why, final = W.consider_fire(fire(F7), time.time())
check("safety: hard fail blocks, final, mint marked dead",
      not opened and final and F7 in W._read()["dead_mints"])
F8 = M(8)
SAFETY[F8] = {"passed": False, "score": 0, "flags": [], "fails": [],
              "unknown": True}
opened, why, final = W.consider_fire(fire(F8), time.time())
check("safety: unknown = no entry (transient)",
      not opened and not final and "unknown" in why)

# ---------------- cost gate ----------------
F9 = M(9)
_orig_gate = W._costs.cost_gate
W._costs.cost_gate = lambda chain, n, **kw: (True, "COST-BLOCKED test")
opened, why, final = W.consider_fire(fire(F9), time.time())
W._costs.cost_gate = _orig_gate
check("cost gate: a blocked cost gate blocks the entry",
      not opened and "COST-BLOCKED" in why)

# ---------------- transient retry + stale drop (the audit trail) -------
F10 = M(10)
SAFETY[F10] = {"passed": False, "score": 0, "flags": [], "fails": [],
               "unknown": True}
sig10 = fire(F10)
BUS.publish(sig10)
t10 = time.time()
res = W.process_new_fires(now=t10)
d = W._read()
check("pending: transiently blocked fire is parked, audited",
      res["pending"] == 1 and F10 in d["pending"]
      and any(a["mint"] == F10 and a["outcome"] == "pending"
              for a in d["fire_audit"]))
SAFETY[F10] = {"passed": True, "score": 80, "flags": [], "fails": [],
               "unknown": False}
res = W.process_new_fires(now=t10 + 5)
d = W._read()
check("pending: retry opens once the block clears",
      res["opened"] == 1 and F10 in d["positions"]
      and F10 not in d["pending"]
      and any(a["mint"] == F10 and a["outcome"] == "opened"
              for a in d["fire_audit"]))
F11 = M(11)
TAPES[F11] = None
sig11 = fire(F11)
BUS.publish(sig11)
t11 = time.time()
W.process_new_fires(now=t11)
res = W.process_new_fires(now=t11 + W.FIRE_TTL_S + 1)
d = W._read()
check("pending: a fire still blocked past the TTL is dropped, audited",
      res["dropped"] == 1 and F11 not in d["pending"]
      and any(a["mint"] == F11 and a["outcome"] == "dropped_stale"
              for a in d["fire_audit"]))

# ---------------- ladder exits on synthetic prices ----------------
ep1 = W._read()["positions"][F1]["entry_price"]
closes = W.poll_exits(price_fn=lambda c, m, s: ep1 * 2.1)
pos = W._read()["positions"][F1]
check("ladder: 2.1x banks leg A only",
      pos["legs"]["a"]["done"] and not pos["legs"]["b"]["done"]
      and not closes)
closes = W.poll_exits(price_fn=lambda c, m, s: ep1 * 0.4)
d = W._read()
f1_closes = [c for c in closes if c["mint"] == F1]
check("ladder: -60% hard-stops the rest and finalizes",
      len(f1_closes) == 1 and F1 not in d["positions"]
      and f1_closes[0]["closed_reason"] == "hard_stop_-50pct"
      and f1_closes[0]["legs"]["a"]["done"]
      and f1_closes[0]["realized_mult"] > 1.0)  # 0.5@2.1x + 0.5@0.4x

# ---------------- F2 wiring -------------------------------------------
F12 = M(12)
opened, why, final = W.consider_fire(fire(F12), time.time())
check("f2 setup: entry opened", opened)
ep12 = W._read()["positions"][F12]["entry_price"]
W.poll_exits(price_fn=lambda c, m, s: ep12 * 2.1)  # banks leg A, journals
with W._ledger_lock():
    d = W._read()
    jq = d["positions"][F12].setdefault("quote_journal", [])
    for e in jq:
        e["ts"] = time.time() - 400   # last real quote 400s ago
    W._write(d)
closes = W.poll_exits(price_fn=lambda c, m, s: 0.0)
check("f2: armed + 400s dark exits at the last real price, stamped",
      len(closes) == 1
      and closes[0]["closed_reason"] == F2.F2_REASON
      and closes[0].get("f2", {}).get("last_real_price") is not None
      and abs(closes[0]["f2"]["last_real_price"] - ep12 * 2.1) < 1e-9
      and closes[0]["f2"]["unquotable_seconds"] >= 300)
F13 = M(13)
opened, why, final = W.consider_fire(fire(F13), time.time())
closes = W.poll_exits(price_fn=lambda c, m, s: 0.0)
check("f2: never-armed position in the dark is NOT F2-exited",
      opened and not closes and F13 in W._read()["positions"])
with W._ledger_lock():
    d = W._read()
    del d["positions"][F13]   # test hygiene: retire the control
    W._write(d)

# ---------------- reserves refusal provenance (2026-11-22) --------
class _RefusingReserves:
    @staticmethod
    def price_for_position(pos):
        return 0.0, ("curve: implausible curve price 3.4e-04 USD "
                     "(band 1e-09..0.1); decode not trusted")


F14 = M(14)
opened, why, final = W.consider_fire(fire(F14), time.time())
ep14 = W._read()["positions"][F14]["entry_price"]
W.poll_exits(price_fn=lambda c, m, s: ep14 * 2.1)  # bank leg A, arm
_real_res = W._reserves
_real_qp = W.quote_price
_real_ft = W._fresh_tape
W._reserves = _RefusingReserves
W.quote_price = lambda chain, mint: 0.0
W._fresh_tape = lambda chain, mint: None
try:
    W._poll_exits(None)           # dark cycle: probe fires, refuses
finally:
    W._reserves = _real_res
    W.quote_price = _real_qp
    W._fresh_tape = _real_ft
pos14 = W._read()["positions"].get(F14) or {}
ref14 = [e for e in (pos14.get("quote_journal") or [])
         if e.get("src") == "reserves_refused"]
check("reserves refusal journalled with its reason",
      len(ref14) == 1 and "implausible" in (ref14[0].get("detail") or "")
      and ref14[0].get("px") is None)
check("one dark cycle is NOT an F2 close (position still open)",
      F14 in W._read()["positions"])
with W._ledger_lock():
    d = W._read()
    del d["positions"][F14]   # test hygiene
    W._write(d)

# verdict-ledger chain carries the closes so far
chain_path = os.path.join(_tmp, "verdict_ledger", "events.jsonl")
n_chain = 0
if os.path.exists(chain_path):
    with open(chain_path) as f:
        n_chain = sum(1 for ln in f if '"watcher"' in ln)
check("verdict ledger: watcher closes are chained (incl. the F2 one)",
      n_chain >= 2)

# ---------------- pre-registered verdict at 100 closes ----------------
n0 = len(W._read()["closes"])
need = W.VERDICT_SAMPLE_CLOSES - n0
for i in range(need):
    mint = M(1000 + i)
    opened, why, final = W.consider_fire(fire(mint), time.time())
    if not opened:
        check("verdict loop: entry %d opened (%s)" % (i, why), False)
        break
    ep = W._read()["positions"][mint]["entry_price"]
    W.poll_exits(price_fn=lambda c, m, s, _ep=ep: _ep * 0.4)
d = W._read()
check("verdict: 100 closes reached", len(d["closes"]) == 100)
check("verdict: recorded in-ledger, expectancy <= 0 -> HALT",
      d["verdict"] is not None and d["verdict"]["n_closes"] == 100
      and d["verdict"]["expectancy"] <= 0
      and "HALT" in d["verdict"]["action"])
check("verdict: ex-top-1 expectancy recorded alongside raw",
      d["verdict"].get("expectancy_ex_top1") is not None
      and d["verdict"]["expectancy_ex_top1"]
      <= d["verdict"]["expectancy"])
check("verdict: killswitch lane halted",
      KS.is_halted("watcher"))
opened, why, final = W.consider_fire(fire(M(2000)), time.time())
check("verdict: halted lane takes no new entries",
      not opened and final)
sig_h = fire(M(2001))
BUS.publish(sig_h)
res = W.process_new_fires(now=time.time())
d = W._read()
check("halt audit: a fire on the halted lane is journaled blocked",
      any(a["mint"] == M(2001) and a["outcome"] == "blocked"
          and "halted" in a["reason"] for a in d["fire_audit"]))

# ---------------- heartbeat (2s cadence, merged doc) -------------------
_hb_tmp = tempfile.mkdtemp(prefix="watcher-hb-")
os.makedirs(os.path.join(_hb_tmp, "run"), exist_ok=True)
HB.HEARTBEAT_PATH = os.path.join(_hb_tmp, "run", "heartbeat.json")
with open(HB.HEARTBEAT_PATH, "w") as f:
    json.dump({"checked_at": "2026-10-23T00:00:00+00:00",
               "daemons": ["telegram: running (pid 1)"],
               "actions": "none"}, f)
check("heartbeat: beat lands", HB.beat("watcher-trader") is True)
doc = json.load(open(HB.HEARTBEAT_PATH))
check("heartbeat: watchdog sections preserved, beat recorded",
      doc.get("actions") == "none" and doc.get("checked_at")
      and "watcher-trader" in (doc.get("beats") or {})
      and doc["beats"]["watcher-trader"]["pid"] == os.getpid())
check("heartbeat: second source merges, first survives",
      HB.beat("realtime-sync") is True
      and set(HB.read_beats()) == {"watcher-trader", "realtime-sync"})
check("heartbeat: freshness reads the 2s cadence",
      HB.beats_fresh("watcher-trader")
      and HB.beats_age("watcher-trader") < 5
      and not HB.beats_fresh("watcher-trader",
                             now=time.time() + 3600))
bt = HB.Beater("x")
check("heartbeat: Beater first tick due, then gated by interval",
      bt.due(now=1000.0) and not bt.due(now=1001.0)
      and bt.due(now=1002.1))

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f_ in fails:
        print(" -", f_)
    sys.exit(1)
print("ALL WATCHER TESTS PASSED")
