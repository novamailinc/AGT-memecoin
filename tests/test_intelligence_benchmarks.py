#!/usr/bin/env python3
"""Phase 10 tests — benchmarks (daemon/intelligence/acquisition/benchmark.py).

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_benchmarks.py

Covers:
- percentile / summary math on known samples (exact values, not
  self-consistent tautologies);
- stage_latencies(): stages kept separate (preprocessed vs processed
  vs confirmed vs finalized), missing stage -> None, never an
  estimate borrowed from another stage;
- every benchmark result carries a provenance label, and no result
  claims provider latency on mocked transport;
- router route/call/burst benchmarks on mocked transport: correct
  workloads, refusal paths measured as decisions, scheduler burst
  behaviour counted (never folded into latency);
- adapter benchmarks on mocked transport for alchemy / chainstack /
  public; the Helius adapter is REFUSED by benchmark_adapter and
  reported UNMEASURED instead (it delegates to the live reserves
  transport — importing it is a dual-import hazard);
- event-pipeline benchmark on recorded fixtures: new events stored,
  duplicates counted, per-stage processing cost measured;
- router-cache benchmark: cold miss, warm hits, hit rate 1.0, and
  the ACTUAL shipped TTL (getTransaction 30s, not the 300s the
  spec brief states);
- WSS dispatch benchmarks on fake sockets (Alchemy slot dispatch,
  Helius parsed-stream dispatch + parse-only);
- backfill driver benchmark on mocked transport (primitives +
  router-mediated) and the scheduler-cap observation;
- run_phase10_suite(): full report is JSON-serialisable, every
  table carries provenance, the unmeasured list is present, and
  only fake hosts were ever touched (no live host).

No live provider calls anywhere in this file.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.acquisition import benchmark as B
from daemon.intelligence.acquisition import provider_router as PR
from daemon.intelligence.acquisition.providers import make_adapter
from daemon.intelligence.events import EventProcessor, EventStore

FAILS = []
PASSES = [0]
REPO = os.path.join(os.path.dirname(__file__), "..")

os.environ.setdefault("ALCHEMY_API_KEY", "testkey-not-real")
os.environ.setdefault(
    "CHAINSTACK_ENDPOINT", "https://chainstack.example.invalid/abc")


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


def load_fixture(name):
    with open(os.path.join(REPO, "tests", "data", "intelligence",
                           "acquisition", name + ".json")) as fh:
        return json.load(fh)["result"]


def fresh_router(helius_enabled=False, transport=None):
    home = tempfile.mkdtemp(prefix="bench_test_router_")
    return PR.ProviderRouter(home=home, helius_enabled=helius_enabled,
                             transport=transport)


FIXTURES = {
    "getSlot": load_fixture("live_getSlot"),
    "getAccountInfo": load_fixture("live_getAccountInfo_wsol"),
    "getTransaction": load_fixture("live_getTransaction_pump"),
    "getSignaturesForAddress": load_fixture(
        "live_getSignaturesForAddress_pump"),
}
TRANSPORT = B.MethodFixtureTransport(FIXTURES)
SIG = ("eY5SFmn2qGBCbwqWUQEKJJr6N7VevCWvKTKqkCaC6RBbuSqYTQtfK9djE8"
       "testbench")
TX_PARAMS = [SIG, {"encoding": "jsonParsed",
                   "maxSupportedTransactionVersion": 1}]

# --------------------------------------------------------------------------
# percentile / summary math on known samples
# --------------------------------------------------------------------------
# _percentile: idx = round(pct/100 * (n-1)) over sorted values.
# 1..100: p50 -> ordered[round(49.5)=50] = 51; p95 -> ordered[94] = 95;
# p99 -> ordered[98] = 99; median = 50.5.
check("percentile: p50 of 1..100 is 51",
      B._percentile(list(range(1, 101)), 50) == 51)
check("percentile: p95 of 1..100 is 95",
      B._percentile(list(range(1, 101)), 95) == 95)
check("percentile: p99 of 1..100 is 99",
      B._percentile(list(range(1, 101)), 99) == 99)
check("percentile: empty -> None", B._percentile([], 50) is None)
check("percentile: single value returns it",
      B._percentile([7.5], 99) == 7.5)

summary = B.summarise_samples([1.0, 2.0, 3.0, 4.0])
check("summarise: n/mean/min/max exact",
      summary["n"] == 4 and summary["mean_s"] == 2.5
      and summary["min_s"] == 1.0 and summary["max_s"] == 4.0)
check("summarise: p50/p95/p99 exact on [1,2,3,4]",
      summary["p50_s"] == 3.0 and summary["p95_s"] == 4.0
      and summary["p99_s"] == 4.0)
empty = B.summarise_samples([])
check("summarise: empty -> n=0, None fields (never a fake zero)",
      empty["n"] == 0 and empty["p50_s"] is None
      and empty["mean_s"] is None)
check("summarise_latencies (Phase 2) still works",
      B.summarise_latencies([0.1, 0.2])["n"] == 2)

# --------------------------------------------------------------------------
# stage_latencies(): stages separated, missing stage -> None
# --------------------------------------------------------------------------
staged = B.stage_latencies([
    {"stage": "preprocessed", "observed_at": 1000.0},
    {"lifecycle": "processed", "observed_at": 1000.4},
    {"commitment": "confirmed", "observed_at": 1001.0},
    {"lifecycle_status": "finalized", "observed_at": 1002.5},
])
check("stage_latencies: each transition separate and exact",
      staged["preprocessed_to_processed_s"] == 0.4
      and staged["processed_to_confirmed_s"] == 0.6
      and staged["confirmed_to_finalized_s"] == 1.5)
check("stage_latencies: total is earliest->latest present stage",
      staged["total_s"] == 2.5)
check("stage_latencies: stages_present in canonical order",
      staged["stages_present"] == ["preprocessed", "processed",
                                    "confirmed", "finalized"])
check("stage_latencies: 'preliminary' aliases to preprocessed",
      B.stage_latencies([
          {"stage": "preliminary", "observed_at": 10.0},
          {"stage": "processed", "observed_at": 10.25},
      ])["preprocessed_to_processed_s"] == 0.25)
partial = B.stage_latencies([
    {"stage": "processed", "observed_at": 100.0},
    {"stage": "finalized", "observed_at": 103.0},
])
check("stage_latencies: missing stages -> None, never estimated",
      partial["processed_to_confirmed_s"] is None
      and partial["confirmed_to_finalized_s"] is None
      and partial["preprocessed_to_processed_s"] is None)
check("stage_latencies: total spans only present stages",
      partial["total_s"] == 3.0)
check("stage_latencies: one stage only -> total None",
      B.stage_latencies([{"stage": "confirmed",
                          "observed_at": 5.0}])["total_s"] is None)
check("stage_latencies: empty -> all None, stages_present empty",
      B.stage_latencies([])["stages_present"] == []
      and B.stage_latencies([])["total_s"] is None)
check("stage_latencies: earliest observed_at per stage wins",
      B.stage_latencies([
          {"stage": "processed", "observed_at": 50.0},
          {"stage": "processed", "observed_at": 40.0},
          {"stage": "confirmed", "observed_at": 41.0},
      ])["processed_to_confirmed_s"] == 1.0)
check("stage_latencies: non-numeric observed_at ignored",
      B.stage_latencies([
          {"stage": "processed", "observed_at": "junk"},
          {"stage": "confirmed", "observed_at": 9.0},
      ])["processed_to_confirmed_s"] is None)

# --------------------------------------------------------------------------
# router benchmarks on mocked transport
# --------------------------------------------------------------------------
router = fresh_router(transport=TRANSPORT)
route = B.benchmark_router_route(router, "getBalance", [], calls=50)
check("router_route: p50/p95/p99 present, n == calls",
      route["n"] == 50 and route["p50_s"] is not None
      and route["p95_s"] is not None and route["p99_s"] is not None)
check("router_route: workload routine_account, provenance is overhead",
      route["workload"] == PR.ROUTINE_ACCOUNT
      and route["provenance"] == B.PROVENANCE_ROUTER_OVERHEAD)
check("router_route: provenance never claims provider latency",
      "NOT provider latency" in route["provenance"])

call = B.benchmark_router_call(
    lambda: fresh_router(transport=TRANSPORT),
    "getTransaction", TX_PARAMS, calls=10)
check("router_call: all ok on mocked transport, n == ok",
      call["ok"] == 10 and call["n"] == 10)
check("router_call: workload routine_tx",
      call["workload"] == PR.ROUTINE_TX)
check("router_call: provenance is router overhead",
      call["provenance"] == B.PROVENANCE_ROUTER_OVERHEAD)

burst = B.benchmark_router_burst(
    fresh_router(transport=TRANSPORT), "getSlot", [], calls=30)
check("router_burst: 30 rapid getSlot calls, ok + rate_limited == 30",
      burst["ok"] + burst["rate_limited"] == 30
      and burst["ok"] > 0 and burst["rate_limited"] > 0)
check("router_burst: bucket drains (burst 10 + refill), not all ok",
      burst["ok"] < 30)
check("router_burst: no latency stats on refused calls",
      "p50_s" not in burst)

refused_route = B.benchmark_router_route(
    fresh_router(transport=TRANSPORT), "getTransaction", ["sig"],
    calls=20, hints={"workload": PR.PREPROCESSED_OBSERVATION})
check("router_route: preprocessed refused on every call",
      refused_route["refused"] == 20
      and refused_route["workload"] == PR.PREPROCESSED_OBSERVATION)
refused_call = B.benchmark_router_call(
    lambda: fresh_router(transport=TRANSPORT),
    "getTransaction", ["sig"], calls=5,
    hints={"workload": PR.PREPROCESSED_OBSERVATION})
check("router_call: preprocessed refusal is a decision, 0 ok, "
      "0 transport calls for it",
      refused_call["ok"] == 0 and refused_call["n"] == 0
      and refused_call["outcome_counts"].get("unsupported") == 5)
complex_call = B.benchmark_router_call(
    lambda: fresh_router(transport=TRANSPORT),
    "getTransaction", ["sig"], calls=5,
    hints={"workload": PR.COMPLEX_TX_PARSE})
check("router_call: complex-parse refused with Helius disabled",
      complex_call["ok"] == 0
      and complex_call["outcome_counts"].get("unsupported") == 5)

# --------------------------------------------------------------------------
# adapter benchmarks on mocked transport; Helius refused, not called
# --------------------------------------------------------------------------
for provider in ("alchemy", "chainstack", "public"):
    adapter = make_adapter(provider, transport=TRANSPORT)
    res = B.benchmark_adapter(adapter, "getSlot", [], calls=20)
    check("adapter %s: all ok, provenance mocked transport"
          % provider,
          res["ok"] == 20 and res["n"] == 20
          and res["provenance"] == B.PROVENANCE_MOCKED_TRANSPORT
          and res["provider"] == provider)
    check("adapter %s: provenance never claims provider latency"
          % provider,
          "NOT provider latency" in res["provenance"])

helius_adapter = make_adapter("helius", transport=TRANSPORT)
try:
    B.benchmark_adapter(helius_adapter, "getSlot", [])
    check("adapter helius: benchmark_adapter refuses it", False)
except ValueError as exc:
    check("adapter helius: benchmark_adapter refuses it",
          "live reserves transport" in str(exc))
unm = B.unmeasured_helius_adapter()
check("helius adapter: UNMEASURED entry, no run executed",
      unm["measured"] is False
      and unm["provenance"] == B.PROVENANCE_UNMEASURED
      and "AGT" in unm["agt_procedure"])
check("unmeasured(): carries reason + AGT procedure",
      B.unmeasured("x", "r", "p")["reason"] == "r"
      and B.unmeasured("x", "r", "p")["agt_procedure"] == "p")

# --------------------------------------------------------------------------
# event pipeline benchmark on recorded fixtures
# --------------------------------------------------------------------------
with open(os.path.join(REPO, "tests", "data", "intelligence",
                       "atx_create.json")) as fh:
    tx_create = json.load(fh)
tx_create = (tx_create.get("transaction") or tx_create.get("rpc_result")
             or tx_create)
base_sig = tx_create["transaction"]["signatures"][0]
base_slot = tx_create["slot"]
home = tempfile.mkdtemp(prefix="bench_test_events_")
store = EventStore(os.path.join(home, "intelligence_events.db")).open()
processor = EventProcessor(store)
observations = []
for i in range(20):
    tx = json.loads(json.dumps(tx_create))
    tx["transaction"]["signatures"] = ["%s%04d" % (base_sig[:80], i)]
    tx["slot"] = base_slot + i
    observations.append({"provider": "alchemy", "kind": "getTransaction",
                         "tx": tx, "commitment": "confirmed",
                         "observed_at": 1795273900.0 + i})
pipe = B.benchmark_pipeline(processor.process_observation, observations)
check("pipeline: 20 new events, events/s > 0, provenance fixture",
      pipe["events"] == 20 and pipe["events_per_s"] > 0
      and pipe["provenance"] == B.PROVENANCE_FIXTURE)
check("pipeline: outcomes counted, no failures",
      pipe["outcomes"].get("failed", 0) == 0
      and sum(pipe["outcomes"].values()) == 20)
check("pipeline: p50/p95/p99 present",
      pipe["p50_s"] is not None and pipe["p99_s"] is not None)
dup = B.benchmark_pipeline(processor.process_observation,
                           observations[:10])
check("pipeline duplicates: same 10 events all duplicate",
      dup["outcomes"].get("duplicate") == 10
      and dup["events_per_s"] > 0)
check("pipeline duplicates: provenance still fixture",
      dup["provenance"] == B.PROVENANCE_FIXTURE)

# --------------------------------------------------------------------------
# router cache benchmark: cold miss, warm hits, actual TTL
# --------------------------------------------------------------------------
cache = B.benchmark_cache(
    lambda: fresh_router(transport=TRANSPORT), "getTransaction",
    [["cachebench%064d" % i,
      {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]
     for i in range(5)], repeats=3)
check("cache: warm hit rate 1.0, cold had no unexpected hits",
      cache["warm_hit_rate"] == 1.0
      and cache["cold_unexpected_hits"] == 0
      and cache["warm_hits"] == 15)
check("cache: cold and warm latency stats both present",
      cache["cold"]["n"] == 5 and cache["warm"]["n"] == 15
      and cache["cold"]["p50_s"] is not None
      and cache["warm"]["p50_s"] is not None)
check("cache: reports the ACTUAL shipped TTL (30s, not 300s)",
      cache["cache_ttl_s"]["getTransaction"] == 30.0)
check("cache: provenance is mocked transport",
      cache["provenance"] == B.PROVENANCE_MOCKED_TRANSPORT)

# --------------------------------------------------------------------------
# WSS dispatch benchmarks on fake sockets
# --------------------------------------------------------------------------
from daemon.intelligence.acquisition.websocket_manager import (
    AlchemySubscriptions)
from daemon.intelligence.acquisition import helius_streams as HS


class _FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        pass

    def run_forever(self):
        return None


def _fake_factory(url, on_message, on_error, on_close, on_open):
    ws = _FakeWS()
    _fake_factory.handlers = (on_message, on_error, on_close, on_open)
    return ws


received = []
subs = AlchemySubscriptions(url="wss://fake.invalid",
                            ws_factory=_fake_factory,
                            on_event=lambda p, d: received.append(d))
subs.subscribe_slot()
subs.connect()
slot_msg = json.dumps(
    {"jsonrpc": "2.0", "method": "slotNotification",
     "params": {"subscription": 1, "result": {"slot": 472499400}}})
wss = B.benchmark_wss_dispatch(
    lambda m: subs.manager._on_message(None, m), [slot_msg], repeat=50)
check("wss alchemy: 50 slot notifications dispatched, all received",
      wss["messages"] == 50 and len(received) == 50
      and wss["messages_per_s"] > 0)
check("wss alchemy: provenance is fake socket",
      wss["provenance"] == B.PROVENANCE_FAKE_SOCKET)

fix_tx = FIXTURES["getTransaction"]
from daemon.intelligence.decoders import solana_transaction as ST
own = ST.decode_transaction(fix_tx)
own_programs = sorted(p for p in own["program_ids"] if p)
notif = {
    "jsonrpc": "2.0", "method": "parsedTransactionNotification",
    "params": {"subscription": 7, "result": {"value": {
        "signature": fix_tx["transaction"]["signatures"][0],
        "slot": fix_tx["slot"], "status": "error",
        "error": {"InstructionError": [6, {"Custom": 6013}]},
        "fee": fix_tx["meta"]["fee"],
        "feePayer": own["account_keys"][0],
        "accountKeys": own["account_keys"],
        "instructions": [
            {"instructionIndex": i, "innerInstructionIndex": None,
             "stackHeight": 1, "programId": prog,
             "decoded": ({"name": "route",
                          "args": {"in_amount": "1000000"},
                          "accounts": {}}
                         if prog == HS.JUPITER_PROGRAM_ID else
                         {"name": "transfer", "args": {},
                          "accounts": {}}),
             "summary": ({"type": "swap", "description": "bench",
                          "parsedData": {"protocol": "jupiter"}}
                         if prog == HS.JUPITER_PROGRAM_ID else None)}
            for i, prog in enumerate(own_programs)],
        "matchedIndexes": [own_programs.index(HS.JUPITER_PROGRAM_ID)],
        "summary": {"type": "swap", "description": "bench",
                    "parsedData": {"protocol": "jupiter"}},
        "blockTime": None}}}}
helius_events = []
hrouter = fresh_router(helius_enabled=True, transport=TRANSPORT)
stream = HS.HeliusParsedStreamClient(
    wss_url="wss://fake.invalid/?api-key=testkey-not-real",
    router=hrouter, ws_factory=_fake_factory,
    on_event=helius_events.append)
ok, _note = stream.subscribe(HS.pumpfun_filter(), "pumpfun")
check("wss helius: pumpfun subscribe accepted (router P1)",
      ok is True)
hwss = B.benchmark_wss_dispatch(
    lambda m: stream._on_message(None, json.dumps(m)), [notif],
    repeat=50)
check("wss helius: 50 parsed notifications dispatched, all received",
      hwss["messages"] == 50 and len(helius_events) == 50
      and hwss["messages_per_s"] > 0)
parse_only = B.benchmark_wss_dispatch(
    lambda m: HS.parse_notification(m), [notif], repeat=50)
check("wss helius parse-only: 50 notifications parsed",
      parse_only["messages"] == 50
      and parse_only["messages_per_s"] > 0)
check("wss helius parse-only: per-message cost is sub-millisecond",
      parse_only["p50_s"] is not None and parse_only["p50_s"] < 0.005)

# --------------------------------------------------------------------------
# backfill driver benchmark on mocked transport
# --------------------------------------------------------------------------
from daemon.intelligence.acquisition.event_backfill import (
    BackfillDriver, DedupEventSink, fetch_transactions_for_signatures,
    recover_signature_range)
from daemon.intelligence.acquisition.chainstack_workload import (
    measure_backfill_performance)

bf_transport = B.MethodFixtureTransport(FIXTURES, sig_page_size=50)
ADDRESS = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
GAP = {"from_slot": 472499400, "to_slot": 472499532}


def _direct_rpc(method, params):
    if method == "getSignaturesForAddress":
        return {"kind": "ok", "result": bf_transport._sig_page()}
    return {"kind": "ok", "result": bf_transport.synthetic_transaction(params[0])}


def _recover_primitives_once():
    sink = DedupEventSink()
    rows, pages, _s = recover_signature_range(
        ADDRESS, _direct_rpc, None, seen=sink.seen, max_pages=4)
    events, _t = fetch_transactions_for_signatures(rows, _direct_rpc)
    new = 0
    for ev in events:
        if sink.add(ev):
            new += 1
    return {"events_new": new, "signatures_found": len(rows),
            "pages": pages}


bf = B.benchmark_backfill_driver(_recover_primitives_once, runs=3)
check("backfill primitives: 50 events/run, rate > 0, provenance mocked",
      bf["events_new_per_run"] == [50, 50, 50]
      and bf["events_per_s"]["p50_s"] is not None
      and bf["events_per_s"]["p50_s"] > 0
      and bf["provenance"] == B.PROVENANCE_MOCKED_TRANSPORT)
check("backfill primitives: measure_backfill_performance labels mocked",
      "MOCKED TRANSPORT" in
      measure_backfill_performance(_recover_primitives_once)["timing"])


def _recover_router_once():
    router = fresh_router(transport=TRANSPORT)
    driver = BackfillDriver(
        router, home=tempfile.mkdtemp(prefix="bench_test_bf_"))
    report = driver.recover_gap("bench", ADDRESS, GAP)
    assert len(report['fetch_failed']) == 1 and report['unrecovered_gap'] is not None
    return report


bf_router = B.benchmark_backfill_driver(_recover_router_once, runs=3)
check("backfill router-mediated: one bound event, unrelated recorded row refused",
      bf_router["events_new_per_run"] == [1, 1, 1]
      and bf_router["events_per_s"]["p50_s"] > 0)

# --------------------------------------------------------------------------
# full suite smoke: JSON-serialisable, provenance everywhere, fake hosts
# --------------------------------------------------------------------------
report = B.run_phase10_suite(
    REPO, calls_route=20, calls_router=5, calls_adapter=10,
    pipeline_events=20, wss_messages=20, backfill_runs=2)
try:
    json.dumps(report, default=str)
    serialisable = True
except (TypeError, ValueError):
    serialisable = False
check("suite: full report is JSON-serialisable", serialisable)
check("suite: six unmeasured entries with AGT procedures",
      len(report["unmeasured"]) == 6
      and all("agt_procedure" in u and u["agt_procedure"]
              for u in report["unmeasured"]))
check("suite: only fake hosts ever touched (no live host)",
      set(report["transport_hosts_touched"]) <= {
          "solana-mainnet.g.alchemy.com",
          "chainstack.example.invalid",
          "api.mainnet-beta.solana.com"})
check("suite: every table present",
      set(report["tables"]) == {"router_workloads", "adapters",
                                "event_pipeline", "backfill",
                                "router_cache", "wss",
                                "lifecycle_stages"})
check("suite: helius adapter entry is UNMEASURED",
      report["tables"]["adapters"]["helius"]["measured"] is False)
check("suite: lifecycle table separates stages (synthetic 0.4s)",
      report["tables"]["lifecycle_stages"]
      ["stage_latencies_synthetic"]["processed_to_confirmed_s"] == 0.4)
check("suite: scheduler-cap observation recorded with finding",
      report["tables"]["backfill"]["scheduler_cap_100row"]
      ["unrecovered_gap_recorded"] is True
      and "SCHEDULER-BOUND" in report["tables"]["backfill"]
      ["scheduler_cap_100row"]["finding"])

print("\n%d passed, %d failed" % (PASSES[0], len(FAILS)))
if FAILS:
    print("FAILURES:")
    for name in FAILS:
        print(" - " + name)
    sys.exit(1)
