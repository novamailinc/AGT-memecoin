#!/usr/bin/env python3
"""Phase 5 tests — Alchemy routine acquisition workload
(alchemy_workload.py) + narrow Alchemy WSS subscriptions
(websocket_manager.AlchemySubscriptions).

Plain script, no network: HTTP is driven through an injected fake
opener keyed by host, replaying the recorded fixtures under
tests/data/intelligence/acquisition/ (real on-chain data captured
2026-11-22 via reserves.rpc()); the WSS side uses a fake socket.
Live Alchemy verification is DEFERRED to AGT (url_path_segment
surrogate swap unproven from this VM; the key re-entered 2026-11-22
~02:11 PST from the dashboard still 401s) — nothing here makes a
live call.

Covers (spec section 4): routine traffic routes Alchemy-first per
the router rules (end state) while the shadow phase still reports
Helius first and the workload never overrides it; every routine
method in section 4A (account lookups, tx retrieval, slot/block
reads) via the router; getTransaction cached per signature (never
re-fetched); request batching (chunked multi-account / status
calls); getTokenLargestAccounts flagged PROVISIONAL (UNVERIFIED);
getTokenAccountsByOwner flagged PROVISIONAL (cost UNVERIFIED —
its router gap was fixed 2026-11-22 by the router-owner
addition); WSS
subscription lifecycle (connect, drop, reconnect, handshake-429
counted separately from RPC 429s); no unfiltered program
subscriptions exist; scheduler + usage tracking honoured on every
call.
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

from daemon.intelligence.acquisition import alchemy_workload as AW
from daemon.intelligence.acquisition import websocket_manager as WM
from daemon.intelligence.acquisition.provider_router import ProviderRouter

fails = []
total = [0]
HOME = tempfile.mkdtemp(prefix="acq-phase5-")
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def load_fixture(name):
    with open(os.path.join(REPO, "tests/data/intelligence/acquisition",
                           name + ".json")) as fh:
        return json.load(fh)


FIX_SLOT = load_fixture("live_getSlot")
FIX_ACCT = load_fixture("live_getAccountInfo_wsol")
FIX_TX = load_fixture("live_getTransaction_pump")
FIX_SIGS = load_fixture("live_getSignaturesForAddress_pump")


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


class RoutingTransport:
    """Fake opener: answers per method from recorded fixtures /
    generic values; records every (host, method, params) call."""

    def __init__(self):
        self.calls = []  # (host, method, params)

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        method, params = body["method"], body.get("params", [])
        host = req.full_url.split("://", 1)[1].split("/", 1)[0]
        self.calls.append((host, method, params))
        if method == "getSlot":
            result = FIX_SLOT["result"]
        elif method == "getAccountInfo":
            result = FIX_ACCT["result"]
        elif method == "getTransaction":
            result = FIX_TX["result"]
        elif method == "getSignaturesForAddress":
            result = FIX_SIGS["result"]
        elif method == "getBlock":
            result = {"slot": params[0], "blockTime": 1795341615,
                      "transactions": []}
        elif method == "getLatestBlockhash":
            result = {"blockhash": "B" * 44, "lastValidBlockHeight": 1}
        elif method == "getMultipleAccounts":
            result = {"value": [{"data": None} for _ in params[0]]}
        elif method == "getSignatureStatuses":
            result = {"value": [None for _ in params[0]]}
        elif method in ("getBalance", "getTokenAccountBalance"):
            result = {"value": 12345}
        elif method == "getTokenLargestAccounts":
            result = {"value": []}
        else:
            result = {"value": None}
        return FakeResp({"jsonrpc": "2.0", "id": 1, "result": result})


def method_calls(transport, method):
    return [c for c in transport.calls if c[1] == method]


# ---------------------------------------------------------------------------
# Workload over an end-state router (helius disabled -> Alchemy first)
# ---------------------------------------------------------------------------
transport = RoutingTransport()
router = ProviderRouter(home=HOME, helius_enabled=False,
                        transport=transport)
wl = AW.AlchemyWorkload(router=router)

# -- routing: Alchemy-first in the end state, per the router rules -----
for method in ("getAccountInfo", "getBalance", "getMultipleAccounts",
               "getTokenAccountBalance", "getTransaction", "getBlock",
               "getSlot", "getLatestBlockhash", "getSignatureStatuses",
               "getSignaturesForAddress"):
    dec = wl.route_preview(method)
    check("end-state route primary is alchemy for %s" % method,
          dec["primary"] == "alchemy" and not dec["refused"])

# Shadow phase honesty: with Helius enabled the router still puts
# the live leg first for P0-P2 work; the workload must not override.
shadow_router = ProviderRouter(home=HOME, helius_enabled=True,
                               transport=transport)
shadow_wl = AW.AlchemyWorkload(router=shadow_router)
dec = shadow_wl.route_preview("getAccountInfo")
check("shadow phase still reports helius first (workload does not "
      "override the router)", dec["primary"] == "helius")

# -- verify_methods: every section 4A method supported-or-flagged ----
verified = wl.verify_methods()
by_method = {v["method"]: v for v in verified}
check("verify_methods covers all 12 section-4A methods",
      len(verified) == 12
      and set(by_method) == set(AW.ROUTINE_METHODS)
      | set(AW.PROVISIONAL_METHODS))
supported = [m for m, v in by_method.items() if v["status"] == "supported"]
check("10 routine methods supported on Alchemy",
      len(supported) == 10)
check("getTokenLargestAccounts flagged PROVISIONAL (UNVERIFIED), "
      "cost unknown",
      by_method["getTokenLargestAccounts"]["status"] == "provisional"
      and by_method["getTokenLargestAccounts"]["alchemy_cost_cu"] is None)
check("getTokenAccountsByOwner flagged PROVISIONAL (cost "
      "UNVERIFIED; router gap fixed 2026-11-22)",
      by_method["getTokenAccountsByOwner"]["status"] == "provisional"
      and by_method["getTokenAccountsByOwner"]["alchemy_cost_cu"] is None
      and by_method["getTokenAccountsByOwner"]
      ["router_primary_end_state"] == "alchemy")
check("every routable method has Alchemy as end-state primary",
      all(v["router_primary_end_state"] == "alchemy"
          for m, v in by_method.items()
          if v["status"] in ("supported", "provisional")))
check("Alchemy CU table: account 10, slot 20, history 40",
      by_method["getAccountInfo"]["alchemy_cost_cu"] == 10
      and by_method["getSlot"]["alchemy_cost_cu"] == 20
      and by_method["getTransaction"]["alchemy_cost_cu"] == 40)

# -- account lookups (fixture replay) ---------------------------------
res = wl.get_account_info(
    "So11111111111111111111111111111111111111112")
check("getAccountInfo routes alchemy + replays wSOL fixture",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and res["result"]["value"]["owner"]
      == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
      and res["fallback"] is False)
res = wl.get_balance("So11111111111111111111111111111111111111112")
check("getBalance ok via alchemy", res["kind"] == "ok"
      and res["provider"] == "alchemy"
      and res["result"] == {"value": 12345})
res = wl.get_token_account_balance("TokenAcc1111111111111111111111111")
check("getTokenAccountBalance ok via alchemy",
      res["kind"] == "ok" and res["provider"] == "alchemy")

# Router response cache honoured: identical account read is cached,
# no second transport call.
before = len(method_calls(transport, "getAccountInfo"))
res2 = wl.get_account_info(
    "So11111111111111111111111111111111111111112")
check("router response cache honoured (cached account read, no "
      "new transport call)",
      res2.get("cached") is True
      and len(method_calls(transport, "getAccountInfo")) == before)

# -- batching: getMultipleAccounts chunked at 100 ----------------------
pubkeys = ["acct%03d" % i for i in range(250)]
results = wl.get_multiple_accounts(pubkeys)
check("getMultipleAccounts(250) chunked into 3 router calls",
      len(results) == 3
      and len(method_calls(transport, "getMultipleAccounts")) == 3
      and all(r["kind"] == "ok" for r in results))
sizes = [len(c[2][0]) for c in
         method_calls(transport, "getMultipleAccounts")]
check("chunks are 100/100/50", sizes == [100, 100, 50])

# -- getTokenAccountsByOwner: routable via the router (gap fixed) --
n_before = len(transport.calls)
res = wl.get_token_accounts_by_owner(
    "Owner1111111111111111111111111111111111111",
    mint="So11111111111111111111111111111111111111112")
check("getTokenAccountsByOwner routes alchemy (router gap fixed "
      "2026-11-22), one transport call",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and len(transport.calls) == n_before + 1)

# -- transaction retrieval: fixture replay + per-signature cache ------
SIG = "eY5SFmn2qGBCbwqWUQEKJJr6N7VevCWvKTKqkCaC6RBbuSqYTQtfK9djE8EWSTHYFbnyMLUoYiC3ZAWVDiCt7q8"
n_tx_before = len(method_calls(transport, "getTransaction"))
res = wl.get_transaction(SIG)
check("getTransaction routes alchemy + replays pump fixture",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and res["result"]["blockTime"] == 1795341615)
check("first getTransaction made one transport call",
      len(method_calls(transport, "getTransaction")) == n_tx_before + 1)
res_cached = wl.get_transaction(SIG)
check("second getTransaction same signature is cached, never "
      "re-fetched",
      res_cached.get("cached") is True
      and res_cached["result"]["blockTime"] == 1795341615
      and len(method_calls(transport, "getTransaction"))
      == n_tx_before + 1)
res_other = wl.get_transaction("OtherSignature11111111111111111111")
check("different signature is fetched (cache is per-signature)",
      res_other.get("cached") is not True
      and len(method_calls(transport, "getTransaction"))
      == n_tx_before + 2)

res = wl.get_signatures_for_address(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", limit=2)
check("getSignaturesForAddress routes alchemy + replays fixture",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and len(res["result"]) == 2
      and res["result"][0]["signature"] == SIG)

# Paging helper, exercised against a stub router (no transport).
class StubRouter:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def call(self, method, params, priority=None, hints=None):
        self.calls.append((method, params))
        page = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]
        return dict(page)

    def route(self, *a, **k):
        return {"primary": "alchemy", "refused": False}

    def usage_summary(self):
        return {}

    def health_snapshot(self):
        return {}


stub = StubRouter([
    {"kind": "ok", "result": [{"signature": "s3"}, {"signature": "s2"}]},
    {"kind": "ok", "result": [{"signature": "s1"}]},
])
wl_stub = AW.AlchemyWorkload(router=stub)
sigs, npages = wl_stub.signatures_for_address_all("addr", limit=2,
                                                  max_pages=4)
check("signatures paging recovers across pages with before cursor",
      sigs == ["s3", "s2", "s1"] and npages == 2
      and stub.calls[1][1][1].get("before") == "s2")

# -- slot / block reads -------------------------------------------------
# Fresh quota bucket: the section above already spent the alchemy
# burst (10 tokens) — the scheduler refusing further calls here
# would be the scheduler working, not a workload failure. A fresh
# HOME gives this section its own bucket.
HOME2 = tempfile.mkdtemp(prefix="acq-phase5-slot-")
transport2 = RoutingTransport()
router2 = ProviderRouter(home=HOME2, helius_enabled=False,
                         transport=transport2)
wl2 = AW.AlchemyWorkload(router=router2)
res = wl2.get_slot()
check("getSlot routes alchemy + replays fixture slot",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and res["result"] == FIX_SLOT["result"] == 472499391)
res = wl2.get_block(472499391)
check("getBlock ok via alchemy",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and res["result"]["slot"] == 472499391)
res = wl2.get_latest_blockhash()
check("getLatestBlockhash ok via alchemy",
      res["kind"] == "ok" and res["provider"] == "alchemy"
      and "blockhash" in res["result"])
statuses = wl2.get_signature_statuses(["sig%03d" % i for i in range(300)])
check("getSignatureStatuses(300) chunked at 256 into 2 calls",
      len(statuses) == 2
      and len(method_calls(transport2, "getSignatureStatuses")) == 2
      and all(r["kind"] == "ok" for r in statuses))

# -- getTokenLargestAccounts: provisional flag on the result -----------
res = wl.get_token_largest_accounts(
    "So11111111111111111111111111111111111111112")
check("getTokenLargestAccounts result flagged provisional",
      res.get("provisional") is True
      and "UNVERIFIED" in (res.get("note") or ""))

# -- scheduler + usage tracking honoured on every call -----------------
quota_path = os.path.join(HOME, "acquisition_quota_alchemy.json")
check("scheduler quota state file exists for alchemy",
      os.path.exists(quota_path))
with open(quota_path) as fh:
    quota = json.load(fh)
check("scheduler recorded alchemy calls (tokens spent)",
      quota.get("calls", 0) > 0)
summary = wl.usage_summary()
check("usage tracker recorded alchemy account calls",
      summary.get("alchemy", {}).get("calls", 0) > 0)

# -- no second routing path in the workload module ---------------------
src = open(os.path.join(REPO, "daemon/intelligence/acquisition",
                        "alchemy_workload.py")).read()
check("workload calls only router.call (no direct rpc/adapter use)",
      "self.router.call" in src
      and "rpc_client.rpc(" not in src
      and "make_adapter(" not in src
      and "http_json_rpc" not in src
      and "WebSocketManager" not in src
      and "import websocket" not in src)
check("workload never imports the launch_scanner monolith",
      "from daemon.launch_scanner" not in src
      and "launch_scanner.reserves" not in src
      and "import reserves" not in src)

# ---------------------------------------------------------------------------
# WSS: narrow Alchemy subscriptions (fake socket, no network)
# ---------------------------------------------------------------------------
class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, msg):
        self.sent.append(msg)

    def run_forever(self):
        return None

    def close(self):
        self.closed = True


events = []
handshake_429s = []


def factory(url, on_message, on_error, on_close, on_open):
    ws = FakeWS()
    factory.last = ws
    factory.cbs = (on_message, on_error, on_close, on_open)
    return ws


subs = WM.AlchemySubscriptions(
    url="wss://example.invalid/v2/k", ws_factory=factory,
    on_handshake_429=lambda p: handshake_429s.append(p),
    on_event=lambda p, d: events.append(d))

# Unfiltered / out-of-scope attempts are REFUSED, nothing is sent.
for bad_call, name in [
        (lambda: subs.subscribe_logs([]), "empty mentions"),
        (lambda: subs.subscribe_logs(["SomeRandom11111111111111111111"]),
         "non-program mention"),
        (lambda: subs.subscribe_program("SomeRandom11111111111111111111"),
         "non-allowed program"),
        (lambda: subs.subscribe_account("acct1", "everything"),
         "unlabelled account kind"),
        (lambda: subs.subscribe_account("", "pool"), "empty pubkey"),
        (lambda: subs.subscribe_signature(""), "empty signature")]:
    try:
        bad_call()
        check("refused: %s" % name, False)
    except ValueError:
        check("refused: %s" % name, True)

# Narrow subscriptions only: pump logs, pumpswap program, one
# bonding-curve account, one signature, the slot feed.
id_logs = subs.subscribe_pump_logs()
id_prog = subs.subscribe_program(WM.PUMPSWAP_PROGRAM_ID)
id_acct = subs.subscribe_account("CurveAcct111111111111111111111111",
                                 "bonding_curve")
id_sig = subs.subscribe_signature(SIG)
id_slot = subs.subscribe_slot()
check("five narrow subscriptions registered",
      len(subs.live_subscriptions()) == 5)
check("account subscription carries its kind label",
      any(r.get("account_kind") == "bonding_curve"
          for r in subs.live_subscriptions()))
check("no unfiltered program subscription exists in the book",
      all(r["method"] != "programSubscribe"
          or r["params"][0] in WM.ALLOWED_PROGRAM_IDS
          for r in subs.live_subscriptions())
      and all(r["method"] != "logsSubscribe"
              or set(r["params"][0]["mentions"]) <= WM.ALLOWED_PROGRAM_IDS
              for r in subs.live_subscriptions()))

# Lifecycle: connect constructs the transport; nothing is sent until
# the handshake opens (D-02); on_open sends all five subscribes.
check("connect constructs the transport", subs.connect())
check("no subscribe sent before the handshake opens (D-02)",
      len(factory.last.sent) == 0
      and subs.manager.connected is False)
factory.cbs[3](factory.last)  # handshake completes -> on_open
check("connected only after open", subs.manager.connected is True)
check("all five subscribe requests sent exactly once after open",
      len(factory.last.sent) == 5
      and sum("Subscribe" in s for s in factory.last.sent) == 5)
check("pump logsSubscribe carries the mentions filter",
      any("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in s
          and "mentions" in s for s in factory.last.sent))

# Subscribe acks record server ids in the book.
on_message, on_error, on_close, on_open = factory.cbs
for local_id in (id_logs, id_prog, id_acct, id_sig, id_slot):
    on_message(None, json.dumps({"jsonrpc": "2.0", "id": local_id,
                                 "result": 100 + local_id}))
check("subscribe acks recorded server ids",
      all(r.get("server_id") is not None
          for r in subs.live_subscriptions())
      and all(r["status"] == "active"
              for r in subs.live_subscriptions()))

# A notification updates last-delivery bookkeeping for that sub.
on_message(None, json.dumps({
    "jsonrpc": "2.0", "method": "slotNotification",
    "params": {"subscription": 100 + id_slot,
               "result": {"slot": 472499500}}}))
slot_rec = [r for r in subs.live_subscriptions()
            if r["method"] == "slotSubscribe"][0]
check("slot notification updates last-delivery slot + count",
      slot_rec["last_msg_slot"] == 472499500
      and slot_rec["messages"] == 1)
check("notification delivered to caller callback",
      events and events[-1]["params"]["result"]["slot"] == 472499500)

# Handshake 429 counted SEPARATELY from RPC 429s.
on_error(None, Exception("429 Too Many Requests"))
check("handshake 429 counted on the subscriptions object",
      subs.wss_handshake_429s == 1 and handshake_429s == ["alchemy"])
check("handshake 429 does NOT touch any rpc counter (stats "
      "reports rpc_429s as None)",
      subs.stats()["rpc_429s"] is None
      and subs.stats()["wss_handshake_429s"] == 1)

# Drop + reconnect: backoff doubles, subs are re-issued on connect.
on_close(None)
w1 = subs.next_reconnect_wait_s()
check("reconnect backoff engaged after drop (4s after the 2s step)",
      w1 == 4.0)
check("reconnects counted", subs.reconnects >= 1)
sent_before = len(factory.last.sent)  # noqa: F841 (documents the count)
check("reconnect constructs a fresh transport, nothing sent yet",
      subs.connect() and len(factory.last.sent) == 0
      and subs.manager.connected is False)
factory.cbs[3](factory.last)  # handshake completes -> on_open
check("reconnect re-issues all wanted subscriptions on open",
      len(factory.last.sent) == 5)

# Unsubscribe removes the wanted sub and sends the unsubscribe.
subs.unsubscribe_program(WM.PUMPSWAP_PROGRAM_ID)
check("unsubscribe sends programUnsubscribe + drops the sub",
      any("programUnsubscribe" in s for s in factory.last.sent)
      and len(subs.live_subscriptions()) == 5  # book keeps history
      and len(subs.manager.wanted_subscriptions()) == 4)

# Staleness bookkeeping: with an injected clock, a quiet logs sub
# goes stale past its expected interval (300s).
clock = {"t": 1000.0}
subs2 = WM.AlchemySubscriptions(
    url="wss://example.invalid/v2/k", ws_factory=factory,
    now=lambda: clock["t"])
subs2.subscribe_pump_logs()
subs2.connect()
clock["t"] = 1000.0 + 301.0
stale = subs2.stale_subscriptions()
check("stale detection flags a quiet logs sub past 300s",
      len(stale) == 1 and stale[0]["status"] == "stale"
      and stale[0]["method"] == "logsSubscribe")

# Source-level guard: no unfiltered subscription path exists in the
# WSS module's Alchemy section — every subscribe goes through a
# guard that requires a filter / allowed program / kind.
wm_src = open(os.path.join(REPO, "daemon/intelligence/acquisition",
                           "websocket_manager.py")).read()
check("no generic unguarded subscribe passthrough in "
      "AlchemySubscriptions",
      "def subscribe(self, method, params)" not in
      wm_src.split("class AlchemySubscriptions")[1])

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
