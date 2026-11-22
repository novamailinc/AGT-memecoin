#!/usr/bin/env python3
"""Journaling-hole regression tests (2026-10-23).

The ledger-evidence study found closes that journal placeholder or
missing entry data, which caps every winner/loser analysis:

1. CEO: smart-watcher cards publish a constant score (85) and NO
   buys/sells/age, so 30/36 historical closes carry 85.0/0/0 in
   entry_features. ceo.py now also stamps the runner gate's
   confirming LIVE TAPE read (real entry-time buys_m5/sells_m5/
   liquidity/mcap) into entry_features.gate_tape. These tests prove
   a close carries the entry's real card values (distinctive
   fixture: score 63.5) AND the real tape values, even when the
   card's own fields are placeholders.
2. Mirror: positions had NO entry_features at all (ceo/grad had
   one), so the card's smart-money evidence lived only inside
   entry_snapshot. mirror.py now builds entry_features at entry
   (evidence + gate tape) and _finalize copies it into every close
   on BOTH exit paths — scripts/mirror-exits' poll (poll_exits with
   live quotes) and rt_sync's tape-driven poll (poll_exits with a
   price_fn) both land in the same _finalize, and both are proven
   here.
3. Grad: entries consult birth dossiers (dossiers.py) but stamped
   nothing — zero closes carried dossier fields. grad.py now
   stamps the consulted dossier summary (dossier_used, safety
   source/verdict/score, curve_velocity_sol_s, deployer census)
   into entry_features, which _finalize already copied. grad_raw
   shares the engine code path but its no-gate _safety_ok override
   never reads a dossier: its entries stamp dossier_used=False
   ONLY, even when a dossier fixture exists for the mint.

Journaling only: no gate, threshold, size, or exit behavior is
exercised differently — entries open and closes finalize through
the lanes' own public functions with stubbed feeds. No network.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
from daemon.launch_scanner import mirror as M
from daemon.launch_scanner import grad as G
from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import dossiers as DOSS
from daemon.launch_scanner import signal_bus as BUS

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


class FakeKS:
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
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        self.halted.add(lane)
        return {"lane": lane, "action": "HALTED"}


def _prices(px):
    return lambda chain, mint, spot_index=None: px


# ============================ CEO =====================================
TMPD = tempfile.mkdtemp(prefix="journal-ceo-")
C.LEDGER_PATH = os.path.join(TMPD, "ceo_ledger.json")
C.BUS_PATH = os.path.join(TMPD, "signal_bus.jsonl")

TAPE = {"usd_liquidity": 120_000.0, "usd_market_cap": 640_000.0,
        "chg_m5": 12.0, "buys_m5": 137, "sells_m5": 52,
        "vol_m5": 9_876.0, "price_usd": 0.01,
        "tape_source": "dexscreener"}

ps = []
for p in (patch.object(C, "_veto"), patch.object(C, "_safety"),
          patch.object(C, "_ks", FakeKS()),
          patch.object(C, "quote_price", return_value=0.01),
          patch.object(C, "_fresh_tape", return_value=dict(TAPE)),
          patch.object(C, "RUNNER_CONFIRM_GAP_S", 0.0)):
    p.start()
    ps.append(p)
C._veto.veto_check.return_value = {"passed": True, "vetoes": []}
C._safety.check_token.return_value = {"passed": True, "unknown": False}
C.ensure_ledger()

# Distinctive card values: score 63.5, buys 17, sells 5, age 4.5.
sig = BUS.make_signal("solana", "test-feed", "CEOMINT1", symbol="TKN",
                      score=63.5, tier="pick", age_min=4.5,
                      mcap=640_000.0, liquidity=120_000.0,
                      buys=17, sells=5,
                      evidence={"smart_hit": True, "safety": "ok"})
ok, why = C.consider_card(sig)
check("ceo journal: distinctive card opens", ok)
pos = C.open_positions().get("CEOMINT1") or {}
ef = pos.get("entry_features") or {}
check("ceo journal: position entry_features carry the card's real "
      "score/buys/sells/age",
      ef.get("score") == 63.5 and ef.get("buys") == 17
      and ef.get("sells") == 5 and ef.get("age_min") == 4.5)
gt = ef.get("gate_tape") or {}
check("ceo journal: position stamps the gate's real tape read",
      gt.get("buys_m5") == 137 and gt.get("sells_m5") == 52
      and gt.get("liquidity") == 120_000.0
      and gt.get("mcap") == 640_000.0
      and gt.get("tape_source") == "dexscreener")
entry = pos["entry_price"]
closes = C.poll_exits(price_fn=_prices(entry * 0.4))
check("ceo journal: hard-stop close produced", len(closes) == 1)
if closes:
    cef = closes[0].get("entry_features") or {}
    check("ceo journal: close carries the entry's real score/buys/age",
          cef.get("score") == 63.5 and cef.get("buys") == 17
          and cef.get("sells") == 5 and cef.get("age_min") == 4.5)
    check("ceo journal: close carries the gate tape",
          (cef.get("gate_tape") or {}).get("buys_m5") == 137)

# Placeholder card (smart-watcher shape: constant 85, no buys/age):
# the close must STILL carry real entry-time values — from the tape.
sig2 = BUS.make_signal("solana", "smart-watcher", "CEOMINT2",
                       symbol="SW", score=85, tier="converged",
                       mcap=640_000.0, liquidity=120_000.0,
                       evidence={"smart_wallets": 2, "safety": "ok"})
ok, why = C.consider_card(sig2)
check("ceo journal: smart-watcher-style card opens", ok)
pos2 = C.open_positions().get("CEOMINT2") or {}
entry2 = pos2["entry_price"]
closes2 = C.poll_exits(price_fn=_prices(entry2 * 0.4))
check("ceo journal: placeholder-card close produced", len(closes2) == 1)
if closes2:
    ef2 = closes2[0].get("entry_features") or {}
    check("ceo journal: placeholder card's buys are honestly 0 on the "
          "card fields, real 137/52 in gate_tape",
          ef2.get("buys") == 0
          and (ef2.get("gate_tape") or {}).get("buys_m5") == 137
          and (ef2.get("gate_tape") or {}).get("sells_m5") == 52)

# GeckoTerminal-shaped tape (txn windows are None): those fields are
# omitted from the stamp, never zero-filled.
GT_TAPE = dict(TAPE, buys_m5=None, sells_m5=None, vol_m5=None,
               tape_source="geckoterminal")
with patch.object(C, "_fresh_tape", return_value=dict(GT_TAPE)):
    ok, why = C.consider_card(BUS.make_signal(
        "solana", "test-feed", "CEOMINT3", symbol="GT", score=63.5,
        tier="pick", mcap=640_000.0, liquidity=120_000.0))
check("ceo journal: GT-tape card opens", ok)
gt3 = (C.open_positions().get("CEOMINT3") or {}).get(
    "entry_features", {}).get("gate_tape") or {}
check("ceo journal: GT tape stamp omits unknown txn fields",
      "buys_m5" not in gt3 and "sells_m5" not in gt3
      and gt3.get("tape_source") == "geckoterminal"
      and gt3.get("liquidity") == 120_000.0)
for p in ps:
    p.stop()


# ============================ MIRROR ==================================
TMPM = tempfile.mkdtemp(prefix="journal-mirror-")
M.LEDGER_PATH = os.path.join(TMPM, "degen_mirror_ledger.json")
QUOTE = {"px": 0.01}
MTAPE = {"usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
         "chg_m5": 10.0, "buys_m5": 88, "sells_m5": 31,
         "vol_m5": 4_321.0, "price_usd": 0.01,
         "tape_source": "dexscreener"}

ps = []
for p in (patch.object(M, "_veto"), patch.object(M, "_safety"),
          patch.object(M, "_ks", FakeKS()),
          patch.object(M, "quote_price",
                       side_effect=lambda c, m, s=None: QUOTE["px"]),
          patch.object(M, "_fresh_tape", return_value=dict(MTAPE)),
          patch.object(M, "RUNNER_CONFIRM_GAP_S", 0.0)):
    p.start()
    ps.append(p)
M._veto.veto_check.return_value = {"passed": True, "vetoes": []}
M._safety.check_token.return_value = {"passed": True, "unknown": False}

EVID = {"smart_wallets": 3, "wallet_names": "Aa1111, Bb2222",
        "safety": "ok"}

# Path A — scripts/mirror-exits style: poll_exits() on live quotes.
ok, why = M.mirror_card("solana", "smart-watcher", "MIRMINT1",
                        symbol="MIR", score=85, tier="converged",
                        mcap=500_000.0, liquidity=100_000.0,
                        evidence=dict(EVID))
check("mirror journal: smart-watcher card mirrored", ok)
pos = M.open_positions().get("MIRMINT1") or {}
check("mirror journal: position now carries entry_features with the "
      "smart evidence + gate tape",
      (pos.get("entry_features") or {}).get("evidence", {})
      .get("smart_wallets") == 3
      and (pos.get("entry_features") or {}).get("gate_tape", {})
      .get("buys_m5") == 88)
QUOTE["px"] = 0.004  # 0.4x of the $0.01 quote -> hard stop
closes = M.poll_exits()
check("mirror journal: own-poll close produced", len(closes) == 1)
if closes:
    ef = closes[0].get("entry_features") or {}
    check("mirror journal: own-poll close carries smart evidence",
          (ef.get("evidence") or {}).get("smart_wallets") == 3
          and (closes[0].get("entry_snapshot") or {})
          .get("evidence", {}).get("smart_wallets") == 3)
    check("mirror journal: own-poll close carries gate tape",
          (ef.get("gate_tape") or {}).get("buys_m5") == 88)

# Path B — rt_sync style: poll_exits(price_fn=tape prices).
QUOTE["px"] = 0.01
ok, why = M.mirror_card("solana", "smart-watcher", "MIRMINT2",
                        symbol="MIR2", score=85, tier="converged",
                        mcap=500_000.0, liquidity=100_000.0,
                        evidence=dict(EVID))
check("mirror journal: second card mirrored", ok)
entry = M.open_positions()["MIRMINT2"]["entry_price"]
closes = M.poll_exits(price_fn=_prices(entry * 0.4))
check("mirror journal: rt_sync-path close produced", len(closes) == 1)
if closes:
    ef = closes[0].get("entry_features") or {}
    check("mirror journal: rt_sync-path close carries smart evidence",
          (ef.get("evidence") or {}).get("smart_wallets") == 3
          and (ef.get("evidence") or {}).get("wallet_names")
          == "Aa1111, Bb2222")

# degen-feed style evidence (smart_hit bool, no wallet count): the
# close carries exactly what the card carried — nothing invented.
ok, why = M.mirror_card(
    "solana", "degen-feed", "MIRMINT3", symbol="DEG", score=71.0,
    tier="pick", mcap=500_000.0, liquidity=100_000.0,
    evidence={"smart_hit": False, "safety": "ok", "age_min": 3.0})
check("mirror journal: degen-feed card mirrored", ok)
entry = M.open_positions()["MIRMINT3"]["entry_price"]
closes = M.poll_exits(price_fn=_prices(entry * 0.4))
if closes:
    ev = (closes[0].get("entry_features") or {}).get("evidence") or {}
    check("mirror journal: degen close carries smart_hit=False and "
          "no invented wallet count",
          ev.get("smart_hit") is False
          and "smart_wallets" not in ev)
for p in ps:
    p.stop()


# ============================ GRAD ====================================
TMPG = tempfile.mkdtemp(prefix="journal-grad-")
G.LEDGER_PATH = os.path.join(TMPG, "grad_ledger.json")
DOSSDIR = os.path.join(TMPG, "dossiers")
os.makedirs(DOSSDIR, exist_ok=True)
DOSS.DOSSIER_DIR = DOSSDIR

NOW = time.time()
DMINT = "DosMint11111111111111111111111111111111pump"
NMINT = "NoDos111111111111111111111111111111111pump"
DOSSIER = {
    "mint": DMINT, "symbol": "DOS", "born_ts": NOW - 120,
    "opened_ts": NOW - 110, "deployer": "Depl0yer111",
    "safety": {"ts": NOW - 5,
               "report": {"passed": True, "unknown": False,
                          "score": 42, "flags": ["f1"], "fails": [],
                          "source": "rugcheck"}},
    "curve_samples": [{"ts": NOW - 100, "v_sol": 1.0},
                      {"ts": NOW - 5, "v_sol": 2.0}],
    "curve_velocity_sol_s": 0.123456,
    "deployer_dossiers_live": 7,
    "deployer_rugger": {"flagged": False, "launches": 9, "dead": 4},
}
with open(os.path.join(DOSSDIR, DMINT + ".json"), "w") as f:
    json.dump(DOSSIER, f)

GTAPE = {"usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
         "chg_m5": 3.0, "price_usd": 0.00007,
         "tape_source": "dexscreener"}

ps = []
for p in (patch.object(G, "_veto"), patch.object(G, "_safety"),
          patch.object(G, "_ks", FakeKS()),
          patch.object(G, "quote_price", return_value=0.00007),
          patch.object(G, "_fresh_tape", return_value=dict(GTAPE)),
          patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)):
    p.start()
    ps.append(p)
G._veto.veto_check.return_value = {"passed": True, "vetoes": []}
# The LIVE check would say score 7 — the dossier's 42 must win,
# proving the stamped verdict is the one actually consulted.
G._safety.check_token.return_value = {
    "passed": True, "unknown": False, "score": 7, "flags": [],
    "fails": [], "source": "rugcheck"}
G.ensure_ledger()


def _cand(mint, age_s=60.0):
    return {"mint": mint, "symbol": "TKN", "chain": "solana",
            "dex": "pumpswap", "created_ts": time.time() - age_s,
            "pool_address": "POOL1", "reserve_usd": 6_000.0,
            "fdv_usd": 69_000.0, "price_usd": 0.00007,
            "source": "gecko-new-pool"}


opened, why, _fin = G.consider_candidate(_cand(DMINT))
check("grad journal: dossier mint opens", opened)
pos = G.open_positions().get(DMINT) or {}
ef = pos.get("entry_features") or {}
check("grad journal: safety verdict came from the dossier "
      "(score 42, not the live check's 7)",
      (pos.get("entry_snapshot") or {}).get("safety", {})
      .get("score") == 42)
check("grad journal: entry_features stamp dossier_used + provenance",
      ef.get("dossier_used") is True
      and ef.get("dossier_safety_source") == "dossier:rugcheck"
      and ef.get("dossier_safety_verdict") == "pass"
      and ef.get("dossier_safety_score") == 42)
check("grad journal: entry_features stamp curve velocity + census",
      ef.get("curve_velocity_sol_s") == 0.123456
      and ef.get("deployer_launches") == 9
      and ef.get("deployer_dead") == 4
      and ef.get("deployer_dossiers_live") == 7)
entry = pos["entry_price"]
closes = G.poll_exits(price_fn=_prices(entry * 0.4))
check("grad journal: close produced", len(closes) == 1)
if closes:
    cef = closes[0].get("entry_features") or {}
    check("grad journal: close carries the full dossier stamp",
          cef.get("dossier_used") is True
          and cef.get("curve_velocity_sol_s") == 0.123456
          and cef.get("deployer_launches") == 9
          and cef.get("deployer_dead") == 4)

opened, why, _fin = G.consider_candidate(_cand(NMINT))
check("grad journal: no-dossier mint opens via the live check",
      opened)
pos = G.open_positions().get(NMINT) or {}
ef = pos.get("entry_features") or {}
check("grad journal: absent dossier stamps dossier_used=False only",
      ef.get("dossier_used") is False
      and "curve_velocity_sol_s" not in ef
      and "dossier_safety_source" not in ef
      and "deployer_launches" not in ef)
entry = pos["entry_price"]
closes = G.poll_exits(price_fn=_prices(entry * 0.4))
if closes:
    cef = closes[0].get("entry_features") or {}
    check("grad journal: no-dossier close carries dossier_used=False",
          cef.get("dossier_used") is False
          and "curve_velocity_sol_s" not in cef)
for p in ps:
    p.stop()


# ============================ GRAD_RAW ================================
# The raw engine shares grad's _consider_candidate (and therefore
# the entry_features update), but its _safety_ok is the no-gate
# stub: a dossier fixture for the mint must NOT be read into the
# entry — dossier_used=False and nothing else.
TMPR = tempfile.mkdtemp(prefix="journal-gradraw-")
eng = GR.make_engine()
eng.LEDGER_PATH = os.path.join(TMPR, "grad_raw_ledger.json")
eng._ks = FakeKS()
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}
eng.ensure_ledger()
RMINT = "RawDos111111111111111111111111111111111pump"
with open(os.path.join(DOSSDIR, RMINT + ".json"), "w") as f:
    json.dump(dict(DOSSIER, mint=RMINT), f)
opened, why, _fin = eng.consider_candidate(_cand(RMINT), time.time())
check("grad_raw journal: raw entry opens with a dossier on disk",
      opened)
pos = eng.open_positions().get(RMINT) or {}
ef = pos.get("entry_features") or {}
check("grad_raw journal: dossier_used=False stamped, dossier never "
      "read into the entry",
      ef.get("dossier_used") is False
      and "curve_velocity_sol_s" not in ef
      and "dossier_safety_source" not in ef
      and "deployer_launches" not in ef)
entry = pos["entry_price"]
closes = eng.poll_exits(price_fn=_prices(entry * 0.4))
if closes:
    cef = closes[0].get("entry_features") or {}
    check("grad_raw journal: raw close carries dossier_used=False "
          "only", cef.get("dossier_used") is False
          and "curve_velocity_sol_s" not in cef)

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all journaling tests passed")
