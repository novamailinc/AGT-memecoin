#!/usr/bin/env python3
"""CEO account + bot-self-audit tests (pure logic + injected feeds).

Covers ceo.py ($100 bankroll discipline, bus watermark idempotency,
dedupe + 24h cooldown, cost gate, the user's exit ladder, killswitch
halt rule) and bot-self-audit's decision functions (supervised-list
derivation from the watchdog, repair planning, lock classification,
log rotation) with monkeypatched system state. No network.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import fcntl
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import ceo as C
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import signal_bus as BUS

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- killswitch registration ----------------
check("killswitch: ceo lane registered",
      "ceo" in KS.CFG and KS.CFG["ceo"]["min_trades"] == 15
      and KS.CFG["ceo"]["halt"] is True)


# ---------------- harness ----------------
TMPD = tempfile.mkdtemp(prefix="ceo-test-")
C.LEDGER_PATH = os.path.join(TMPD, "ceo_ledger.json")
C.BUS_PATH = os.path.join(TMPD, "signal_bus.jsonl")


class FakeKS:
    """Mirrors killswitch CFG semantics for the ceo lane (window 30,
    min_trades 15, halt on negative rolling expectancy)."""

    def __init__(self):
        self.halted = set()
        self.recorded = []

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        if len(self.recorded) >= 15:
            exp = sum(self.recorded[-30:]) / len(self.recorded[-30:])
            if exp < 0:
                self.halted.add(lane)
        return {"lane": lane}


FAKE_KS = FakeKS()


_RUNNER_TAPE = {"usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
                "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40}


def _patch_all(price=0.01, patch_quote=True):
    p1 = patch.object(C, "_veto")
    p2 = patch.object(C, "_safety")
    p3 = patch.object(C, "_ks", FAKE_KS)
    # patch_quote=False leaves the REAL quote_price in place so the
    # entry-quote tests can drive it via _http_json/_fresh_tape stubs.
    p4 = (patch.object(C, "quote_price", return_value=price)
          if patch_quote else None)
    p5 = patch.object(C, "_fresh_tape", return_value=dict(_RUNNER_TAPE))
    # Consecutive-confirmation gap zeroed: the gate still performs both
    # reads, the suite just doesn't wait 5s between them.
    p6 = patch.object(C, "RUNNER_CONFIRM_GAP_S", 0.0)
    m_veto = p1.start()
    m_safety = p2.start()
    p3.start()
    if p4 is not None:
        p4.start()
    p5.start()
    p6.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = {"passed": True, "unknown": False}
    return tuple(p for p in (p1, p2, p3, p4, p5, p6) if p is not None)


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.01, patch_quote=True):
    """Fresh $100 ledger + empty bus, deps patched. Returns patches."""
    for p in (C.LEDGER_PATH, C.BUS_PATH):
        if os.path.exists(p):
            os.remove(p)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps = _patch_all(price=price, patch_quote=patch_quote)
    C.ensure_ledger()
    return ps


def _sig(chain="solana", mint="MINT1", **kw):
    args = dict(chain=chain, source="test-feed", mint=mint, symbol="TKN",
                score=70.0, tier="pick", mcap=500_000.0, liquidity=50_000.0)
    args.update(kw)
    return BUS.make_signal(**args)


def _ledger():
    with open(C.LEDGER_PATH) as f:
        return json.load(f)


def _prices(seq):
    it = iter(seq)

    def fn(chain, mint, spot_index=None):
        try:
            return next(it)
        except StopIteration:
            return seq[-1]
    return fn


# ---------------- ledger creation + bankroll math ----------------
ps = _fresh()
st = C.account_state()
check("ceo: ledger created at $100.00",
      st["bankroll"] == 100.00 and st["bankroll_start"] == 100.00
      and st["state"] == "active")
check("ceo: watermark starts at bus EOF (0 for empty bus)",
      _ledger()["watermark_offset"] == 0)

ok, why = C.consider_card(_sig())
check("ceo: solana card opens $1 position", ok)
check("ceo: entry debits exactly $1.00", C.account_cash() == 99.00)
pos = C.open_positions().get("MINT1")
check("ceo: position recorded with legs + features",
      pos is not None and "entry_features" in pos
      and pos["entry_features"]["chain"] == "solana"
      and set(pos["legs"].keys()) == {"a", "b", "c"})
check("ceo: solana gas deducted from notional ($0.001)",
      abs(pos["tokens"] * pos["entry_price"] - 0.999) < 1e-6)
check("ceo: entry is adverse vs quoted (honest fill)",
      pos["entry_price"] >= 0.01)

ok, why = C.consider_card(_sig())
check("ceo: duplicate open mint skipped", not ok and "already open" in why)
check("ceo: duplicate costs nothing", C.account_cash() == 99.00)

ok, why = C.consider_card(_sig(chain="ethereum", mint="MINTE"))
check("ceo: ethereum COST-BLOCKED at $1 notional",
      not ok and "COST-BLOCKED" in why)
check("ceo: cost-blocked ETH debits nothing", C.account_cash() == 99.00)
check("ceo: cost-blocked ETH opens nothing",
      "MINTE" not in C.open_positions())

ok, why = C.consider_card(_sig(chain="base", mint="MINTB"))
posb = C.open_positions().get("MINTB")
check("ceo: base opens, $0.03 gas from notional",
      ok and abs(posb["tokens"] * posb["entry_price"] - 0.97) < 1e-6)
check("ceo: cash after two entries = $98.00", C.account_cash() == 98.00)

ok, why = C.consider_card(_sig(chain="hyperliquid", mint="HLCOIN",
                               extra={"spot_index": 7, "mark_px": 2.0}))
# quote_price is patched to 0.01 for all chains in this harness block is
# false for HL below; here quote returns 0.01, still opens.
check("ceo: hyperliquid card opens", ok)
_unpatch(ps)

# HL fallback: no live quote -> listing mark_px on the event is used
ps = _fresh()
with patch.object(C, "quote_price", return_value=0.0):
    ok, why = C.consider_card(_sig(chain="hyperliquid", mint="HLONLY",
                                   extra={"spot_index": 3,
                                          "mark_px": 2.0}))
pos = C.open_positions().get("HLONLY")
check("ceo: HL opens from event mark_px when quote is dark",
      ok and pos is not None and pos["quoted_price"] == 2.0)
check("ceo: HL taker fee deducted (0.035%)",
      abs(pos["tokens"] * pos["entry_price"] - (1 - 0.00035)) < 1e-6)
_unpatch(ps)

# veto re-check at entry
ps = _fresh()
with patch.object(C, "_veto") as mv:
    mv.veto_check.return_value = {"passed": False, "vetoes": ["bundle: 80%"]}
    ok, why = C.consider_card(_sig(mint="MINTV"))
check("ceo: veto at entry blocks", not ok and "veto" in why.lower())
check("ceo: vetoed entry debits nothing", C.account_cash() == 100.00)
_unpatch(ps)

# runner gate at entry (2026-10-21): Solana entries must be runner-grade
# on the live tape — thin liq / low mcap / falling / unknown all block.
ps = _fresh()
with patch.object(C, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE, usd_liquidity=9_000.0)):
    ok, why = C.consider_card(_sig(mint="THIN1"))
check("ceo: runner gate blocks thin liquidity",
      not ok and "runner gate" in why and "liq" in why)
with patch.object(C, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE,
                                    usd_market_cap=20_000.0)):
    ok, why = C.consider_card(_sig(mint="LOWMC"))
check("ceo: runner gate blocks low mcap",
      not ok and "runner gate" in why and "mcap" in why)
with patch.object(C, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE, chg_m5=-4.0)):
    ok, why = C.consider_card(_sig(mint="FALL1"))
check("ceo: runner gate blocks falling tape",
      not ok and "runner gate" in why and "not rising" in why)
with patch.object(C, "_fresh_tape", return_value=None):
    ok, why = C.consider_card(_sig(mint="DARK1"))
check("ceo: runner gate fail-closed on unknown tape",
      not ok and "no live tape" in why)
check("ceo: gated entries debit nothing", C.account_cash() == 100.00)
# A KIMCHI-grade tape (graduated, deep liq, rising) opens.
ok, why = C.consider_card(_sig(mint="RUNNER1"))
check("ceo: runner-grade tape opens", ok)
# The gate is Solana-only: a Base card never consults the tape.
with patch.object(C, "_fresh_tape", return_value=None) as mt:
    ok, why = C.consider_card(_sig(chain="base", mint="BASE1"))
check("ceo: runner gate does not apply to base", ok and mt.call_count == 0)
# Consecutive confirmation (2026-10-21): the tape must pass on TWO
# consecutive fresh reads — a degraded or dark second read blocks.
with patch.object(C, "_fresh_tape",
                  side_effect=[dict(_RUNNER_TAPE),
                               dict(_RUNNER_TAPE,
                                    usd_liquidity=9_000.0)]) as mt:
    ok, why = C.consider_card(_sig(mint="CONF1"))
check("ceo: confirm read degraded (liq) blocks",
      not ok and "unconfirmed" in why and "liq" in why
      and mt.call_count == 2)
with patch.object(C, "_fresh_tape",
                  side_effect=[dict(_RUNNER_TAPE), None]) as mt:
    ok, why = C.consider_card(_sig(mint="CONF2"))
check("ceo: confirm read unknown blocks fail-closed",
      not ok and "unconfirmed" in why and "no live tape" in why
      and mt.call_count == 2)
with patch.object(C, "_fresh_tape",
                  side_effect=[dict(_RUNNER_TAPE),
                               dict(_RUNNER_TAPE)]) as mt:
    ok, why = C.consider_card(_sig(mint="CONF3"))
check("ceo: two good reads confirm and open",
      ok and mt.call_count == 2)
check("ceo: unconfirmed entries debit nothing extra",
      "CONF1" not in C.open_positions()
      and "CONF2" not in C.open_positions())
# A first-read failure never pays for the confirmation read.
with patch.object(C, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE, chg_m5=-1.0)) as mt:
    ok, why = C.consider_card(_sig(mint="CONF4"))
check("ceo: first-read failure skips confirmation",
      not ok and "unconfirmed" not in why and mt.call_count == 1)
_unpatch(ps)


# ---------------- bus tail: watermark + replay idempotency ----------------
ps = _fresh()
lines = [json.dumps(_sig(mint="BUSA")), json.dumps(_sig(mint="BUSB"))]
with open(C.BUS_PATH, "w") as f:
    f.write("\n".join(lines) + "\n")
res = C.process_bus()
check("ceo: bus tail opens both new cards",
      res["opened"] == 2 and len(C.open_positions()) == 2)
check("ceo: watermark advanced to EOF",
      _ledger()["watermark_offset"] == os.path.getsize(C.BUS_PATH))
res = C.process_bus()
check("ceo: second tail is a no-op (offset at EOF)",
      res["lines"] == 0 and res["opened"] == 0)
# Simulate a bus rewrite: same lines, watermark rewound — seen hashes
# must make the replay open nothing twice.
d = _ledger()
d["watermark_offset"] = 0
with open(C.LEDGER_PATH, "w") as f:
    json.dump(d, f)
res = C.process_bus()
check("ceo: replayed bus lines open nothing twice",
      res["opened"] == 0 and len(C.open_positions()) == 2
      and C.account_cash() == 98.00)
_unpatch(ps)


# ---------------- exits: the user's ladder ----------------
# leg A at 2x, leg B at 3x, leg C trails out -> ladder_complete
ps = _fresh(price=0.01)
ok, _ = C.consider_card(_sig(mint="LAD1"))
entry = C.open_positions()["LAD1"]["entry_price"]
C.poll_exits(price_fn=_prices([entry * 2.0]))
pos = C.open_positions()["LAD1"]
check("ceo exits: leg A closes at 2x", pos["legs"]["a"]["done"])
check("ceo exits: leg A realized in (0.9, 1.1)",
      0.9 < pos["legs"]["a"]["realized_usd"] < 1.1)
check("ceo exits: cash NOT settled until finalize",
      C.account_cash() == 99.00)
C.poll_exits(price_fn=_prices([entry * 3.0]))
pos = C.open_positions()["LAD1"]
check("ceo exits: leg B closes at 3x", pos["legs"]["b"]["done"])
peak = entry * 3.4
C.poll_exits(price_fn=_prices([peak]))
closes = C.poll_exits(price_fn=_prices([peak * 0.69]))  # -31% from peak
check("ceo exits: trail closes leg C -> position finalized",
      "LAD1" not in C.open_positions() and len(closes) == 1)
rec = closes[0]
check("ceo exits: close records realized_mult + pnl + reason",
      rec["realized_mult"] > 1.5 and rec["pnl_usd"] > 0
      and rec["closed_reason"] == "ladder_complete")
check("ceo exits: cash settled at finalize (99 + realized)",
      abs(C.account_cash() - (99.00 + rec["realized_usd"])) < 1e-6)
_unpatch(ps)

# hard stop: -50% closes everything at once
ps = _fresh(price=0.01)
ok, _ = C.consider_card(_sig(mint="STOP1"))
entry = C.open_positions()["STOP1"]["entry_price"]
closes = C.poll_exits(price_fn=_prices([entry * 0.4]))
check("ceo exits: hard stop finalizes",
      len(closes) == 1
      and closes[0]["closed_reason"] == "hard_stop_-50pct")
check("ceo exits: hard stop pnl ≈ -$0.60 (0.4x back, minus fill+gas)",
      -0.65 < closes[0]["pnl_usd"] < -0.55)
check("ceo exits: hard stop cash = 99 + ~0.397",
      99.35 < C.account_cash() < 99.45)
_unpatch(ps)

# 24h unquotable: past the backstop with no price -> finalize at 0
ps = _fresh(price=0.01)
ok, _ = C.consider_card(_sig(mint="DEAD1"))
d = _ledger()
d["positions"]["DEAD1"]["entry_ts"] = time.time() - 25 * 3600
with open(C.LEDGER_PATH, "w") as f:
    json.dump(d, f)
closes = C.poll_exits(price_fn=_prices([0.0]))
check("ceo exits: 24h unquotable finalizes at 0 (flat exit gas only)",
      len(closes) == 1
      and closes[0]["closed_reason"] == "time_stop_24h_unquotable"
      and -0.01 < closes[0]["realized_usd"] <= 0.0
      and closes[0]["realized_mult"] < 0.01)
check("ceo exits: unquotable loss is final (cash stays ~99)",
      98.99 < C.account_cash() < 99.01)
_unpatch(ps)


# ---------------- cooldown after close ----------------
ps = _fresh(price=0.01)
ok, _ = C.consider_card(_sig(mint="COOL1"))
entry = C.open_positions()["COOL1"]["entry_price"]
C.poll_exits(price_fn=_prices([entry * 0.4]))  # hard-stop close
ok, why = C.consider_card(_sig(mint="COOL1"))
check("ceo: mint closed <24h ago is in cooldown",
      not ok and "cooldown" in why)
d = _ledger()
d["closes"][0]["close_ts"] = time.time() - 25 * 3600
with open(C.LEDGER_PATH, "w") as f:
    json.dump(d, f)
ok, why = C.consider_card(_sig(mint="COOL1"))
check("ceo: mint re-enters after 24h cooldown", ok)
_unpatch(ps)


# ---------------- MAX_OPEN + bankroll exhaustion ----------------
ps = _fresh(price=0.01)
with patch.object(C, "MAX_OPEN", 2):
    ok1, _ = C.consider_card(_sig(mint="CAP1"))
    ok2, _ = C.consider_card(_sig(mint="CAP2"))
    ok3, why3 = C.consider_card(_sig(mint="CAP3"))
check("ceo: MAX_OPEN cap enforced",
      ok1 and ok2 and not ok3 and "max open" in why3)
_unpatch(ps)

ps = _fresh(price=0.01)
d = _ledger()
d["bankroll"] = 0.50
with open(C.LEDGER_PATH, "w") as f:
    json.dump(d, f)
ok, why = C.consider_card(_sig(mint="BROKE1"))
check("ceo: cash < $1 stops entries", not ok and "exhausted" in why)
check("ceo: state flips to bankroll_exhausted",
      C.account_state()["state"] == "bankroll_exhausted")
check("ceo: no position opened on empty bankroll",
      "BROKE1" not in C.open_positions())
_unpatch(ps)


# ---------------- killswitch halt rule: 15 closes, negative expectancy -----
ps = _fresh(price=0.01)
for i in range(15):
    mint = "HALT%02d" % i
    ok, why = C.consider_card(_sig(mint=mint))
    assert ok, why
    e = C.open_positions()[mint]["entry_price"]
    C.poll_exits(price_fn=_prices([e * 0.4]))  # hard-stop loss
check("ceo: 15 losing closes recorded to killswitch",
      len(FAKE_KS.recorded) == 15 and all(p < 0 for p in FAKE_KS.recorded))
check("ceo: lane halts on negative expectancy after 15 closes",
      "ceo" in FAKE_KS.halted)
ok, why = C.consider_card(_sig(mint="HALTED1"))
check("ceo: halted lane refuses new entries",
      not ok and "halted" in why)
_unpatch(ps)


# ---------------- bot-self-audit decision functions ----------------
AUDIT_SRC = os.path.join(REPO, "scripts", "bot-self-audit")
_ns = {"__name__": "self_audit_test", "__file__": AUDIT_SRC}
exec(compile(open(AUDIT_SRC).read().split('if __name__ == "__main__":')[0],
             "bot-self-audit", "exec"), _ns)

entries = _ns["supervised_daemons"]()
names = {e["name"] for e in entries}
# degen-feed removed 2026-10-30 (user-ordered permanent degen kill:
# watchdog stanza commented out, so the audit correctly derives it
# as NOT supervised — the old expectation here was stale, same class
# as the 2026-10-29 supervisor-count fix after the smart-watcher kill).
check("audit: supervised list derived incl. ceo-trader + core",
      {"ceo-trader", "mirror-exits", "launch_scanner",
       "telegram", "challenge-trader"} <= names)
check("audit: degen-feed NOT supervised (permanent kill 2026-10-30)",
      "degen-feed" not in names)
# eagle-eye trio retired 2026-10-23 (user-ordered fix batch: output
# had no consumer) — the roster must NOT resurrect them.
check("audit: eagle-eye trio NOT supervised (retired 2026-10-23)",
      not ({"eagle-eye-discovery", "eagle-eye-momentum",
            "eagle-eye-smartflow"} & names))
check("audit: launch-radar (paused) NOT supervised",
      "launch-radar" not in names)

plan = _ns["plan_repairs"]({"dead_daemons": ["x"],
                            "deletable_locks": ["/tmp/l1"],
                            "big_logs": ["/tmp/g1"]})
check("audit: plan orders watchdog first, then lock, then rotate",
      [a["action"] for a in plan] ==
      ["run_watchdog", "delete_lock", "rotate_log"])
check("audit: empty findings -> empty plan",
      _ns["plan_repairs"]({"dead_daemons": [], "deletable_locks": [],
                           "big_logs": []}) == [])

# lock classification against a temp home
ATMP = tempfile.mkdtemp(prefix="audit-test-")
_ns["HOME"] = ATMP
old_lock = os.path.join(ATMP, "a_ledger.lock")
held_lock = os.path.join(ATMP, "b_ledger.lock")
fresh_lock = os.path.join(ATMP, "c_ledger.lock")
for p in (old_lock, held_lock, fresh_lock):
    open(p, "w").close()
past = time.time() - 2 * 3600
os.utime(old_lock, (past, past))
os.utime(held_lock, (past, past))
fd = os.open(held_lock, os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
cls = _ns["classify_locks"]()
check("audit: stale unheld lock is deletable", old_lock in cls["deletable"])
check("audit: stale HELD lock is never deletable",
      held_lock in cls["held"] and held_lock not in cls["deletable"])
check("audit: fresh lock untouched", fresh_lock in cls["fresh"])
done, _note = _ns["_repair_delete_lock"](held_lock)
check("audit: repair refuses a held lock", not done
      and os.path.exists(held_lock))
done, _note = _ns["_repair_delete_lock"](old_lock)
check("audit: repair deletes a stale unheld lock", done
      and not os.path.exists(old_lock))
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)

# log rotation
_ns["LOG_DIR"] = ATMP
_ns["ROTATE_BYTES"] = 100
big = os.path.join(ATMP, "big.log")
with open(big, "wb") as f:
    f.write(b"x" * 500)
check("audit: oversized log detected", big in _ns["find_big_logs"]())
done, _note = _ns["_repair_rotate_log"](big)
check("audit: rotation gzips + truncates",
      done and os.path.getsize(big) == 0
      and os.path.exists(big + ".1.gz"))

# decommissioned-components registry (2026-10-23): the spark_seen
# freshness check is silenced ONLY by the registry at
# <HOME>/decommissioned.json — never hardcoded — and revives the
# moment the entry is removed or its flag flips false.
DTMP = tempfile.mkdtemp(prefix="audit-decom-")
_ns["HOME"] = DTMP
_ns["SPARK_SEEN_PATH"] = os.path.join(DTMP, "spark_seen.json")
with open(_ns["SPARK_SEEN_PATH"], "w") as f:
    f.write("{}")
stale = time.time() - 2 * 3600
os.utime(_ns["SPARK_SEEN_PATH"], (stale, stale))
ok_msg, prob = _ns["_check_spark_freshness"]()
check("audit: no registry -> stale spark_seen is a problem",
      ok_msg is None and prob is not None and "stale" in prob)
with open(os.path.join(DTMP, "decommissioned.json"), "w") as f:
    json.dump({"version": 1, "components": {"spark": {
        "decommissioned": True,
        "decommissioned_at": "2026-10-23T09:02:00+01:00",
        "reason": "test"}}}, f)
ok_msg, prob = _ns["_check_spark_freshness"]()
check("audit: registry-decommissioned spark -> ok skip, no problem",
      prob is None and ok_msg is not None
      and "decommissioned" in ok_msg)
with open(os.path.join(DTMP, "decommissioned.json"), "w") as f:
    json.dump({"version": 1, "components": {"spark": {
        "decommissioned": False}}}, f)
ok_msg, prob = _ns["_check_spark_freshness"]()
check("audit: revival (flag false) restores the stale problem",
      ok_msg is None and prob is not None and "stale" in prob)
with open(os.path.join(DTMP, "decommissioned.json"), "w") as f:
    f.write("{corrupt,,,")
ok_msg, prob = _ns["_check_spark_freshness"]()
check("audit: corrupt registry fails toward alerting (problem back)",
      ok_msg is None and prob is not None and "stale" in prob)
os.remove(os.path.join(DTMP, "decommissioned.json"))
fresh = time.time()
os.utime(_ns["SPARK_SEEN_PATH"], (fresh, fresh))
ok_msg, prob = _ns["_check_spark_freshness"]()
check("audit: fresh spark_seen passes when check is active",
      prob is None and ok_msg is not None and "fresh" in ok_msg)

# entry-drought check (2026-11-08): the 43h grad drought was
# invisible to the audit because entry flow was never checked.
# Thresholds calibrated on live ledgers: union of the three lanes
# never silent >1.19h over Nov 3-6 (1,064 entries); worst healthy
# single-lane gap grad_select 5.13h. Aggregate 6h / single 12h.
ETMP = tempfile.mkdtemp(prefix="audit-drought-")
_ns["HOME"] = ETMP
os.makedirs(os.path.join(ETMP, "killswitch"))


def _drought_setup(ages_h, halted=(), dead=()):
    """Fake ledgers + killswitch state. ages_h: lane -> hours since
    last entry (None = no entries recorded); halted: halted lanes."""
    for lane, fname in (("grad_select", "grad_select_ledger.json"),
                        ("tranche", "tranche_ledger.json"),
                        ("tranche_protect",
                         "tranche_protect_ledger.json")):
        age = ages_h.get(lane)
        closes = []
        if age is not None:
            closes = [{"mint": "TEST" + lane[:4],
                       "entry_ts": time.time() - age * 3600}]
        with open(os.path.join(ETMP, fname), "w") as f:
            json.dump({"closes": closes, "positions": {}}, f)
    ks = {"lanes": {lane: {"halted": lane in halted}
                    for lane in ("grad_select", "tranche",
                                 "tranche_protect")}}
    with open(os.path.join(ETMP, "killswitch", "state.json"), "w") as f:
        json.dump(ks, f)
    return _ns["_check_entry_drought"](dead)


ok_n, probs = _drought_setup({"grad_select": 0.5, "tranche": 1.0,
                              "tranche_protect": 0.8})
check("audit: fresh entries -> no drought problem",
      probs == [] and any("flowing" in n for n in ok_n))
ok_n, probs = _drought_setup({"grad_select": 8.0, "tranche": 9.0,
                              "tranche_protect": 7.0})
check("audit: all live lanes silent >6h -> tier-1 problem per lane",
      len(probs) == 3
      and all("entry drought" in p for p in probs)
      and {"grad_select", "tranche", "tranche_protect"}
      <= {p.split(":")[1].split()[0] for p in probs}
      and all("shared-feed" in p for p in probs))
ok_n, probs = _drought_setup({"grad_select": 13.0, "tranche": 1.0,
                              "tranche_protect": 0.5})
check("audit: single lane silent >12h -> tier-2 problem, that lane only",
      len(probs) == 1 and "grad_select" in probs[0]
      and "13.0h" in probs[0])
ok_n, probs = _drought_setup({"grad_select": 8.0, "tranche": 1.0,
                              "tranche_protect": 0.5})
check("audit: single lane silent 8h (below both bars) -> no problem",
      probs == [])
ok_n, probs = _drought_setup({"grad_select": 8.0, "tranche": 9.0,
                              "tranche_protect": 7.0},
                             halted=("tranche_protect",))
check("audit: halted lane skipped, live silent lanes still flagged",
      len(probs) == 2
      and all("tranche_protect" not in p for p in probs)
      and {"grad_select", "tranche"}
      <= {p.split(":")[1].split()[0] for p in probs})
ok_n, probs = _drought_setup({"grad_select": 8.0, "tranche": 9.0,
                              "tranche_protect": 7.0},
                             dead=("grad-select-trader",))
check("audit: dead-trader lane skipped, others still flagged",
      len(probs) == 2
      and all("grad_select" not in p for p in probs))
ok_n, probs = _drought_setup({"grad_select": None, "tranche": 1.0,
                              "tranche_protect": 0.5})
check("audit: ledger with no entries recorded -> problem",
      len(probs) == 1 and "grad_select" in probs[0]
      and "NO entries" in probs[0])
# missing ledger: remove the file AFTER setup, then call the check
# directly (no _drought_setup, which would recreate it).
_drought_setup({"grad_select": 1.0, "tranche": 1.0, "tranche_protect": 0.5})
os.remove(os.path.join(ETMP, "tranche_ledger.json"))
ok_n, probs = _ns["_check_entry_drought"]([])
check("audit: missing ledger -> no drought problem (parseability owns it)",
      probs == [])
_, _p20 = _drought_setup({"grad_select": 20.0, "tranche": 1.0,
                          "tranche_protect": 0.5})
check("audit: stale problem carries lane + last-entry timestamp + hours",
      len(_p20) == 1 and "grad_select" in _p20[0]
      and "20.0h" in _p20[0] and "last entry 2026-11-" in _p20[0])


# ---------------- peak_mult on closes (2026-10-20) ----------------
# Peaked path: 2.6x then hard stop. Close records the tracked peak so
# round-tripper leakage is measurable on the CEO ledger too.
ps = _fresh(price=0.01)
ok, _ = C.consider_card(_sig(mint="PEAK1"))
entry = C.open_positions()["PEAK1"]["entry_price"]
C.poll_exits(price_fn=_prices([entry * 2.6]))
closes = C.poll_exits(price_fn=_prices([entry * 0.4]))
check("ceo peak: peaked position finalized by hard stop",
      len(closes) == 1)
if closes:
    c = closes[0]
    check("ceo peak: close records peak_price/peak_mult ~= 2.6x",
          abs(c.get("peak_mult", 0) - 2.6) < 0.05
          and abs(c.get("peak_price", 0) - entry * 2.6) < entry * 0.05)
    check("ceo peak: peak_source = tracked, no peak_unknown flag",
          c.get("peak_source") == "tracked" and "peak_unknown" not in c)
_unpatch(ps)

# Straight-to-stop: peak seeded at the entry fill -> peak_mult = 1.0,
# the honest reading for a position that never rose.
ps = _fresh(price=0.01)
ok, _ = C.consider_card(_sig(mint="FLAT1"))
entry = C.open_positions()["FLAT1"]["entry_price"]
C.poll_exits(price_fn=_prices([entry * 0.9]))
closes = C.poll_exits(price_fn=_prices([entry * 0.4]))
check("ceo peak: straight-to-stop finalizes", len(closes) == 1)
if closes:
    check("ceo peak: never-rose position peak_mult ~= 1.0",
          abs(closes[0].get("peak_mult", 0) - 1.0) < 0.02)
_unpatch(ps)

# Legacy position (opened before peak tracking): only leg C's
# recorded peak (1.8x) exists -> close reports it as a flagged lower
# bound, never an invented number.
ps = _fresh(price=0.01)
ok, _ = C.consider_card(_sig(mint="LEG1"))
entry = C.open_positions()["LEG1"]["entry_price"]
d = _ledger()
del d["positions"]["LEG1"]["peak_price"]
d["positions"]["LEG1"]["legs"]["c"]["peak"] = entry * 1.8
with open(C.LEDGER_PATH, "w") as f:
    json.dump(d, f)
closes = C.poll_exits(price_fn=_prices([entry * 0.4]))
check("ceo peak: legacy position finalizes", len(closes) == 1)
if closes:
    c = closes[0]
    check("ceo peak: legacy close peak_mult = recorded leg-C peak",
          abs(c.get("peak_mult", 0) - 1.8) < 0.05)
    check("ceo peak: legacy close flagged legacy_lower_bound",
          c.get("peak_source") == "legacy_lower_bound")
_unpatch(ps)


# ---------------- entry-quote repair (2026-10-21) ----------------
# Grok/Speed autopsy: gate confirmed x2, then "no live quote" — the
# quote read pairs[0] blindly on a single no-retry fetch while the
# gate selects the max-liquidity pair through a curl+retry fetcher.
_PAIRS_MIXED = [
    {"priceUsd": None, "liquidity": {"usd": 900_000.0}},
    {"priceUsd": "0.05", "liquidity": {"usd": 5_000.0}},
    {"priceUsd": "0.02", "liquidity": {"usd": 120_000.0}},
]
with patch.object(C, "_http_json", return_value=list(_PAIRS_MIXED)):
    _q = C.quote_price("solana", "MINTQ")
check("ceo quote: max-liquidity priced pair wins over pairs[0]",
      _q == 0.02)
check("ceo quote: detail trail records the pairs fetch",
      "pairs fetch: 3 pair(s), 2 priced" in C._LAST_QUOTE_DETAIL)

# Pairs endpoint down, tape priced: entry proceeds at the tape price,
# adverse fill still applied on top (honest fills unchanged).
ps = _fresh(patch_quote=False)
with patch.object(C, "_http_json", return_value=None), \
        patch.object(C, "_fresh_tape",
                     return_value=dict(_RUNNER_TAPE, price_usd=0.02)), \
        patch.object(C, "QUOTE_RETRY_GAP_S", 0.0):
    ok, why = C.consider_card(_sig(mint="MINTTAPE"))
pos = C.open_positions().get("MINTTAPE")
check("ceo quote: tape fallback opens the entry", ok and pos is not None)
check("ceo quote: quoted price is the tape price",
      pos is not None and pos["quoted_price"] == 0.02)
check("ceo quote: adverse fill still applied over the tape price",
      pos is not None and pos["entry_price"] >= 0.02)
_unpatch(ps)

# Every source dark: the skip stays — and the reason names each lookup.
ps = _fresh(patch_quote=False)
with patch.object(C, "_http_json", return_value=None), \
        patch.object(C, "QUOTE_RETRY_GAP_S", 0.0):
    ok, why = C.consider_card(_sig(mint="MINTDARK"))
check("ceo quote: all sources dark still skips", not ok)
check("ceo quote: skip reason names pairs fetch, tape fallback, retry",
      "no live quote" in why and "pairs fetch" in why
      and "tape fallback" in why and "pairs retry" in why)
_unpatch(ps)


print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all ceo + self-audit tests passed")
