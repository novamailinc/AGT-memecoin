#!/usr/bin/env python3
"""Phase 2 acquisition tests — shadow.py + benchmark.py.

Plain script, no network. Covers (§11): shadow agreement reporting,
live result returned unchanged (nothing downstream consumes a shadow
answer), disagreement as evidence (not a skip), ground-truth
validation (a shadow answer disagreeing with the on-chain record is
a failure, not a tie), removal criteria, transaction-decoding
agreement at the comparison level (creator / migrate-instruction
fields from a jsonParsed fixture — the full decoders are Phase 3),
benchmark latency summaries.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import shadow as SH
from daemon.intelligence.acquisition import benchmark as BM

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def res(result, kind="ok", elapsed=0.1, provider="helius"):
    return {"result": result, "kind": kind, "elapsed": elapsed,
            "provider": provider}


HOME = tempfile.mkdtemp(prefix="acq-shadow-")

# ---------------- agreement -----------------------------------------
check("identical results agree",
      SH.results_agree(res({"a": 1}), res({"a": 1})))
check("key order does not matter",
      SH.results_agree(res({"a": 1, "b": 2}), res({"b": 2, "a": 1})))
check("different values disagree",
      not SH.results_agree(res({"a": 1}), res({"a": 2})))
check("shadow error is a disagreement, not a skip",
      not SH.results_agree(res({"a": 1}), res(None, kind="error")))
check("live error is a disagreement",
      not SH.results_agree(res(None, kind="error"), res({"a": 1})))
check("price within tolerance agrees",
      SH.results_agree(res(100.0), res(101.0), price_tolerance=0.02))
check("price outside tolerance disagrees",
      not SH.results_agree(res(100.0), res(103.0), price_tolerance=0.02))
check("float noise is not disagreement",
      SH.results_agree(res(0.3000000001), res(0.3)))

# ---------------- runner: live only is returned --------------------
live_calls = []
shadow_calls = []


def live_call(method, params):
    live_calls.append((method, params))
    return res({"slot": 300001, "value": 42}, provider="helius")


def shadow_alchemy(method, params):
    shadow_calls.append(("alchemy", method, params))
    return res({"slot": 300001, "value": 42}, elapsed=0.4,
               provider="alchemy")


def shadow_chainstack(method, params):
    shadow_calls.append(("chainstack", method, params))
    return res({"slot": 300001, "value": 43}, elapsed=0.2,
               provider="chainstack")


runner = SH.ShadowRunner(home=HOME, live_call=live_call,
                         shadow_calls={"alchemy": shadow_alchemy,
                                       "chainstack": shadow_chainstack})
live, per = runner.compare("background", "getSlot", [])
check("compare returns the LIVE result only",
      live["result"] == {"slot": 300001, "value": 42}
      and live["provider"] == "helius")
check("shadow providers were called with identical inputs",
      shadow_calls == [("alchemy", "getSlot", []),
                       ("chainstack", "getSlot", [])])
check("alchemy agreement recorded (identical)",
      runner.agreement_rate("background", "alchemy") == 1.0)
check("chainstack disagreement recorded (value differs)",
      runner.agreement_rate("background", "chainstack") == 0.0)
check("journal written",
      os.path.exists(os.path.join(HOME, SH.JOURNAL_FILE)))
lines = open(os.path.join(HOME, SH.JOURNAL_FILE)).read().strip().split("\n")
check("journal has one line per shadow comparison",
      len(lines) == 2 and all(json.loads(l)["workload"] == "background"
                              for l in lines))
check("removal NOT ready below sample size",
      runner.removal_ready("background", "alchemy")[0] is False)

# drive alchemy to the sample size with agreements
for _ in range(49):
    runner.compare("background", "getSlot", [])
check("removal ready at sample size with 100% agreement",
      runner.removal_ready("background", "alchemy")[0] is True)
ready, why = runner.removal_ready("background", "chainstack")
check("removal refused on disagreement", ready is False)

# 429 in the window blocks removal
HOME2 = tempfile.mkdtemp(prefix="acq-shadow2-")


def live2(m, p):
    return res({"slot": 1})


def shadow429(m, p):
    return {"result": None, "kind": "rate_limited", "elapsed": 0.05,
            "provider": "alchemy", "rate_limit_kind": "throughput"}


r2 = SH.ShadowRunner(home=HOME2, live_call=live2,
                     shadow_calls={"alchemy": shadow429})
for _ in range(50):
    r2.compare("background", "getSlot", [])
ready, why = r2.removal_ready("background", "alchemy")
check("throughput 429s recorded and block removal",
      ready is False
      and r2.summary("background")["alchemy"]["provider_429s"] == 50)


def shadow_quota(m, p):
    return {"result": None, "kind": "rate_limited", "elapsed": 0.05,
            "provider": "alchemy", "rate_limit_kind": "monthly_quota"}


r3 = SH.ShadowRunner(home=tempfile.mkdtemp(prefix="acq-shadow3-"),
                     live_call=live2, shadow_calls={"alchemy": shadow_quota})
for _ in range(50):
    r3.compare("background", "getSlot", [])
ready, why = r3.removal_ready("background", "alchemy")
check("monthly-quota 429 recorded and blocks removal outright",
      ready is False
      and r3.summary("background")["alchemy"].get("monthly_quota_429")
      is True)

# ---------------- ground truth --------------------------------------
# A shadow answer disagreeing with the on-chain record is a FAILURE.
ground_truth = {
    "mint": "Mint111111111111111111111111111111111111111",
    "creator": "Creator11111111111111111111111111111111111",
    "curve": "Curve11111111111111111111111111111111111111",
    "slot": 300001,
}
good = res({"mint": ground_truth["mint"],
            "creator": ground_truth["creator"],
            "curve": ground_truth["curve"], "slot": 300001})
agree, bad = SH.check_ground_truth(
    good, ground_truth, ["mint", "creator", "curve", "slot"])
check("ground truth: matching shadow answer passes", agree and bad == [])
wrong = res({"mint": ground_truth["mint"], "creator": "SomeoneElse",
             "curve": ground_truth["curve"], "slot": 300001})
agree, bad = SH.check_ground_truth(
    wrong, ground_truth, ["mint", "creator", "curve", "slot"])
check("ground truth: wrong creator is a failure naming the field",
      not agree and bad == ["creator"])
agree, bad = SH.check_ground_truth(
    res(None, kind="error"), ground_truth, ["mint"])
check("ground truth: failed shadow call fails validation",
      not agree)

# ---------------- transaction-decoding agreement (comparison level)
# A synthetic jsonParsed migration tx: the fields the live code
# already derives (first signer = creator, Migrate log present).
# The shadow comparison must catch a provider whose jsonParsed
# fidelity drops either field. Full decoders are Phase 3.
FIXTURE_TX = {
    "slot": 300001,
    "transaction": {
        "message": {"accountKeys": [
            {"pubkey": "Creator11111111111111111111111111111111111",
             "signer": True},
            {"pubkey": "Curve11111111111111111111111111111111111111",
             "signer": False}],
            "instructions": [{"programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                              "parsed": {"type": "migrate"}}]},
        "signatures": ["sigXYZ"]},
    "meta": {"err": None,
             "logMessages": ["Program log: Instruction: Migrate",
                             "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success"]},
}


def derive_fields(tx):
    """The fields live code derives from a jsonParsed tx (same
    derivation the shadow compares)."""
    msg = tx["transaction"]["message"]
    signers = [k["pubkey"] for k in msg["accountKeys"]
               if k.get("signer")]
    logs = (tx.get("meta") or {}).get("logMessages") or []
    return {"creator": signers[0] if signers else None,
            "migrated": any("Migrate" in l for l in logs),
            "succeeded": (tx.get("meta") or {}).get("err") is None,
            "slot": tx.get("slot")}


check("fixture derives creator + migrate + success",
      derive_fields(FIXTURE_TX) == {
          "creator": "Creator11111111111111111111111111111111111",
          "migrated": True, "succeeded": True, "slot": 300001})
live_tx = res(derive_fields(FIXTURE_TX))
shadow_tx_same = res(derive_fields(FIXTURE_TX))
shadow_tx_drop = res({"creator": None, "migrated": False,
                      "succeeded": True, "slot": 300001})
check("identical decoded fields agree",
      SH.results_agree(live_tx, shadow_tx_same))
check("provider dropping creator/migrate fields disagrees",
      not SH.results_agree(live_tx, shadow_tx_drop))

# ---------------- benchmark -----------------------------------------
calls = {"n": 0}


def bench_call(method, params):
    calls["n"] += 1
    return {"result": 300001, "kind": "ok",
            "elapsed": 0.1 + 0.01 * calls["n"], "provider": "public"}


rep = BM.run_benchmark("public", [("getSlot", [])], calls_per_method=5,
                       rpc_call=bench_call)
m = rep["methods"]["getSlot"]
check("benchmark reports median/p95/p99 + ok count",
      m["n"] == 5 and m["ok"] == 5 and m["median_s"] is not None
      and m["p95_s"] is not None and m["p99_s"] is not None)
check("benchmark p95 >= median", m["p95_s"] >= m["median_s"])
check("benchmark counts quota spent (public cost unknown -> 0)",
      m["quota_spent"] == 0)
rep2 = BM.run_benchmark("alchemy", [("getSlot", [])], calls_per_method=3,
                        rpc_call=bench_call)
check("unconfigured provider reports blocked, never fabricates",
      rep2["blocked"] is True and rep2["methods"] == {})
ev = BM.event_latency_report(received_ts=1000.0, decided_ts=1017.5)
check("event latency splits provider vs processing (processing here)",
      ev["processing_latency_s"] == 17.5
      and ev["provider_latency_s"] is None)  # needs creation_ts
ev2 = BM.event_latency_report(received_ts=None, decided_ts=1017.5)
check("missing timestamps -> None, never fabricated",
      ev2["processing_latency_s"] is None)

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
