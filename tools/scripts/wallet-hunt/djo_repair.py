#!/usr/bin/env python3
"""Bounded repair pass for djo_trace.py transport failures.
Retries ONLY sigs recorded as failed (egress-proxy IncompleteRead /
connection drops — NOT rate limits). Max 2 attempts per sig, same 2s
pace. HARD STOP on first 429: save and exit 3. Dedups by sig.
Usage: djo_repair.py <wallet> [wallet ...]
"""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from djo_trace import rpc, extract, RateLimited, OUT, PACE

def main():
    for wallet in sys.argv[1:]:
        tag = wallet[:6]
        ev_path = f"{OUT}/djo_{tag}_events.json"
        prog_path = f"{OUT}/djo_trace_progress.json"
        prog = json.load(open(prog_path)) if os.path.exists(prog_path) else {}
        state = prog.get(wallet, {})
        failed = state.get("failed", [])
        if not failed:
            print(f"[{tag}] no failed sigs", flush=True)
            continue
        events = json.load(open(ev_path)) if os.path.exists(ev_path) else []
        sig_path = f"{OUT}/djo_{tag}_sigs.json"
        bt = {}
        if os.path.exists(sig_path):
            for s in json.load(open(sig_path)):
                bt[s["signature"]] = s.get("blockTime")
        have = {e["sig"] for e in events}
        remaining = []
        print(f"[{tag}] repairing {len(failed)} failed sigs", flush=True)
        try:
            for f in failed:
                sig = f["sig"]
                if sig in have:
                    continue
                ok = False
                for attempt in range(2):
                    try:
                        d = rpc("getTransaction", [sig,
                             {"encoding": "jsonParsed",
                              "maxSupportedTransactionVersion": 0}])
                        if d.get("error"):
                            break
                        tx = d.get("result")
                        if tx is None:
                            break
                        ev = extract(wallet, sig, bt.get(sig), tx)
                        if ev:
                            # blockTime unknown here; fill from sig list
                            events.append(ev)
                        have.add(sig)
                        ok = True
                        break
                    except RateLimited:
                        raise
                    except Exception as e:
                        last_err = str(e)[:200]
                        time.sleep(PACE)
                if not ok:
                    remaining.append({"sig": sig,
                                      "error": last_err if 'last_err' in dir() else "unknown"})
                json.dump(events, open(ev_path, "w"))
                state["failed"] = remaining + [x for x in failed
                                               if x["sig"] not in have
                                               and x["sig"] != sig]
                # keep only still-failed entries
                state["failed"] = [x for x in state["failed"]
                                   if x["sig"] not in have]
                prog[wallet] = state
                json.dump(prog, open(prog_path, "w"))
                time.sleep(PACE)
        except RateLimited:
            prog[wallet] = state
            json.dump(prog, open(prog_path, "w"))
            print(f"RATE_LIMITED during repair of {tag}", flush=True)
            sys.exit(3)
        print(f"[{tag}] repair done: {len(events)} events, "
              f"{len(state.get('failed', []))} still failed", flush=True)
    print("REPAIR DONE", flush=True)

if __name__ == "__main__":
    main()
