#!/usr/bin/env python3
"""Phase 12 tests — failure matrix for the three-provider Solana
data infrastructure (spec section 15).

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_failure_matrix.py

NO live provider calls: every transport is a mocked opener keyed
by host, and every stream is a fake in-memory WebSocket. The REAL
router, workloads, event pipeline, backfill driver, cost manager
and usage tracker run underneath the mocks.

Covers:
  (a) provider-outage combinations — each provider down alone,
      each pair down, all three down (public-only degrade, fail
      closed, never invents data); Alchemy 401 / Chainstack 401;
      Helius 429 storm -> auto-quiet engagement and recovery.
  (b) WSS failures — parsed-stream disconnect + resume (last_slot
      + RPC backfill, no replay, no duplicate events applied),
      handshake-429 handling, subscription drop mid-stream.
  (c) event-pipeline correctness under failure — missing-event
      range never silently discarded (unrecovered gap recorded,
      checkpoint never advanced past it); duplicate delivery from
      two providers -> one logical record; conflicting
      multi-provider observations -> conflicting, held, market
      state untouched; corrupt event store -> CorruptStoreError,
      file preserved byte-for-byte; failed transaction ->
      transaction_failed, no trade events; preliminary
      (processed-stage) observation held, never updates market
      state.
  (d) cache/correctness — stale-cache TTL/bypass behaviour,
      failover records fallback_difference, usage journal records
      the workload actually served.
  (e) cost-management under failure — budget exhaustion suppresses
      background work but never essential critical/card reads;
      pre-call-check refusals are recorded.

Fixtures are the real on-chain transactions under
tests/data/intelligence/ (atx_create / atx_buy / atx_sell_failed /
notai_create). Helius parsed-stream notifications used here are
locally constructed from the documented sample shape (labelled
SYNTHETIC where built) — no live Helius frame has ever been
received from this VM.
"""
import copy
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import time
import urllib.error

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(REPO))

from daemon.intelligence.acquisition import provider_router as PR
from daemon.intelligence.acquisition import cost_management as CM
from daemon.intelligence.acquisition import helius_preprocessed as HPP
from daemon.intelligence.acquisition import helius_streams as HS
from daemon.intelligence.acquisition import helius_parsed_events as HPE
from daemon.intelligence.acquisition import usage_report as UR
from daemon.intelligence.acquisition.alchemy_workload import (
    AlchemyWorkload)
from daemon.intelligence.acquisition.chainstack_workload import (
    ChainstackWorkload, verify_transaction)
from daemon.intelligence.acquisition.event_backfill import (
    BackfillCheckpointStore, BackfillDriver, DedupEventSink)
from daemon.intelligence.acquisition.provider_health import HealthMonitor
from daemon.intelligence.acquisition.provider_usage import UsageStore
from daemon.intelligence.acquisition.request_scheduler import (
    PRIORITY_BACKGROUND, PRIORITY_CARD, PRIORITY_CRITICAL,
    PRIORITY_DEPLOYER, SharedQuota)
from daemon.intelligence.acquisition.usage_tracker import UsageTracker
from daemon.intelligence.events import (
    CheckpointTracker, EventProcessor, EventStore, RetryQueue,
    backfill_after_disconnect, reprocess_failed)
from daemon.intelligence.events.event_models import (
    EVENT_TYPE_TOKEN_CREATED, EVENT_TYPE_TRANSACTION_FAILED,
    VERIFICATION_CONFLICTING, VERIFICATION_FAILED,
    VERIFICATION_UNVERIFIED, VERIFICATION_VERIFIED)
from daemon.intelligence.events.event_store import CorruptStoreError

FAILS = []
PASSES = [0]
MATRIX = []  # (scenario, expected, observed, pass/fail)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


def matrix(scenario, expected, observed, cond):
    MATRIX.append((scenario, expected, observed,
                   "PASS" if cond else "FAIL"))
    check("matrix: " + scenario, cond)


DATA = os.path.join(os.path.dirname(__file__), "data", "intelligence")
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"


def load_fixture(name):
    with open(os.path.join(DATA, name)) as fh:
        wrap = json.load(fh)
    return wrap.get("transaction") or wrap.get("rpc_result") or wrap


def signature_of(tx):
    return tx["transaction"]["signatures"][0]


# ---------------------------------------------------------------------------
# Mock transport (same shape as the Phase 4 router tests)
# ---------------------------------------------------------------------------
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
    """Fake opener routing by host substring. ``behaviour`` maps a
    host substring to a list of (status, body) or ('raise', exc).
    The last item repeats, so a provider that stays down stays down."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []
        self.by_host = {}
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


def err(status, message, code=None):
    body = {"jsonrpc": "2.0", "id": 1,
            "error": {"code": code if code is not None else status,
                      "message": message}}
    return (status, body)


def err429(message="too many requests"):
    return err(429, message)


def err401(message="unauthorized"):
    return err(401, message)


def err500(message="internal error"):
    return err(500, message)


def router(home=None, transport=None, helius_enabled=False, now=None):
    if home is None:
        home = tempfile.mkdtemp(prefix="fm_router_")
    return PR.ProviderRouter(home=home, helius_enabled=helius_enabled,
                             transport=transport, now=now)


def journal_lines(home):
    path = os.path.join(home, "acquisition_provider_usage.jsonl")
    try:
        with open(path) as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except OSError:
        return []


def temp_store():
    tmpdir = tempfile.mkdtemp(prefix="fm_events_")
    return EventStore(os.path.join(tmpdir, "intelligence_events.db"))


def fresh_processor():
    store = temp_store().open()
    return EventProcessor(store), store


def obs(provider, tx, commitment="confirmed", kind="getTransaction"):
    return {"provider": provider, "kind": kind, "tx": tx,
            "commitment": commitment, "observed_at": 1795273900}


# ===========================================================================
# (a) Provider-outage combinations
# ===========================================================================
def test_outage_matrix():
    # -- each provider down alone -------------------------------------
    t = HostTransport({"alchemy": [err500()], "chainstack": [ok(888)],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("alchemy down alone",
           "chainstack serves ok, fallback_difference names Chainstack",
           "kind=%s provider=%s diff=%s" % (
               res["kind"], res.get("provider"),
               (res.get("fallback_difference") or "")[:60]),
           res["kind"] == "ok" and res["provider"] == "chainstack"
           and res["fallback"] is True
           and res["fallback_difference"] is not None
           and "Chainstack" in res["fallback_difference"]
           and [a["provider"] for a in res["attempts"]]
           == ["alchemy", "chainstack"])

    t = HostTransport({"alchemy": [ok(777)], "chainstack": [err500()],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("chainstack down alone",
           "alchemy (primary) serves, no fallback, chainstack never called",
           "kind=%s provider=%s chainstack_calls=%d" % (
               res["kind"], res.get("provider"),
               t.by_host.get("chainstack", 0)),
           res["kind"] == "ok" and res["provider"] == "alchemy"
           and res["fallback"] is False
           and t.by_host.get("chainstack", 0) == 0)

    t = HostTransport({"alchemy": [ok(777)], "chainstack": [ok(888)],
                       "mainnet-beta": [err500()]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("public down alone",
           "alchemy serves, public never attempted",
           "kind=%s provider=%s public_calls=%d" % (
               res["kind"], res.get("provider"),
               t.by_host.get("mainnet-beta", 0)),
           res["kind"] == "ok" and res["provider"] == "alchemy"
           and t.by_host.get("mainnet-beta", 0) == 0)

    # Helius down / 429-quiet is modelled as helius disabled: the
    # specialised Helius workloads refuse, routine work is unaffected.
    r_live = router(helius_enabled=True)
    d = r_live.route_stream(PR.PARSED_TX_STREAM,
                            priority=PRIORITY_CRITICAL)
    check("helius enabled: parsed stream routes to helius P0",
          d["provider"] == "helius" and d["helius_priority"] == "P0"
          and not d["refused"])
    r_quiet = router(helius_enabled=False)
    d = r_quiet.route_stream(PR.PARSED_TX_STREAM,
                             priority=PRIORITY_CRITICAL)
    matrix("helius down / 429-quiet (disabled)",
           "parsed stream REFUSED, no fallback; routine reads still served",
           "refused=%s reason=%s" % (d["refused"],
                                     (d.get("refusal_reason") or "")[:50]),
           d["refused"] and "No fallback" in d["refusal_reason"])
    d = r_quiet.route("getTransaction", priority=PRIORITY_CARD,
                      hints={"workload": PR.COMPLEX_TX_PARSE})
    check("helius quiet: complex tx parse REFUSED, no substitution",
          d["refused"] and "No fallback" in d["refusal_reason"])
    t = HostTransport({"alchemy": [ok(777)], "chainstack": [ok(888)],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t, helius_enabled=False)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    check("helius quiet: routine read still serves via alchemy",
          res["kind"] == "ok" and res["provider"] == "alchemy")

    # -- each pair down ------------------------------------------------
    t = HostTransport({"alchemy": [err500()], "chainstack": [err500()],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("alchemy + chainstack down",
           "public (tertiary) serves, fallback_difference names public",
           "kind=%s provider=%s diff=%s" % (
               res["kind"], res.get("provider"),
               (res.get("fallback_difference") or "")[:60]),
           res["kind"] == "ok" and res["provider"] == "public"
           and res["fallback"] is True
           and res["fallback_difference"] is not None
           and "public" in res["fallback_difference"]
           and [a["provider"] for a in res["attempts"]]
           == ["alchemy", "chainstack", "public"])

    t = HostTransport({"alchemy": [err500()],
                       "chainstack": [ok(888)],
                       "mainnet-beta": [err500()]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("alchemy + public down",
           "chainstack serves (public failure after it is irrelevant)",
           "kind=%s provider=%s" % (res["kind"], res.get("provider")),
           res["kind"] == "ok" and res["provider"] == "chainstack")

    t = HostTransport({"alchemy": [ok(777)], "chainstack": [err500()],
                       "mainnet-beta": [err500()]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("chainstack + public down",
           "alchemy serves, neither fallback reached",
           "kind=%s provider=%s" % (res["kind"], res.get("provider")),
           res["kind"] == "ok" and res["provider"] == "alchemy")

    # -- all three down: degrade to read-only/public, fail closed ----
    t = HostTransport({"alchemy": [err500()], "chainstack": [err500()],
                       "mainnet-beta": [err500()]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("all three down",
           "non-ok, result None (no invented data), all three attempted",
           "kind=%s result=%r attempts=%s" % (
               res["kind"], res.get("result"),
               [a["provider"] for a in res.get("attempts", [])]),
           res["kind"] != "ok" and res.get("result") is None
           and [a["provider"] for a in res["attempts"]]
           == ["alchemy", "chainstack", "public"])
    check("all down: health snapshot records the failures",
          r.health_snapshot()["alchemy"]["errors"] >= 1
          and r.health_snapshot()["chainstack"]["errors"] >= 1)
    check("all down: public-fallback-only alert fires when only "
          "public is configured",
          HealthMonitor().public_fallback_only(["public"]) is True
          and HealthMonitor().public_fallback_only(
              ["alchemy", "public"]) is False)

    # All down for a Helius-only workload: refused, never substituted.
    calls_before = len(t.calls)
    r = router(transport=t, helius_enabled=False)
    res = r.call("getTransaction", ["sig"], priority=PRIORITY_CRITICAL,
                 hints={"workload": PR.COMPLEX_TX_PARSE})
    matrix("all down + complex parse",
           "REFUSED (unsupported), zero new HTTP calls, no substitution",
           "kind=%s new_calls=%d" % (res["kind"],
                                     len(t.calls) - calls_before),
           res["kind"] == "unsupported"
           and len(t.calls) - calls_before == 0)

    # -- 401s (unconfigured / rejected key) ---------------------------
    t = HostTransport({"alchemy": [err401("invalid api key")],
                       "chainstack": [ok(888)],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("alchemy 401 (rejected key)",
           "401 treated as provider error, chainstack serves",
           "kind=%s provider=%s alchemy_attempt=%s" % (
               res["kind"], res.get("provider"),
               res["attempts"][0]["kind"]),
           res["kind"] == "ok" and res["provider"] == "chainstack"
           and res["attempts"][0]["kind"] == "error")
    check("alchemy 401: health records an error, not a 429",
          r.health_snapshot()["alchemy"]["errors"] >= 1
          and r.health_snapshot()["alchemy"]["rpc_429s"] == 0)

    t = HostTransport({"alchemy": [ok(777)],
                       "chainstack": [err401("unauthorized")],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD,
                 hints={"workload": PR.INDEPENDENT_VERIFICATION})
    matrix("chainstack 401 on verification workload",
           "chainstack 401 -> alchemy serves (archive superset), "
           "difference recorded",
           "kind=%s provider=%s diff=%s" % (
               res["kind"], res.get("provider"),
               (res.get("fallback_difference") or "")[:60]),
           res["kind"] == "ok" and res["provider"] == "alchemy"
           and res["fallback"] is True
           and res["fallback_difference"] is not None
           and "Chainstack unavailable" in res["fallback_difference"])

    # -- timeouts ------------------------------------------------------
    t = HostTransport({"alchemy": [("raise", TimeoutError("t"))],
                       "chainstack": [ok(888)],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("alchemy timeout",
           "timeout fails over to chainstack, breaker counts it",
           "kind=%s provider=%s alchemy_attempt=%s" % (
               res["kind"], res.get("provider"),
               res["attempts"][0]["kind"]),
           res["kind"] == "ok" and res["provider"] == "chainstack"
           and res["attempts"][0]["kind"] == "timeout")


def test_helius_429_storm():
    # Throughput 429 storm on the live leg's analogue (Alchemy here —
    # the Helius adapter itself is the live reserves transport and
    # is never called from this VM): 429s fail over, do NOT trip
    # the circuit breaker, and engage scheduler backoff.
    t = HostTransport({"alchemy": [err429()] * 3,
                       "chainstack": [ok(888)],
                       "mainnet-beta": [ok(999)]})
    home = tempfile.mkdtemp(prefix="fm_429_")
    r = router(home=home, transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("429 storm (throughput) on primary",
           "chainstack serves; breaker stays closed; scheduler backoff on",
           "kind=%s provider=%s breaker=%s backoff_remaining=%.0f" % (
               res["kind"], res.get("provider"),
               r.breaker.status("alchemy")["state"],
               SharedQuota("alchemy", home=home).status().get(
                   "backoff_remaining_s", 0)),
           res["kind"] == "ok" and res["provider"] == "chainstack"
           and r.breaker.status("alchemy")["state"] == "closed"
           and SharedQuota("alchemy", home=home).status().get(
               "backoff_remaining_s", 0) > 0)
    check("429 storm: health counts the 429 separately from errors",
          r.health_snapshot()["alchemy"]["rpc_429s"] >= 1)
    # Second call while backoff is active: alchemy is scheduler-
    # refused (no new HTTP call), chainstack still serves.
    before = t.by_host.get("alchemy", 0)
    res2 = r.call("getBalance", ["addr2"], priority=PRIORITY_CARD)
    matrix("429 storm: call during backoff",
           "alchemy not re-attempted (scheduler refusal), chainstack serves",
           "kind=%s provider=%s alchemy_calls=%d->%d attempts=%s" % (
               res2["kind"], res2.get("provider"), before,
               t.by_host.get("alchemy", 0),
               [a["kind"] for a in res2["attempts"]]),
           res2["kind"] == "ok" and res2["provider"] == "chainstack"
           and t.by_host.get("alchemy", 0) == before
           and any(a["kind"] == "rate_limited"
                   for a in res2["attempts"]))
    # Recovery: one success clears the backoff fleet-wide.
    q = SharedQuota("alchemy", home=home)
    q.report_success()
    check("429 storm: one success clears scheduler backoff",
          q.status().get("backoff_remaining_s", 0) == 0)
    # Direct scheduler proof (fresh bucket, fake clock).
    clock = [1000.0]
    home2 = tempfile.mkdtemp(prefix="fm_429b_")
    q2 = SharedQuota("alchemy", home=home2, now=lambda: clock[0])
    granted, _, _ = q2.acquire(PRIORITY_CARD)
    check("scheduler: token granted before any 429", granted is True)
    q2.report_429("throughput")
    granted_bg, _, reason_bg = q2.acquire(PRIORITY_BACKGROUND)
    granted_card, wait_card, reason_card = q2.acquire(PRIORITY_CARD)
    check("scheduler: background sheds during 429 backoff",
          granted_bg is False and reason_bg == "shed_backoff")
    check("scheduler: critical reports backoff wait, does not shed",
          granted_card is False and reason_card == "backoff"
          and wait_card > 0)
    clock[0] += 31.0
    granted, _, _ = q2.acquire(PRIORITY_CARD)
    check("scheduler: backoff expires, tokens flow again", granted is True)

    # Monthly-quota 429 is a DIFFERENT condition: the account is
    # disabled and never retried into the same provider.
    t = HostTransport({"alchemy": [err429("monthly quota exceeded")],
                       "chainstack": [ok(888)],
                       "mainnet-beta": [ok(999)]})
    home3 = tempfile.mkdtemp(prefix="fm_429c_")
    r = router(home=home3, transport=t)
    res = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("monthly-quota 429 on primary",
           "chainstack serves AND alchemy account is disabled afterwards",
           "kind=%s provider=%s alchemy_disabled=%s" % (
               res["kind"], res.get("provider"),
               r.usage.account_disabled("alchemy")),
           res["kind"] == "ok" and res["provider"] == "chainstack"
           and r.usage.account_disabled("alchemy") is True)
    before = t.by_host.get("alchemy", 0)
    res2 = r.call("getBalance", ["addr2"], priority=PRIORITY_CARD)
    check("monthly-quota 429: disabled account is never re-attempted",
          res2["kind"] == "ok" and res2["provider"] == "chainstack"
          and t.by_host.get("alchemy", 0) == before)
    check("monthly-quota 429: throughput 429s never disable an account",
          UsageTracker(home=tempfile.mkdtemp(prefix="fm_429d_"))
          .account_disabled("alchemy") is False)

    # WSS handshake 429s are counted SEPARATELY from RPC 429s.
    hm = HealthMonitor()
    hm.set_configured("helius", True)
    hm.record_wss_handshake_429("helius")
    hm.record_wss_handshake_429("helius")
    snap = hm.snapshot()["helius"]
    matrix("helius WSS handshake 429s",
           "2 handshake 429s, 0 RPC 429s, status degraded not down",
           "wss_429s=%d rpc_429s=%d status=%s" % (
               snap["wss_handshake_429s"], snap["rpc_429s"],
               snap["status"]),
           snap["wss_handshake_429s"] == 2 and snap["rpc_429s"] == 0
           and snap["status"] == "degraded")
    hm.record_wss_handshake_ok("helius")
    check("handshake ok records a success timestamp",
          hm.snapshot()["helius"]["last_success"] is not None)

    # Helius Parsed Events REST client: 429 -> rate_limited, no
    # credits spent, no calls counted.
    def raising_429(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 429, "err", {},
            io.BytesIO(json.dumps({"error": {"code": 429,
                                             "message": "slow down"}})
                       .encode()))
    client = HPE.HeliusParsedEventsClient(api_key="testkey-not-real",
                                          _opener=raising_429)
    out = client.parse_transactions(["sig1"], "disputed_event")
    matrix("helius parsed-events 429",
           "rate_limited, 0 credits, 0 counted calls",
           "kind=%s credits=%r calls=%d" % (
               out["kind"], out["credits"], client.calls),
           out["kind"] == "rate_limited" and out["credits"] is None
           and client.calls == 0)
    ok_calls = []

    def counting_ok(req, timeout=None):
        ok_calls.append(req.full_url)
        return FakeResp({"result": [{"signature": "sig1"}]})
    client2 = HPE.HeliusParsedEventsClient(api_key="testkey-not-real",
                                           _opener=counting_ok)
    out = client2.parse_transactions(["sig1"], "routine")
    check("parsed events: non-selective reason refused, zero HTTP calls",
          out["kind"] == "refused" and len(ok_calls) == 0)
    client3 = HPE.HeliusParsedEventsClient(api_key=None)
    out = client3.parse_transactions(["sig1"], "disputed_event")
    check("parsed events: unconfigured (no key) -> unconfigured, no call",
          out["kind"] == "unconfigured")


# ===========================================================================
# (b) WSS failures — parsed stream disconnect + resume
# ===========================================================================
class FakeWS:
    """In-memory WebSocket stand-in: records sends, never opens a socket."""

    def __init__(self):
        self.sends = []
        self.closed = False
        self.run_calls = 0

    def send(self, text):
        self.sends.append(text)

    def run_forever(self):
        self.run_calls += 1

    def close(self):
        self.closed = True


def parsed_notification(sig, slot, sub_id=99):
    """SYNTHETIC parsed-stream notification in the documented shape."""
    return {"jsonrpc": "2.0", "method": "parsedTransactionNotification",
            "params": {"subscription": sub_id,
                       "result": {"value": {
                           "signature": sig, "slot": slot, "status": "ok",
                           "fee": 5000, "feePayer": "feePayerAddr",
                           "accountKeys": ["feePayerAddr"],
                           "instructions": [{
                               "programId": HS.PUMP_PROGRAM_ID,
                               "decoded": {"name": "create",
                                           "args": {}},
                               "summary": {"type": "CREATE_TOKEN"}}],
                           "matchedIndexes": [0],
                           "nativeTransfers": [], "tokenTransfers": []}}}}


def test_wss_failures():
    home = tempfile.mkdtemp(prefix="fm_wss_")
    live_router = router(home=home, helius_enabled=True)
    ws = FakeWS()
    created = []

    def factory(url, on_message, on_error, on_close, on_open):
        created.append(url)
        ws._on_message = on_message
        ws._on_error = on_error
        ws._on_close = on_close
        ws._on_open = on_open
        return ws

    client = HS.HeliusParsedStreamClient(
        wss_url="wss://fs-beta.helius-rpc.com/?api-key=testkey-not-real",
        router=live_router, ws_factory=factory,
        on_event=lambda ev: None)
    ok_sub, note = client.subscribe(HS.dex_filter(), "dex")
    check("wss: subscribe accepted under live router", ok_sub is True)
    bad, note = client.subscribe({"bogusField": [1]}, "bad")
    check("wss: invalid filter refused before any socket opens",
          bad is False and "unknown filter field" in (note or ""))
    check("wss: no socket opened by subscribe alone", len(created) == 0)
    check("wss: connect constructs the socket",
          client.connect() is True)
    check("wss: nothing sent before the handshake opens (D-02)",
          len(ws.sends) == 0 and client.connected is False)
    ws._on_open(ws)  # handshake completes -> on_open
    check("wss: connected only after open", client.connected is True)
    check("wss: subscribe request sent exactly once after open",
          len(ws.sends) == 1
          and json.loads(ws.sends[0])["method"]
          == HS.METHOD_SUBSCRIBE)
    # Subscribe ack, then two live notifications.
    client._on_message(ws, json.dumps({"jsonrpc": "2.0", "id": 1,
                                       "result": 99}))
    sig_a = "SigA1111111111111111111111111111111111111111111111111111111"
    sig_b = "SigB1111111111111111111111111111111111111111111111111111111"
    client._on_message(ws, json.dumps(parsed_notification(sig_a, 100)))
    client._on_message(ws, json.dumps(parsed_notification(sig_b, 101)))
    check("wss: two notifications received, last_slot tracks the stream",
          client.events_received == 2 and client.last_slot == 101)
    check("wss: stream events are parsed_unverified, never verified",
          client.stats()["events_received"] == 2)

    # Disconnect mid-stream: reconnect with backoff, subscriptions
    # re-issued, resume from last_slot, NO replay.
    client._on_close(ws)
    matrix("wss disconnect mid-stream",
           "reconnect scheduled (backoff doubles 2s->4s), last_slot kept, "
           "replay unavailable",
           "connected=%s reconnects=%d next_wait=%.0f last_slot=%s "
           "replay=%s" % (client.connected, client.reconnects,
                          client.next_reconnect_wait_s(),
                          client.last_slot,
                          client.resume_info()["replay_available"]),
           client.connected is False and client.reconnects == 1
           and client.next_reconnect_wait_s() == 4.0
           and client.last_slot == 101
           and client.resume_info()["replay_available"] is False
           and client.resume_info()["gap_recovery"]
           == "rpc_backfill_from_last_slot")
    sends_before = len(ws.sends)
    check("wss: reconnect constructs the socket, nothing sent yet",
          client.connect() is True
          and len(ws.sends) == sends_before
          and client.connected is False)
    ws._on_open(ws)  # handshake completes -> on_open
    check("wss: reconnect re-issues the wanted subscription on open",
          len(ws.sends) == sends_before + 1
          and json.loads(ws.sends[-1])["method"] == HS.METHOD_SUBSCRIBE)
    # The stream replays NOTHING: the same notification arriving
    # again after reconnect is a duplicate at the pipeline level.
    client._on_message(ws, json.dumps(parsed_notification(sig_b, 101)))
    check("wss: duplicate notification after reconnect is still counted "
          "at the stream layer (dedup lives in the pipeline)",
          client.events_received == 3 and client.last_slot == 101)

    # Handshake 429: counted, never folded into RPC 429s.
    before_429 = client.handshake_429s
    client._on_error(ws, Exception("handshake failed: 429 Too Many Requests"))
    check("wss: handshake 429 counted on the client",
          client.handshake_429s == before_429 + 1)
    client._on_message(ws, json.dumps(
        {"jsonrpc": "2.0", "id": 2,
         "error": {"code": -32002, "message": "rate limited"}}))
    check("wss: JSON-RPC -32002 rate-limit error counted as handshake 429",
          client.handshake_429s == before_429 + 2)

    def raising_factory(url, on_message, on_error, on_close, on_open):
        raise Exception("429 Too Many Requests")

    client2 = HS.HeliusParsedStreamClient(
        wss_url="wss://fs-beta.helius-rpc.com/?api-key=testkey-not-real",
        router=live_router, ws_factory=raising_factory)
    check("wss: factory raising 429 -> connect refuses, 429 counted",
          client2.connect() is False and client2.handshake_429s == 1)

    # Router-refused stream: no socket is ever opened (auto-quiet).
    quiet_router = router(helius_enabled=False)
    opened = []

    def spy_factory(url, on_message, on_error, on_close, on_open):
        opened.append(url)
        return FakeWS()

    client3 = HS.HeliusParsedStreamClient(
        wss_url="wss://fs-beta.helius-rpc.com/?api-key=testkey-not-real",
        router=quiet_router, ws_factory=spy_factory)
    check("wss: router-refused stream -> connect refuses, zero sockets",
          client3.connect() is False and len(opened) == 0)
    ok_sub, note = client3.subscribe(HS.dex_filter(), "dex")
    check("wss: subscribe refused while router refuses the stream",
          ok_sub is False and note is not None)
    client4 = HS.HeliusParsedStreamClient(wss_url=None, router=live_router,
                                           ws_factory=spy_factory)
    check("wss: unconfigured client (no URL) refuses to connect",
          client4.connect() is False)

    # Resume via RPC backfill after a disconnect: the gap is
    # recovered through the pipeline, the already-seen live event
    # is NOT applied twice.
    proc, store = fresh_processor()
    tx_buy = load_fixture("atx_buy.json")
    sig_buy = signature_of(tx_buy)
    # Live delivery first (as if from the stream, via getTransaction).
    outcome, _ = proc.process_transaction(
        "helius", tx_buy, commitment="confirmed", stream_key="dex")
    check("wss resume: live event applied before the disconnect",
          outcome == "applied")
    gap = proc.observe_slot("dex", 472184744 + 500)
    check("wss resume: slot jump after reconnect is recorded as a gap",
          gap is not None and gap["from_slot"] == 472184744
          and gap["to_slot"] == 472184744 + 500)
    # Backfill the gap: the signature page returns the already-seen
    # buy tx; the pipeline must drop it as a duplicate, not re-apply.
    rpc_calls = {"n": 0}

    def fake_rpc(method, params):
        rpc_calls["n"] += 1
        if method == "getSignaturesForAddress":
            return {"kind": "ok", "result": [
                {"signature": sig_buy, "slot": 472184744}]}
        return {"kind": "ok", "result": tx_buy}

    sigs, outcomes = backfill_after_disconnect(
        proc, "dex", "SomeAddress", fake_rpc, disconnected_s=200,
        fetch_transaction=lambda sig: tx_buy)
    matrix("wss resume: RPC backfill after disconnect",
           "recovered signature processed as a merge (second provider), "
           "market state applied exactly once",
           "sigs=%s outcomes=%s trade_count=%s" % (
               sigs, outcomes,
               proc.registry.get(
                   "BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump")),
           sigs == [sig_buy]
           and outcomes.get(sig_buy) in ("applied", "merged")
           and proc.registry.get(
               "BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump")
           ["trade_count"] == 1)
    store.close()


# ===========================================================================
# (c) Event-pipeline correctness under failure
# ===========================================================================
def test_event_pipeline_failures():
    tx_create = load_fixture("atx_create.json")
    tx_buy = load_fixture("atx_buy.json")
    tx_failed = load_fixture("atx_sell_failed.json")
    mint = "BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump"

    # Duplicate delivery from two providers -> one logical record.
    proc, store = fresh_processor()
    o1, eid1 = proc.process_observation(obs("alchemy", tx_create))
    o2, eid2 = proc.process_observation(obs("chainstack", tx_create))
    o3, eid3 = proc.process_observation(obs("alchemy", tx_create))
    matrix("duplicate delivery: alchemy + chainstack + repeat",
           "one logical record, providers [alchemy, chainstack], "
           "outcomes applied/merged/duplicate",
           "outcomes=%s/%s/%s count=%d providers=%s" % (
               o1, o2, o3, store.count_events(),
               store.get_event(eid1).providers if eid1 else None),
           o1 == "applied" and o2 in ("applied", "merged")
           and o3 == "duplicate" and proc.stats["merged"] == 1
           and store.count_events() == 1 and eid1 == eid2 == eid3
           and sorted(store.get_event(eid1).providers)
           == ["alchemy", "chainstack"])
    check("duplicate delivery: market state updated exactly once",
          proc.registry.get(mint) is not None
          and proc.registry.get(mint)["created_slot"] == 472184636)
    # Same guarantee for a trade (the counter case): a merged
    # second-provider delivery AND an explicit reprocess must not
    # count the trade twice.
    proc2, store2 = fresh_processor()
    proc2.process_observation(obs("alchemy", tx_buy))
    proc2.process_observation(obs("chainstack", tx_buy))
    proc2.reprocess_event(proc2.store.get_by_signature(
        signature_of(tx_buy))[0].event_id)
    check("duplicate delivery: trade counted once across merge + reprocess",
          proc2.registry.get(mint)["trade_count"] == 1)
    store2.close()
    store.close()

    # Conflicting multi-provider observations -> conflicting, held,
    # market state untouched.
    proc, store = fresh_processor()
    o1, eid = proc.process_observation(obs("alchemy", tx_create))
    tx_other_slot = copy.deepcopy(tx_create)
    tx_other_slot["slot"] = 472184637
    o2, _ = proc.process_observation(obs("chainstack", tx_other_slot))
    ev = store.get_event(eid)
    matrix("conflicting observations (slot disagreement)",
           "verification conflicting, processing held, market state "
           "keeps the first slot, disagreement recorded",
           "outcome=%s verification=%s registry_slot=%s disagreements=%d"
           % (o2, ev.verification_status,
              (proc.registry.get(mint) or {}).get("created_slot"),
              len(store.list_disagreements(eid))),
           o2 == "held"
           and ev.verification_status == VERIFICATION_CONFLICTING
           and (proc.registry.get(mint) or {}).get("created_slot")
           == 472184636
           and len(store.list_disagreements(eid)) >= 1)
    check("conflicting: reprocess still refuses market-state update",
          proc.reprocess_event(eid)[0] == "held")
    store.close()

    # Corrupt event store -> CorruptStoreError, file preserved.
    tmpdir = tempfile.mkdtemp(prefix="fm_corrupt_")
    path = os.path.join(tmpdir, "intelligence_events.db")
    garbage = b"this is not a sqlite database at all" * 4
    with open(path, "wb") as fh:
        fh.write(garbage)
    digest_before = hashlib.sha256(garbage).hexdigest()
    raised = False
    try:
        EventStore(path).open()
    except CorruptStoreError:
        raised = True
    with open(path, "rb") as fh:
        digest_after = hashlib.sha256(fh.read()).hexdigest()
    matrix("corrupt event store",
           "CorruptStoreError raised, file preserved byte-for-byte",
           "raised=%s sha_before=%s sha_after=%s" % (
               raised, digest_before[:12], digest_after[:12]),
           raised is True and digest_before == digest_after)
    # A truncated (zero-length-but-existing) file is also corrupt.
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 100)
    raised2 = False
    try:
        EventStore(path).open()
    except CorruptStoreError:
        raised2 = True
    check("corrupt event store: zeroed header also raises, not blanked",
          raised2 is True and os.path.getsize(path) == 100)

    # Failed transaction -> transaction_failed, no trade events.
    proc, store = fresh_processor()
    seen_updates = []
    proc.on_market_update = lambda ev, m: seen_updates.append(m)
    o, eid = proc.process_observation(obs("alchemy", tx_failed))
    ev = store.get_event(eid)
    matrix("failed transaction on chain",
           "event_type transaction_failed, verification failed, held "
           "(never applied), no market update, no trade counted",
           "outcome=%s type=%s verification=%s updates=%d" % (
               o, ev.event_type, ev.verification_status,
               len(seen_updates)),
           ev.event_type == EVENT_TYPE_TRANSACTION_FAILED
           and ev.verification_status == VERIFICATION_FAILED
           and o == "held" and len(seen_updates) == 0
           and proc.registry.get(mint) is None)
    store.close()

    # Preliminary (processed-stage) observation: held, never updates
    # market state; the confirmed observation later merges and applies.
    proc, store = fresh_processor()
    seen_updates = []
    proc.on_market_update = lambda ev, m: seen_updates.append(m)
    o1, eid1 = proc.process_observation(
        obs("alchemy", tx_buy, commitment="processed"))
    ev1 = store.get_event(eid1)
    matrix("preliminary (processed) observation",
           "held, unverified, registry untouched, no market update",
           "outcome=%s verification=%s registry=%s updates=%d" % (
               o1, ev1.verification_status,
               proc.registry.get(mint), len(seen_updates)),
           o1 == "held"
           and ev1.verification_status == VERIFICATION_UNVERIFIED
           and proc.registry.get(mint) is None
           and len(seen_updates) == 0)
    o2, eid2 = proc.process_observation(
        obs("chainstack", tx_buy, commitment="confirmed"))
    ev2 = store.get_event(eid2)
    check("preliminary then confirmed: merge promotes to verified+applied",
          o2 == "applied"
          and ev2.verification_status == VERIFICATION_VERIFIED
          and proc.registry.get(mint) is not None
          and proc.registry.get(mint)["trade_count"] == 1
          and len(seen_updates) == 1)
    store.close()

    # Preliminary preprocessed lifecycle: never updates confirmed
    # state, never authorizes a trade, until stage-2 verified.
    life = HPP.PreprocessedLifecycle(now=lambda: 1795273900.0)
    frame = struct.pack("<BQ", 1, 472184636) + bytes(range(64)) + b"\x01\x02\x03"
    rec, note = life.observe(frame)
    matrix("preprocessed stage-1 observation",
           "preliminary/unverified only, apply + trade both refused",
           "stage=%s verified=%s" % (rec["stage"], rec["verified"]),
           rec is not None and rec["stage"] == "preliminary"
           and rec["verified"] is False)
    refused_apply = refused_trade = False
    try:
        life.apply_to_confirmed_state(rec)
    except ValueError:
        refused_apply = True
    try:
        life.authorize_trade(rec)
    except ValueError:
        refused_trade = True
    check("preprocessed: apply_to_confirmed_state refuses preliminary",
          refused_apply is True)
    check("preprocessed: authorize_trade refuses preliminary",
          refused_trade is True)
    check("preprocessed: stream request itself is always refused",
          HPP.request_stream()["kind"] == "refused"
          and HPP.request_stream()["refused"] is True)
    # Stage 2: verified against a confirmed getTransaction result.
    verified, note = life.verify_execution(
        rec["signature"],
        {"transaction": {"signatures": [rec["signature"]]},
         "slot": 472184636, "meta": {"err": None, "fee": 5000}})
    check("preprocessed stage-2: matching confirmed tx verifies",
          verified is not None and verified["stage"] == "verified"
          and verified["execution"]["succeeded"] is True)
    applied = life.apply_to_confirmed_state(verified)
    check("preprocessed stage-2: verified record may update state",
          applied["applied"] is True)
    check("preprocessed stage-2: verified + succeeded may authorize",
          life.authorize_trade(verified)["authorized"] is True)
    # A failed execution verifies the failure but never authorizes.
    life2 = HPP.PreprocessedLifecycle(now=lambda: 1795273900.0)
    rec2, _ = life2.observe(frame)
    verified2, _ = life2.verify_execution(
        rec2["signature"],
        {"transaction": {"signatures": [rec2["signature"]]},
         "slot": 472184636,
         "meta": {"err": {"InstructionError": [0, "Custom"]},
                  "fee": 5000}})
    refused_failed_trade = False
    try:
        life2.authorize_trade(verified2)
    except ValueError:
        refused_failed_trade = True
    check("preprocessed stage-2: failed execution never authorizes a trade",
          verified2["execution"]["succeeded"] is False
          and refused_failed_trade is True)
    # Slot disagreement at stage 2 is a conflict, not a promotion.
    life3 = HPP.PreprocessedLifecycle(now=lambda: 1795273900.0)
    rec3, _ = life3.observe(frame)
    verified3, note3 = life3.verify_execution(
        rec3["signature"],
        {"transaction": {"signatures": [rec3["signature"]]},
         "slot": 472184637, "meta": {"err": None, "fee": 5000}})
    check("preprocessed stage-2: slot mismatch is a conflict, not verified",
          verified3 is None and len(life3.conflicts()) == 1)

    # Missing-event range: NEVER silently discarded.
    ck_home = tempfile.mkdtemp(prefix="fm_backfill_")
    sig_create = signature_of(tx_create)
    gap = {"key": "dex", "from_slot": 472184630, "to_slot": 472184640}
    failing = HostTransport({
        "alchemy": [err500()],
        "chainstack": [ok([])],
        "mainnet-beta": [err500()]})
    driver = BackfillDriver(router(transport=failing), home=ck_home)
    report = driver.recover_gap("dex", "SomeAddress", gap)
    ck = driver.checkpoints.get_checkpoint("dex")
    matrix("missing-event range: providers fail",
           "unrecovered gap recorded, checkpoint NOT advanced past it",
           "gap=%s checkpoint=%s events_new=%d" % (
               report["unrecovered_gap"] is not None, ck,
               report["events_new"]),
           report["unrecovered_gap"] is not None
           and ck is None and report["events_new"] == 0
           and len(driver.checkpoints.unrecovered_gaps("dex")) == 1)
    # Retry while still failing bumps attempts, never duplicates the gap.
    report2 = driver.recover_gap("dex", "SomeAddress", gap)
    check("missing-event range: retry bumps attempts, gap stays single",
          len(driver.checkpoints.unrecovered_gaps("dex")) == 1
          and driver.checkpoints.unrecovered_gaps("dex")[0]["attempts"]
          == 2)
    # Retry after service returns: gap closes, checkpoint advances.
    working = HostTransport({
        "alchemy": [ok([{"signature": sig_create, "slot": 472184636}]),
                    ok(tx_create)],
        "chainstack": [ok([])],
        "mainnet-beta": [ok([])]})
    driver2 = BackfillDriver(router(transport=working), home=ck_home,
                             sink=driver.sink)
    reports = driver2.retry_unrecovered_gaps({"dex": "SomeAddress"})
    ck = driver2.checkpoints.get_checkpoint("dex")
    matrix("missing-event range: retry after recovery",
           "gap cleared, checkpoint advanced to the recovered slot",
           "reports=%d gaps_left=%d checkpoint=%s" % (
               len(reports),
               len(driver2.checkpoints.unrecovered_gaps("dex")), ck),
           len(reports) == 1
           and len(driver2.checkpoints.unrecovered_gaps("dex")) == 0
           and ck is not None and ck["last_slot"] == 472184636
           and ck["last_signature"] == sig_create)
    # A failed getTransaction inside an otherwise-good range records
    # the failed slot range and does NOT advance past it.
    ck_home2 = tempfile.mkdtemp(prefix="fm_backfill2_")
    partial = HostTransport({
        "alchemy": [ok([{"signature": sig_create, "slot": 472184636}]),
                    err500()],
        "chainstack": [ok([])],
        "mainnet-beta": [err500()]})
    driver3 = BackfillDriver(router(transport=partial), home=ck_home2)
    report3 = driver3.recover_gap("dex", "SomeAddress", gap)
    matrix("missing-event range: tx fetch fails mid-range",
           "failed slot range recorded as unrecovered, checkpoint unmoved",
           "gap=%s checkpoint=%s fetch_failed=%d" % (
               report3["unrecovered_gap"] is not None,
               driver3.checkpoints.get_checkpoint("dex"),
               len(report3["fetch_failed"])),
           report3["unrecovered_gap"] is not None
           and driver3.checkpoints.get_checkpoint("dex") is None
           and len(report3["fetch_failed"]) == 1)

    # Failed-event retry queue: backoff, due only after it expires,
    # reprocess resolves.
    proc, store = fresh_processor()
    rq = RetryQueue(store)
    attempts = rq.enqueue("ev-missing", sig_create, 472184636,
                          "simulated pipeline failure")
    check("retry queue: first failure enqueued with attempt count 1",
          attempts == 1 and rq.pending_count() == 1)
    check("retry queue: not due before the backoff expires",
          rq.due(now=time.time()) == [])
    resolved, still = reprocess_failed(
        proc,
        lambda eid, sig, slot: obs("alchemy", tx_create),
        now=time.time() + 10000, retry_queue=rq)
    check("retry queue: reprocess after backoff resolves the event",
          resolved == ["ev-missing"] and still == []
          and rq.pending_count() == 0
          and store.count_events() == 1)
    store.close()

    # Checkpoint tracker: resume from the last processed slot.
    proc, store = fresh_processor()
    tracker = CheckpointTracker(store)
    proc.process_observation(obs("alchemy", tx_create),
                             stream_key="dex")
    tracker.record("dex", 472184636, signature_of(tx_create))
    check("checkpoint tracker: last slot/signature resume correctly",
          tracker.last_slot("dex") == 472184636
          and tracker.last_signature("dex") == signature_of(tx_create))
    store.close()


# ===========================================================================
# (d) Cache / correctness
# ===========================================================================
def test_cache_correctness():
    # Router response cache: a cached answer is served inside its
    # TTL, and a fresh read is taken once the TTL expires — a stale
    # cache must never mask a newer on-chain state past its TTL.
    clock = [1000.0]
    now = lambda: clock[0]
    t = HostTransport({"alchemy": [ok(100), ok(200)],
                       "chainstack": [ok(888)],
                       "mainnet-beta": [ok(999)]})
    r = router(transport=t, now=now)
    res1 = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    res2 = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("cache: identical read inside TTL",
           "second call served from cache, one HTTP call, same value",
           "cached=%s calls=%d value=%s" % (
               res2.get("cached"), t.by_host.get("alchemy", 0),
               res2.get("result")),
           res1["kind"] == "ok" and res1["result"] == 100
           and res2.get("cached") is True and res2["result"] == 100
           and t.by_host.get("alchemy", 0) == 1)
    check("cache: a different address is NOT masked by the cached entry",
          r.call("getBalance", ["addr2"],
                 priority=PRIORITY_CARD)["result"] == 200
          and t.by_host.get("alchemy", 0) == 2)
    clock[0] += 6.0  # getBalance TTL is 5s
    res3 = r.call("getBalance", ["addr1"], priority=PRIORITY_CARD)
    matrix("cache: read after TTL expiry",
           "cache expired, provider re-read, newer value returned",
           "cached=%s value=%s calls=%d" % (
               res3.get("cached"), res3.get("result"),
               t.by_host.get("alchemy", 0)),
           res3.get("cached") is False and res3["result"] == 200
           and t.by_host.get("alchemy", 0) == 3)
    # Live values are never cached: getSlot re-reads every time.
    t2 = HostTransport({"alchemy": [ok(500), ok(501)],
                        "chainstack": [ok(0)],
                        "mainnet-beta": [ok(0)]})
    r2 = router(transport=t2, now=now)
    s1 = r2.call("getSlot", [], priority=PRIORITY_CARD)
    s2 = r2.call("getSlot", [], priority=PRIORITY_CARD)
    check("cache: getSlot is never cached (live value, 2 HTTP calls)",
          s1["result"] == 500 and s2["result"] == 501
          and s2.get("cached") is False
          and t2.by_host.get("alchemy", 0) == 2)
    # A cached answer never crosses providers.
    check("cache: per-provider keys (chainstack cache is separate)",
          r.cache.get("chainstack", "getBalance", ["addr1"]) is None)

    # Alchemy workload's own tx cache: 300s TTL, then refetch.
    clock2 = [2000.0]
    now2 = lambda: clock2[0]
    tx1 = load_fixture("atx_create.json")
    tx2 = load_fixture("atx_buy.json")
    t3 = HostTransport({"alchemy": [ok(tx1), ok(tx2)],
                        "chainstack": [ok(0)],
                        "mainnet-beta": [ok(0)]})
    r3 = router(transport=t3, now=now2)
    wl = AlchemyWorkload(router=r3, now=now2)
    sig = signature_of(tx1)
    w1 = wl.get_transaction(sig)
    w2 = wl.get_transaction(sig)
    matrix("workload tx cache: read inside 300s TTL",
           "second read served from cache, one HTTP call",
           "cached=%s calls=%d slot=%s" % (
               w2.get("cached"), t3.by_host.get("alchemy", 0),
               w2.get("result", {}).get("slot")),
           w1["kind"] == "ok" and w2.get("cached") is True
           and w2["result"]["slot"] == 472184636
           and t3.by_host.get("alchemy", 0) == 1)
    clock2[0] += 301.0
    w3 = wl.get_transaction(sig)
    check("workload tx cache: read after TTL refetches the provider",
          w3.get("cached") is False
          and w3["result"]["slot"] == 472184744
          and t3.by_host.get("alchemy", 0) == 2)

    # Failover records fallback_difference AND the usage journal
    # records the workload actually served.
    home = tempfile.mkdtemp(prefix="fm_journal_")
    t4 = HostTransport({"alchemy": [err500()], "chainstack": [ok(888)],
                        "mainnet-beta": [ok(999)]})
    r4 = router(home=home, transport=t4)
    res = r4.call("getBalance", ["addr1"], priority=PRIORITY_CRITICAL)
    lines = journal_lines(home)
    matrix("journal: failover records the workload actually served",
           "journal line: account chainstack, workload routine_account, "
           "fallback true, fallback_difference set",
           "lines=%d %s" % (len(lines), lines[0] if lines else None),
           len(lines) >= 2
           and any(l.get("account") == "chainstack"
                   and l.get("workload") == PR.ROUTINE_ACCOUNT
                   and l.get("fallback") is True
                   and l.get("fallback_difference") for l in lines)
           and any(l.get("account") == "alchemy"
                   and l.get("kind") == "error" for l in lines))
    check("journal: fallback_difference text names the serving provider",
          res["fallback_difference"] is not None
          and "Chainstack" in res["fallback_difference"])
    # The usage report groups the failover by provider x workload.
    report = UR.build_report(home=home, provenance="failure-matrix run")
    rows = {(row["provider"], row["workload"]): row
            for row in report["rows"]}
    matrix("usage report: failover visible by provider x workload",
           "chainstack/routine_account row exists with ok>=1; "
           "alchemy row records the error",
           "rows=%s" % sorted(rows.keys()),
           ("chainstack", PR.ROUTINE_ACCOUNT) in rows
           and rows[("chainstack", PR.ROUTINE_ACCOUNT)]["ok"] >= 1
           and rows[("alchemy", PR.ROUTINE_ACCOUNT)]["errors"] >= 1)
    check("usage report: provenance is labelled, not claimed live",
          report["provenance"] == "failure-matrix run")

    # Chainstack workload verification: a disagreeing second read
    # is a conflict, never silently resolved. The canonical digest
    # covers transaction CONTENT (fee, balances, accounts,
    # programs, blockTime) — so the disagreement must be in a
    # digested field; a slot-only difference is outside the digest's
    # documented scope (slot conflicts are caught by the event
    # normalizer's merge check instead).
    home5 = tempfile.mkdtemp(prefix="fm_verify_")
    tx_a = load_fixture("atx_create.json")
    tx_b = copy.deepcopy(tx_a)
    tx_b["meta"] = dict(tx_a["meta"])
    tx_b["meta"]["fee"] = tx_a["meta"]["fee"] + 5000
    t5 = HostTransport({"alchemy": [ok(tx_a)],
                        "chainstack": [ok(tx_b)],
                        "mainnet-beta": [ok(0)]})
    r5 = router(home=home5, transport=t5)
    out = verify_transaction(r5, signature_of(tx_a), tx_a, recent=True)
    check("chainstack verify: fee disagreement surfaces as a conflict",
          out.get("match") is False and out.get("status") == "conflict")
    check("chainstack verify: digest differences name the field",
          "fee" in list(out.get("differences") or []))
    check("chainstack verify: the disagreeing read came from chainstack",
          out.get("provider") == "chainstack")


# ===========================================================================
# (e) Cost management under failure
# ===========================================================================
def test_cost_under_failure():
    # Spend Alchemy to 96%: background + deployer suppressed, card
    # and critical NEVER suppressed.
    home = tempfile.mkdtemp(prefix="fm_cost_")
    store = UsageStore(home=home)
    store.record("alchemy", "alchemy", "getTransaction",
                 PR.ROUTINE_TX, PRIORITY_CARD, cost=28_800_000,
                 kind="ok")
    cm = CM.CostManager(home=home)
    status = cm.suppression_status("alchemy")
    matrix("budget 96%: suppression state",
           "background + nonessential suppressed, essential never",
           "pct=%s bg_suppressed=%s noness_suppressed=%s "
           "essential_suppressed=%s" % (
               status["pct"], status["background_suppressed"],
               status["nonessential_suppressed"],
               status["essential_suppressed"]),
           status["pct"] == 0.96
           and status["background_suppressed"] is True
           and status["nonessential_suppressed"] is True
           and status["essential_suppressed"] is False)
    allowed_bg, reason_bg = cm.pre_call_check("alchemy",
                                              PRIORITY_BACKGROUND)
    allowed_dep, reason_dep = cm.pre_call_check("alchemy",
                                                PRIORITY_DEPLOYER)
    allowed_card, _ = cm.pre_call_check("alchemy", PRIORITY_CARD)
    allowed_crit, _ = cm.pre_call_check("alchemy", PRIORITY_CRITICAL)
    check("budget 96%: background refused at the stop threshold",
          allowed_bg is False
          and reason_bg == "budget_stop_nonessential")
    check("budget 96%: deployer (nonessential) refused at the stop",
          allowed_dep is False
          and reason_dep == "budget_stop_nonessential")
    check("budget 96%: card (essential) still allowed",
          allowed_card is True)
    check("budget 96%: critical (essential) still allowed",
          allowed_crit is True)
    acc = cm.accounting()
    check("budget 96%: pre-call refusals are recorded in the "
          "suppression log",
          len(acc.get("suppressions", [])) >= 2
          and all(s["account"] == "alchemy"
                  for s in acc["suppressions"]))
    check("budget 96%: cost alerts fire (warn + stop)",
          any(a["account"] == "alchemy"
              and a["stop_nonessential"] for a in cm.alerts()))

    # Router level: a background call at 96% skips Alchemy and is
    # served by Chainstack; a critical call still uses Alchemy.
    t = HostTransport({"alchemy": [ok(111)], "chainstack": [ok(222)],
                       "mainnet-beta": [ok(333)]})
    r = PR.ProviderRouter(home=home, helius_enabled=False, transport=t,
                          usage_tracker=cm.tracker)
    res_bg = r.call("getBalance", ["a"], priority=PRIORITY_BACKGROUND)
    res_crit = r.call("getBalance", ["a"], priority=PRIORITY_CRITICAL)
    matrix("budget 96% at the router",
           "background served via chainstack, critical still via alchemy",
           "bg=%s crit=%s" % (res_bg.get("provider"),
                              res_crit.get("provider")),
           res_bg["kind"] == "ok" and res_bg["provider"] == "chainstack"
           and res_crit["kind"] == "ok"
           and res_crit["provider"] == "alchemy")

    # At 80-95%: background suppressed (warn), deployer still allowed.
    home2 = tempfile.mkdtemp(prefix="fm_cost2_")
    store2 = UsageStore(home=home2)
    store2.record("alchemy", "alchemy", "getTransaction",
                  PR.ROUTINE_TX, PRIORITY_CARD, cost=25_500_000,
                  kind="ok")
    cm2 = CM.CostManager(home=home2)
    allowed_bg, reason_bg = cm2.pre_call_check("alchemy",
                                               PRIORITY_BACKGROUND)
    allowed_dep, _ = cm2.pre_call_check("alchemy", PRIORITY_DEPLOYER)
    matrix("budget 85%: warn threshold",
           "background suppressed (warn), deployer still allowed",
           "bg_allowed=%s reason=%s dep_allowed=%s" % (
               allowed_bg, reason_bg, allowed_dep),
           allowed_bg is False
           and reason_bg == "budget_warn_suppress_background"
           and allowed_dep is True)
    # Global pressure: a strained Alchemy sheds background on the
    # HEALTHY Chainstack account too.
    allowed_cs_bg, reason_cs = cm2.pre_call_check("chainstack",
                                                  PRIORITY_BACKGROUND)
    check("budget 85%: global pressure suppresses background elsewhere",
          allowed_cs_bg is False
          and reason_cs == "global_budget_pressure_background")
    check("budget 85%: global pressure never touches essential",
          cm2.pre_call_check("chainstack", PRIORITY_CRITICAL)[0] is True)

    # Disabled account: everything refused, including essential.
    home3 = tempfile.mkdtemp(prefix="fm_cost3_")
    cm3 = CM.CostManager(home=home3)
    cm3.tracker.disable_account("chainstack", "test disablement")
    allowed, reason = cm3.pre_call_check("chainstack", PRIORITY_CRITICAL)
    check("disabled account: even critical is refused (operator decision)",
          allowed is False and reason == "account_disabled")
    cm3.tracker.enable_account("chainstack")
    check("disabled account: re-enable restores access",
          cm3.pre_call_check("chainstack", PRIORITY_CRITICAL)[0] is True)

    # Unenforced budget (public: no quota amount): background allowed.
    check("unenforced budget: public background is never suppressed",
          cm3.pre_call_check("public", PRIORITY_BACKGROUND)[0] is True)

    # Savings accounting: dedup + cache hits are counted as saved
    # cost in the provider's own unit, never summed across providers.
    home4 = tempfile.mkdtemp(prefix="fm_cost4_")
    t4 = HostTransport({"alchemy": [ok(777)], "chainstack": [ok(888)],
                        "mainnet-beta": [ok(999)]})
    r4 = router(home=home4, transport=t4)
    r4.call("getBalance", ["a"], priority=PRIORITY_CARD)
    r4.call("getBalance", ["a"], priority=PRIORITY_CARD)  # cache hit
    cm4 = CM.CostManager(home=home4)
    cm4.record_dedup("alchemy", "alchemy", "getBalance",
                     PR.ROUTINE_ACCOUNT, count=2)
    savings = cm4.total_saved()
    cache = cm4.cache_savings()
    matrix("savings accounting under failure load",
           "cache hit + 2 deduped reads counted as saved cost (CU)",
           "cache=%s total_saved=%s" % (cache, savings),
           cache.get("alchemy", {}).get("cached_requests", 0) == 1
           and savings.get("alchemy", {}).get("saved_cost", 0) > 0)
    check("savings: WS bytes are unmeasured when the transport "
          "reports none — never invented",
          cm4.record_ws("alchemy", "alchemy", bytes_received=None,
                        events=3)["bytes_unmeasured_events"] == 3
          and cm4.record_ws("alchemy", "alchemy",
                            bytes_received=None,
                            events=0)["bytes_measured"] == 0)


# ===========================================================================
# Runner
# ===========================================================================
def main():
    test_outage_matrix()
    test_helius_429_storm()
    test_wss_failures()
    test_event_pipeline_failures()
    test_cache_correctness()
    test_cost_under_failure()
    print("\n--- failure matrix ---")
    for scenario, expected, observed, result in MATRIX:
        print("[%s] %s\n    expected: %s\n    observed: %s"
              % (result, scenario, expected, observed))
    print("\n%d checks, %d failed" % (PASSES[0], len(FAILS)))
    if FAILS:
        print("FAILED: %s" % FAILS)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        os.environ.pop("ALCHEMY_API_KEY", None)
        os.environ.pop("CHAINSTACK_ENDPOINT", None)
