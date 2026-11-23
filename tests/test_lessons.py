#!/usr/bin/env python3
"""Decision-memory tests (2026-11-17).

Covers daemon/launch_scanner/lessons.py, scripts/lessons-reflect,
and their wiring into the shared grad engine (grad_select,
grad_raw, tranche, tranche_protect) and the evaluation path:
  * pending decision logged at entry (engine end-to-end: the
    grad.py entry log + the lane stamp merge of the select
    measurements)
  * settle at close with pnl / realized_mult / close reason
  * idempotent settlement (double-settle is a no-op; exactly one
    record per (mint, entry_ts))
  * templated lesson generation from a fixture ledger — the
    fast/slow split numbers EXACT (the Rule A discovery shape:
    fast +$175.60 on 13 closes / 11 wins, slow -$507.55 on 32
    closes / 4 wins), plus deployer / curve-speed / copycat bands
  * lesson file + resolved_at (per-lesson newest close_ts)
  * get_lessons top-N ordering (most material first)
  * lessons NEVER gate an entry: evaluate_candidate returns the
    identical verdict with a hostile lessons file in context as
    with no lessons file at all
No network. Plain script — run directly, not via pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

_TMP_HOME = tempfile.mkdtemp(prefix="lessons-test-home-")
os.environ["FOREX_AGENT_HOME"] = _TMP_HOME

from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import lessons as L
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
BASE = 1794000000.0  # fixed close_ts base so resolved_at is exact


def _dec(mint, entry_ts, close_ts, pnl, age=None, dep=None,
         ttg=None, trades=None, copycat=None,
         kind="current_price_simulated_fill"):
    return {
        "kind": "decision", "lane": "grad_select", "mint": mint,
        "symbol": "T", "status": "settled", "entry_ts": entry_ts,
        "features": {
            "grad_age_s": age, "deployer_launches_24h": dep,
            "ttg_s": ttg, "curve_trades": trades,
            "copycat_others_24h": copycat,
            "entry_price": 0.001, "mcap": 60000.0,
            "liquidity": 5000.0},
        "lessons_in_context": [], "pnl_usd": pnl,
        "realized_mult": max(0.0, 1.0 + pnl / 25.0),
        "close_reason": "time_stop_24h", "close_ts": close_ts,
        "exit_price_kind": kind,
    }


# ================= unit: log / settle / idempotency =================
H1 = tempfile.mkdtemp(prefix="lessons-unit-")
check("log: pending decision appended at entry",
      L.log_decision("grad_select", "MintA", 1000.0,
                     {"grad_age_s": 12.0, "mcap": 60000.0},
                     symbol="AAA", home=H1) is True)
recs = L.load_decisions("grad_select", home=H1)
check("log: one pending record, features kept",
      len(recs) == 1 and recs[0]["status"] == "pending"
      and recs[0]["features"]["grad_age_s"] == 12.0
      and recs[0]["features"]["mcap"] == 60000.0)
check("log: re-log same entry merges, never duplicates",
      L.log_decision("grad_select", "MintA", 1000.0,
                     {"deployer_launches_24h": 1,
                      "curve_trades": 80}, home=H1) is True
      and len(L.load_decisions("grad_select", home=H1)) == 1)
recs = L.load_decisions("grad_select", home=H1)
check("log: merge fills missing measurements, keeps old ones",
      recs[0]["features"]["grad_age_s"] == 12.0
      and recs[0]["features"]["deployer_launches_24h"] == 1
      and recs[0]["features"]["curve_trades"] == 80)
check("settle: close settles the pending record with pnl",
      L.settle_decision("grad_select", "MintA", 1000.0, -12.5, 0.5,
                        "hard_stop_-50pct", 2000.0, home=H1) is True)
recs = L.load_decisions("grad_select", home=H1)
check("settle: record settled with pnl/mult/reason/close_ts",
      len(recs) == 1 and recs[0]["status"] == "settled"
      and recs[0]["pnl_usd"] == -12.5
      and recs[0]["realized_mult"] == 0.5
      and recs[0]["close_reason"] == "hard_stop_-50pct"
      and recs[0]["close_ts"] == 2000.0)
check("settle: double-settle is a no-op",
      L.settle_decision("grad_select", "MintA", 1000.0, -12.5, 0.5,
                        "hard_stop_-50pct", 2000.0, home=H1) is False
      and len(L.load_decisions("grad_select", home=H1)) == 1)
check("settle: unknown mint settles nothing",
      L.settle_decision("grad_select", "Nope", 9999.0, 1.0, 1.0,
                        "x", 2000.0, home=H1) is False)

# settle_from_close: no pending -> one settled record from close
H2 = tempfile.mkdtemp(prefix="lessons-close-")
close_rec = {"mint": "MintB", "symbol": "BBB",
             "entry_ts": 3000.0, "close_ts": 4000.0,
             "pnl_usd": 25.0, "realized_mult": 2.0,
             "closed_reason": "leg_a_2x",
             "entry_features": {
                 "grad_age_s": 5.0, "mcap": 70000.0,
                 "liquidity": 6000.0,
                 "select": {"grad_age_s": 5.0,
                            "deployer_launches_24h": 0,
                            "curve_trades": 90, "ttg_s": 120.0,
                            "ticker_others_24h": 0}}}
check("settle_from_close: creates one settled record, no pending",
      L.settle_from_close("grad_raw", close_rec, home=H2) is True)
recs = L.load_decisions("grad_raw", home=H2)
check("settle_from_close: features extracted from close record",
      len(recs) == 1 and recs[0]["status"] == "settled"
      and recs[0]["features"]["grad_age_s"] == 5.0
      and recs[0]["features"]["deployer_launches_24h"] == 0
      and recs[0]["features"]["ttg_s"] == 120.0
      and recs[0]["features"]["copycat_others_24h"] == 0
      and recs[0]["pnl_usd"] == 25.0)
check("settle_from_close: second call is a no-op (idempotent)",
      L.settle_from_close("grad_raw", close_rec, home=H2) is False
      and len(L.load_decisions("grad_raw", home=H2)) == 1)

# ================= templated lessons: fast/slow exact =================
fast = []
for i in range(11):  # 11 wins x +$20.00
    fast.append(_dec("FastWin%d" % i, BASE - 1000 + i,
                     BASE + 100 + i, 20.0, age=10.0))
for i in range(2):  # 2 losses x -$22.20
    fast.append(_dec("FastLoss%d" % i, BASE - 900 + i,
                     BASE + 200 + i, -22.20, age=25.0))
slow = []
for i in range(4):  # 4 wins x +$15.00
    slow.append(_dec("SlowWin%d" % i, BASE - 800 + i,
                     BASE + 300 + i, 15.0, age=120.0))
for i in range(27):  # 27 losses x -$20.00
    slow.append(_dec("SlowLoss%d" % i, BASE - 700 + i,
                     BASE + 400 + i, -20.0, age=90.0))
slow.append(_dec("SlowLoss27", BASE - 600, BASE + 427,
                 -27.55, age=200.0))
payload = L.generate_lessons("grad_select", fast + slow,
                             generated_at=BASE + 1000)
by_id = {l["id"]: l for l in payload["lessons"]}
check("lessons: fast/slow fixture has 13 fast + 32 slow closes",
      len(fast) == 13 and len(slow) == 32)
fl = by_id.get("grad_select:entry_speed:fast (age<=30s)")
sl = by_id.get("grad_select:entry_speed:slow (age>30s)")
check("lessons: fast entries +$175.60 on 13 closes, 11 wins",
      fl is not None and fl["closes"] == 13 and fl["wins"] == 11
      and fl["net_pnl_usd"] == 175.60)
check("lessons: slow entries -$507.55 on 32 closes, 4 wins",
      sl is not None and sl["closes"] == 32 and sl["wins"] == 4
      and sl["net_pnl_usd"] == -507.55)
check("lessons: templated text carries the exact numbers",
      fl is not None
      and fl["text"] == "fast (age<=30s) entries: +$175.60 on 13 "
      "closes, 11 wins (84.6% win rate)"
      and sl is not None
      and sl["text"] == "slow (age>30s) entries: -$507.55 on 32 "
      "closes, 4 wins (12.5% win rate)")
check("lessons: resolved_at is the newest close in the band",
      fl is not None and fl["resolved_at"] == BASE + 201
      and sl is not None and sl["resolved_at"] == BASE + 427)
check("lessons: file-level resolved_at is the newest close overall",
      payload["resolved_at"] == BASE + 427
      and payload["n_settled"] == 45
      and payload["generated_at"] == BASE + 1000)
ov = by_id.get("grad_select:overall:all")
check("lessons: overall lesson sums both bands",
      ov is not None and ov["closes"] == 45 and ov["wins"] == 15
      and ov["net_pnl_usd"] == -331.95)

# ================= bands: deployer / curve / copycat =================
band_decs = [
    _dec("D0a", BASE, BASE + 1, 30.0, dep=0),
    _dec("D0b", BASE, BASE + 2, -20.0, dep=0),
    _dec("D1a", BASE, BASE + 3, 5.0, dep=1),
    _dec("D1b", BASE, BASE + 4, -5.0, dep=2),
    _dec("D1c", BASE, BASE + 5, -5.0, dep=1),
    _dec("D3a", BASE, BASE + 6, -25.0, dep=3),
    _dec("DU", BASE, BASE + 7, 10.0, dep=None),
]
p2 = L.generate_lessons("grad_select", band_decs)
b2 = {l["id"]: l for l in p2["lessons"]}
check("bands: deployer 0 / 1-2 / >=3 / unknown split exactly",
      b2["grad_select:deployer:0"]["closes"] == 2
      and b2["grad_select:deployer:0"]["net_pnl_usd"] == 10.0
      and b2["grad_select:deployer:1-2"]["closes"] == 3
      and b2["grad_select:deployer:1-2"]["net_pnl_usd"] == -5.0
      and b2["grad_select:deployer:>=3"]["closes"] == 1
      and b2["grad_select:deployer:>=3"]["net_pnl_usd"] == -25.0
      and b2["grad_select:deployer:unknown"]["closes"] == 1)
curve_decs = [
    _dec("C1", BASE, BASE + 1, 10.0, ttg=30.0, trades=50),
    _dec("C2", BASE, BASE + 2, -10.0, ttg=300.0, trades=150),
    _dec("C3", BASE, BASE + 3, -20.0, ttg=900.0, trades=300),
    _dec("C4", BASE, BASE + 4, 5.0, ttg=None, trades=None),
]
p3 = L.generate_lessons("grad_select", curve_decs)
b3 = {l["id"]: l for l in p3["lessons"]}
check("bands: curve-speed ttg <=60s / 60-600s / >600s split",
      b3["grad_select:curve_speed:<=60s"]["net_pnl_usd"] == 10.0
      and b3["grad_select:curve_speed:60-600s"]["net_pnl_usd"] == -10.0
      and b3["grad_select:curve_speed:>600s"]["net_pnl_usd"] == -20.0
      and b3["grad_select:curve_speed:unknown"]["closes"] == 1)
check("bands: curve-trades <=100 / 101-250 / >250 split",
      b3["grad_select:curve_trades:<=100"]["net_pnl_usd"] == 10.0
      and b3["grad_select:curve_trades:101-250"]["net_pnl_usd"] == -10.0
      and b3["grad_select:curve_trades:>250"]["net_pnl_usd"] == -20.0)
copy_decs = [
    _dec("K0", BASE, BASE + 1, 8.0, copycat=0),
    _dec("K1", BASE, BASE + 2, -4.0, copycat=1),
    _dec("K3", BASE, BASE + 3, -12.0, copycat=3),
    _dec("KU", BASE, BASE + 4, 2.0, copycat=None),
]
p4 = L.generate_lessons("grad_select", copy_decs)
b4 = {l["id"]: l for l in p4["lessons"]}
check("bands: copycat 0 / 1-2 / >=3 / unknown split",
      b4["grad_select:copycat:0"]["net_pnl_usd"] == 8.0
      and b4["grad_select:copycat:1-2"]["net_pnl_usd"] == -4.0
      and b4["grad_select:copycat:>=3"]["net_pnl_usd"] == -12.0
      and b4["grad_select:copycat:unknown"]["closes"] == 1)
check("lessons: empty decisions -> no lessons, resolved_at None",
      L.generate_lessons("grad_select", [])["lessons"] == []
      and L.generate_lessons("grad_select", [])["resolved_at"] is None)

# ================= lesson file + get_lessons ordering =================
H3 = tempfile.mkdtemp(prefix="lessons-file-")
check("file: write + read round-trips the payload",
      L.write_lessons("grad_select", payload, home=H3) is True
      and L.read_lessons("grad_select", home=H3)["n_settled"] == 45)
top = L.get_lessons("grad_select", n=2, home=H3)
check("get_lessons: top-N ordered by |net pnl| first",
      len(top) == 2
      and top[0]["id"] == "grad_select:entry_speed:slow (age>30s)"
      and top[0]["net_pnl_usd"] == -507.55
      # second is a |-$331.95| tie (overall + every unknown band);
      # deterministic tie-break is closes desc, then id asc.
      and abs(top[1]["net_pnl_usd"]) == 331.95
      and top[1]["id"] == "grad_select:copycat:unknown")
check("get_lessons: overall lesson is in the top 6 (five "
      "|-$331.95| ties precede it alphabetically)",
      any(l["id"] == "grad_select:overall:all"
          for l in L.get_lessons("grad_select", n=6, home=H3)))
check("get_lessons: missing file -> [] (never raises)",
      L.get_lessons("tranche", home=H3) == []
      and L.lessons_in_context("tranche", home=H3) == [])
check("context_block: names the lane, advisory-only wording",
      "advisory only" in L.context_block("grad_select", home=H3)
      and "-$507.55" in L.context_block("grad_select", home=H3))

# ================= ledger backfill (reflector input) =================
H4 = tempfile.mkdtemp(prefix="lessons-ledger-")
ledger = {"closes": [
    {"mint": "LedA", "symbol": "LA", "entry_ts": BASE,
     "close_ts": BASE + 10, "pnl_usd": 12.0, "realized_mult": 1.48,
     "closed_reason": "time_stop_24h",
     "entry_features": {"grad_age_s": 8.0,
                        "select": {"grad_age_s": 8.0,
                                   "deployer_launches_24h": 0,
                                   "curve_trades": 60, "ttg_s": 45.0,
                                   "ticker_others_24h": 0}}},
    {"mint": "LedB", "symbol": "LB", "entry_ts": BASE + 1,
     "close_ts": BASE + 20, "pnl_usd": -20.0,
     "realized_mult": 0.2, "closed_reason": "hard_stop_-50pct",
     "entry_features": {"grad_age_s": 300.0,
                        "select": {"grad_age_s": 300.0,
                                   "deployer_launches_24h": 2,
                                   "curve_trades": 200, "ttg_s": 500.0,
                                   "ticker_others_24h": 1}}},
]}
with open(os.path.join(H4, "grad_select_ledger.json"), "w") as f:
    json.dump(ledger, f)
merged = L.merged_settled_decisions("grad_select", home=H4)
check("backfill: ledger closes become settled decisions",
      len(merged) == 2 and merged[0]["mint"] == "LedA"
      and merged[1]["mint"] == "LedB"
      and merged[0]["features"]["grad_age_s"] == 8.0
      and merged[1]["features"]["copycat_others_24h"] == 1)
# a jsonl settled record for LedA dedupes the ledger row (one row)
L.settle_from_close("grad_select", ledger["closes"][0], home=H4)
merged2 = L.merged_settled_decisions("grad_select", home=H4)
check("backfill: jsonl + ledger dedupe to one row per close",
      len(merged2) == 2)

# ================= engine end-to-end: entry log + close settle =================
_ks_tmp = tempfile.mkdtemp(prefix="lessons-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")
eng = GR.make_engine()
_tmp = tempfile.mkdtemp(prefix="lessons-eng-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_raw_ledger.json")
eng.ensure_ledger()
eng._jup = None  # no unconditional batch HTTP before fixture price_fn
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}
SEL = GR._select
TRADER1 = "TraderLes11111111111111111111111111111111"
MINT1 = "LesMint11111111111111111111111111111111pump"
SEL._index_refresh = lambda now: None
SEL._INDEX["available"] = True
SEL._INDEX["coverage_start"] = NOW - 90000
SEL._INDEX["by_mint"][MINT1] = {"trader": TRADER1, "ts": NOW - 600,
                                "bonding_curve": "CurveL1"}
SEL._INDEX["by_trader"][TRADER1] = [NOW - 600]
SEL._curve_stats = lambda addr: {"trades": 80,
                                 "created_ts": NOW - 600,
                                 "oldest_sig": "sig-l1",
                                 "capped": False, "exhausted": True}


class _FakeSat:
    def __init__(self):
        self.updated = NOW

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": {}}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


SEL._saturation = _FakeSat()


class FakeFeed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


mig = {"mint": MINT1, "symbol": "LES1",
       "source": "pumpportal-migration", "created_ts": NOW,
       "extra": {"migration_pool": "pumpswap", "market_id": "PoolL"}}
res = GR.scan_once(eng, feed=FakeFeed([mig]), now=NOW)
check("engine: stream candidate opened", res["opened"] == 1)
d = eng._read()
pos = d["positions"][MINT1]
entry_ts = pos["entry_ts"]
decisions = L.load_decisions(
    "grad_raw",
    home=os.path.dirname(os.path.abspath(eng.LEDGER_PATH)))
check("engine: pending decision logged at entry",
      len(decisions) == 1 and decisions[0]["status"] == "pending"
      and decisions[0]["mint"] == MINT1
      and decisions[0]["entry_ts"] == entry_ts)
check("engine: stamp merged select measurements into the log",
      decisions[0]["features"]["deployer_launches_24h"] == 1
      and decisions[0]["features"]["curve_trades"] == 80
      and decisions[0]["features"]["copycat_others_24h"] == 0
      and decisions[0]["features"]["grad_age_s"] is not None
      and decisions[0]["features"]["grad_age_s"] <= 30.0)
check("engine: evaluation journaled lessons_in_context (list)",
      isinstance(pos["entry_features"]["select"].get(
          "lessons_in_context"), list))
closes = eng.poll_exits(price_fn=lambda c, m, s: 0.0004)  # -60%
d = eng._read()
check("engine: hard stop closed the position",
      d["positions"] == {} and len(d["closes"]) == 1
      and d["closes"][0]["closed_reason"] == "hard_stop_-50pct")
decisions = L.load_decisions(
    "grad_raw",
    home=os.path.dirname(os.path.abspath(eng.LEDGER_PATH)))
check("engine: close settled the pending decision, exactly once",
      len(decisions) == 1 and decisions[0]["status"] == "settled"
      and decisions[0]["pnl_usd"] == d["closes"][0]["pnl_usd"]
      and decisions[0]["close_reason"] == "hard_stop_-50pct"
      and decisions[0]["close_ts"] == d["closes"][0]["close_ts"])
check("engine: re-settling the same close is a no-op",
      L.settle_from_close(
          "grad_raw", d["closes"][0],
          home=os.path.dirname(os.path.abspath(eng.LEDGER_PATH)))
      is False)

# ================= reflector script on the engine home =================
r = subprocess.run(
    [sys.executable, os.path.join(REPO, "scripts", "lessons-reflect"),
     "--home", os.path.dirname(os.path.abspath(eng.LEDGER_PATH)),
     "--lane", "grad_raw"],
    capture_output=True, text=True, timeout=60)
check("reflector: exits 0 on a real (single-close) lane",
      r.returncode == 0)
gen = L.read_lessons(
    "grad_raw",
    home=os.path.dirname(os.path.abspath(eng.LEDGER_PATH)))
check("reflector: wrote a lessons file with the settled close",
      gen is not None and gen["n_settled"] == 1
      and any("fast (age<=30s)" in l["text"]
              for l in gen["lessons"]))
check("reflector: every lesson carries resolved_at",
      gen is not None
      and all(l.get("resolved_at") is not None
              for l in gen["lessons"]))

# ================= lessons NEVER gate an entry =================
# A hostile lessons file: fast entries "lose" -$9,999. The same
# candidate must evaluate identically with and without it.
GS._index_refresh = lambda now: None
GS._INDEX["available"] = True
GS._INDEX["coverage_start"] = NOW - 90000
TRADER_OK = "TraderOK1111111111111111111111111111111111"
MINT_OK = "LesMintOK111111111111111111111111111111pump"
MINT_SER = "LesMintSer11111111111111111111111111111pump"
TRADER_SER = "TraderSer11111111111111111111111111111111"
GS._INDEX["by_mint"][MINT_OK] = {"trader": TRADER_OK,
                                 "ts": NOW - 600,
                                 "bonding_curve": "CurveOK"}
GS._INDEX["by_trader"][TRADER_OK] = [NOW - 600]
GS._INDEX["by_mint"][MINT_SER] = {"trader": TRADER_SER,
                                  "ts": NOW - 300,
                                  "bonding_curve": "CurveSer"}
GS._INDEX["by_trader"][TRADER_SER] = [NOW - 3600, NOW - 3500, NOW - 300]
_CURVE = {
    MINT_OK: {"trades": 80, "created_ts": NOW - 600,
              "oldest_sig": "sig-ok", "capped": False,
              "exhausted": True},
    MINT_SER: {"trades": 90, "created_ts": NOW - 300,
               "oldest_sig": "sig-ser", "capped": False,
               "exhausted": True},
}
_fake = {"mint": None}


def _fake_curve(addr):
    return _CURVE[_fake["mint"]]


GS._curve_stats = _fake_curve
GS._creator_from_oldest_tx = lambda sig: None
GS._saturation = _FakeSat()
eng2 = GS.make_engine()


def _eval(mint, symbol):
    _fake["mint"] = mint
    return GS.evaluate_candidate(
        eng2, {"mint": mint, "symbol": symbol, "created_ts": NOW,
               "dex": "pumpswap", "pool_address": "PoolX",
               "migration_sig": "", "reserve_usd": 0.0,
               "fdv_usd": 0.0, "source": "pumpportal-migration"},
        NOW)


# No lessons file in this home yet: clean pass / serial reject.
H5 = tempfile.mkdtemp(prefix="lessons-gate-")
os.environ["FOREX_AGENT_HOME"] = H5
v_ok_clean = _eval(MINT_OK, "OKONE")
v_ser_clean = _eval(MINT_SER, "SERONE")
check("gate: clean fast graduation PASSES without lessons",
      v_ok_clean["verdict"] == "pass")
check("gate: serial deployer REJECTED without lessons",
      v_ser_clean["verdict"] == "reject"
      and v_ser_clean["reason"] == "serial_deployer")
# Now plant hostile lessons for grad_select in the same home.
hostile = L.generate_lessons("grad_select", [
    _dec("H%d" % i, BASE, BASE + i, -999.0, age=5.0)
    for i in range(10)])
L.write_lessons("grad_select", hostile, home=H5)
check("gate: hostile lessons file is readable by get_lessons",
      len(L.get_lessons("grad_select", home=H5)) > 0)
v_ok_hostile = _eval(MINT_OK, "OKONE")
v_ser_hostile = _eval(MINT_SER, "SERONE")
check("gate: hostile lessons do NOT reject the passing candidate",
      v_ok_hostile["verdict"] == "pass"
      and v_ok_hostile["reason"] == v_ok_clean["reason"])
check("gate: hostile lessons do NOT change the reject reason",
      v_ser_hostile["verdict"] == "reject"
      and v_ser_hostile["reason"] == "serial_deployer")
check("gate: lessons_in_context is journaled, verdict untouched",
      isinstance(v_ok_hostile["values"].get("lessons_in_context"),
                 list))

# ================= CEO read surface =================
from daemon.launch_scanner import ceo as C
check("ceo: lessons_context returns the block (advisory only)",
      isinstance(C.lessons_context("grad_select"), str)
      and "grad_select" in C.lessons_context("grad_select"))

# ============ 4.6 learning hygiene: incomplete records excluded ============
hq = [_dec("QWin%d" % i, BASE - 500 + i, BASE + 500 + i, 10.0, age=10.0)
      for i in range(3)]
hx = [_dec("Stale%d" % i, BASE - 400 + i, BASE + 600 + i, -50.0,
           age=10.0, kind="stale_price_estimate") for i in range(2)]
hx.append(_dec("WD0", BASE - 300, BASE + 700, -25.0, age=10.0,
               kind="write_down_zero"))
hx.append(_dec("Legacy", BASE - 200, BASE + 800, 99.0, age=10.0,
               kind="unknown"))
hp = L.generate_lessons("grad_select", hq + hx, generated_at=BASE + 900)
hid = {l["id"]: l for l in hp["lessons"]}
ho = hid.get("grad_select:overall:all")
hf = hid.get("grad_select:entry_speed:fast (age<=30s)")
check("hygiene: only current-price outcomes enter lesson numbers",
      ho is not None and ho["closes"] == 3 and ho["wins"] == 3
      and ho["net_pnl_usd"] == 30.0)
check("hygiene: exclusion is reported per lesson and per payload",
      ho["excluded_unqualified"] == 4
      and hf["excluded_unqualified"] == 4
      and hp["n_settled"] == 7 and hp["n_qualified"] == 3
      and hp["n_excluded_unqualified"] == 4
      and hp["learning_price_kind"] == "current_price_simulated_fill")
check("hygiene: all-unqualified lane yields no lessons, counts honest",
      L.generate_lessons("grad_select", hx,
                         generated_at=BASE + 901)["lessons"] == []
      and L.generate_lessons(
          "grad_select", hx,
          generated_at=BASE + 901)["n_excluded_unqualified"] == 4)
check("hygiene: missing label fails closed (excluded, not learned)",
      L.generate_lessons(
          "grad_select",
          [{k: v for k, v in hq[0].items() if k != "exit_price_kind"}],
          generated_at=BASE + 902)["n_qualified"] == 0)

for bad in (None, float("nan"), float("inf"), "bad"):
    broken = dict(hq[0], pnl_usd=bad)
    check("hygiene: invalid PnL excluded %r" % bad,
          L.generate_lessons("grad_select", [broken])["n_qualified"] == 0)
check("hygiene: close before entry excluded",
      L.generate_lessons("grad_select", [dict(hq[0], close_ts=0)])["n_qualified"] == 0)
check("hygiene: all-settled recorded economics preserved separately",
      hp["all_settled_recorded_economics"]["records"] == 7
      and hp["all_settled_recorded_economics"]["recorded_pnl_usd"] == 4.0
      and hp["excluded_recorded_economics"]["recorded_pnl_usd"] == -26.0
      and hp["excluded_by_price_kind"]["stale_price_estimate"]["records"] == 2)
_backfill_home = tempfile.mkdtemp(prefix="lessons-provenance-")
_backfill_row = dict(mint="BF", entry_ts=1000, close_ts=2000, pnl_usd=1,
                     realized_mult=1.04, exit_price_kind="current_price_simulated_fill")
with open(L.ledger_path("grad_select", _backfill_home), "w") as f:
    json.dump({"closes": [_backfill_row]}, f)
check("hygiene: ledger backfill preserves existing price provenance",
      L.decisions_from_ledger("grad_select", home=_backfill_home)[0]["exit_price_kind"]
      == "current_price_simulated_fill")
check("hygiene: missing close reason excluded as incomplete",
      L.generate_lessons("grad_select", [dict(hq[0], close_reason="")])["n_qualified"] == 0)
check("hygiene: whitespace close reason excluded as incomplete",
      L.generate_lessons("grad_select", [dict(hq[0], close_reason=" ")])["n_qualified"] == 0)
check("hygiene: numeric-string PnL follows numeric schema",
      L.generate_lessons("grad_select", [dict(hq[0], pnl_usd="2.5")])["n_qualified"] == 1)

# Point-in-time strategy memory (4.4): as-of queries see no future.
hq2 = [dict(hq[0], close_ts=BASE + 100, pnl_usd=10.0),
       dict(hq[0], mint="MINT-PIT2", close_ts=BASE + 200,
            pnl_usd=-4.0)]
pit = L.generate_lessons_as_of("grad_select", hq2, BASE + 150)
check("point-in-time: outcomes settled after as_of are invisible",
      pit.get("cutoff_semantics")
      == "outcome_event_time_only_not_knowledge_time"
      and pit.get("as_of") == BASE + 150
      and pit.get("n_settled") == 1
      and pit.get("resolved_at") == BASE + 100)
full = L.generate_lessons_as_of("grad_select", hq2, BASE + 500)
check("point-in-time: as_of past all settles equals full history",
      full.get("n_settled") == 2)
bad = L.generate_lessons_as_of("grad_select", hq2, None)
check("point-in-time: invalid as_of labeled, never full-history",
      bad.get("error") == "invalid_as_of"
      and bad.get("n_settled") == 0)
nan_cut = L.generate_lessons_as_of("grad_select", hq2, float("nan"))
inf_cut = L.generate_lessons_as_of("grad_select", hq2, float("inf"))
check("point-in-time: non-finite as_of rejected, not 0/full",
      nan_cut.get("error") == "invalid_as_of"
      and inf_cut.get("error") == "invalid_as_of")
fut = L.generate_lessons_as_of("grad_select", hq2, BASE + 500,
                               generated_at=BASE + 300)
check("point-in-time: future as_of labeled explicitly",
      fut.get("as_of_in_future") is True)

# Knowledge-time mode (4.4): cutoff on settled_ts (when the outcome
# was RECORDED), not close_ts (when it happened).
kt = [dict(hq[0], close_ts=BASE + 100, settled_ts=BASE + 100,
           pnl_usd=10.0),
      dict(hq[0], mint="MINT-KT2", close_ts=BASE + 120,
           settled_ts=BASE + 400, pnl_usd=-4.0),  # late backfill
      dict(hq[0], mint="MINT-KT3", close_ts=BASE + 130,
           pnl_usd=7.0)]  # no settled_ts: unknown availability
kres = L.generate_lessons_as_of("grad_select", kt, BASE + 150,
                                mode="knowledge_time")
check("knowledge-time: recorded-after-cutoff invisible despite "
      "close before cutoff",
      kres.get("n_settled") == 1
      and kres.get("resolved_at") == BASE + 100
      and kres.get("cutoff_mode") == "knowledge_time"
      and kres.get("cutoff_semantics")
      == "append_recorded_time_known_availability_subset")
check("knowledge-time: unknown availability excluded and labeled, "
      "never silently known",
      kres.get("unknown_availability_excluded") == 1)
kres2 = L.generate_lessons_as_of("grad_select", kt, BASE + 500,
                                 mode="knowledge_time")
check("knowledge-time: late backfill included once recorded, "
      "labeled separately",
      kres2.get("n_settled") == 2
      and kres2.get("late_backfill_included") == 1
      and kres2.get("unknown_availability_excluded") == 1)
check("knowledge-time: replay limits labeled, not claimed leak-free",
      "leak-free" in str(kres2.get("knowledge_replay_limits"))
      and "not a verified" in str(kres2.get("knowledge_replay_limits")))
badmode = L.generate_lessons_as_of("grad_select", kt, BASE + 150,
                                   mode="everything")
check("knowledge-time: unknown mode rejected, never full history",
      badmode.get("error") == "invalid_mode"
      and badmode.get("n_settled") == 0)
defkt = L.generate_lessons_as_of("grad_select", kt, BASE + 500)
check("knowledge-time: default mode stays outcome-event",
      defkt.get("cutoff_mode") == "outcome_event"
      and defkt.get("n_settled") == 3
      and "unknown_availability_excluded" not in defkt)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print("  FAIL " + f)
    sys.exit(1)
print("ALL LESSONS TESTS PASSED")
