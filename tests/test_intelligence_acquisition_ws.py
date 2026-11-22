#!/usr/bin/env python3
"""Phase 2 acquisition tests — websocket_manager.py,
subscription_manager.py, event_backfill.py (WS side).

Plain script, no network: a fake WS transport is injected.
Covers (§11): WebSocket disconnections + reconnect backoff,
handshake-429 counted SEPARATELY from RPC 429s, duplicate events,
event ordering, missing-event detection.
"""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import websocket_manager as WM
from daemon.intelligence.acquisition import subscription_manager as SM
from daemon.intelligence.acquisition import event_backfill as EB
from daemon.intelligence.acquisition import health_monitor as HM

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


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


# ---------------- WS manager: subscribe / reconnect / 429 ---------
events = []
handshake_429s = []


def factory(url, on_message, on_error, on_close, on_open):
    ws = FakeWS()
    factory.last = ws
    factory.cbs = (on_message, on_error, on_close, on_open)
    return ws


mgr = WM.WebSocketManager("alchemy", "wss://example.invalid/v2/k",
                          ws_factory=factory,
                          on_handshake_429=lambda p: handshake_429s.append(p),
                          on_event=lambda p, d: events.append(d))
local_id = mgr.subscribe("accountSubscribe", ["vault1", {"encoding": "jsonParsed"}])
check("subscribe returns a local id", isinstance(local_id, int))
check("duplicate subscribe deduped to one wanted sub",
      mgr.subscribe("accountSubscribe", ["vault1", {"encoding": "jsonParsed"}])
      == local_id and len(mgr.wanted_subscriptions()) == 1)
try:
    mgr.subscribe("notASubscribe", [])
    check("invalid subscription method rejected", False)
except ValueError:
    check("invalid subscription method rejected", True)

check("connect constructs the transport", mgr.connect())
check("no subscribe sent before the handshake opens (D-02)",
      len(factory.last.sent) == 0 and mgr.connected is False)
factory.cbs[3](factory.last)  # handshake completes -> on_open
check("connected only after open", mgr.connected is True)
check("subscribe request sent exactly once after open",
      len(factory.last.sent) == 1
      and "accountSubscribe" in factory.last.sent[0])
check("backoff resets on open", mgr.next_reconnect_wait_s() == 2.0)

# handshake 429 counted separately
mgr._on_error(None, Exception("429 Too Many Requests"))
check("handshake 429 counted on the manager",
      mgr.wss_handshake_429s == 1 and handshake_429s == ["alchemy"])
check("handshake 429 does NOT touch any rpc counter (manager has none)",
      not hasattr(mgr, "rpc_429s"))

# disconnect -> reconnect backoff doubles, capped at 60s
# (_on_close already consumed the 2s step, so the next waits are 4, 8, ...)
mgr._on_close(None)
w1 = mgr._schedule_reconnect()
w2 = mgr._schedule_reconnect()
check("reconnect backoff doubles (4 -> 8)", w1 == 4.0 and w2 == 8.0)
for _ in range(10):
    w = mgr._schedule_reconnect()
check("reconnect backoff caps at 60s", w == 60.0)

# message handling: subscribe ack records server id; events delivered
mgr2 = WM.WebSocketManager("alchemy", "wss://example.invalid/v2/k",
                           ws_factory=factory,
                           on_event=lambda p, d: events.append(d))
mgr2.subscribe("slotSubscribe", [])
mgr2.connect()
check("mgr2 sends nothing before open",
      len(factory.last.sent) == 0 and mgr2.connected is False)
factory.cbs[3](factory.last)  # handshake completes -> on_open
check("mgr2 sends its subscribe once after open",
      len(factory.last.sent) == 1)
mgr2._on_message(None, '{"jsonrpc":"2.0","id":1,"result":42}')
check("subscribe ack recorded server id",
      mgr2.wanted_subscriptions()[0].get("server_id") == 42)
mgr2._on_message(None, '{"jsonrpc":"2.0","method":"slotNotification",'
                       '"params":{"result":{"slot":99}}}')
check("event delivered to callback",
      events and events[-1]["params"]["result"]["slot"] == 99)
check("malformed message ignored, no raise",
      mgr2._on_message(None, "not json") is None)

# ---------------- subscription manager ------------------------------
sm = SM.SubscriptionManager(now=lambda: 1000.0)
rec = sm.register("alchemy", "accountSubscribe", ["vault1"])
check("register creates wanted record", rec["status"] == "wanted")
sm.mark_subscribed("accountSubscribe", ["vault1"], server_id=7)
check("mark_subscribed -> active + server id",
      sm.all()[0]["status"] == "active"
      and sm.all()[0]["server_id"] == 7)
sm.mark_message("accountSubscribe", ["vault1"], slot=500)
check("mark_message updates slot + count",
      sm.all()[0]["last_msg_slot"] == 500
      and sm.all()[0]["messages"] == 1)
stale = sm.stale(now=1000.0 + 121.0)
check("stale after expected interval (120s)",
      len(stale) == 1 and stale[0]["status"] == "stale")
disconnected = sm.mark_disconnected("alchemy")
check("disconnected marks actives only (stale untouched)",
      disconnected == [])
sm2 = SM.SubscriptionManager(now=lambda: 2000.0)
sm2.register("alchemy", "slotSubscribe", [])
sm2.mark_subscribed("slotSubscribe", [], server_id=3)
gone = sm2.mark_disconnected("alchemy")
check("mark_disconnected flags active subs for re-issue + backfill",
      len(gone) == 1 and gone[0]["status"] == "disconnected")

# ---------------- gap detection + ordering --------------------------
gd = EB.GapDetector(tolerated_jump=10)
check("first observation never reports a gap",
      gd.observe("slot", 1000) is None)
check("small jump is not a gap", gd.observe("slot", 1005) is None)
gap = gd.observe("slot", 1050)
check("jump > tolerance reports a gap with slots",
      gap is not None and gap["missed_slots"] == 45
      and gap["from_slot"] == 1005 and gap["to_slot"] == 1050)
check("in-order slots produce no gap",
      gd.observe("slot", 1051) is None
      and gd.observe("slot", 1052) is None)
gap2 = gd.gap_for_disconnect("slot", disconnected_s=120.0)
check("120s disconnect is a gap (~300 slots)",
      gap2 is not None and gap2["missed_slots"] >= 300)
check("2s disconnect is within tolerance (no gap)",
      gd.gap_for_disconnect("slot", disconnected_s=2.0) is None)

# ---------------- dedupe + backfill ---------------------------------
dedup = EB.EventDeduplicator(capacity=3)
check("new signature accepted", dedup.add("sigA"))
check("duplicate signature rejected", not dedup.add("sigA"))
dedup.add("sigB")
dedup.add("sigC")
dedup.add("sigD")  # evicts sigA (capacity 3)
check("bounded dedup evicts oldest", dedup.add("sigA"))

pages = [
    {"result": [{"signature": "s3"}, {"signature": "s2"}], "kind": "ok"},
    {"result": [{"signature": "s1"}], "kind": "ok"},
]
calls = {"n": 0}


def fake_rpc(method, params):
    calls["n"] += 1
    assert method == "getSignaturesForAddress"
    assert params[1]["limit"] == 2  # bounded page size honoured
    return pages[min(calls["n"] - 1, 1)]


# page 1 returns 2 rows == limit, so a second page is fetched; page 2
# returns 1 row < limit, so paging stops.
calls["n"] = 0
sigs, npages = EB.backfill_signatures("addr", fake_rpc, limit=2,
                                      max_pages=4)
check("backfill recovers signatures across pages",
      sigs == ["s3", "s2", "s1"] and npages == 2)
# second run: dedup drops everything already seen
calls["n"] = 0
sigs2, _ = EB.backfill_signatures("addr", fake_rpc,
                                  dedup=EB.EventDeduplicator(), limit=2)
check("backfill without dedup re-reads (caller owns dedup)",
      sigs2 == ["s3", "s2", "s1"])
dedup2 = EB.EventDeduplicator()
calls["n"] = 0
EB.backfill_signatures("addr", fake_rpc, dedup=dedup2, limit=2)
calls["n"] = 0
sigs3, _ = EB.backfill_signatures("addr", fake_rpc, dedup=dedup2, limit=2)
check("backfill with dedup drops already-seen signatures",
      sigs3 == [])


def failing_rpc(method, params):
    return {"result": None, "kind": "error", "elapsed": 0.1}


sigs4, npages4 = EB.backfill_signatures("addr", failing_rpc)
check("backfill stops cleanly on provider error",
      sigs4 == [] and npages4 == 1)

# ---------------- health: WSS 429 separate from RPC 429 ------------
mon = HM.HealthMonitor(home="/tmp")
mon.set_configured("alchemy", True)
mon.record_wss_handshake_429("alchemy")
mon.record_wss_handshake_429("alchemy")
mon.record_rpc({"provider": "alchemy", "kind": "rate_limited",
                "elapsed": 0.5, "rate_limit_kind": "throughput"})
snap = mon.snapshot()["alchemy"]
check("WSS handshake 429s counted separately from RPC 429s",
      snap["wss_handshake_429s"] == 2 and snap["rpc_429s"] == 1)
check("any 429 degrades health status", snap["status"] == "degraded")

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
