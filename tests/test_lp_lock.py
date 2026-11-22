#!/usr/bin/env python3
"""LP-lock badge tests (no network: _v2_burned_share is monkeypatched).

Covers daemon/launch_scanner/lp_lock.py classification + badge rendering,
the card_design lp_lock line, and the base/eth feed wiring (pair extraction
+ badge pass-through).
"""
import os
import sys

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))

import lp_lock
from card_design import build_card

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


PAIR = "0x" + "ab" * 20  # 42-char checksummed-shape address

# ---- badge rendering ----
check("badge locked", lp_lock.badge({"status": "locked",
                                     "detail": "LP burned 96%"}).startswith("🔒"))
check("badge unverified", lp_lock.badge({"status": "unverified",
                                         "detail": "x"}).startswith("⚠️"))
check("badge na", lp_lock.badge({"status": "na",
                                 "detail": "y"}).startswith("➖"))
check("badge empty report", lp_lock.badge({}).startswith("⚠️"))

# ---- classification with mocked RPC ----
_orig = lp_lock._v2_burned_share
lp_lock._v2_burned_share = lambda c, p, t: 0.96
r = lp_lock.check_lp_lock("base", "uniswap-v2-base", PAIR)
check("v2 96% burned -> locked",
      r["status"] == "locked" and "96%" in r["detail"])

lp_lock._v2_burned_share = lambda c, p, t: 0.0
r = lp_lock.check_lp_lock("base", "sushiswap", PAIR)
check("v2 0% burned -> unverified (not 'unlocked')",
      r["status"] == "unverified" and "unlocked" not in r["detail"].lower())

lp_lock._v2_burned_share = lambda c, p, t: None
r = lp_lock.check_lp_lock("ethereum", "uniswap_v2", PAIR)
check("v2 RPC failure -> unverified (fail-soft)",
      r["status"] == "unverified")

called = []
lp_lock._v2_burned_share = lambda c, p, t: called.append(1) or 0.99
r = lp_lock.check_lp_lock("base", "uniswap-v4-base", PAIR)
check("v4 -> na without RPC call",
      r["status"] == "na" and not called)
r = lp_lock.check_lp_lock("base", "aerodrome-slipstream-3", PAIR)
check("aerodrome CL -> na", r["status"] == "na")
lp_lock._v2_burned_share = _orig

r = lp_lock.check_lp_lock("base", "uniswap-v2-base", "not-an-address")
check("bad pair address -> unverified", r["status"] == "unverified")
r = lp_lock.check_lp_lock("base", "uniswap-v2-base", "")
check("empty pair address -> unverified", r["status"] == "unverified")

# never raises, even on garbage input
try:
    lp_lock.check_lp_lock(None, None, None)
    lp_lock.badge(None)
    check("garbage input never raises", True)
except Exception:
    check("garbage input never raises", False)

# ---- card_design wiring ----
card = build_card(symbol="T", score=80, mint="0x" + "c" * 40,
                  safety="clean", lp_lock="🔒 LP burned 96%",
                  chain="base", source="base-runner")
check("card renders LP badge line", "🔒 LP burned 96%" in card)
card2 = build_card(symbol="T", score=80, mint="0x" + "c" * 40,
                   safety="clean", chain="solana", source="degen-feed")
check("card omits LP line when empty",
      "LP burned" not in card2 and "LP lock" not in card2
      and "LP n/a" not in card2)

# ---- feed wiring: pair extraction + badge pass-through ----
for script, net in (("scripts/base-runner-feed", "base"),
                    ("scripts/eth-runner-feed", "eth")):
    path = os.path.join(REPO, script)
    src = open(path).read()
    ns = {"__name__": script.replace("-", "_") + "_test", "__file__": path}
    exec(compile(src.split('if __name__ == "__main__":')[0],
                 script, "exec"), ns)
    check("%s parses pair address" % net,
          '"pair"' in src and 'a.get("address"' in src)
    check("%s imports lp_lock guarded" % net, "_HAS_LPLOCK" in src)
    check("%s passes badge to build_text" % net,
          "lp_badge" in src and "lp_lock=lp_badge" in src)

# ---- token_supply ----
check("fmt 1.5T", lp_lock._fmt_supply(1_500_000_000_000) == "1.5T")
check("fmt 969.5M", lp_lock._fmt_supply(969_499_855) == "969.5M")
check("fmt 1.0B", lp_lock._fmt_supply(1_000_000_000) == "1.0B")
check("fmt 5k", lp_lock._fmt_supply(5000) == "5.0k")
check("fmt garbage", lp_lock._fmt_supply("nope") == "")

_orig_post = lp_lock._post_json
lp_lock._post_json = lambda url, payload, timeout: {
    "result": {"value": {"amount": "1000000000000000", "decimals": 6}}}
check("solana supply mocked",
      lp_lock.token_supply("solana", "SoLaNaMiNt") == "1.0B")
lp_lock._post_json = lambda url, payload, timeout: None
check("solana RPC down -> empty (fail-soft)",
      lp_lock.token_supply("solana", "SoLaNaMiNt") == "")
lp_lock._post_json = _orig_post

_orig_call = lp_lock._eth_call
lp_lock._eth_call = lambda rpc, to, data, t: (
    2_000_000_000 * 10 ** 18 if data == lp_lock._TOTAL_SUPPLY else 18)
check("evm supply mocked",
      lp_lock.token_supply("base", PAIR) == "2.0B")
lp_lock._eth_call = lambda rpc, to, data, t: None
check("evm RPC down -> empty (fail-soft)",
      lp_lock.token_supply("base", PAIR) == "")
lp_lock._eth_call = _orig_call
check("evm bad mint -> empty",
      lp_lock.token_supply("base", "not-an-address") == "")

# ---- card supply line ----
card = build_card(symbol="T", score=80, mint="0x" + "c" * 40,
                  safety="clean", supply="1.0B",
                  chain="base", source="base-runner")
check("card renders Supply line", "Supply 1.0B" in card)
card2 = build_card(symbol="T", score=80, mint="0x" + "c" * 40,
                   safety="clean", chain="solana", source="degen-feed")
check("card omits Supply when empty", "Supply" not in card2)

# ---- per-RPC retry (2026-10-18: Base endpoints flap) ----
_orig_call = lp_lock._eth_call
calls = []
lp_lock._eth_call = lambda rpc, to, data, t: calls.append(rpc) or 42
check("retry: first-try success, one call",
      lp_lock._eth_call_retry("http://x", PAIR, "0x", 1) == 42
      and len(calls) == 1)

attempts = iter([None, 99])
lp_lock._eth_call = lambda rpc, to, data, t: next(attempts)
check("retry: fail-then-success returns value",
      lp_lock._eth_call_retry("http://x", PAIR, "0x", 1) == 99)

lp_lock._eth_call = lambda rpc, to, data, t: None
check("retry: all attempts fail -> None",
      lp_lock._eth_call_retry("http://x", PAIR, "0x", 1) is None)

lp_lock._eth_call = lambda rpc, to, data, t: 0
check("retry: 0 is a valid answer (not retried as failure)",
      lp_lock._eth_call_retry("http://x", PAIR, "0x", 1) == 0)

lp_lock._eth_call = lambda rpc, to, data, t: (_ for _ in ()).throw(
    RuntimeError("boom"))
check("retry: raising _eth_call never propagates",
      lp_lock._eth_call_retry("http://x", PAIR, "0x", 1) is None)
lp_lock._eth_call = _orig_call

check("base RPC list widened",
      len(lp_lock._RPCS["base"]) >= 4
      and lp_lock._RPCS["base"][0] == "https://mainnet.base.org")

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all lp_lock tests passed")
