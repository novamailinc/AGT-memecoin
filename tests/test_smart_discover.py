#!/usr/bin/env python3
"""Discovery parsing tests (pure functions, synthetic txs, no network)."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import importlib.util
import sys

spec = importlib.util.spec_from_file_location(
    "disc", _REPO_ROOT + "/scripts/smart-discover")
# stub out the __main__ guard by exec'ing module source minus the guard
_src = open(_REPO_ROOT + "/scripts/smart-discover").read()
_ns = {"__name__": "disc_test",
       "__file__": _REPO_ROOT + "/scripts/smart-discover"}
exec(compile(_src.split('if __name__ == "__main__":')[0], "disc", "exec"), _ns)
_buyers_of_mint = _ns["_buyers_of_mint"]
_wallet_swaps = _ns["_wallet_swaps"]

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

MINT = "TargetMint11111111111111111111111111111111"
W = "WalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

def tx(pre, post, bts=1000):
    def bal(owner, mint, amt):
        return {"owner": owner, "mint": mint,
                "uiTokenAmount": {"uiAmount": amt}}
    return {"blockTime": bts, "meta": {
        "preTokenBalances": [bal(o, m, a) for (o, m, a) in pre],
        "postTokenBalances": [bal(o, m, a) for (o, m, a) in post]}}

# buyer gains 500 of target mint
t1 = tx([(W, MINT, 0.0)], [(W, MINT, 500.0)])
check("buyer extracted", _buyers_of_mint(t1, MINT) == {W: 500.0})
# seller (balance down) is not a buyer
t2 = tx([(W, MINT, 500.0)], [(W, MINT, 100.0)])
check("seller not a buyer", _buyers_of_mint(t2, MINT) == {})
# other mint ignored
t3 = tx([("X", "OtherMint", 0.0)], [("X", "OtherMint", 9.0)])
check("other mint ignored", _buyers_of_mint(t3, MINT) == {})
# empty tx safe
check("empty tx safe", _buyers_of_mint({}, MINT) == {})

# wallet swaps: buy then sell
b = tx([(W, MINT, 0.0)], [(W, MINT, 500.0)], bts=1000)
s = tx([(W, MINT, 500.0)], [(W, MINT, 0.0)], bts=1600)
check("buy parsed", _wallet_swaps(W, b) == [(MINT, "buy", 1000)])
check("sell parsed", _wallet_swaps(W, s) == [(MINT, "sell", 1600)])
# other wallet's activity ignored
check("other wallet ignored", _wallet_swaps("Nobody", b) == [])

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
