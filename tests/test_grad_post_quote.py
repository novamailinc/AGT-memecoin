#!/usr/bin/env python3
"""Grad lane POST-QUOTE commit-time gate (demo fill path) + (header of entry-age harness reused). Grad lane ENTRY data-age gate (same scope as test_watcher_entry_age / test_momentum_entry_age): the tape row this entry
read is aged against the owner's mandatory max_data_age_s before the account debit. TEST-FIXTURE values, no network, all
four killswitch paths isolated. Plain script."""
import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys, tempfile, time, importlib as _il
from unittest.mock import patch
sys.path.insert(0, os.path.join(_ROOT, "daemon", "launch_scanner")); sys.path.insert(0, _ROOT)
from daemon.launch_scanner import grad as G
from daemon.launch_scanner import killswitch as KS
fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)
_ks = tempfile.mkdtemp(prefix="gradage-ks-")
KS.STATE_DIR = _ks
for _n, _f in (("STATE_FILE", "state.json"), ("SHADOW_FILE", "shadow.json"), ("LOCK_FILE", "state.lock")):
    setattr(KS, _n, os.path.join(_ks, _f))
check("KS isolation: all four paths under the temp dir",
      all(str(getattr(KS, n, "")).startswith(_ks) for n in ("STATE_DIR", "STATE_FILE", "LOCK_FILE", "SHADOW_FILE")))
G.LEDGER_PATH = os.path.join(tempfile.mkdtemp(prefix="gradage-led-"), "grad_ledger.json")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import tests._guard_fixture as _GF
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="shared-acct-home-")
os.environ["PAPER_ACCOUNT_START"] = "1000"
os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None); os.environ.pop("PAPER_ACCOUNT_TRADE_SIZE_USD", None)
import daemon.launch_scanner.paper_account as _pa
_pa = _il.reload(_pa); _GF.install(_pa)
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "60"
os.environ.pop("AGT_EXECUTION_MODE", None)
G.ensure_ledger()

class _KS:
    def is_halted(self, l): return False
    def gate(self, l): return True
    def record_trade(self, *a, **k): pass
    def check_lane(self, l): return {"lane": l}
    def halt_lane(self, *a, **k): return {"action": "HALTED"}
BASE = {"usd_liquidity": 8000.0, "usd_market_cap": 69000.0, "chg_m5": 3.0, "price_usd": 0.00007, "tape_source": "dexscreener"}
TAPES = {}
def _fresh(**kw):
    n = time.time(); return dict(BASE, fetched_at=n, received_at=n, **kw)
def cand(mint):
    return {"mint": mint, "symbol": "TKN", "name": "TKN / SOL", "chain": "solana", "dex": "pumpswap",
            "created_ts": time.time() - 60.0, "pool_address": "POOL1", "reserve_usd": 6000.0, "fdv_usd": 69000.0,
            "price_usd": 0.00007, "source": "gecko-new-pool"}
def cash(): return _pa.snapshot()["cash"]
SAFE = {"passed": True, "unknown": False, "score": 7, "flags": [], "fails": [], "source": "rugcheck"}
READS = {}
def _tape(m):
    READS[m] = READS.get(m, 0) + 1
    if m == D_MINT and READS[m] >= 2: return _fresh()   # D: 1st read stale, EVERY later read fresh
    t = TAPES.get(m); return dict(t) if isinstance(t, dict) else t
D_MINT = "GradAgeD1pump"

class FakeFiller:
    """Inert quote-simulation filler: records intents/releases; buy() takes `delay` seconds then returns a NEW fill
    whose quote was requested at the START of the call (so a slow quote ages at commit)."""
    def __init__(s): s.delay = 0.0; s.released = []; s.buys = 0; s.saved = {}; s.tamper = False; s.committed = set(); s.release_fails = False
    def intent_id(s, lane, mint): return "%s:intent:%s" % (lane, mint)
    def buy(s, op, mint, notional, cap):
        s.buys += 1
        if op in s.saved and not s.tamper: return s.saved[op]
        t = time.time(); time.sleep(s.delay)
        s.saved[op] = {"qty_tokens": notional / 0.00007, "price_usd": 0.00007, "fee_usd": 0.0, "execution_source": "quote_sim",
                "signature": "SIG", "synthetic_signature": True, "op_id": op,
                "provenance": {"quote_requested_at": t, "quote_received_at": time.time()}}
        return s.saved[op]
    def release_intent(s, lane, mint):
        if s.release_fails: raise RuntimeError('release failed')
        s.released.append(mint)
    def op_committed(s, op): return op in s.committed
    def get_fill(s, op): return s.saved.get(op)
    consume_intent = release_intent
FAKE = FakeFiller()
def run(mint, quote=None):
    with patch.object(G, "_veto") as mv, patch.object(G, "_safety") as ms, patch.object(G, "_ks", _KS()), \
         patch.object(G, "quote_price", quote or (lambda *a, **k: 0.00007)), \
         patch.object(G, "_fresh_tape", _tape), \
         patch.object(G, "_demo_mode", lambda: True), patch.object(G, "_demo_filler", lambda: FAKE), patch.object(G, "QUOTE_RETRY_GAP_S", 0.0), patch.object(G, "MAX_OPEN", 50):
        mv.veto_check.return_value = {"passed": True, "vetoes": []}
        ms.check_token.return_value = dict(SAFE)
        opened, why, final = G.consider_candidate(cand(mint), time.time())
    return bool(opened), str(why)
n = time.time()
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "2"
A = "GradPQ1pump"; TAPES[A] = _fresh(); FAKE.delay = 0.0; c0 = cash()
o, w = run(A)
check("post-quote control: prompt quote opens (%s)" % w[:60], o and FAKE.buys == 1)
B = "GradPQ2pump"; TAPES[B] = _fresh(); FAKE.delay = 2.5; c1 = cash(); nb = FAKE.buys
o, w = run(B)
check("post-quote: slow quote (age > cap at commit) refuses (%s)" % w[:70], (not o) and "post-quote" in w and FAKE.buys == nb + 1)
check("post-quote: refusal released the intent and debited nothing", B in FAKE.released and B not in G.open_positions() and abs(cash() - c1) < 1e-9)
# REPLAY: the durable journal already holds the original (old) fill for this op: not re-aged, opens with the original economics
C = "GradPQ3pump"; TAPES[C] = _fresh(); op = FAKE.intent_id("grad", C); t_old = time.time() - 500
FAKE.committed.add(op); FAKE.saved[op] = {"qty_tokens": 1000.0, "price_usd": 0.00007, "fee_usd": 0.0, "execution_source": "quote_sim", "signature": "SIG",
                  "synthetic_signature": True, "op_id": op, "provenance": {"quote_requested_at": t_old, "quote_received_at": t_old}}
o, w = run(C)
check("replay of the durable journaled fill is not re-aged (%s)" % w[:60], o and C in G.open_positions())
# a fill that DIFFERS from a durable prior (re-quote / tamper) refuses even though its own timestamp is fresh
D = "GradPQ4pump"; TAPES[D] = _fresh(); op = FAKE.intent_id("grad", D); FAKE.committed.add(op); FAKE.saved[op] = ({"op_id": op, "qty_tokens": 1.0, "provenance": {"quote_requested_at": t_old}})
FAKE.tamper = True; c3 = cash(); o, w = run(D); FAKE.tamper = False
check("fill differing from the durable original refuses, no debit (%s)" % w[:60], (not o) and D not in G.open_positions() and abs(cash() - c3) < 1e-9)
# REVIEWER DEFECT (crash/retry window): a new fill persisted by buy() and REFUSED, with release failing, must not pass on retry
E = "GradPQ5pump"; TAPES[E] = _fresh(); FAKE.delay = 2.5; FAKE.release_fails = True; c4 = cash()
o, w = run(E); check("refused fill with FAILED release still refuses (%s)" % w[:50], not o and E in {m for m in [E]} and FAKE.saved)
FAKE.release_fails = False; FAKE.delay = 0.0; TAPES[E] = _fresh(); nb = FAKE.buys
o, w = run(E)
check("retry replays the persisted-but-uncommitted refused fill: still refused, never debited (%s)" % w[:60], (not o) and FAKE.buys == nb + 1 and E not in G.open_positions() and abs(cash() - c4) < 1e-9)
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"
print("FAILS: %d" % len(fails)); sys.exit(1 if fails else 0)
