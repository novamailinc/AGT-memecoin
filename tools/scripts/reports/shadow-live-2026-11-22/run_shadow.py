#!/usr/bin/env python3
"""Live shadow comparison runner — three-provider program (2026-11-22).

Calls the SAME probe set on Alchemy, Chainstack and Helius via the real
adapters in daemon/intelligence/acquisition/providers/. No live path,
killswitch, ledger, stake or entry logic is touched; results are written
to JSON only, never returned to any caller.

Secrets: the Chainstack adapter needs CHAINSTACK_ENDPOINT (full URL
with the node auth key). It is built in-memory from the disk key file
and set as a process env var only — never printed, never written to
any file. The Alchemy adapter reads ~/.forex-agent/alchemy/api_key
via home=FOREX_AGENT_HOME. No URL or key material is logged anywhere.

Usage:
  FOREX_AGENT_HOME=$HOME/.forex-agent python3 run_shadow.py \
      --providers alchemy,chainstack --out DIR [--pace 1.2]
  FOREX_AGENT_HOME=$HOME/.forex-agent python3 run_shadow.py \
      --providers helius --out DIR
Helius calls go through reserves.rpc (pacer-governed, 1 call/20s) and
each answer's source is verified with reserves.last_rpc_source() —
only source == "helius" counts as a Helius answer.
"""
import argparse
import json
import os
import sys
import time

REPO = os.path.expanduser("~/workspace/Forex-agent/Forex-agent-main")
sys.path.insert(0, REPO)

HOME = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent")

WSOL = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TX_SIG = "eY5SFmn2qGBCbwqWUQEKJJr6N7VevCWvKTKqkCaC6RBbuSqYTQtfK9djE8EWSTHYFbnyMLUoYiC3ZAWVDiCt7q8"
TX_SLOT = 472499532
CURVE = "AKw3WbKLds6u6Mu3URSiYtu3K8FNna9Vh4yommtyy8Un"
POOL = "3Y7Knabn84dpB6yy5LbCFt574E8ifTMeWkBMTPvGZCwE"

# (method, params, rounds)
PROBES = [
    ("getSlot", [], 10),
    ("getAccountInfo", [WSOL, {"encoding": "jsonParsed"}], 3),
    ("getTransaction", [TX_SIG, {"encoding": "jsonParsed",
                                 "maxSupportedTransactionVersion": 1}], 3),
    ("getSignaturesForAddress", [PUMP_PROGRAM, {"limit": 5}], 3),
    ("getBlock", [TX_SLOT, {"encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 1,
                            "transactionDetails": "none"}], 3),
    ("getMultipleAccounts", [[CURVE, POOL], {"encoding": "jsonParsed"}], 3),
]


def _chainstack_endpoint_from_disk_key():
    path = os.path.join(HOME, "chainstack", "api_key")
    with open(path) as fh:
        key = fh.read().strip()
    if not key:
        raise RuntimeError("empty chainstack disk key")
    return "https://solana-mainnet.core.chainstack.com/%s" % key


def _quiet_state():
    try:
        with open(os.path.join(HOME, "helius_quiet.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pace", type=float, default=1.2)
    args = ap.parse_args(argv)
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    os.makedirs(args.out, exist_ok=True)

    from daemon.intelligence.acquisition.providers.alchemy import AlchemyAdapter
    from daemon.intelligence.acquisition.providers.chainstack import ChainstackAdapter
    from daemon.intelligence.acquisition.providers.helius import HeliusAdapter

    if "chainstack" in providers and not os.environ.get("CHAINSTACK_ENDPOINT"):
        # Adapter convention is an endpoint file that does not exist on
        # this VM; supply the same value as a process env var instead.
        os.environ["CHAINSTACK_ENDPOINT"] = _chainstack_endpoint_from_disk_key()

    adapters = {}
    if "alchemy" in providers:
        adapters["alchemy"] = AlchemyAdapter(home=HOME)
    if "chainstack" in providers:
        adapters["chainstack"] = ChainstackAdapter(home=HOME)
    if "helius" in providers:
        adapters["helius"] = HeliusAdapter(home=HOME)

    meta = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "home": HOME, "helius_quiet_at_start": _quiet_state(),
            "adapters": {}}
    for name, ad in adapters.items():
        meta["adapters"][name] = {
            "configured": bool(ad.configured()),
            "supports": {m: bool(ad.supports(m)) for m, _, _ in PROBES},
            "unverified": {m: bool(ad.unverified(m)) for m, _, _ in PROBES},
            "cost": {m: ad.cost(m) for m, _, _ in PROBES},
        }
        print("%s configured=%s" % (name, meta["adapters"][name]["configured"]),
              flush=True)

    results = []
    for name in providers:
        ad = adapters[name]
        for method, params, rounds in PROBES:
            for rnd in range(rounds):
                t0 = time.time()
                res = ad.call(method, params, timeout=20)
                rec = {"provider": name, "method": method,
                       "params": params, "round": rnd,
                       "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(t0)),
                       "kind": res.get("kind"),
                       "http_status": res.get("http_status"),
                       "elapsed_s": round(res.get("elapsed") or 0.0, 3),
                       "note": res.get("note"),
                       "rate_limit_kind": res.get("rate_limit_kind"),
                       "result": res.get("result")}
                if name == "helius":
                    try:
                        from daemon.launch_scanner import reserves
                        rec["rpc_source"] = reserves.last_rpc_source()
                    except Exception:
                        rec["rpc_source"] = None
                results.append(rec)
                print("%s %s r%d kind=%s elapsed=%.2fs source=%s" % (
                    name, method, rnd, rec["kind"], rec["elapsed_s"],
                    rec.get("rpc_source")), flush=True)
                if name != "helius":
                    time.sleep(args.pace)

    meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["helius_quiet_at_end"] = _quiet_state()
    out_path = os.path.join(args.out, "results_%s.json" % "_".join(providers))
    with open(out_path, "w") as fh:
        json.dump({"meta": meta, "results": results}, fh, indent=1)
    print("wrote %s (%d calls)" % (out_path, len(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
