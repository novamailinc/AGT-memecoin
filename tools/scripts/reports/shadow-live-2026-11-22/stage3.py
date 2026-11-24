#!/usr/bin/env python3
"""Stage 3: near-simultaneous Alchemy-vs-Helius probes for the two
time-sensitive methods (getSlot, getSignaturesForAddress). Helius
goes through reserves.rpc (pacer-governed); every answer's source is
recorded — only rpc_source == "helius" counts as a Helius answer.
"""
import json
import os
import sys
import time

REPO = os.path.expanduser("~/workspace/Forex-agent/Forex-agent-main")
sys.path.insert(0, REPO)
HOME = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def main():
    from daemon.intelligence.acquisition.providers.alchemy import AlchemyAdapter
    from daemon.intelligence.acquisition.providers.helius import HeliusAdapter
    from daemon.launch_scanner import reserves

    al = AlchemyAdapter(home=HOME)
    he = HeliusAdapter(home=HOME)
    out = []
    for rnd in range(3):
        for name, ad in (("alchemy", al), ("helius", he)):
            t0 = time.time()
            res = ad.call("getSlot", [], timeout=20)
            rec = {"provider": name, "method": "getSlot", "round": rnd,
                   "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
                   "kind": res.get("kind"),
                   "elapsed_s": round(res.get("elapsed") or 0.0, 3),
                   "result": res.get("result")}
            if name == "helius":
                rec["rpc_source"] = reserves.last_rpc_source()
            out.append(rec)
            print("%s getSlot r%d kind=%s elapsed=%.2f result=%s source=%s" % (
                name, rnd, rec["kind"], rec["elapsed_s"], rec["result"],
                rec.get("rpc_source")), flush=True)
    for rnd in range(2):
        for name, ad in (("alchemy", al), ("helius", he)):
            t0 = time.time()
            res = ad.call("getSignaturesForAddress",
                          [PUMP_PROGRAM, {"limit": 5}], timeout=20)
            rec = {"provider": name, "method": "getSignaturesForAddress",
                   "round": rnd,
                   "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
                   "kind": res.get("kind"),
                   "elapsed_s": round(res.get("elapsed") or 0.0, 3),
                   "result": res.get("result")}
            if name == "helius":
                rec["rpc_source"] = reserves.last_rpc_source()
            out.append(rec)
            sigs = [s["signature"][:8] for s in (res.get("result") or [])] \
                if res.get("kind") == "ok" else []
            print("%s getSigs r%d kind=%s elapsed=%.2f sigs=%s source=%s" % (
                name, rnd, rec["kind"], rec["elapsed_s"], sigs,
                rec.get("rpc_source")), flush=True)
    with open("results_stage3.json", "w") as fh:
        json.dump({"meta": {"started_utc": out[0]["ts_utc"]},
                   "results": out}, fh, indent=1)
    print("wrote results_stage3.json (%d calls)" % len(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
