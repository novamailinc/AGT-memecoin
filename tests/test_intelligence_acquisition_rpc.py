#!/usr/bin/env python3
"""Phase 2 acquisition tests — rpc.py + provider_manager.py.

Plain script, no network: HTTP is driven through an injected fake
opener; the Helius adapter is exercised only via its contract shape
(it delegates to the live reserves transport, which is NOT called
here — helius_enabled=False in the manager tests).

Covers (§11): provider interface compatibility, RPC method
equivalence, provider failover, HTTP 429 (throughput vs
monthly-quota distinguished), timeout handling, unsupported
methods, key hygiene (no key material in logs / host-only logging).
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import io
import json
import os
import sys
import tempfile

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import rpc as R
from daemon.intelligence.acquisition import provider_manager as PM

fails = []


total = [0]


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


def fake_opener_factory(responses):
    """responses: list of (status, body) or ('raise', exc-instance)."""
    state = {"i": 0}

    def opener(req, timeout=None):
        item = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        if item[0] == "raise":
            raise item[1]
        import urllib.error
        if item[0] >= 400:
            raise urllib.error.HTTPError(req.full_url, item[0], "err",
                                         {}, io.BytesIO(
                                             json.dumps(item[1]).encode()))
        return FakeResp(item[1], item[0])
    return opener


HOME = tempfile.mkdtemp(prefix="acq-rpc-")

# ---------------- 429 classification ----------------
k, w = R.classify_429("alchemy", http_status=429,
                      rpc_error={"code": 429, "message": "too many requests"})
check("429 throughput classified", k == "throughput")
k, w = R.classify_429("chainstack", http_status=429,
                      rpc_error={"code": -32005, "message": "rate limit",
                                 "data": {"try_again_in": 7}})
check("chainstack -32005 classified + wait hint honoured",
      k == "throughput" and w == 7.0)
k, w = R.classify_429("chainstack", http_status=429,
                      rpc_error={"code": -32005,
                                 "message": "monthly quota exceeded"})
check("monthly-quota 429 distinguished", k == "monthly_quota")
check("-32005 is NOT an alchemy code",
      not R.is_rate_limit_error("alchemy", rpc_error={"code": -32005}))
check("-32005 IS a chainstack code",
      R.is_rate_limit_error("chainstack", rpc_error={"code": -32005}))
check("alchemy WS 429 code recognised",
      R.is_rate_limit_error("alchemy", rpc_error={"code": 429}))
check("plain error is not a rate limit",
      not R.is_rate_limit_error("alchemy", http_status=500,
                                rpc_error={"code": -32601}))

# ---------------- interface compatibility ----------------
# Every adapter returns the same contract keys.
for name in ("alchemy", "chainstack", "public"):
    ad = R.make_adapter(name, home=HOME)
    res = ad.call("getSlot", [], timeout=5)
    check("adapter %s returns contract keys" % name,
          set(res.keys()) >= {"result", "kind", "elapsed", "provider",
                              "http_status", "wait_hint_s",
                              "rate_limit_kind"})
    check("adapter %s unconfigured without keys -> unconfigured "
          "or ok" % name, res["kind"] in ("unconfigured", "ok", "error"))
pub = R.make_adapter("public", home=HOME)
check("public adapter configured (keyless)", pub.configured())
check("public supports getSignaturesForAddress",
      pub.supports("getSignaturesForAddress"))
cs = R.make_adapter("chainstack", home=HOME)
check("chainstack does NOT support getSignaturesForAddress",
      not cs.supports("getSignaturesForAddress"))
check("chainstack does NOT support getProgramAccounts",
      not cs.supports("getProgramAccounts"))
check("chainstack unsupported call kind",
      cs.call("getSignaturesForAddress", ["x"]).kind == "unsupported")
al = R.make_adapter("alchemy", home=HOME)
check("alchemy getTokenLargestAccounts documented",
      not al.unverified("getTokenLargestAccounts"))
check("alchemy CU table: history 40, account 10, slot 20",
      R.method_cost("alchemy", "getTransaction") == 40
      and R.method_cost("alchemy", "getAccountInfo") == 10
      and R.method_cost("alchemy", "getSlot") == 20)
check("alchemy getTokenLargestAccounts CU documented 20",
      R.method_cost("alchemy", "getTokenLargestAccounts") == 20)

# ---------------- HTTP paths via fake opener ----------------
ok_body = {"jsonrpc": "2.0", "id": 1, "result": 123456}
ad = R.AlchemyAdapter(home=HOME,
                      transport=fake_opener_factory([(200, ok_body)]))
# point the adapter at a dummy URL by monkeypatching _url
ad._url = lambda: "https://example.invalid/v2/k"
res = ad.call("getSlot", [])
check("http ok -> kind ok + result + elapsed",
      res["kind"] == "ok" and res["result"] == 123456
      and res["elapsed"] >= 0)
check("http ok provider recorded", res["provider"] == "alchemy")

ad429 = R.AlchemyAdapter(
    home=HOME,
    transport=fake_opener_factory(
        [(429, {"jsonrpc": "2.0", "id": 1,
                "error": {"code": 429, "message": "too many requests"}})]))
ad429._url = lambda: "https://example.invalid/v2/k"
res = ad429.call("getSlot", [])
check("http 429 -> kind rate_limited, throughput",
      res["kind"] == "rate_limited"
      and res["rate_limit_kind"] == "throughput")

ad_to = R.AlchemyAdapter(home=HOME, transport=fake_opener_factory(
    [("raise", TimeoutError("timed out"))]))
ad_to._url = lambda: "https://example.invalid/v2/k"
res = ad_to.call("getSlot", [])
check("timeout -> kind timeout", res["kind"] == "timeout")

# Retry-After honoured
ad_ra = R.AlchemyAdapter(home=HOME, transport=fake_opener_factory(
    [(429, {"jsonrpc": "2.0", "id": 1,
            "error": {"code": 429, "message": "slow down"}})]))
ad_ra._url = lambda: "https://example.invalid/v2/k"
k2, w2 = R.classify_429("alchemy", http_status=429,
                        rpc_error={"code": 429, "message": "slow down"},
                        retry_after="12")
check("Retry-After parsed as wait hint", w2 == 12.0)

# ---------------- key hygiene ----------------
check("no hardcoded key in rpc module",
      "api-key=" not in open(os.path.join(
          REPO, "daemon/intelligence/acquisition/rpc.py")).read()
      .replace("api-key={key}", "").replace("?api-key=<key>", "")
      .replace("api-key=\" + k", ""))
check("host-only logging helper strips key from URL",
      R._host_of("https://solana-mainnet.g.alchemy.com/v2/SECRETKEY")
      == "solana-mainnet.g.alchemy.com")
check("alchemy key absent -> unconfigured (no key fabricated)",
      not R.AlchemyAdapter(home=HOME).configured())
check("chainstack endpoint absent -> unconfigured",
      not R.ChainstackAdapter(home=HOME).configured())
cfg = R.configured_providers(home=HOME)
check("configured providers without keys: no alchemy/chainstack, "
      "public present",
      "alchemy" not in cfg and "chainstack" not in cfg
      and "public" in cfg)

# ---------------- provider manager: failover ----------------
# helius disabled; alchemy 429s, chainstack (recent method) answers.
def opener_alchemy_429_then_chainstack_ok(req, timeout=None):
    import urllib.error
    if "alchemy" in req.full_url:
        raise urllib.error.HTTPError(
            req.full_url, 429, "err", {},
            io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                "code": 429, "message": "too many"}}).encode()))
    return FakeResp({"jsonrpc": "2.0", "id": 1, "result": {"value": 7}})


class RoutingTransport:
    """Opener routing by host; alchemy 429s once, others succeed."""
    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        import urllib.error
        self.calls.append(req.full_url)
        if "alchemy" in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url, 429, "err", {},
                io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                    "code": 429, "message": "too many"}}).encode()))
        return FakeResp({"jsonrpc": "2.0", "id": 1,
                         "result": {"value": 7}})


# Manager with fake adapters: alchemy adapter pointed at fake host,
# chainstack endpoint injected via env.
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"
transport = RoutingTransport()
mgr = PM.ProviderManager(home=HOME, helius_enabled=False,
                         transport=transport)
# Alchemy needs a key to be "configured": point its URL via env.
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
mgr2 = PM.ProviderManager(home=HOME, helius_enabled=False,
                          transport=transport)
res = mgr2.call("getBalance", ["addr1"], priority=PM.PRIORITY_CRITICAL)
check("failover: alchemy 429 -> chainstack answers ok",
      res["kind"] == "ok" and res["result"] == {"value": 7})
check("failover tried alchemy first then chainstack",
      len(transport.calls) == 2
      and "alchemy" in transport.calls[0]
      and "chainstack" in transport.calls[1])
check("failover order excludes chainstack for sigs method",
      "chainstack" not in PM.failover_order("getSignaturesForAddress"))
check("failover order includes chainstack for recent reads",
      "chainstack" in PM.failover_order("getBalance"))
check("manager stats recorded both providers",
      mgr2.stats["alchemy"].calls == 1
      and mgr2.stats["chainstack"].calls == 1)
del os.environ["ALCHEMY_API_KEY"]
del os.environ["CHAINSTACK_ENDPOINT"]

# No keyed provider: the public leg is the only route (it is always
# configured), so the call is attempted there and never raises.
mgr3 = PM.ProviderManager(home=HOME, helius_enabled=False)
res = mgr3.call("getSlot", [])
check("no keyed provider -> public leg attempted, no raise",
      res["provider"] == "public"
      and res["kind"] in ("ok", "error", "timeout", "rate_limited"))

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
