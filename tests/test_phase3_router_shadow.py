#!/usr/bin/env python3
"""Phase 3 tests — unified provider infrastructure.

Plain script, no network (one suite uses a fake transport; the
slot probe uses a patched rpc call). Covers:
- router modes: shadow (Helius leads) vs end_state (Alchemy-first,
  Helius absent from ordinary routing); route() carries mode;
  end_state_order() view; verification never includes Helius;
  getSignaturesForAddress is never sent to Chainstack (capability
  gate in router.call with a fake transport).
- shadow observer: agreement classes (exact / fallback_consistent /
  divergent / no_serve / refused), journal persistence across a
  fresh observer instance (restart proof), summarize() rates.
- persistent usage accounting (D-07): parsed-stream events,
  parsed-events calls (10 credits), preprocessed observations +
  verifications all recorded in the UsageStore and surviving a
  fresh store instance (restart proof).
- hard budgets: daily counters + daily rollover, daily stop for
  nonessential only, per-feature budget stop, essential traffic
  never stopped, essential_over_budget flag.
- health persistence: persist/restore round-trip incl.
  monthly_quota_down latch.
- D-16 probe math: lag medians from canned slots (no network).
- reserves shadow hook: wrapper observes via a patched _rpc_impl
  and survives an observer failure without changing the result.
"""
import io
import json
import os
import struct
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["ROUTER_SHADOW_OFF"] = "1"

from daemon.intelligence.acquisition import helius_parsed_events as HPE
from daemon.intelligence.acquisition import helius_preprocessed as HPP
from daemon.intelligence.acquisition import helius_streams as HS
from daemon.intelligence.acquisition import probes as PROBES
from daemon.intelligence.acquisition import provider_capabilities as CAPS
from daemon.intelligence.acquisition import provider_health as PH
from daemon.intelligence.acquisition import provider_router as ROUTER
from daemon.intelligence.acquisition import provider_usage as PU
from daemon.intelligence.acquisition import rpc_client as rpc_mod
from daemon.intelligence.acquisition.provider_health import HealthMonitor
from daemon.intelligence.acquisition.provider_usage import UsageStore
from daemon.intelligence.acquisition.shadow_observer import (
    ShadowObserver, agreement_class)
from daemon.intelligence.acquisition.usage_tracker import UsageTracker
from daemon.intelligence.decoders._base58 import b58decode

fails = []
total = [0]
HOME = tempfile.mkdtemp(prefix="phase3_")


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- router modes ----------------
r_default = ROUTER.ProviderRouter(home=HOME)
check("default is end_state without routine Helius",r_default.mode=="end_state" and not r_default.helius_enabled)
r_shadow = ROUTER.ProviderRouter(home=HOME,mode="shadow",helius_enabled=True)
check("explicit shadow mode retains Helius diagnostic support",
      r_shadow.mode == "shadow" and r_shadow.helius_enabled is True)
dec = r_shadow.route("getAccountInfo", ["addr"])
check("shadow route() carries mode + helius primary",
      dec["mode"] == "shadow" and dec["primary"] == "helius"
      and dec["refused"] is False)
r_end = ROUTER.ProviderRouter(home=HOME, mode="end_state",
                              helius_enabled=False)
dec_e = r_end.route("getAccountInfo", ["addr"])
check("end_state route(): alchemy primary, helius absent",
      dec_e["mode"] == "end_state" and dec_e["primary"] == "alchemy"
      and "helius" not in dec_e["candidates"])
dec_v = r_end.route("getAccountInfo", ["addr"],
                    hints={"workload": "independent_verification"})
check("end_state verification: chainstack first, helius absent",
      dec_v["candidates"][0] == "chainstack"
      and "helius" not in dec_v["candidates"])
dec_h = r_end.route("getTransaction", ["sig"])
check("end_state routine tx: alchemy first",
      dec_h["candidates"][0] == "alchemy")
order = ROUTER.end_state_order("routine_account", "getAccountInfo")
check("end_state_order() view: alchemy, chainstack, public",
      order == ["alchemy", "chainstack", "public"])
check("preprocessed refused in both modes",
      r_shadow.route("getSlot", [], hints={"workload":
                     "preprocessed_observation"})["refused"] is True
      and r_end.route("getSlot", [], hints={"workload":
                     "preprocessed_observation"})["refused"] is True)
check("complex parse refused when helius disabled (end_state)",
      r_end.route("getTransaction", ["sig"], hints={"workload":
                  "complex_tx_parse"})["refused"] is True)
try:
    ROUTER.ProviderRouter(home=HOME, mode="bogus")
    check("unknown routing mode raises", False)
except ValueError:
    check("unknown routing mode raises", True)

# Chainstack never receives getSignaturesForAddress (capability gate).
seen = []


def _fake_transport(req, timeout=None):
    seen.append(req.full_url)
    import urllib.request as _u
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "result": "ok"}).encode()

    class Resp:
        status = 200

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return Resp()


os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
r_call = ROUTER.ProviderRouter(home=HOME, transport=_fake_transport)
res = r_call.call("getSignaturesForAddress", ["addr", {"limit": 1}],
                  priority="card")
check("router.call sigs: chainstack skipped by capability gate, "
      "alchemy serves",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and not any("chainstack" in u for u in seen)
      and any("alchemy" in u for u in seen))
del os.environ["ALCHEMY_API_KEY"]
# Alchemy key dual lookup (Phase 3 fix): a caller passing the user
# home still finds the key under ~/.forex-agent/alchemy/api_key.
from daemon.intelligence.acquisition.providers.base import alchemy_key
_key_home = tempfile.mkdtemp(prefix="phase3_alchemykey_")
os.makedirs(os.path.join(_key_home, ".forex-agent", "alchemy"))
with open(os.path.join(_key_home, ".forex-agent", "alchemy",
                       "api_key"), "w") as fh:
    fh.write("k-test-not-real")
check("alchemy_key finds the runtime-home key file from a plain "
      "user home",
      alchemy_key(home=_key_home) == "k-test-not-real")

# ---------------- shadow observer ----------------
class FakeRouter:
    def route(self, method, params=None, priority="card", hints=None):
        return {"workload": "routine_account", "helius_priority": "P2",
                "mode": "shadow", "primary": "helius",
                "candidates": ["helius", "alchemy", "public"],
                "refused": False, "refusal_reason": None}


obs = ShadowObserver(home=HOME, router=FakeRouter())
e1 = obs.observe("getSlot", [], served="helius", kind="ok")
e2 = obs.observe("getSlot", [], served="public-rpc", kind="ok")
e3 = obs.observe("getSlot", [], served="chainstack", kind="ok")
e4 = obs.observe("getSlot", [], served=None, kind="error")
check("shadow agreement classes: exact / fallback_consistent / "
      "divergent / no_serve",
      e1["agreement"] == "exact" and e2["agreement"] ==
      "fallback_consistent" and e3["agreement"] == "divergent"
      and e4["agreement"] == "no_serve")
check("fallback_consistent carries a fallback_difference",
      e2["fallback_difference"] is not None
      and "public" in e2["fallback_difference"])
check("exact carries no fallback_difference",
      e1["fallback_difference"] is None)
summ = obs.summarize()
check("shadow summarize: 4 total, agreement rate 2/3",
      summ["total"] == 4 and summ["counts"] == {
          "exact": 1, "fallback_consistent": 1, "divergent": 1,
          "no_serve": 1, "refused": 0}
      and abs(summ["agreement_rate"] - 2.0 / 3.0) < 1e-9
      and len(summ["divergences"]) == 1)
# Restart proof: a FRESH observer instance reads the same journal.
obs2 = ShadowObserver(home=HOME, router=FakeRouter())
summ2 = obs2.summarize()
check("shadow journal survives a fresh observer (restart proof)",
      summ2["total"] == 4
      and abs(summ2["agreement_rate"] - 2.0 / 3.0) < 1e-9)
check("agreement_class refused",
      agreement_class({"refused": True,
                       "refusal_reason": "paid"}, "helius",
                      "ok")[0] == "refused")

# ---------------- D-07 persistent accounting ----------------
store = UsageStore(home=HOME)
# Parsed stream: feed two synthetic notifications.
client = HS.HeliusParsedStreamClient(
    wss_url="wss://example.invalid/?api-key=x",
    ws_factory=lambda *a: None, usage_store=store)
FIXDIR = os.path.join(REPO, "tests", "data", "intelligence",
                      "acquisition")
with open(os.path.join(FIXDIR, "live_getTransaction_pump.json")) as fh:
    FIX_TX = json.load(fh)["result"]
FIX_SIG = FIX_TX["transaction"]["signatures"][0]
FIX_SLOT = FIX_TX["slot"]


def _notification(slot):
    return json.dumps({
        "jsonrpc": "2.0",
        "method": "parsedTransactionNotification",
        "params": {"subscription": 7, "result": {"value": {
            "signature": FIX_SIG, "slot": slot, "status": "success",
            "error": None, "fee": 5000,
            "feePayer": "7jjEEgTx3xjGMHrykHQmjcWQjzXmZx83SQJWnYwREK5H",
            "accountKeys": ["7jjEEgTx3xjGMHrykHQmjcWQjzXmZx83SQJWnYwREK5H"],
            "instructions": [{"programId": HS.PUMP_PROGRAM_ID,
                              "decoded": {"name": "buy", "args": {},
                                          "accounts": {}}}],
            "matchedIndexes": [0],
            "summary": {"type": "buy", "description": "x",
                        "parsedData": {}},
            "nativeTransfers": [], "tokenTransfers": []}}}})


client._on_message(None, _notification(FIX_SLOT))
client._on_message(None, _notification(FIX_SLOT + 1))
client.record_verified_event()
check("stream: in-memory events_received=2, verified=1",
      client.events_received == 2 and client.events_verified == 1)
persist = client.persistent_stats()
check("stream: persistent parsed_streams calls=3 (2 events + "
      "1 verified), cost=2 credits",
      persist is not None and persist["calls"] == 3
      and persist["cost"] == 2)
store2 = UsageStore(home=HOME)
rec = store2.get("helius_main")
check("stream counters survive a fresh UsageStore (restart proof)",
      (rec.get("by_feature") or {}).get("parsed_streams", {}).get(
          "calls") == 3)
# 429 path persists too.
client._on_error(None, "429 Too Many Requests")
check("stream handshake 429 persists as rate_limited",
      store2.get("helius_main")["rate_limited"] >= 1)

# Parsed events: fake opener, one ok call = 10 credits persistent.
class FakeResp:
    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __call__(self, req, timeout=None):
        return FakeResp([{"signature": FIX_SIG, "slot": FIX_SLOT,
                          "status": "success", "fee": 5000,
                          "accountKeys": [], "instructions": [],
                          "nativeTransfers": [], "tokenTransfers": [],
                          "summary": {"type": "buy"}}])


pe = HPE.HeliusParsedEventsClient(api_key="testkey-not-real",
                                  _opener=FakeOpener(),
                                  usage_store=store)
res = pe.parse_transactions([FIX_SIG], "complex_investigation")
check("parsed-events ok call recorded persistently (10 credits)",
      res["kind"] == "ok" and pe.credits_spent == 10
      and pe.persistent_stats()["cost"] == 10
      and pe.persistent_stats()["calls"] == 1)
store3 = UsageStore(home=HOME)
check("parsed-events spend survives a fresh store (restart proof)",
      (store3.get("helius_main").get("by_feature") or {}).get(
          "parsed_events", {}).get("cost") == 10)

# Preprocessed lifecycle: observe + verify persist (cost None).
def synth_frame(slot, signature, tx_bytes=b"\x01\x02\x03"):
    return (bytes([1]) + struct.pack("<Q", slot)
            + b58decode(signature) + tx_bytes)


life = HPP.PreprocessedLifecycle(usage_store=store)
frame = synth_frame(FIX_SLOT, FIX_SIG)
r, _n = life.observe(frame)
check("preprocessed observe recorded persistently (cost None)",
      r is not None and life.persistent_stats()["calls"] == 1
      and life.persistent_stats()["cost"] == 0)
ver, _n2 = life.verify_execution(
    FIX_SIG, {"transaction": {"signatures": [FIX_SIG]},
              "slot": FIX_SLOT, "meta": {"err": None, "fee": 5000}})
check("preprocessed verify recorded persistently",
      ver is not None and life.verified_count == 1
      and life.persistent_stats()["calls"] == 2)
store4 = UsageStore(home=HOME)
check("preprocessed counters survive a fresh store (restart proof)",
      (store4.get("helius_main").get("by_feature") or {}).get(
          "preprocessed_observation", {}).get("calls") == 2)

# ---------------- hard budgets: daily + feature ----------------
# Isolated home: earlier sections already recorded helius_main /
# alchemy spend in HOME, and budget assertions need clean counters.
HOME_BUDGETS = tempfile.mkdtemp(prefix="phase3_budgets_")
trk = UsageTracker(home=HOME_BUDGETS,
                   store=UsageStore(home=HOME_BUDGETS))
check("daily budget unset by default (operator-configured only; "
      "no invented monthly/30 default)",
      PU.daily_budget_for("helius_main") is None
      and PU.daily_budget_for("public") is None)
# Daily rollover: record yesterday, then today.
yday = time.time() - 86400
s = UsageStore(home=HOME_BUDGETS)
s.record("alchemy", "alchemy", "getSlot", "routine_account",
         "card", cost=100, kind="ok", ts=yday)
s.record("alchemy", "alchemy", "getSlot", "routine_account",
         "card", cost=50, kind="ok")
rec_a = s.get("alchemy")
check("daily counters reset on day rollover, monthly accumulates",
      rec_a["daily_cost_spent"] == 50 and rec_a["cost_spent"] == 150)
# Daily stop: operator sets a daily amount, then spend past 95%
# of it stops nonessential only.
PU.BUDGET_OVERRIDES["alchemy"] = {"daily_amount": 1_000_000}
daily_cap = PU.daily_budget_for("alchemy")
check("daily budget returns the operator-configured amount",
      daily_cap == 1_000_000)
s.record("alchemy", "alchemy", "getSlot", "routine_account",
         "background", cost=int(daily_cap), kind="ok")
trk2 = UsageTracker(home=HOME_BUDGETS, store=s)
ok, reason = trk2.nonessential_allowed("alchemy", "background")
check("daily exhausted budget blocks nonessential",
      ok is False and reason == "daily_budget_exhausted")
ok_e, _r = trk2.nonessential_allowed("alchemy", "critical")
check("daily exhausted budget also blocks essential (critical)",
      ok_e is False and _r == "daily_budget_exhausted")
status = trk2.budget_status("alchemy")
check("budget_status reports daily fields + essential_over_budget "
      "flag present",
      status["daily_spent"] >= int(daily_cap)
      and status["daily_amount"] == daily_cap
      and "essential_over_budget" in status)
PU.BUDGET_OVERRIDES["alchemy"] = {"daily_amount": 1_050_000}
check("daily below exhaustion still sheds nonessential at95percent",
      trk2.nonessential_allowed("alchemy","background")[0] is False)
check("daily below exhaustion still allows essential",
      trk2.nonessential_allowed("alchemy","critical")[0] is True)
del PU.BUDGET_OVERRIDES["alchemy"]
# Feature budget stop.
PU.set_feature_budget("helius_main", "parsed_events", 20,
                      reason="phase3 test")
trk3 = UsageTracker(home=HOME_BUDGETS,
                    store=UsageStore(home=HOME_BUDGETS))
trk3.record("helius_main", "helius", "parsed_events/transactions",
            "complex_tx_parse", "background", cost=10, kind="ok")
trk3.record("helius_main", "helius", "parsed_events/transactions",
            "complex_tx_parse", "background", cost=10, kind="ok")
ok_f, reason_f = trk3.nonessential_allowed(
    "helius_main", "background", workload="complex_tx_parse",
    provider="helius")
check("feature exhausted budget (20 of 20) blocks nonessential",
      ok_f is False
      and reason_f == "feature_budget_exhausted:parsed_events")
ok_fe, reason_fe = trk3.nonessential_allowed(
    "helius_main", "card", workload="complex_tx_parse",
    provider="helius")
check("feature exhausted budget also blocks essential (card)",
      ok_fe is False and reason_fe == "feature_budget_exhausted:parsed_events")
check("feature_status reports enforced spend",
      trk3.feature_status("helius_main", "parsed_events")["pct"] == 1.0)
# Raising the test-only limit to 21 leaves20/21 above95% but below exhaustion.
PU.set_feature_budget("helius_main", "parsed_events", 21, reason="below-exhaustion fixture")
check("feature below exhaustion still sheds nonessential at95percent",
      trk3.nonessential_allowed("helius_main","background",workload="complex_tx_parse",provider="helius")[0] is False)
check("feature below exhaustion still allows essential",
      trk3.nonessential_allowed("helius_main","card",workload="complex_tx_parse",provider="helius")[0] is True)
del PU.FEATURE_BUDGETS[("helius_main", "parsed_events")]

# ---------------- health persistence ----------------
hm = HealthMonitor(home=HOME)
hm.set_configured("helius", True)
hm.record_rpc({"provider": "helius", "kind": "ok", "elapsed": 0.2})
hm.record_rpc({"provider": "helius", "kind": "rate_limited",
               "elapsed": 0.1, "rate_limit_kind": "monthly_quota"})
hm.record_wss_handshake_429("helius")
hm.persist()
hm2 = HealthMonitor(home=HOME)
restored = hm2.restore()
check("health persist/restore round-trip (calls, 429s, quota-down)",
      restored["helius"]["calls"] == 2
      and restored["helius"]["rpc_429s"] == 1
      and restored["helius"]["wss_handshake_429s"] == 1
      and restored["helius"]["monthly_quota_down"] is True
      and restored["helius"]["status"] == "down")
# Corrupt health file restores to empty, never raises.
with open(os.path.join(HOME, "acquisition_provider_health.json"),
          "w") as fh:
    fh.write("{not json")
hm3 = HealthMonitor(home=HOME)
check("corrupt health file restores to empty, never raises",
      hm3.restore() == {})

# ---------------- D-16 probe math (canned, no network) ----------------
slots = {("alchemy", "processed"): 100, ("helius", "processed"): 70,
         ("public", "processed"): 71,
         ("alchemy", "confirmed"): 90, ("helius", "confirmed"): 60,
         ("public", "confirmed"): 61,
         ("alchemy", "finalized"): 50, ("helius", "finalized"): 45,
         ("public", "finalized"): 46}


def _canned_rpc(provider, method, params, timeout=12, home=None):
    commitment = (params or [{}])[0].get("commitment")
    return rpc_mod._result(slots[(provider, commitment)], "ok", 0.01,
                           provider)


orig_rpc = rpc_mod.rpc
rpc_mod.rpc = _canned_rpc
orig_quiet = PROBES.helius_quiet_active
PROBES.helius_quiet_active = lambda home=None: False
try:
    probe = PROBES.probe_slot_commitments(rounds=2, pause_s=0,
                                          sleep=lambda s: None)
finally:
    rpc_mod.rpc = orig_rpc
    PROBES.helius_quiet_active = orig_quiet
lag = probe["lag"]
check("D-16 probe math: processed lag vs helius median 30",
      lag["processed"]["vs_helius"]["median"] == 30
      and lag["processed"]["vs_helius"]["n"] == 2)
check("D-16 probe math: confirmed lag 30, finalized lag 5",
      lag["confirmed"]["vs_helius"]["median"] == 30
      and lag["finalized"]["vs_helius"]["median"] == 5)
check("D-16 probe math: vs public lag processed 29",
      lag["processed"]["vs_public"]["median"] == 29)

# Routine reserves no longer invokes the retired Helius shadow hook.
import daemon.launch_scanner.reserves as reserves
from daemon.intelligence.acquisition import routine
from unittest.mock import patch
with patch.object(routine,'call',return_value={"provider":"alchemy","kind":"ok","result":"RES","decision_usable":True}) as rpc:
    check("reserves central wrapper preserves result",reserves.rpc("getSlot",[])=="RES")
    check("reserves central wrapper called once",rpc.call_count==1)
with patch.object(routine,'call',side_effect=RuntimeError('fixture failure')):
    check("reserves acquisition exception returns None",reserves.rpc("getSlot",[]) is None)

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
