#!/usr/bin/env python3
"""Phase 2 regression tests — WebSocket lifecycle (D-02).

The defect: WebSocketManager.connect() and
HeliusParsedStreamClient.connect() set ``connected = True`` right
after the factory returned and sent subscriptions on a socket whose
handshake had never run. The sends failed silently (caught and
logged) and the client reported healthy with nothing subscribed.

The fix: ``connected`` becomes True ONLY in the ``on_open``
callback, and subscriptions are sent (and re-sent on reconnect)
ONLY from ``on_open``. ``connect()`` merely constructs the
transport.

This suite pins that contract on both clients plus the
AlchemySubscriptions wrapper, with a fake socket (no network):
  * no subscribe is sent before open, and the client is not
    connected before open;
  * a subscribe is sent exactly once after open;
  * a reconnect re-issues the wanted subscription exactly once;
  * a stop that lands before the open leaves the client
    disconnected (no late connect, no late send).

Plain script — run directly with the venv python, not pytest.
"""
import json
import os
import sys
import tempfile

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, REPO)

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


from daemon.intelligence.acquisition import websocket_manager as WM
from daemon.intelligence.acquisition import helius_streams as HS
from daemon.intelligence.acquisition import provider_router as PR


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


def make_factory():
    def factory(url, on_message, on_error, on_close, on_open):
        ws = FakeWS()
        factory.last = ws
        factory.cbs = (on_message, on_error, on_close, on_open)
        return ws
    return factory


# ---------------- WebSocketManager ----------------
factory = make_factory()
mgr = WM.WebSocketManager("alchemy", "wss://example.invalid/v2/k",
                          ws_factory=factory)
mgr.subscribe("slotSubscribe", [])
check("manager: connect constructs the transport",
      mgr.connect() is True)
check("manager: not connected before open",
      mgr.connected is False)
check("manager: no subscribe sent before open (D-02)",
      factory.last.sent == [])
factory.cbs[3](factory.last)  # handshake completes -> on_open
check("manager: connected after open", mgr.connected is True)
check("manager: subscribe sent exactly once after open",
      len(factory.last.sent) == 1
      and json.loads(factory.last.sent[0])["method"] == "slotSubscribe")
check("manager: connect timestamp set by open, not by construct",
      mgr.last_connect_ts is not None)

# A subscription registered while disconnected is sent on the NEXT
# open, not before it.
mgr.subscribe("accountSubscribe", ["vault1", {"encoding": "jsonParsed"}])
check("manager: late subscribe sent at once while connected",
      len(factory.last.sent) == 2)

# Reconnect: close, then a fresh transport; nothing is sent until
# the new handshake opens, then the wanted subs are re-issued
# exactly once each.
mgr._on_close(None)
check("manager: disconnected after close", mgr.connected is False)
check("manager: reconnect constructs the transport",
      mgr.connect() is True)
check("manager: nothing re-sent before the reconnect opens",
      len(factory.last.sent) == 0 and mgr.connected is False)
factory.cbs[3](factory.last)
check("manager: reconnect re-issues each wanted sub exactly once",
      len(factory.last.sent) == 2
      and sorted(json.loads(s)["method"] for s in factory.last.sent)
      == ["accountSubscribe", "slotSubscribe"])

# Stop before open: a late on_open must not connect or send.
factory2 = make_factory()
mgr2 = WM.WebSocketManager("alchemy", "wss://example.invalid/v2/k",
                           ws_factory=factory2)
mgr2.subscribe("slotSubscribe", [])
mgr2.connect()
mgr2.stop()
factory2.cbs[3](factory2.last)
check("manager: stop-before-open stays disconnected, sends nothing",
      mgr2.connected is False and factory2.last.sent == [])

# ---------------- HeliusParsedStreamClient ----------------
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"
HOME = tempfile.mkdtemp(prefix="ws_lifecycle_")
router = PR.ProviderRouter(home=HOME, helius_enabled=True)

factory3 = make_factory()
stream = HS.HeliusParsedStreamClient(
    wss_url="wss://fs-beta.helius-rpc.com/?api-key=testkey-not-real",
    router=router, ws_factory=factory3,
    on_event=lambda ev: None)
ok, note = stream.subscribe(HS.pumpfun_filter(), "pumpfun")
check("stream: subscribe accepted under live router", ok is True)
check("stream: connect constructs the transport",
      stream.connect() is True)
check("stream: not connected before open", stream.connected is False)
check("stream: no subscribe sent before open (D-02)",
      factory3.last.sent == [])
factory3.cbs[3](factory3.last)
check("stream: connected after open", stream.connected is True)
check("stream: subscribe sent exactly once after open",
      len(factory3.last.sent) == 1
      and json.loads(factory3.last.sent[0])["method"]
      == HS.METHOD_SUBSCRIBE)

stream._on_close(None)
check("stream: disconnected after close", stream.connected is False)
check("stream: reconnect constructs the transport",
      stream.connect() is True)
check("stream: nothing re-sent before the reconnect opens",
      len(factory3.last.sent) == 0 and stream.connected is False)
factory3.cbs[3](factory3.last)
check("stream: reconnect re-issues the wanted subscription once",
      len(factory3.last.sent) == 1
      and json.loads(factory3.last.sent[0])["method"]
      == HS.METHOD_SUBSCRIBE)

# ---------------- AlchemySubscriptions (manager wrapper) ----------------
factory4 = make_factory()
subs = WM.AlchemySubscriptions(
    url="wss://example.invalid/v2/k", ws_factory=factory4)
subs.subscribe_pump_logs()
subs.subscribe_slot()
check("alchemy subs: connect constructs the transport",
      subs.connect() is True)
check("alchemy subs: nothing sent before open (D-02)",
      len(factory4.last.sent) == 0
      and subs.manager.connected is False)
factory4.cbs[3](factory4.last)
check("alchemy subs: both subscribes sent exactly once after open",
      len(factory4.last.sent) == 2
      and sum("Subscribe" in s for s in factory4.last.sent) == 2)

del os.environ["ALCHEMY_API_KEY"]
del os.environ["CHAINSTACK_ENDPOINT"]

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
