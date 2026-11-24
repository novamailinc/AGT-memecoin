#!/usr/bin/env python3
"""Conviction scoring + REVIEW queue (advanced build, piece 1,
2026-11-21).

Covers conviction.py (five-tier scoring, gate verdicts,
fail-closed on unknown age) and review_queue.py (enqueue/list/
idempotence/bound), plus the evaluate_candidate integration:
a filter PASS with WEAK conviction returns "review" and lands
in the queue; STRONG/STANDARD pass; a filter REJECT is untouched
by the gate; the conviction rides into entry_features via
_stamp_detection and therefore into every close.

No network, no live ledgers (a temp home isolates the queue;
the replay check runs scripts/conviction-replay against fixture
ledgers in a temp home). Plain script — run directly with the
venv python, not pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import conviction as C  # noqa: E402
from daemon.launch_scanner import review_queue as RQ  # noqa: E402
from daemon.launch_scanner import grad_select as GS  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- scorer: tiers ------------------------------------
STRONG = {"grad_age_s": 3.0, "mcap": 20000.0,
          "deployer_launches_24h": 1, "ttg_s": 30.0,
          "curve_trades": 50, "ticker_others_24h": 0,
          "bear_score": {"bear_score": 10}, "liquidity": 10000.0}
r = C.score_candidate(STRONG)
check("STRONG: fast+fresh+deployer1 scores 9", r["score"] == 9)
check("STRONG: tier STRONG", r["tier"] == "STRONG")

STANDARD = {"grad_age_s": 10.0, "mcap": 100000.0,
            "deployer_launches_24h": 1, "ttg_s": 300.0,
            "curve_trades": 150, "ticker_others_24h": 1,
            "bear_score": {"bear_score": 20}, "liquidity": 50000.0}
r = C.score_candidate(STANDARD)
check("STANDARD: mid candidate scores 4", r["score"] == 4)
check("STANDARD: tier STANDARD", r["tier"] == "STANDARD")

WEAK = {"grad_age_s": 10.0, "mcap": 100000.0,
        "deployer_launches_24h": 0, "ttg_s": 300.0,
        "curve_trades": 150, "ticker_others_24h": 1,
        "bear_score": {"bear_score": 45}, "liquidity": 50000.0}
r = C.score_candidate(WEAK)
check("WEAK: borderline candidate scores 0", r["score"] == 0)
check("WEAK: tier WEAK", r["tier"] == "WEAK")

SKIP = {"grad_age_s": 20.0, "mcap": 100000.0,
        "deployer_launches_24h": 0, "ttg_s": 300.0,
        "curve_trades": 150, "ticker_others_24h": 1,
        "bear_score": {"bear_score": 20}, "liquidity": 50000.0}
r = C.score_candidate(SKIP)
check("SKIP: -1 candidate scores -1", r["score"] == -1)
check("SKIP: tier SKIP", r["tier"] == "SKIP")

BLOCK_AGE = dict(STRONG, grad_age_s=45.0)
r = C.score_candidate(BLOCK_AGE)
check("BLOCK: age >30s blocks", r["tier"] == "BLOCK")
check("BLOCK: age component is -10", r["components"]["grad_age_s"] == -10)

r = C.score_candidate({})
check("BLOCK: unknown age blocks (fail-closed)",
      r["tier"] == "BLOCK")
check("BLOCK: unknown age reason recorded",
      "unknown_grad_age" in r["reasons"])

BLOCK_SERIAL = dict(STRONG, deployer_launches_24h=5)
check("BLOCK: serial deployer >=3 blocks",
      C.score_candidate(BLOCK_SERIAL)["tier"] == "BLOCK")

BLOCK_COPY = dict(STRONG, ticker_others_24h=4)
check("BLOCK: copycat >=3 blocks",
      C.score_candidate(BLOCK_COPY)["tier"] == "BLOCK")

BLOCK_MCAP = {"grad_age_s": 20.0, "mcap": 2000000.0,
              "deployer_launches_24h": 0, "ttg_s": 300.0,
              "curve_trades": 150, "ticker_others_24h": 1,
              "bear_score": {"bear_score": 20},
              "liquidity": 50000.0}
r = C.score_candidate(BLOCK_MCAP)
check("BLOCK: $1M+ mcap with negatives blocks (total -4)",
      r["tier"] == "BLOCK" and r["score"] == -4)

# A 0 mcap / 0 liquidity is UNKNOWN (no measurement), never
# fresh: a 0 placeholder must not farm the fresh band.
r = C.score_candidate(dict(STRONG, mcap=0.0, liquidity=0.0))
check("mcap: 0 is unknown (0 points, not fresh)",
      r["components"]["mcap"] == 0)
check("liquidity: 0 is unknown (0 points)",
      r["components"]["liquidity"] == 0)

# bear_score as a bare int (not a dict) works the same.
r = C.score_candidate(dict(STRONG, bear_score=70))
check("bear: bare int 70 subtracts 2",
      r["components"]["bear_score"] == -2)
check("bear: dict and int agree",
      C.score_candidate(dict(STRONG, bear_score={"bear_score": 70}))
      ["components"]["bear_score"] == -2)

# mcap via fdv_usd fallback.
r = C.score_candidate({"grad_age_s": 3.0, "fdv_usd": 20000.0,
                       "deployer_launches_24h": 1, "ttg_s": 30.0,
                       "curve_trades": 50, "ticker_others_24h": 0,
                       "bear_score": 10, "liquidity": 10000.0})
check("mcap: fdv_usd fallback scores fresh", r["tier"] == "STRONG")

# liquidity >=$250k (already-run) subtracts 1.
r = C.score_candidate(dict(STRONG, liquidity=500000.0))
check("liquidity: >=$250k subtracts 1",
      r["components"]["liquidity"] == -1)

# ---------------- gate: verdicts -----------------------------------
g = C.gate(STRONG)
check("gate: STRONG passes as conviction_strong",
      g["verdict"] == "pass" and g["reason"] == "conviction_strong")
g = C.gate(STANDARD)
check("gate: STANDARD passes as conviction_standard",
      g["verdict"] == "pass" and g["reason"] == "conviction_standard")
g = C.gate(WEAK)
check("gate: WEAK reviews as conviction_review",
      g["verdict"] == "review" and g["reason"] == "conviction_review")
g = C.gate(SKIP)
check("gate: SKIP rejects as conviction_skip",
      g["verdict"] == "reject" and g["reason"] == "conviction_skip")
g = C.gate(BLOCK_AGE)
check("gate: BLOCK rejects as conviction_block",
      g["verdict"] == "reject" and g["reason"] == "conviction_block")
check("gate: conviction rides in the verdict",
      g["conviction"]["tier"] == "BLOCK")

# ---------------- review queue -------------------------------------
TMP = tempfile.mkdtemp(prefix="conviction-queue-")
MINT = "ReviewMint11111111111111111111111111111111pump"
ok = RQ.enqueue("grad_select", MINT,
                C.score_candidate(WEAK), WEAK,
                symbol="WEAKCOIN", source="pumpportal-migration",
                now=1700000000.0, home=TMP)
check("queue: enqueue returns True", ok is True)
check("queue: count is 1", RQ.count("grad_select", home=TMP) == 1)
rows = RQ.list_queue("grad_select", home=TMP)
check("queue: list returns the record",
      len(rows) == 1 and rows[0]["mint"] == MINT)
check("queue: conviction tier recorded",
      rows[0]["conviction"]["tier"] == "WEAK")
check("queue: values snapshot bounded to measured keys",
      set(rows[0]["values"]).issubset(
          {"grad_age_s", "deployer_launches_24h", "curve_trades",
           "ttg_s", "ticker_others_24h", "mcap", "fdv_usd",
           "liquidity", "reserve_usd", "bear_score"}))
# Idempotent: a second enqueue merges, never duplicates.
ok2 = RQ.enqueue("grad_select", MINT,
                 C.score_candidate(WEAK), WEAK,
                 symbol="WEAKCOIN", now=1700000001.0, home=TMP)
check("queue: second enqueue merges (still 1)",
      ok2 is True and RQ.count("grad_select", home=TMP) == 1)
check("queue: unknown lane refuses",
      RQ.enqueue("nope", MINT, {}, {}, home=TMP) is False)
check("queue: unknown lane lists empty",
      RQ.list_queue("nope", home=TMP) == [])
# Bound: 205 enqueues keep the newest 200.
for i in range(205):
    RQ.enqueue("grad_raw", "Mint%03d" % i,
               {"score": 1, "tier": "WEAK", "components": {},
                "reasons": []}, WEAK, now=1700000000.0 + i,
               home=TMP)
check("queue: bound keeps newest 200",
      RQ.count("grad_raw", home=TMP) == 200)
rows = RQ.list_queue("grad_raw", home=TMP, n=1)
check("queue: newest first after bound",
      rows[0]["mint"] == "Mint204")

# ---------------- evaluate_candidate integration -------------------
# Monkeypatch _evaluate (the filter pipeline) to return a pass
# with known values; _bear/_lessons are neutralised so the test
# never touches live state.
_orig_eval = GS._evaluate
_orig_bear = GS._bear
_orig_lessons = GS._lessons
GS._bear = None
GS._lessons = None


class _FakeEngine:
    LEDGER_PATH = os.path.join(TMP, "grad_select_ledger.json")

    def _ledger_lock(self):
        return contextlib.nullcontext()


def _fake_pass(values):
    def _ev(engine, cand, now, vals):
        vals.update(values)
        return {"verdict": "pass", "reason": "", "values": vals}
    return _ev


def _fake_reject(reason):
    def _ev(engine, cand, now, vals):
        return {"verdict": "reject", "reason": reason, "values": vals}
    return _ev


try:
    eng = _FakeEngine()
    cand = {"mint": "WeakMint111111111111111111111111111111111pump",
            "symbol": "WEAK", "source": "pumpportal-migration",
            "created_ts": time.time() - 10.0, "fdv_usd": 100000.0,
            "reserve_usd": 50000.0}
    GS._evaluate = _fake_pass(dict(WEAK))
    res = GS.evaluate_candidate(eng, cand, time.time(),
                                lane="grad_select")
    check("integration: WEAK filter-pass returns review",
          res["verdict"] == "review")
    check("integration: WEAK reason is conviction_review",
          res["reason"] == "conviction_review")
    check("integration: conviction stamped in values",
          (res.get("values") or {}).get("conviction", {}).get(
              "tier") == "WEAK")
    check("integration: WEAK candidate landed in the review queue",
          RQ.count("grad_select", home=TMP) >= 2)

    GS._evaluate = _fake_pass(dict(STRONG))
    res = GS.evaluate_candidate(eng, cand, time.time(),
                                lane="grad_select")
    check("integration: STRONG filter-pass returns pass",
          res["verdict"] == "pass")
    # A pass keeps the filter's reason ("") — the conviction tier
    # is journaled in values, not in the reason field (the
    # fail-soft contract in tests/test_bear_score.py depends on
    # the pass shape staying byte-identical).
    check("integration: STRONG pass keeps reason ''",
          res["reason"] == "")
    check("integration: STRONG conviction tier journaled",
          (res.get("values") or {}).get("conviction", {}).get(
              "tier") == "STRONG")

    GS._evaluate = _fake_pass(dict(STANDARD))
    res = GS.evaluate_candidate(eng, cand, time.time(),
                                lane="grad_select")
    check("integration: STANDARD filter-pass returns pass",
          res["verdict"] == "pass")
    check("integration: STANDARD pass keeps reason ''",
          res["reason"] == "")

    GS._evaluate = _fake_pass(dict(SKIP))
    res = GS.evaluate_candidate(eng, cand, time.time(),
                                lane="grad_select")
    check("integration: SKIP filter-pass returns reject",
          res["verdict"] == "reject")
    check("integration: SKIP reason is conviction_skip",
          res["reason"] == "conviction_skip")

    GS._evaluate = _fake_reject("slow_curve")
    res = GS.evaluate_candidate(eng, cand, time.time(),
                                lane="grad_select")
    check("integration: filter REJECT is untouched by the gate",
          res["verdict"] == "reject" and res["reason"] == "slow_curve")
    check("integration: filter REJECT carries no conviction",
          "conviction" not in (res.get("values") or {}))
finally:
    GS._evaluate = _orig_eval
    GS._bear = _orig_bear
    GS._lessons = _orig_lessons

# ---------------- _stamp_detection carries conviction --------------
eng2 = _FakeEngine()
eng2.d = {"positions": {}}


class _FakeEngine2(_FakeEngine):
    def __init__(self):
        self.d = {"positions": {}}

    def _read(self):
        return self.d

    def _write(self, d):
        self.d = d


e2 = _FakeEngine2()
mint2 = "StampMint11111111111111111111111111111111pump"
e2.d["positions"][mint2] = {"mint": mint2, "entry_ts": time.time()}
vals = dict(STRONG)
vals["conviction"] = C.score_candidate(STRONG)
GS._stamp_detection(e2, mint2, "pumpportal-migration", vals)
ef = e2.d["positions"][mint2].get("entry_features") or {}
check("stamp: conviction rides into entry_features",
      (ef.get("conviction") or {}).get("tier") == "STRONG")
check("stamp: select values preserved alongside conviction",
      (ef.get("select") or {}).get("grad_age_s") == 3.0)

# ---------------- replay script on fixture ledgers -----------------
FIX_HOME = tempfile.mkdtemp(prefix="conviction-replay-")
FIX_CLOSES = [
    {"mint": "A1", "symbol": "STRONG1", "entry_ts": 1700000000.0,
     "close_ts": 1700001000.0, "pnl_usd": 40.0,
     "entry_features": {"grad_age_s": 3.0, "mcap": 20000.0,
                        "liquidity": 10000.0,
                        "select": dict(STRONG)}},
    {"mint": "A2", "symbol": "STRONG2", "entry_ts": 1700000000.0,
     "close_ts": 1700001000.0, "pnl_usd": -20.0,
     "entry_features": {"grad_age_s": 4.0, "mcap": 30000.0,
                        "liquidity": 12000.0,
                        "select": dict(STRONG)}},
    {"mint": "B1", "symbol": "BLOCK1", "entry_ts": 1700000000.0,
     "close_ts": 1700001000.0, "pnl_usd": -15.0,
     "entry_features": {"grad_age_s": 120.0, "mcap": 80000.0,
                        "liquidity": 30000.0,
                        "select": dict(STRONG, grad_age_s=120.0)}},
]
with open(os.path.join(FIX_HOME, "grad_select_ledger.json"),
          "w") as fh:
    json.dump({"closes": FIX_CLOSES, "positions": {}}, fh)
env = dict(os.environ, FOREX_AGENT_HOME=FIX_HOME)
proc = subprocess.run(
    [sys.executable, os.path.join(REPO, "scripts",
                                 "conviction-replay"),
     "--lane", "grad_select"],
    capture_output=True, text=True, env=env, timeout=60)
check("replay: script exits 0 on fixture ledgers",
      proc.returncode == 0)
check("replay: STRONG tier tallied (n=2)",
      "STRONG    n=2" in proc.stdout)
check("replay: BLOCK tier tallied (n=1)",
      "BLOCK     n=1" in proc.stdout)
check("replay: gate would ENTER 2 and REFUSE 1",
      "gate would ENTER 2" in proc.stdout
      and "REFUSE 1" in proc.stdout)

print("")
if fails:
    print("FAILURES: %d — %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("ALL CONVICTION TESTS PASSED")
