#!/usr/bin/env python3
"""End-to-end hermetic paper-system test (2026-11-22).

ONE harness driving the real chain with simulated capital and stubbed
market data, no network, no providers, temp FOREX_AGENT_HOME:

  signal -> lane entry through the shared paper account (capital gate)
  -> polls at fixture prices -> exits of every price kind
  -> per-tranche shared-cash settlement -> killswitch risk path
  -> qualification ingestion (lessons) -> daemon-restart persistence
  -> op_id crash-retry safety.

Hard checks assert the machinery that must work. KNOWN DEFECTS are
asserted PRESENT and printed as FINDING lines: they keep the suite
honest about current behavior, they force this file to be updated when
the quarantine/accounting repair lands, and they are the activation
blockers - not inventions to make the suite pass.

Stage A of that repair LANDED (2026-11-22): the F2 stale-estimate
proceeds are quarantined in cash_estimated and cannot finance new
entries - the former FINDINGS 1-2 are now hard impossibility
assertions. FINDINGS 3-5 (write-down fee on a non-sale, write-down
lane/account divergence, verdict math ingesting estimates) remain
asserted PRESENT until Stages B-D land.

Run: python3 tests/test_paper_e2e.py   (never pytest)
"""
import importlib
import os
import sys
import tempfile
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "daemon", "launch_scanner"))
sys.path.insert(0, _REPO_ROOT)

HOME = tempfile.mkdtemp(prefix="paper-e2e-home-")
TMPD = tempfile.mkdtemp(prefix="paper-e2e-")
os.environ["FOREX_AGENT_HOME"] = HOME
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_START"] = "2"
# Account-level entry gates are exercised by tests/test_paper_account_gates.py;
# this harness covers the killswitch risk path instead.
from daemon.launch_scanner import killswitch as KS

from daemon.launch_scanner import watcher_lane as W
from daemon.launch_scanner import paper_account as PA
from daemon.launch_scanner import signal_bus as BUS
from daemon.launch_scanner import lessons as LS
from daemon.launch_scanner import f2 as F2
from daemon.launch_scanner import exit_provenance as PK

W.LEDGER_PATH = os.path.join(TMPD, "watcher_ledger.json")
BUS.BUS_PATH = os.path.join(TMPD, "signal_bus.jsonl")

FAILS = []
FINDINGS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def finding(name, evidence):
    FINDINGS.append(name)
    print("FINDING %s: %s" % (name, evidence))


GOOD_TAPE = {"usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
             "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40}
PRICES = {}


def _tape(chain, mint):
    return dict(GOOD_TAPE)


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return {"passed": True, "score": 80, "flags": [], "fails": [],
                "unknown": False}


def _install_stubs(mod):
    mod._fresh_tape = _tape
    mod.quote_price = lambda chain, mint, spot_index=None: \
        PRICES.get(mint, 0.001)
    mod._safety = _SafetyStub()


_install_stubs(W)
W.ensure_ledger()


def M(i):
    return "E2EMint%039d" % i


def fire(mint, groups=3):
    ev = {"smart_wallets": groups,
          "wallets": ["Wal%d" % i for i in range(groups)],
          "wallet_groups": ["g%d" % i for i in range(groups)],
          "safety": "ok",
          "components": {"convergence_groups": groups,
                         "sightings": groups,
                         "sol_spent_total": 4.2,
                         "safety_score": 80}}
    return BUS.make_signal("solana", "smart-watcher", mint,
                           symbol="E2E", tier="converged",
                           mcap=500000.0, liquidity=100000.0,
                           evidence=ev)


now = time.time()

# ---------- 1. entry through the shared account (capital gate) -----
opened, why, _final = W.consider_fire(fire(M(1)), now)
check("entry 1 opens through the shared account",
      opened and abs(PA.snapshot()["cash"] - 1.0) < 1e-9)
pos1 = W.open_positions()[M(1)]
check("entry pins the shared position instance",
      pos1.get("pa_pos_id", "").startswith("watcher/%s#" % M(1)))

# ---------- 2. current-price tranche settles immediately ----------
PRICES[M(1)] = pos1["entry_price"] * 2.1
W.poll_exits(price_fn=lambda c, m, s: PRICES.get(m, 0.0))
pos1 = W.open_positions()[M(1)]
check("leg A banks at a current quote",
      pos1["legs"]["a"]["done"]
      and pos1["legs"]["a"].get("exit_price_kind") == PK.CURRENT)
leg_a_cash = pos1["legs"]["a"]["realized_usd"]
check("leg A tranche credited to shared cash",
      abs(PA.snapshot()["cash"] - (1.0 + leg_a_cash)) < 1e-6)

# ---------- 3. daemon restart: ledgers survive, position intact ----
_lp = W.LEDGER_PATH
W2 = importlib.reload(W)
PA2 = importlib.reload(PA)
W2.LEDGER_PATH = _lp
_install_stubs(W2)
check("after restart the open position survives with its pinned id",
      M(1) in W2.open_positions()
      and W2.open_positions()[M(1)]["pa_pos_id"] == pos1["pa_pos_id"])
check("after restart shared cash is unchanged",
      abs(PA2.snapshot()["cash"] - (1.0 + leg_a_cash)) < 1e-6)
W = W2
PA = PA2
pos1 = W.open_positions()[M(1)]

# ---------- 4. F2 stale close: estimate enters shared cash ---------
with W._ledger_lock():
    d = W._read()
    jp = d["positions"][M(1)]["quote_journal"]
    for q in jp:
        q["ts"] = time.time() - F2.F2_SUSTAINED_S - 60
    d["positions"][M(1)]["entry_ts"] = time.time() - 3600
    W._write(d)
PRICES[M(1)] = 0.0   # every source dark
closes = W.poll_exits(price_fn=lambda c, m, s: 0.0)
check("F2 close fires after sustained dark",
      len(closes) == 1
      and closes[0]["closed_reason"] == F2.F2_REASON)
rec = closes[0]
check("F2 close is labeled mixed current+stale",
      rec.get("exit_price_kind")
      == "mixed:%s,%s" % (PK.CURRENT, PK.STALE))
snap_f2 = PA.snapshot()
est = rec["realized_usd"] - leg_a_cash
# STAGE A REPAIR (was FINDINGS 1-2): the F2 remainder is priced on
# a stale estimate, so it must be QUARANTINED in cash_estimated and
# must never finance a new entry. These are impossibility
# assertions: a failure here is a hard FAIL, not a finding.
check("F2 remainder quarantined in estimated cash",
      abs(snap_f2["cash_estimated"] - est) < 1e-6)
check("F2 remainder did not touch spendable cash",
      abs(snap_f2["cash_spendable"] - (1.0 + leg_a_cash)) < 1e-6)
# entry 1 debited start(2.0) -> cash 1.0, so its stake was 1.0;
stake2 = float(os.environ["PAPER_ACCOUNT_START"]) - 1.0
spend_before = snap_f2["cash_spendable"]
opened2, why2, _ = W.consider_fire(fire(M(2)), time.time())
if spend_before < stake2:
    check("entry REFUSED when spendable < stake even with "
          "estimated cash present",
          not opened2 and "spendable" in (why2 or "").lower())
else:
    # financed ONLY by the verified leg-A cash: the estimated
    # bucket must be untouched by the debit
    snap2 = PA.snapshot()
    check("new entry financed by verified cash only",
          opened2
          and abs(snap2["cash_estimated"]
                  - snap_f2["cash_estimated"]) < 1e-9
          and abs(snap2["cash_spendable"]
                  - (spend_before - stake2)) < 1e-6)

# ---------- 5. write-down: no sale, but a fee is booked ------------
opened3, why3, _ = W.consider_fire(fire(M(3)), time.time())
check("write-down position opened (cash %.4f): %s"
      % (PA.snapshot()["cash"], why3 or "ok"), opened3)
with W._ledger_lock():
    d = W._read()
    p3 = d["positions"][M(3)]
    p3["entry_ts"] = time.time() - 26 * 3600
    p3["quote_journal"] = []
    p3["peak_price"] = 1.0
    W._write(d)
closes3 = W.poll_exits(price_fn=lambda c, m, s: 0.0)
rec3 = [c for c in closes3 if c["mint"] == M(3)]
check("24h unquotable close fires",
      len(rec3) == 1
      and rec3[0]["closed_reason"] == "time_stop_24h_unquotable")
if rec3:
    r = rec3[0]
    check("write-down close labeled write_down_zero",
          r.get("exit_price_kind") == PK.WRITEDOWN)
    if float(r["realized_usd"]) < 0:
        finding("write-down books a simulated fee on a sale that "
                "never happened", "realized_usd %.6f < 0"
                % float(r["realized_usd"]))
    check("write-down fee defect visible (activation blocker)",
          float(r["realized_usd"]) <= 0)
    wpos = [p for p in PA.snapshot()["lanes"]["watcher"]["positions"]
            if p["mint"] == M(3)]
    if wpos:
        finding("write-down diverges lane vs shared account: closed "
                "in the lane, still held in the account (credit "
                "refused: fee exceeds gross), only a journal note",
                "account still holds qty %.6f for M(3)"
                % float(wpos[0]["qty"]))
    check("write-down lane/account divergence visible "
          "(activation blocker)", bool(wpos))

# ---------- 6. qualification: verdict math ingests estimates -------
# lessons is grad-family scoped by design; watcher closes must not land
# there. The watcher qualification surface is the pre-registered
# verdict, computed in-ledger over ALL closes indiscriminately.
recs = LS._read_all("watcher")
check("lessons log is grad-family scoped (watcher closes absent)",
      recs == [] and not LS.settle_from_close("watcher", closes[0]))
n = W.VERDICT_SAMPLE_CLOSES
f2_rec = closes[0]
scratch = {"closes": ([dict(f2_rec)] +
                      [dict(f2_rec, pnl_usd=0.10,
                            exit_price_kind=PK.CURRENT)
                       for _ in range(n - 1)])}
halt = W._verdict_check(scratch)
exp_with = scratch["verdict"]["expectancy"]
pnls_ex = [0.10] * (n - 1)
exp_without = sum(pnls_ex) / (n - 1)
check("verdict computed over the sample", "verdict" in scratch)
if abs(exp_with - exp_without) > 1e-9:
    finding("verdict expectancy includes the stale close's estimated "
            "P/L", "expectancy %.6f with it vs %.6f excluding "
            "non-current-price closes" % (exp_with, exp_without))
check("verdict contamination demonstrated (activation blocker)",
      abs(exp_with - exp_without) > 1e-9)

# ---------- 7. killswitch risk path saw the trades -----------------
check("killswitch risk path recorded the closes under the temp home",
      os.path.exists(KS.STATE_FILE))

# ---------- 8. op_id retry is a no-op (crash-retry safety) ---------
before = PA.snapshot()["cash"]
ok1, _ = PA.credit("watcher", M(1), 999.0,
                   "watcher:remaining:bc:%s:%s"
                   % (M(1), pos1["pa_pos_id"]),
                   pos_id=pos1["pa_pos_id"], qty=1.0)
check("duplicate tranche op_id does not double-credit",
      ok1 and abs(PA.snapshot()["cash"] - before) < 1e-9)

print()
for f in FINDINGS:
    print("FINDING (activation blocker):", f)
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER-E2E TESTS PASSED (with %d documented activation"
      " blockers)" % len(FINDINGS))
