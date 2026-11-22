#!/usr/bin/env python3
"""Trial registry tests (plain script, test_grad.py style).

Covers daemon/launch_scanner/trials.py: register + idempotency,
terms_hash stability (key order, independent sha256), close_trial,
update_stats, corrupt/missing-registry tolerance, and the ledger
backfill against temp ledgers + temp killswitch state. No network;
the registry and all ledgers live in temp dirs.
"""
import hashlib
import json
import os
import sys
import tempfile

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import trials as T

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


TMPD = tempfile.mkdtemp(prefix="trials-test-")
T.REGISTRY_PATH = os.path.join(TMPD, "trial_registry.json")


def _reset():
    if os.path.exists(T.REGISTRY_PATH):
        os.remove(T.REGISTRY_PATH)


# ---------------- missing registry tolerance ----------------
_reset()
check("missing registry: all_trials [] (no raise)", T.all_trials() == [])
check("missing registry: active_trials []", T.active_trials() == [])
check("missing registry: trials_for []", T.trials_for("grad") == [])

# ---------------- register + record shape ----------------
_reset()
terms = {"notional_usd": 1.0, "max_open": 20, "daily_entry_cap": 20}
t1 = T.register("grad", "lane", terms, note="first")
check("register: status active, stopped_ts null",
      t1["status"] == "active" and t1["stopped_ts"] is None)
check("register: closes 0, expectancy null",
      t1["closes"] == 0 and t1["expectancy"] is None)
check("register: lane/kind/note recorded",
      t1["lane"] == "grad" and t1["kind"] == "lane"
      and t1["note"] == "first")
check("register: trial_id present",
      isinstance(t1["trial_id"], str) and len(t1["trial_id"]) > 4)
check("register: started_ts set", t1["started_ts"] > 0)
check("register: terms stored (normalized)",
      t1["terms"] == terms)
check("register: persisted to disk",
      len(T.all_trials()) == 1
      and T.all_trials()[0]["trial_id"] == t1["trial_id"])

# ---------------- terms_hash stability ----------------
expected = hashlib.sha256(
    json.dumps(terms, sort_keys=True, separators=(",", ":"),
               default=str).encode("utf-8")).hexdigest()
check("hash: matches independent sha256 of canonical JSON",
      t1["terms_hash"] == expected and T.terms_hash(terms) == expected)
reordered = {"daily_entry_cap": 20, "max_open": 20, "notional_usd": 1.0}
check("hash: stable under key reordering",
      T.terms_hash(reordered) == expected)
nested_a = {"ladder": {"b": 2, "a": 1}, "x": [1, 2]}
nested_b = {"x": [1, 2], "ladder": {"a": 1, "b": 2}}
check("hash: stable for nested dicts",
      T.terms_hash(nested_a) == T.terms_hash(nested_b))
check("hash: changes when terms change",
      T.terms_hash({"notional_usd": 2.0}) != expected)

# ---------------- idempotency ----------------
t1b = T.register("grad", "lane", dict(reordered), note="duplicate call")
check("idempotent: same lane+terms (reordered) returns existing",
      t1b["trial_id"] == t1["trial_id"])
check("idempotent: registry still holds exactly one trial",
      len(T.all_trials()) == 1)
t2 = T.register("grad", "lane", {"notional_usd": 2.0}, note="new config")
check("idempotent: different terms -> new trial",
      t2["trial_id"] != t1["trial_id"] and len(T.all_trials()) == 2)
t3 = T.register("ceo", "lane", terms)
check("idempotent: same terms, different lane -> new trial",
      t3["trial_id"] != t1["trial_id"] and len(T.all_trials()) == 3)
t4 = T.register("grad", "config_change", {"gate": "runner"},
                note="cfg change")
check("register: config_change kind accepted",
      t4["kind"] == "config_change" and len(T.all_trials()) == 4)

# ---------------- queries ----------------
check("queries: active_trials returns all 4 active",
      len(T.active_trials()) == 4)
check("queries: trials_for(grad) = 3",
      len(T.trials_for("grad")) == 3)
check("queries: trials_for(unknown) = []",
      T.trials_for("nope") == [])

# ---------------- update_stats (active trials only move counters) -------
u = T.update_stats(t1["trial_id"], closes=12, expectancy=-0.25)
check("update_stats: counters set, still active",
      u["closes"] == 12 and u["expectancy"] == -0.25
      and u["status"] == "active" and u["stopped_ts"] is None)
check("update_stats: unknown id -> None",
      T.update_stats("nope-000000", closes=1) is None)

# ---------------- close_trial ----------------
c = T.close_trial(t2["trial_id"], "verdict_halt", 100, -0.123456)
check("close: terminal status + stopped_ts stamped",
      c["status"] == "verdict_halt" and c["stopped_ts"] is not None)
check("close: closes + expectancy recorded",
      c["closes"] == 100 and abs(c["expectancy"] - (-0.123456)) < 1e-9)
check("close: active_trials shrinks to 3",
      len(T.active_trials()) == 3)
check("close: unknown id -> None",
      T.close_trial("nope-000000", "retired", 0, None) is None)
# After closing, the same terms may be re-registered as a NEW trial
# (idempotency covers ACTIVE trials only).
t2b = T.register("grad", "lane", {"notional_usd": 2.0})
check("close: re-register same terms after close -> new trial",
      t2b["trial_id"] != t2["trial_id"] and t2b["status"] == "active")
c2 = T.close_trial(t3["trial_id"], "survived", 100, 0.05)
check("close: survived status accepted",
      c2["status"] == "survived" and c2["expectancy"] == 0.05)

# ---------------- corrupt registry tolerance ----------------
with open(T.REGISTRY_PATH, "w") as f:
    f.write("{not json at all,,,")
check("corrupt: all_trials [] (no raise)", T.all_trials() == [])
check("corrupt: active_trials [] (no raise)", T.active_trials() == [])
t5 = T.register("grad", "lane", {"fresh": True})
check("corrupt: register recovers and stores a valid registry",
      t5["status"] == "active" and len(T.all_trials()) == 1
      and T.all_trials()[0]["trial_id"] == t5["trial_id"])
with open(T.REGISTRY_PATH, "w") as f:
    json.dump({"not": "a list"}, f)
check("corrupt: non-list registry reads as []", T.all_trials() == [])

# ---------------- backfill against temp ledgers ----------------
LED = tempfile.mkdtemp(prefix="trials-backfill-")
REG2 = tempfile.mkdtemp(prefix="trials-backfill-reg-")
T.REGISTRY_PATH = os.path.join(REG2, "trial_registry.json")

grad_ledger = {
    "bankroll": 84.4, "created_ts": 1792609764.0, "state": "active",
    "verdict": None,
    "preregistered_verdict": {
        "lane": "grad", "sample_closes": 100,
        "terms": {"notional_usd": 1.0, "max_open": 20,
                  "runner_rule_applies": False},
        "registered_ts": 1792609764.0},
    "closes": [{"pnl_usd": -0.5}, {"pnl_usd": 0.25},
               {"pnl_usd": -0.25}],
    "positions": {},
}
ceo_ledger = {  # no pre-registration -> minimal backfill terms
    "bankroll": 86.0, "created_ts": 1792497413.0, "state": "active",
    "closes": [{"pnl_usd": -1.0}, {"pnl_usd": 0.5}],
    "positions": {},
}
mirror_ledger = {  # no created_ts, no prereg at all
    "closes": [{"pnl_usd": -0.4}, {"pnl_usd": -0.6}],
    "positions": {},
}
with open(os.path.join(LED, "grad_ledger.json"), "w") as f:
    json.dump(grad_ledger, f)
with open(os.path.join(LED, "ceo_ledger.json"), "w") as f:
    json.dump(ceo_ledger, f)
with open(os.path.join(LED, "degen_mirror_ledger.json"), "w") as f:
    json.dump(mirror_ledger, f)
# grad_raw ledger deliberately absent -> skipped, no crash.
os.makedirs(os.path.join(LED, "killswitch"))
with open(os.path.join(LED, "killswitch", "state.json"), "w") as f:
    json.dump({"lanes": {
        "ceo": {"halted": True},
        "degen_mirror": {"halted": True},
        "grad": {"halted": False}}}, f)

rows = T.backfill_from_ledgers(base_dir=LED)
by_lane = {r["lane"]: r for r in rows}
check("backfill: 3 ledgers found -> 3 trials (missing one skipped)",
      len(rows) == 3 and set(by_lane) == {"grad", "ceo", "degen_mirror"})
g = by_lane.get("grad") or {}
check("backfill: grad terms from preregistered_verdict.terms",
      g.get("terms") == {"notional_usd": 1.0, "max_open": 20,
                         "runner_rule_applies": False})
check("backfill: grad closes + expectancy from closes",
      g.get("closes") == 3
      and abs((g.get("expectancy") or 0) - (-0.5 / 3)) < 1e-6)
check("backfill: grad active (not halted, no verdict)",
      g.get("status") == "active" and g.get("stopped_ts") is None)
check("backfill: grad started_ts from ledger created_ts",
      g.get("started_ts") == 1792609764.0)
ceo = by_lane.get("ceo") or {}
check("backfill: ceo minimal backfill terms (no prereg in ledger)",
      ceo.get("terms") == {"source": "backfill",
                           "note": "terms not pre-registered in ledger"})
check("backfill: ceo halted per killswitch state, closes/exp set",
      ceo.get("status") == "halted" and ceo.get("closes") == 2
      and abs((ceo.get("expectancy") or 0) - (-0.25)) < 1e-9
      and ceo.get("stopped_ts") is not None)
mir = by_lane.get("degen_mirror") or {}
check("backfill: degen_mirror halted, expectancy mean pnl_usd",
      mir.get("status") == "halted" and mir.get("closes") == 2
      and abs((mir.get("expectancy") or 0) - (-0.5)) < 1e-9)
# Backfill again: fully idempotent — active grad trial is returned by
# register(), terminal ceo/mirror trials are refreshed in place.
rows2 = T.backfill_from_ledgers(base_dir=LED)
check("backfill: re-run does not duplicate any trial",
      len(T.trials_for("grad")) == 1 and len(T.trials_for("ceo")) == 1
      and len(T.trials_for("degen_mirror")) == 1
      and len(T.all_trials()) == 3)
check("backfill: ledgers untouched (read-only)",
      json.load(open(os.path.join(LED, "grad_ledger.json")))
      == grad_ledger)

# verdict in ledger -> verdict_halt / survived
LED2 = tempfile.mkdtemp(prefix="trials-backfill-v-")
REG3 = tempfile.mkdtemp(prefix="trials-backfill-reg3-")
T.REGISTRY_PATH = os.path.join(REG3, "trial_registry.json")
vledger = dict(grad_ledger)
vledger["verdict"] = {"action": "HALT — sample expectancy <= 0",
                      "expectancy": -0.1, "n_closes": 3}
with open(os.path.join(LED2, "grad_ledger.json"), "w") as f:
    json.dump(vledger, f)
rows3 = T.backfill_from_ledgers(base_dir=LED2)
check("backfill: ledger verdict HALT -> verdict_halt",
      len(rows3) == 1 and rows3[0]["status"] == "verdict_halt")
vledger["verdict"] = {"action": "SURVIVED — sample expectancy > 0"}
with open(os.path.join(LED2, "grad_ledger.json"), "w") as f:
    json.dump(vledger, f)
T.REGISTRY_PATH = os.path.join(REG3, "trial_registry2.json")
rows4 = T.backfill_from_ledgers(base_dir=LED2)
check("backfill: ledger verdict SURVIVED -> survived",
      len(rows4) == 1 and rows4[0]["status"] == "survived")

# momentum ledger backfill (registered in BACKFILL_LEDGERS 2026-10-23;
# the lane ALSO registers explicitly from its daemon boot path —
# momentum_lane.register_trial — so backfill must stay idempotent
# against those same pre-registered terms).
LED3 = tempfile.mkdtemp(prefix="trials-backfill-mom-")
REG4 = tempfile.mkdtemp(prefix="trials-backfill-reg4-")
T.REGISTRY_PATH = os.path.join(REG4, "trial_registry.json")
mom_terms = {"notional_usd": 1.0, "max_open": 3,
             "daily_entry_cap": None}
mom_ledger = {
    "bankroll": 99.0, "created_ts": 1792747350.0, "state": "active",
    "verdict": None,
    "preregistered_verdict": {
        "lane": "momentum", "sample_closes": 100,
        "terms": mom_terms, "registered_ts": 1792747350.0},
    "closes": [{"pnl_usd": 0.5}, {"pnl_usd": -0.25}],
    "positions": {},
}
with open(os.path.join(LED3, "momentum_ledger.json"), "w") as f:
    json.dump(mom_ledger, f)
# No killswitch state in LED3 -> fallback halted set {ceo,
# degen_mirror}; momentum is not in it, so the trial stays active.
rows_m = T.backfill_from_ledgers(base_dir=LED3)
check("backfill: momentum ledger picked up -> 1 trial",
      len(rows_m) == 1 and rows_m[0]["lane"] == "momentum")
mrow = rows_m[0] if rows_m else {}
check("backfill: momentum terms from preregistered_verdict.terms",
      mrow.get("terms") == mom_terms)
check("backfill: momentum closes + expectancy + active status",
      mrow.get("closes") == 2
      and abs((mrow.get("expectancy") or 0) - 0.125) < 1e-9
      and mrow.get("status") == "active")
rows_m2 = T.backfill_from_ledgers(base_dir=LED3)
check("backfill: momentum re-run does not duplicate",
      len(rows_m2) == 1 and len(T.trials_for("momentum")) == 1)


print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all trials tests passed")
