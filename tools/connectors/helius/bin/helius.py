#!/usr/bin/env python3
"""Helius RPC CLI (uses the user-connected custom.helius credential).

Subcommands:
  health                      -- getHealth sanity check (auth probe)
  sigs <wallet> [limit]       -- recent signatures for an address
  tx <signature>               -- one transaction (jsonParsed)
  buys <wallet> [limit]        -- mints this wallet bought in recent txs
  poll <registry.json> <state.json>
                              -- smart-money poll: prints fresh
                                 {mint: [wallets]} as JSON and updates
                                 <state.json> (last signatures seen)

Never prints or persists the raw key: only the hsurr:* surrogate travels,
and only to the allowed Helius hosts.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/templates/skill/bin")
from dynamic_credentials import (  # noqa: E402
    read_json_response,
    url_with_surrogate_query_param,
)

ALLOWED = ("mainnet.helius-rpc.com", "api.helius.xyz")
CRED = "custom.helius"
RPC = "https://mainnet.helius-rpc.com/"


def rpc(method, params):
    url = url_with_surrogate_query_param(RPC, CRED, allowed_hosts=ALLOWED)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return read_json_response(resp).get("result")
    except Exception as exc:
        return {"_error": "%s: %s" % (type(exc).__name__, exc)}


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def bought_mints(wallet, signature):
    tx = rpc("getTransaction",
             [signature, {"encoding": "jsonParsed",
                           "maxSupportedTransactionVersion": 0}]) or {}
    if isinstance(tx, dict) and tx.get("_error"):
        return set()
    meta = tx.get("meta") or {}
    pre = {(str(b.get("owner")), str(b.get("mint"))):
           _f((b.get("uiTokenAmount") or {}).get("uiAmount"))
           for b in meta.get("preTokenBalances") or []}
    out = set()
    for b in meta.get("postTokenBalances") or []:
        owner, mint = str(b.get("owner")), str(b.get("mint"))
        post = _f((b.get("uiTokenAmount") or {}).get("uiAmount"))
        if owner == wallet and mint and post - pre.get((owner, mint), 0.0) > 0:
            out.add(mint)
    return out


def cmd_health(_a):
    print(json.dumps(rpc("getHealth", [])))


def cmd_sigs(a):
    wallet = a[0]
    limit = int(a[1]) if len(a) > 1 else 10
    print(json.dumps(rpc("getSignaturesForAddress",
                         [wallet, {"limit": limit}])))


def cmd_tx(a):
    print(json.dumps(rpc("getTransaction",
                         [a[0], {"encoding": "jsonParsed",
                                  "maxSupportedTransactionVersion": 0}])))


def cmd_buys(a):
    wallet = a[0]
    limit = int(a[1]) if len(a) > 1 else 10
    sigs = rpc("getSignaturesForAddress", [wallet, {"limit": limit}]) or []
    mints = set()
    for s in sigs:
        sig = (s or {}).get("signature")
        if sig:
            mints |= bought_mints(wallet, sig)
            time.sleep(0.15)
    print(json.dumps(sorted(mints)))


def cmd_poll(a):
    """Poll every wallet in the registry; emit fresh {mint: [wallets]}."""
    reg_path, state_path = a[0], a[1]
    try:
        registry = json.loads(open(reg_path).read())
    except (OSError, ValueError):
        registry = {}
    try:
        state = json.loads(open(state_path).read())
    except (OSError, ValueError):
        state = {}
    last_sig = state.setdefault("smart_last_sig", {})
    fresh = {}
    for wallet in list(registry)[:20]:
        sigs = rpc("getSignaturesForAddress", [wallet, {"limit": 10}]) or []
        if not sigs or isinstance(sigs, dict):
            continue
        prev = last_sig.get(wallet)
        new = []
        for s in sigs:
            sig = (s or {}).get("signature")
            if not sig:
                continue
            if sig == prev:
                break
            new.append(sig)
        if sigs and (sigs[0] or {}).get("signature"):
            last_sig[wallet] = (sigs[0] or {})["signature"]
        for sig in reversed(new):
            for mint in bought_mints(wallet, sig):
                fresh.setdefault(mint, [])
                if wallet not in fresh[mint]:
                    fresh[mint].append(wallet)
            time.sleep(0.15)
    with open(state_path, "w") as fh:
        json.dump(state, fh)
    print(json.dumps(fresh))


CMDS = {"health": cmd_health, "sigs": cmd_sigs, "tx": cmd_tx,
        "buys": cmd_buys, "poll": cmd_poll}


def main(argv):
    if len(argv) < 2 or argv[1] not in CMDS:
        print("usage: helius.py {%s} ..." % "|".join(sorted(CMDS)),
              file=sys.stderr)
        return 2
    CMDS[argv[1]](argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
