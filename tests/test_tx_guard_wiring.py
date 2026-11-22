#!/usr/bin/env python3
"""Wiring tests for the Batch 3 meta.err gates.

Proves a REVERTED transaction is refused at every parse that
reads one: the migration-tx price (grad_raw, raw_tight,
moon_lane), the creator-from-oldest-tx (grad_select), the
deployer resolver (deployers), and the logstream mint lookup.
Uses the recorded migration-tx fixture from the snapshot
harness, with err injected. No network, no disk (temp HOME).
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import copy
import json
import os
import sys
import tempfile

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="txguard-wiring-")

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, os.path.join(REPO, "tests", "snapshots"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import raw_tight as RT
from daemon.launch_scanner import moon_lane as ML
from daemon.launch_scanner import deployers as DP
from daemon.launch_scanner import logstream as LS

FIX = os.path.join(REPO, "tests", "snapshots", "fixtures",
                   "helius_migration_tx.json")
with open(FIX) as f:
    TX = json.load(f)

MINT = "H8DX2KJNV3c9Dg4L7sZ85qS5Mr9ZZ4y36tgsEFMopump"
CREATOR = "2BdnAxsgVehTFu14FmBJSXeCUnujkG8hVaZ4yxPufodz"

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def _reverted(tx):
    t = copy.deepcopy(tx)
    t["meta"]["err"] = {"InstructionError": [0, {"Custom": 6000}]}
    return t


def _no_meta(tx):
    t = copy.deepcopy(tx)
    t.pop("meta", None)
    return t


# 1. Migration-tx price: good tx prices, reverted/unknown refuse.
info, why = GR._ff_parse_tx_price(TX, MINT, sol_usd=70.0)
check("grad_raw: good tx prices",
      info is not None and abs(info["mcap_usd"] - 875000.0) < 0.01)
info, why = GR._ff_parse_tx_price(_reverted(TX), MINT, sol_usd=70.0)
check("grad_raw: reverted tx refused",
      info is None and why == "migration tx tx_reverted")
info, why = GR._ff_parse_tx_price(_no_meta(TX), MINT, sol_usd=70.0)
check("grad_raw: missing meta refused (unknown, not priced)",
      info is None and why == "migration tx tx_unknown")

info, why = RT._ff_parse_tx_price(_reverted(TX), MINT, sol_usd=70.0)
check("raw_tight: reverted tx refused", info is None)
info, why = ML._ff_parse_tx_price(_reverted(TX), MINT, sol_usd=70.0)
check("moon_lane: reverted tx refused", info is None)

# 2. Creator from oldest tx: good tx names the signer, reverted
# names nobody.
class _FakeReserves:
    def __init__(self, tx):
        self.tx = tx
        self.calls = []

    def rpc(self, method, params=None, **kw):
        self.calls.append((method, params))
        return self.tx


old = GS._reserves
GS._reserves = _FakeReserves(TX)
try:
    check("grad_select: good tx names creator",
          GS._creator_from_oldest_tx("somesig") == CREATOR)
finally:
    GS._reserves = old
GS._reserves = _FakeReserves(_reverted(TX))
try:
    check("grad_select: reverted tx names nobody",
          GS._creator_from_oldest_tx("somesig") is None)
finally:
    GS._reserves = old
GS._reserves = _FakeReserves(_no_meta(TX))
try:
    check("grad_select: missing meta names nobody",
          GS._creator_from_oldest_tx("somesig") is None)
finally:
    GS._reserves = old

# 3. Deployer resolver: reverted oldest tx resolves nothing.
calls = []


def _fake_rpc(method, params=None, **kw):
    calls.append((method, params))
    if method == "getSignaturesForAddress":
        return [{"signature": "oldsig", "blockTime": 1794787202,
                 "slot": 300000100}]
    if method == "getTransaction":
        return _reverted(TX)
    return None


old_rpc = DP._rpc
DP._rpc = _fake_rpc
try:
    check("deployers: reverted oldest tx resolves nothing",
          DP.resolve_deployer(MINT, "solana", ctx={}) is None)
finally:
    DP._rpc = old_rpc


def _fake_rpc_good(method, params=None, **kw):
    if method == "getSignaturesForAddress":
        return [{"signature": "oldsig", "blockTime": 1794787202,
                 "slot": 300000100}]
    if method == "getTransaction":
        return TX
    return None


DP._rpc = _fake_rpc_good
try:
    check("deployers: good oldest tx resolves the signer",
          DP.resolve_deployer(MINT, "solana", ctx={}) == CREATOR)
finally:
    DP._rpc = old_rpc

# 4. Logstream mint lookup: reverted tx yields no mint.
# extract_mint reads initializeMint instructions, so the tx
# here is a synthetic create (the migration fixture has no
# instructions at all).
CREATE_TX = {
    "meta": {"err": None, "innerInstructions": []},
    "transaction": {
        "message": {
            "accountKeys": [
                {"pubkey": CREATOR, "signer": True,
                 "writable": True}],
            "instructions": [
                {"program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "parsed": {"type": "initializeMint",
                            "info": {"mint": MINT}}},
            ],
        },
        "signatures": ["somesig"],
    },
}

stream = LS.LogStream.__new__(LS.LogStream)
stream._last_rpc_ts = 0.0


def _http_rpc_good(method, params=None, **kw):
    return CREATE_TX


def _http_rpc_bad(method, params=None, **kw):
    return _reverted(CREATE_TX)


stream._http_rpc = _http_rpc_good
try:
    got = stream._mint_for_signature("somesig")
    check("logstream: good tx yields a mint",
          got == MINT)
finally:
    pass
stream._last_rpc_ts = 0.0
stream._http_rpc = _http_rpc_bad
try:
    check("logstream: reverted tx yields no mint",
          stream._mint_for_signature("somesig") is None)
finally:
    pass

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("ALL TX GUARD WIRING TESTS PASSED")
