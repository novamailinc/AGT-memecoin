#!/usr/bin/env python3
"""Phase 3 tests — standardized provider adapters (providers/*).

Plain script, no network: HTTP is driven through an injected fake
opener; the Helius adapter is exercised via a stubbed reserves
module (sys.modules) so no live transport is touched, plus one
live-gated check in test_intelligence_router_live_helius.py.

Covers: one interface for all adapters (same contract keys),
capability matching from provider_capabilities.py (Chainstack
free-tier refusals; Alchemy UNVERIFIED methods; costs from
METHOD_COSTS), 429 classification (throughput vs monthly-quota,
Chainstack -32005 + try_again_in), timeout, unsupported methods
(zero HTTP calls), key hygiene (no key material in source or logs),
WSS URL resolution, configured_providers.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import io
import json
import os
import sys
import tempfile
import types

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import provider_capabilities as CAPS
from daemon.intelligence.acquisition import rpc_client as RC
from daemon.intelligence.acquisition.providers import base as B
from daemon.intelligence.acquisition.providers import make_adapter

fails = []
total = [0]
HOME = tempfile.mkdtemp(prefix="acq_adapters_")


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
    state = {"i": 0, "calls": 0}

    def opener(req, timeout=None):
        state["calls"] += 1
        item = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        if item[0] == "raise":
            raise item[1]
        status, body = item
        if status >= 400:
            import urllib.error
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {},
                io.BytesIO(json.dumps(body).encode()))
        return FakeResp(body, status)

    opener.state = state
    return opener


# ---------------- interface: one contract for all adapters ----------------
for name in ("helius", "alchemy", "chainstack", "public"):
    ad = make_adapter(name, home=HOME)
    check("adapter %s has the standard interface" % name,
          all(hasattr(ad, m) for m in (
              "name", "account", "configured", "supports",
              "unverified", "cost", "call", "wss_url"))
          and ad.name == name)
    check("adapter %s account is a known capability account" % name,
          ad.account in CAPS.ACCOUNTS)

check("helius adapter bills to helius_main (single account, no "
      "rotation)", make_adapter("helius", home=HOME).account
      == "helius_main")
check("helius_discovery is never an adapter account (research-only, "
      "never routed for entry/exit)",
      "helius_discovery" not in
      {a.account for a in (make_adapter(n, home=HOME)
                           for n in RC.PROVIDERS)})

# ---------------- capability matching (from provider_capabilities) ----------------
cs = make_adapter("chainstack", home=HOME)
check("chainstack does NOT support getSignaturesForAddress",
      not cs.supports("getSignaturesForAddress"))
check("chainstack does NOT support getProgramAccounts",
      not cs.supports("getProgramAccounts"))
check("chainstack does NOT support getTokenLargestAccounts",
      not cs.supports("getTokenLargestAccounts"))
check("chainstack DOES support recent getTransaction",
      cs.supports("getTransaction"))
# (real zero-call check: unsupported refuses before transport)
op0 = fake_opener_factory([(200, {"result": []})])
cs0 = make_adapter("chainstack", home=HOME, transport=op0)
check("chainstack unsupported call makes zero HTTP calls",
      cs0.call("getSignaturesForAddress", ["x"]).kind == "unsupported"
      and op0.state["calls"] == 0)

al = make_adapter("alchemy", home=HOME)
check("alchemy getTokenLargestAccounts officially supported",
      not al.unverified("getTokenLargestAccounts"))
check("alchemy getTokenSupply officially supported",
      not al.unverified("getTokenSupply"))
check("alchemy getTransaction NOT flagged unverified",
      not al.unverified("getTransaction"))
pub = make_adapter("public", home=HOME)
check("public supports getSignaturesForAddress",
      pub.supports("getSignaturesForAddress"))
check("public configured (keyless)", pub.configured())
check("alchemy unconfigured without a key", not al.configured())
check("chainstack unconfigured without an endpoint",
      not cs.configured())

# ---------------- costs from METHOD_COSTS (single source) ----------------
check("costs come from provider_capabilities.METHOD_COSTS",
      RC.method_cost("alchemy", "getTransaction") == 40
      and RC.method_cost("alchemy", "getAccountInfo") == 10
      and RC.method_cost("alchemy", "getSlot") == 20
      and RC.method_cost("alchemy", "getTokenLargestAccounts") == 20
      and RC.method_cost("chainstack", "getSignaturesForAddress") == 2
      and RC.method_cost("chainstack", "getProgramAccounts") is None
      and RC.method_cost("helius", "getTransaction") == 10
      and RC.method_cost("public", "getSlot") is None)
check("ALCHEMY_CU / CHAINSTACK_RU derived from METHOD_COSTS",
      RC.ALCHEMY_CU["getTransaction"] == 40
      and RC.CHAINSTACK_RU["getSignaturesForAddress"] == 2
      and RC.CHAINSTACK_RU["getProgramAccounts"] is None)
check("adapter.cost() agrees with method_cost()",
      al.cost("getTransaction") == 40
      and cs.cost("getTransaction") == 1
      and pub.cost("getTransaction") is None)

# ---------------- HTTP paths via fake opener ----------------
ok_body = {"jsonrpc": "2.0", "id": 1, "result": 123456}
op = fake_opener_factory([(200, ok_body)])
ad = RC.AlchemyAdapter(home=HOME, transport=op)
ad._url = lambda: "https://example.invalid/v2/k"
res = ad.call("getSlot", [])
check("http ok -> kind ok + result + elapsed + contract keys",
      res["kind"] == "ok" and res["result"] == 123456
      and res["elapsed"] >= 0 and res["provider"] == "alchemy"
      and set(res.keys()) >= {"result", "kind", "elapsed", "provider",
                              "http_status", "wait_hint_s",
                              "rate_limit_kind"})
op2 = fake_opener_factory([(200, {"jsonrpc": "2.0", "id": 1,
                                  "result": []})])
ad2 = RC.AlchemyAdapter(home=HOME, transport=op2)
ad2._url = lambda: "https://example.invalid/v2/k"
res2 = ad2.call("getTokenLargestAccounts", ["x"])
check("documented Alchemy method is not provisional",
      res2["kind"] == "ok" and res2.get("note") is None)

op429 = fake_opener_factory([(429, {"jsonrpc": "2.0", "id": 1, "error": {
    "code": -32005, "message": "slow down",
    "data": {"try_again_in": 7}}})])
ad429 = RC.ChainstackAdapter(home=HOME, transport=op429)
ad429._url = lambda: "https://example.invalid/abc"
res429 = ad429.call("getSlot", [])
check("chainstack -32005 -> rate_limited, wait hint from try_again_in",
      res429["kind"] == "rate_limited"
      and res429["rate_limit_kind"] == "throughput"
      and res429["wait_hint_s"] == 7.0)

op_mq = fake_opener_factory([(429, {"jsonrpc": "2.0", "id": 1, "error": {
    "code": 429, "message": "monthly quota exceeded, please upgrade"}})])
ad_mq = RC.AlchemyAdapter(home=HOME, transport=op_mq)
ad_mq._url = lambda: "https://example.invalid/v2/k"
res_mq = ad_mq.call("getSlot", [])
check("monthly-quota 429 classified monthly_quota (not throughput)",
      res_mq["kind"] == "rate_limited"
      and res_mq["rate_limit_kind"] == "monthly_quota")

op_to = fake_opener_factory([("raise", TimeoutError("timed out"))])
ad_to = RC.AlchemyAdapter(home=HOME, transport=op_to)
ad_to._url = lambda: "https://example.invalid/v2/k"
check("timeout -> kind timeout",
      ad_to.call("getSlot", []).kind == "timeout")

k2, w2 = RC.classify_429("alchemy", http_status=429,
                         rpc_error={"code": 429, "message": "slow down"},
                         retry_after="12")
check("Retry-After parsed as wait hint", w2 == 12.0)
check("429 classification: -32005 is Chainstack-only",
      not RC.is_rate_limit_error("alchemy", rpc_error={"code": -32005})
      and RC.is_rate_limit_error("chainstack",
                                 rpc_error={"code": -32005}))

# ---------------- key hygiene ----------------
for fname in ("providers/base.py", "providers/alchemy.py",
              "providers/chainstack.py", "providers/helius.py",
              "providers/public.py", "rpc_client.py"):
    src = open(os.path.join(REPO, "daemon/intelligence/acquisition",
                            fname)).read()
    check("no key material in %s" % fname,
          "api-key=" not in src.replace("api-key={key}", "")
          .replace("?api-key=<key>", "").replace("api-key=\" + k", "")
          and "SECRETKEY" not in src and "testkey" not in src)
check("host-only logging helper strips key from URL",
      B._host_of("https://solana-mainnet.g.alchemy.com/v2/SECRETKEY")
      == "solana-mainnet.g.alchemy.com")
cfg = RC.configured_providers(home=HOME)
check("configured providers without keys: no alchemy/chainstack, "
      "public present (helius depends on the disk keys, not this test)",
      "alchemy" not in cfg and "chainstack" not in cfg
      and "public" in cfg)

# ---------------- WSS URLs ----------------
check("public wss url is the keyless endpoint",
      RC.wss_url("public", home=HOME)
      == "wss://api.mainnet-beta.solana.com")
check("helius wss url is None from this layer (live transport "
      "owns the keys)", RC.wss_url("helius", home=HOME) is None)
check("alchemy wss url None without a key",
      RC.wss_url("alchemy", home=HOME) is None)
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"
check("alchemy wss url resolves with a key",
      (RC.wss_url("alchemy", home=HOME) or "").startswith(
          "wss://solana-mainnet.streaming.alchemy.com/v2/"))
check("chainstack wss url reuses the endpoint host",
      (RC.wss_url("chainstack", home=HOME) or "").startswith(
          "wss://chainstack.example.invalid/"))
del os.environ["ALCHEMY_API_KEY"]
del os.environ["CHAINSTACK_ENDPOINT"]

# ---------------- Chainstack endpoint from disk api_key (fallback) -----
# First live shadow comparison §7: the adapter wanted
# CHAINSTACK_ENDPOINT env or an endpoint file; the VM fix created
# chainstack/api_key instead, leaving the adapter unconfigured.
# Resolution order: env > endpoint file > api_key file.
_cs_dir = os.path.join(HOME, ".forex-agent", "chainstack")
os.makedirs(_cs_dir, exist_ok=True)
with open(os.path.join(_cs_dir, "api_key"), "w") as fh:
    fh.write("testnodekey-not-real\n")
check("chainstack endpoint built from disk api_key alone",
      B.chainstack_endpoint(home=HOME)
      == "https://solana-mainnet.core.chainstack.com/testnodekey-not-real")
check("chainstack configured from disk api_key alone",
      make_adapter("chainstack", home=HOME).configured())
check("chainstack wss url built from disk api_key alone",
      (RC.wss_url("chainstack", home=HOME) or "").startswith(
          "wss://solana-mainnet.core.chainstack.com/"))
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"
check("chainstack env endpoint still wins over api_key file",
      B.chainstack_endpoint(home=HOME)
      == "https://chainstack.example.invalid/abc")
del os.environ["CHAINSTACK_ENDPOINT"]
with open(os.path.join(_cs_dir, "endpoint"), "w") as fh:
    fh.write("https://chainstack.example.invalid/xyz")
check("chainstack endpoint file still wins over api_key file",
      B.chainstack_endpoint(home=HOME)
      == "https://chainstack.example.invalid/xyz")
os.remove(os.path.join(_cs_dir, "endpoint"))
os.remove(os.path.join(_cs_dir, "api_key"))
check("chainstack unconfigured again once api_key file removed",
      not make_adapter("chainstack", home=HOME).configured())
_fx_dir = os.path.join(HOME, "chainstack")
os.makedirs(_fx_dir, exist_ok=True)
with open(os.path.join(_fx_dir, "api_key"), "w") as fh:
    fh.write("testnodekey-not-real\n")
check("chainstack endpoint from api_key under FOREX_AGENT_HOME home",
      B.chainstack_endpoint(home=HOME)
      == "https://solana-mainnet.core.chainstack.com/testnodekey-not-real")
os.remove(os.path.join(_fx_dir, "api_key"))
check("chainstack unconfigured after FOREX_AGENT_HOME key removed",
      not make_adapter("chainstack", home=HOME).configured())

# ---------------- Helius adapter via stubbed reserves (LAST) ----------------
saved_pkg = sys.modules.get("daemon.launch_scanner")
saved_res = sys.modules.get("daemon.launch_scanner.reserves")
try:
    fake_reserves = types.ModuleType("daemon.launch_scanner.reserves")
    fake_reserves.rpc = lambda method, params, timeout=12: {"slot": 999}
    fake_reserves.helius_rpc_typed = lambda method, params, timeout=12: (
        {"slot": 999}, "ok", 1)
    fake_reserves.rpc_url_pairs = lambda: [("https://mainnet.helius-rpc.com",
                                            "k")]
    fake_pkg = types.ModuleType("daemon.launch_scanner")
    fake_pkg.__path__ = []
    sys.modules["daemon.launch_scanner"] = fake_pkg
    sys.modules["daemon.launch_scanner.reserves"] = fake_reserves

    had = make_adapter("helius", home=HOME)
    check("helius adapter configured when reserves holds keys",
          had.configured())
    res_h = had.call("getSlot", [])
    check("helius adapter delegates to reserves.helius_rpc_typed (stubbed)",
          res_h["kind"] == "ok" and res_h["result"] == {"slot": 999}
          and res_h["provider"] == "helius")

    fake_reserves.helius_rpc_typed = lambda method, params, timeout=12: (
        None, "null", 1)
    res_null = had.call("getSlot", [])
    check("helius adapter: null answer -> kind ok, null payload "
          "(an answer, not an outage; 2026-11-22 null-vs-error)",
          res_null["kind"] == "ok" and res_null["result"] is None)

    fake_reserves.helius_rpc_typed = lambda method, params, timeout=12: (
        None, "error", 1)
    res_none = had.call("getSlot", [])
    check("helius adapter: transport failure -> kind error",
          res_none["kind"] == "error")

    def _boom(method, params, timeout=12):
        raise RuntimeError("boom")
    fake_reserves.helius_rpc_typed = _boom
    check("helius adapter: reserves raising -> kind error, no raise",
          had.call("getSlot", []).kind == "error")

    fake_reserves.rpc_url_pairs = lambda: []
    check("helius adapter unconfigured when reserves holds no keys",
          not had.configured())
finally:
    if saved_pkg is None:
        sys.modules.pop("daemon.launch_scanner", None)
    else:
        sys.modules["daemon.launch_scanner"] = saved_pkg
    if saved_res is None:
        sys.modules.pop("daemon.launch_scanner.reserves", None)
    else:
        sys.modules["daemon.launch_scanner.reserves"] = saved_res

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
