#!/usr/bin/env python3
"""Phase 4 tests — central provider router (provider_router.py).

Plain script, no network: HTTP is driven through an injected fake
opener keyed by host; recorded fixtures under
tests/data/intelligence/acquisition/ are replayed as fake responses
(real on-chain data captured 2026-11-22 via reserves.rpc()).

Covers (§8 + §7): workload classification, the routing rules
(routine -> Alchemy; history/recovery -> Chainstack in-window else
Alchemy; verification -> Chainstack; complex parse / parsed stream
-> Helius only; preprocessed -> REFUSED, paid-gated), Helius P0-P3
prioritisation, failover with fallback-difference recording,
capability refusal (Chainstack never sent getSignaturesForAddress),
circuit breaker, response cache, budget stop for nonessential
traffic (essential never stopped), account disablement, usage
journal recording which account serviced each request, no account
rotation, and provider_manager delegating to the router (the router
is the ONLY place routing decisions live).
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import io
import json
import os
import sys
import tempfile
import urllib.error

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import provider_manager as PM
from daemon.intelligence.acquisition import provider_router as PR
from daemon.intelligence.acquisition.provider_usage import UsageStore
from daemon.intelligence.acquisition.request_scheduler import (
    PRIORITY_BACKGROUND, PRIORITY_CARD, PRIORITY_CRITICAL,
    PRIORITY_DEPLOYER)
from daemon.intelligence.acquisition.usage_tracker import UsageTracker

fails = []
total = [0]
HOME = tempfile.mkdtemp(prefix="acq_router_")
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


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


class HostTransport:
    """Fake opener routing by host. ``behaviour`` maps a host
    substring to a list of (status, body) or ('raise', exc). Calls
    are recorded per host."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []       # full urls in order
        self.by_host = {}     # host-substring -> count
        self._idx = {}

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        host = None
        for key in self.behaviour:
            if key in url:
                host = key
                break
        if host is None:
            return FakeResp({"jsonrpc": "2.0", "id": 1, "result": None})
        self.by_host[host] = self.by_host.get(host, 0) + 1
        items = self.behaviour[host]
        i = self._idx.get(host, 0)
        self._idx[host] = min(i + 1, len(items) - 1)
        item = items[min(i, len(items) - 1)]
        if item[0] == "raise":
            raise item[1]
        status, body = item
        if status >= 400:
            raise urllib.error.HTTPError(
                url, status, "err", {},
                io.BytesIO(json.dumps(body).encode()))
        return FakeResp(body, status)


def ok(value):
    return (200, {"jsonrpc": "2.0", "id": 1, "result": value})


def err429(msg="too many requests"):
    return (429, {"jsonrpc": "2.0", "id": 1,
                  "error": {"code": 429, "message": msg}})


def router(home=None, helius_enabled=False, transport=None):
    # Fresh home per router by default: scheduler quota files and
    # usage state are shared per home, so reusing one home across
    # tests would let one test's 429 backoff / token drain leak
    # into the next test's routing decisions.
    if home is None:
        home = tempfile.mkdtemp(prefix="acq_router_")
    return PR.ProviderRouter(home=home, helius_enabled=helius_enabled,
                             transport=transport)


def load_fixture(name):
    with open(os.path.join(REPO, "tests", "data", "intelligence",
                           "acquisition", name + ".json")) as fh:
        return json.load(fh)


# ---------------- workload classification ----------------
check("classify getBalance -> routine_account",
      PR.classify_workload("getBalance")[0] == PR.ROUTINE_ACCOUNT)
check("classify getAccountInfo -> routine_account",
      PR.classify_workload("getAccountInfo")[0] == PR.ROUTINE_ACCOUNT)
check("classify getTransaction -> routine_tx",
      PR.classify_workload("getTransaction")[0] == PR.ROUTINE_TX)
check("classify getSignaturesForAddress -> historical_range",
      PR.classify_workload("getSignaturesForAddress")[0]
      == PR.HISTORICAL_RANGE)
check("classify getSlot -> routine_account",
      PR.classify_workload("getSlot")[0] == PR.ROUTINE_ACCOUNT)
check("classify getProgramAccounts -> routine_account",
      PR.classify_workload("getProgramAccounts")[0] == PR.ROUTINE_ACCOUNT)
check("classify unknown method -> error, no workload",
      PR.classify_workload("bogusMethod")[2] is not None)
check("declared historical override on routine tx",
      PR.classify_workload("getTransaction", hints={
          "workload": PR.HISTORICAL_RANGE})[0] == PR.HISTORICAL_RANGE)
check("declared recovery override on sigs method",
      PR.classify_workload("getSignaturesForAddress", hints={
          "workload": PR.MISSED_EVENT_RECOVERY})[0]
      == PR.MISSED_EVENT_RECOVERY)
check("contradictory declaration refused, not reinterpreted",
      PR.classify_workload("getBalance", hints={
          "workload": PR.HISTORICAL_RANGE})[2] is not None)
check("specialised workload via call() refused with guidance",
      PR.classify_workload("getTransaction", hints={
          "workload": PR.PARSED_TX_STREAM})[2] is not None)

# ---------------- Helius P0-P3 ----------------
check("P0: parsed stream, critical",
      PR.helius_priority(PR.PARSED_TX_STREAM, {},
                         PRIORITY_CRITICAL) == "P0")
check("P1: parsed stream, non-critical",
      PR.helius_priority(PR.PARSED_TX_STREAM, {},
                         PRIORITY_CARD) == "P1")
check("P1: complex parse, critical",
      PR.helius_priority(PR.COMPLEX_TX_PARSE, {},
                         PRIORITY_CRITICAL) == "P1")
check("P2: complex parse, non-critical (complex interpretation)",
      PR.helius_priority(PR.COMPLEX_TX_PARSE, {},
                         PRIORITY_CARD) == "P2")
check("P0: high-value time-sensitive monitoring",
      PR.helius_priority(PR.ROUTINE_TX, {"high_value": True},
                         PRIORITY_CRITICAL) == "P0")
check("P1: entry/exit-critical routine read",
      PR.helius_priority(PR.ROUTINE_TX, {}, PRIORITY_CRITICAL) == "P1")
check("P2: wallet investigation",
      PR.helius_priority(PR.ROUTINE_ACCOUNT,
                         {"wallet_investigation": True},
                         PRIORITY_CARD) == "P2")
check("P2: conflicting-decoder verification",
      PR.helius_priority(PR.INDEPENDENT_VERIFICATION,
                         {"conflicting": True}, PRIORITY_CARD) == "P2")
check("P3: background routine (ordinary balances)",
      PR.helius_priority(PR.ROUTINE_ACCOUNT, {},
                         PRIORITY_BACKGROUND) == "P3")
check("P3: historical range Chainstack can handle (recent)",
      PR.helius_priority(PR.HISTORICAL_RANGE, {"recent": True},
                         PRIORITY_CARD) == "P3")
check("P2: historical range, no recent evidence (archive leg)",
      PR.helius_priority(PR.HISTORICAL_RANGE, {},
                         PRIORITY_CARD) == "P2")

# ---------------- routing rules ----------------
r = router()
d = r.route("getBalance", priority=PRIORITY_CARD)
check("routine account, end state -> Alchemy primary",
      d["primary"] == "alchemy"
      and d["fallbacks"] == ["chainstack", "public"])
d = r.route("getTransaction", priority=PRIORITY_CARD)
check("routine tx, end state -> Alchemy, public fallback "
      "(Chainstack only when recent)",
      d["primary"] == "alchemy" and d["fallbacks"] == ["public"])
d = r.route("getTransaction", priority=PRIORITY_CARD,
            hints={"recent": True})
check("routine tx, recent -> Chainstack in the chain",
      d["candidates"] == ["alchemy", "chainstack", "public"])
d = r.route("getSignaturesForAddress", priority=PRIORITY_CARD)
check("historical range, sigs method -> Alchemy (Chainstack "
      "cannot serve it), public fallback",
      d["candidates"] == ["alchemy", "public"])
d = r.route("getTransaction", priority=PRIORITY_CARD,
            hints={"workload": PR.HISTORICAL_RANGE, "recent": True})
check("historical range, recent tx -> Chainstack first",
      d["primary"] == "chainstack")
d = r.route("getTransaction", priority=PRIORITY_CARD,
            hints={"workload": PR.HISTORICAL_RANGE,
                   "target_slot": 100, "latest_slot": 472_499_391})
check("historical range, old tx, no measured window -> Alchemy "
      "(coverage never assumed)",
      d["primary"] == "alchemy")
d = r.route("getTransaction", priority=PRIORITY_CARD,
            hints={"workload": PR.MISSED_EVENT_RECOVERY,
                   "recent": True})
check("missed event recovery, recent -> Chainstack first",
      d["primary"] == "chainstack")
d = r.route("getBalance", priority=PRIORITY_CARD,
            hints={"workload": PR.INDEPENDENT_VERIFICATION})
check("independent verification -> Chainstack first",
      d["primary"] == "chainstack"
      and d["candidates"] == ["chainstack", "alchemy", "public"])
d = r.route("getTransaction", priority=PRIORITY_CARD,
            hints={"workload": PR.INDEPENDENT_VERIFICATION})
check("tx verification: getTransaction + independent_verification "
      "-> Chainstack first, not refused (router fix 2026-11-22)",
      d["primary"] == "chainstack" and not d["refused"]
      and "helius" not in d["candidates"])
r_live = router(helius_enabled=True)
d = r_live.route("getBalance", priority=PRIORITY_CARD)
check("shadow phase: Helius leads P2 routine work",
      d["primary"] == "helius")
d = r_live.route("getBalance", priority=PRIORITY_BACKGROUND)
check("shadow phase: P3 routine work -> Helius LAST (routed "
      "elsewhere)", d["candidates"][-1] == "helius"
      and d["primary"] == "alchemy")
d = r_live.route("getBalance", priority=PRIORITY_CARD,
                 hints={"workload": PR.INDEPENDENT_VERIFICATION})
check("verification never includes Helius (it is the subject, "
      "not the verifier)", "helius" not in d["candidates"])
d = r.route("getTransaction", priority=PRIORITY_CARD,
            hints={"workload": PR.COMPLEX_TX_PARSE})
check("complex tx parse, Helius disabled -> REFUSED, no fallback",
      d["refused"] and "No fallback" in d["refusal_reason"])
d = r_live.route("getTransaction", priority=PRIORITY_CARD,
                 hints={"workload": PR.COMPLEX_TX_PARSE})
check("complex tx parse, Helius enabled -> Helius only",
      d["candidates"] == ["helius"] and not d["refused"])
s = r.route_stream(PR.PARSED_TX_STREAM, priority=PRIORITY_CRITICAL)
check("parsed tx stream, Helius disabled -> refused",
      s["refused"])
s = r_live.route_stream(PR.PARSED_TX_STREAM, priority=PRIORITY_CRITICAL)
check("parsed tx stream, Helius enabled -> helius, P0",
      s["provider"] == "helius" and s["helius_priority"] == "P0"
      and s["stream"] == "parsed_streams")
s = r_live.route_stream(PR.PREPROCESSED_OBSERVATION)
check("preprocessed observation ALWAYS refused (paid-gated), "
      "even with Helius enabled",
      s["refused"] and "PAID-GATED" in s["refusal_reason"])
s = r_live.route_stream(PR.COMPLEX_TX_PARSE)
check("complex parse stream route -> helius parsed_events",
      s["provider"] == "helius" and s["stream"] == "parsed_events")

# ---------------- failover_order compatibility ----------------
check("failover_order: sigs excludes chainstack",
      "chainstack" not in PR.failover_order("getSignaturesForAddress"))
check("failover_order: recent reads include chainstack",
      "chainstack" in PR.failover_order("getBalance"))
check("failover_order: no-helius end state",
      PR.failover_order("getBalance", helius_enabled=False)
      == ["alchemy", "chainstack", "public"])

# ---------------- call(): happy path + usage recording ----------------
home_j = tempfile.mkdtemp(prefix="acq_router_journal_")
t = HostTransport({"alchemy": [ok(777)], "chainstack": [ok(888)],
                   "public": [ok(999)]})
r = router(home=home_j, transport=t)
res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
check("call routine -> alchemy serves, ok, metadata attached",
      res["kind"] == "ok" and res["result"] == 777
      and res["provider"] == "alchemy"
      and res["workload"] == PR.ROUTINE_ACCOUNT
      and res["account"] == "alchemy"
      and res["fallback"] is False
      and res["fallback_difference"] is None
      and res["cached"] is False)
journal = open(os.path.join(home_j,
                            "acquisition_provider_usage.jsonl")).read()
lines = [json.loads(x) for x in journal.strip().split("\n") if x]
check("usage journal records which account serviced the request",
      len(lines) == 1 and lines[0]["account"] == "alchemy"
      and lines[0]["method"] == "getBalance"
      and lines[0]["workload"] == PR.ROUTINE_ACCOUNT
      and lines[0]["cost"] == 10
      and lines[0]["kind"] == "ok")

# ---------------- call(): failover + fallback difference ----------------
t2 = HostTransport({"alchemy": [err429()], "chainstack": [ok({"value": 7})],
                    "public": [ok(999)]})
r2 = router(transport=t2)
res2 = r2.call("getBalance", ["addr1"], priority=PRIORITY_CRITICAL)
check("failover: alchemy 429 -> chainstack answers ok",
      res2["kind"] == "ok" and res2["result"] == {"value": 7}
      and res2["provider"] == "chainstack")
check("failover tried alchemy first then chainstack",
      len(t2.calls) == 2 and "alchemy" in t2.calls[0]
      and "chainstack" in t2.calls[1])
check("fallback difference recorded (Chainstack recent-only)",
      res2["fallback"] is True
      and res2["fallback_difference"] is not None
      and "Chainstack" in res2["fallback_difference"])
check("attempts list records both providers",
      [a["provider"] for a in res2["attempts"]]
      == ["alchemy", "chainstack"])

# ---------------- capability refusal: Chainstack never sent sigs ----------------
t3 = HostTransport({"alchemy": [ok([{"signature": "s1"}])],
                    "chainstack": [ok([{"signature": "BAD"}])],
                    "public": [ok([])]})
r3 = router(transport=t3)
res3 = r3.call("getSignaturesForAddress", ["addr1"],
               priority=PRIORITY_CARD)
check("getSignaturesForAddress served by Alchemy, Chainstack "
      "never called", res3["kind"] == "ok"
      and res3["provider"] == "alchemy"
      and t3.by_host.get("chainstack", 0) == 0)

# ---------------- preprocessed via call(): refused, zero HTTP calls ----------------
t4 = HostTransport({"alchemy": [ok(1)], "chainstack": [ok(1)],
                    "public": [ok(1)]})
r4 = router(helius_enabled=True, transport=t4)
res4 = r4.call("getTransaction", ["sig"], priority=PRIORITY_CRITICAL,
               hints={"workload": PR.PREPROCESSED_OBSERVATION})
check("preprocessed via call() -> unsupported, zero HTTP calls",
      res4["kind"] == "unsupported" and len(t4.calls) == 0
      and "PAID-GATED" in (res4.get("note") or ""))

# ---------------- circuit breaker ----------------
br = PR.CircuitBreaker(failure_threshold=3, cooldown_s=60.0,
                        now=lambda: 1000.0)
for _ in range(3):
    br.record("alchemy", {"kind": "error"})
check("breaker opens after threshold consecutive errors",
      br.status("alchemy")["state"] == "open"
      and not br.allow("alchemy"))
br2 = PR.CircuitBreaker(failure_threshold=3, cooldown_s=60.0,
                         now=lambda: 1000.0)
for _ in range(3):
    br2.record("alchemy", {"kind": "rate_limited"})
check("429s do NOT trip the breaker (scheduler owns 429s)",
      br2.status("alchemy")["state"] == "closed")
t5 = HostTransport({"alchemy": [("raise", TimeoutError("t"))] * 6,
                    "chainstack": [ok(5)], "public": [ok(6)]})
r5 = router(transport=t5)
r5.breaker.failure_threshold = 3
for _ in range(3):
    r5.call("getBalance", ["a"], priority=PRIORITY_CARD)
res5 = r5.call("getBalance", ["a"], priority=PRIORITY_CARD)
check("router skips breaker-open provider, serves via fallback",
      res5["kind"] == "ok" and res5["provider"] == "chainstack"
      and r5.breaker.status("alchemy")["state"] == "open")

# ---------------- response cache ----------------
tx_fx = load_fixture("live_getTransaction_pump")
sig = tx_fx["params"][0]
t6 = HostTransport({"alchemy": [ok(tx_fx["result"])],
                    "chainstack": [ok(0)], "public": [ok(0)]})
r6 = router(transport=t6)
res6a = r6.call("getTransaction", tx_fx["params"],
                priority=PRIORITY_CARD)
res6b = r6.call("getTransaction", tx_fx["params"],
                priority=PRIORITY_CARD)
check("cache: second identical getTransaction served from cache, "
      "one HTTP call", res6a["kind"] == "ok"
      and res6b["kind"] == "ok" and res6b["cached"] is True
      and t6.by_host.get("alchemy", 0) == 1)
check("cache: cached result matches the recorded fixture",
      res6b["result"]["slot"] == tx_fx["result"]["slot"]
      and res6b["result"]["transaction"]["signatures"][0] == sig)

# Null results are never cached (2026-11-22 audit fix): a
# getTransaction for a not-yet-indexed signature returns
# {"result": null}, which the adapter classifies as kind "ok";
# caching it would serve the stale null until the TTL lapsed (the
# Alchemy null-transaction caching defect, bounded form).
t6n = HostTransport({"alchemy": [ok(None), ok(None)],
                     "chainstack": [ok(0)], "public": [ok(0)]})
r6n = router(transport=t6n)
nul1 = r6n.call("getTransaction", ["NotYetIndexedSig111"],
                priority=PRIORITY_CARD)
nul2 = r6n.call("getTransaction", ["NotYetIndexedSig111"],
                priority=PRIORITY_CARD)
check("cache: null getTransaction result is never cached",
      nul1["kind"] == "ok" and nul1.get("result") is None
      and nul2["kind"] == "ok" and nul2.get("cached") is not True
      and t6n.by_host.get("alchemy", 0) == 2)
check("cache: getSlot is never cached",
      PR.CACHE_TTL_S.get("getSlot") is None)
slot_fx = load_fixture("live_getSlot")
acct_fx = load_fixture("live_getAccountInfo_wsol")
t7 = HostTransport({"alchemy": [ok(slot_fx["result"])],
                    "chainstack": [ok(0)], "public": [ok(0)]})
r7 = router(transport=t7)
res7 = r7.call("getSlot", [], priority=PRIORITY_CARD)
check("fixture replay: recorded getSlot result returned verbatim",
      res7["kind"] == "ok" and res7["result"] == slot_fx["result"])
t8 = HostTransport({"alchemy": [ok(acct_fx["result"])],
                    "chainstack": [ok(0)], "public": [ok(0)]})
r8 = router(transport=t8)
res8 = r8.call("getAccountInfo", acct_fx["params"],
               priority=PRIORITY_CARD)
check("fixture replay: recorded wSOL account info returned, "
      "decimals 9", res8["kind"] == "ok"
      and res8["result"]["value"]["data"]["parsed"]["info"]["decimals"]
      == 9)

# ---------------- budgets: stop nonessential, never essential ----------------
home_b = tempfile.mkdtemp(prefix="acq_router_budget_")
store_b = UsageStore(home=home_b)
# Spend 96% of the Alchemy 30M CU budget in one recorded batch.
store_b.record("alchemy", "alchemy", "getTransaction",
               PR.ROUTINE_TX, PRIORITY_CARD, cost=28_800_000, kind="ok")
trk_b = UsageTracker(home=home_b, store=store_b)
check("budget status reports 96% spent, enforced",
      abs(trk_b.budget_status("alchemy")["pct"] - 0.96) < 0.001)
check("warn pending at 96%",
      trk_b.warn_pending("alchemy") is not None)
allowed, reason = trk_b.nonessential_allowed("alchemy",
                                             PRIORITY_BACKGROUND)
check("nonessential stopped at 95% threshold",
      allowed is False and reason == "budget_stop_nonessential")
allowed_c, _ = trk_b.nonessential_allowed("alchemy", PRIORITY_CRITICAL)
check("essential (critical) NEVER stopped by the budget manager",
      allowed_c is True)
t9 = HostTransport({"alchemy": [ok(111)], "chainstack": [ok(222)],
                    "public": [ok(333)]})
r9 = PR.ProviderRouter(home=home_b, helius_enabled=False,
                       transport=t9, usage_tracker=trk_b)
res9 = r9.call("getBalance", ["a"], priority=PRIORITY_BACKGROUND)
check("router: background call skips budget-stopped Alchemy, "
      "serves via Chainstack with the difference recorded",
      res9["kind"] == "ok" and res9["provider"] == "chainstack"
      and res9["fallback_difference"] is not None)
res9c = r9.call("getBalance", ["a"], priority=PRIORITY_CRITICAL)
check("router: critical call still serves via Alchemy at 96%",
      res9c["kind"] == "ok" and res9c["provider"] == "alchemy")

# ---------------- account disablement ----------------
home_d = tempfile.mkdtemp(prefix="acq_router_disable_")
trk_d = UsageTracker(home=home_d)
trk_d.disable_account("chainstack", "test disablement")
check("disabled account is reported disabled",
      trk_d.account_disabled("chainstack"))
t10 = HostTransport({"alchemy": [ok(11)], "chainstack": [ok(22)],
                     "public": [ok(33)]})
r10 = PR.ProviderRouter(home=home_d, helius_enabled=False,
                        transport=t10, usage_tracker=trk_d)
res10 = r10.call("getBalance", ["a"], priority=PRIORITY_CARD)
check("router skips a disabled account",
      res10["kind"] == "ok" and res10["provider"] == "alchemy"
      and t10.by_host.get("chainstack", 0) == 0)
trk_d.enable_account("chainstack")
check("re-enable restores the account",
      not trk_d.account_disabled("chainstack"))
trk_d.record_monthly_quota("chainstack")
check("monthly-quota 429 disables the account (throughput 429s "
      "never do)", trk_d.account_disabled("chainstack"))

# ---------------- no rotation / single account per provider ----------------
check("router bills Helius to helius_main only (no spreading)",
      PR.ProviderRouter(home=HOME, helius_enabled=True)
      ._account("helius") == "helius_main")
no_disc = True
for workload in PR.WORKLOADS:
    for prio in (PRIORITY_CRITICAL, PRIORITY_CARD,
                 PRIORITY_BACKGROUND):
        order = PR.candidate_order(workload, "getBalance", {},
                                   True, PR.helius_priority(workload, {},
                                                            prio))
        if "helius_discovery" in order or "helius_vault" in order:
            no_disc = False
check("helius_discovery / helius_vault never appear in any "
      "candidate order", no_disc)

# ---------------- provider_manager delegates to the router ----------------
check("provider_manager.failover_order IS the router's function "
      "(one routing owner)", PM.failover_order is PR.failover_order)
mgr = PM.ProviderManager(home=tempfile.mkdtemp(prefix="acq_mgr_"),
                         helius_enabled=False,
                         transport=HostTransport(
                             {"alchemy": [ok(42)],
                              "chainstack": [ok(43)],
                              "public": [ok(44)]}))
sel = mgr.select("getBalance")
check("manager.select delegates to the router order",
      sel == ("alchemy", ["chainstack", "public"]))
resm = mgr.call("getBalance", ["a"], priority=PRIORITY_CARD)
check("manager.call via router serves + records stats",
      resm["kind"] == "ok" and resm["result"] == 42
      and mgr.stats["alchemy"].calls == 1)
check("manager has no hardcoded order of its own (ORDER_* are "
      "the router's)", PM.ORDER_DEFAULT is PR.ORDER_DEFAULT
      and PM.ORDER_RECENT is PR.ORDER_RECENT)

# ---------------- shadow-phase Helius call path (stubbed transport) ----------------
# Helius adapter needs reserves; with helius_enabled=True and no
# stub it would import the live transport. Route-only checks above
# already cover the Helius-first order; the live Helius call is
# covered by test_intelligence_router_live_helius.py.

del os.environ["ALCHEMY_API_KEY"]
del os.environ["CHAINSTACK_ENDPOINT"]

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
