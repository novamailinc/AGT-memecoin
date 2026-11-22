#!/usr/bin/env python3
"""DjoDYTgj outflow walk: who did the common top-up funder fund in the
24h before 3SVJ migration? Public RPC, gentle pacing, resumable.
Output: 3SVJ_djo_outflows.json (list of {recipient, amount_sol, ts, sig})
+ summary counts vs the window/hour buyer sets."""
import json, os, sys, time, urllib.request, urllib.error

W = os.path.expanduser("~/workspace/wallet-hunt")
RPC = "https://api.mainnet-beta.solana.com"
DJO = "DjoDYTgjHnZb8eFu16MqXdAQbk1xvs4BhRXigr5ieYnB"
MIG = 1792871066
WIN_START, WIN_END = MIG - 86400, MIG

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    for i in range(4):
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read()).get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(60, 10 * (i + 1)))
            else:
                time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None

def main():
    out_path = os.path.join(W, "3SVJ_djo_outflows.json")
    outflows = []
    if os.path.exists(out_path):
        try: outflows = json.load(open(out_path))
        except Exception: outflows = []
    done_sigs = {o["sig"] for o in outflows}
    before = None
    pages = 0
    while True:
        params = [DJO, {"limit": 1000}]
        if before: params[1]["before"] = before
        sigs = rpc("getSignaturesForAddress", params) or []
        pages += 1
        if not sigs:
            print(f"pages {pages}: empty, done", flush=True)
            break
        before = sigs[-1]["signature"]
        inwin = [s for s in sigs if s.get("blockTime") and WIN_START <= s["blockTime"] <= WIN_END and not s.get("err")]
        if not inwin and sigs[-1].get("blockTime", 10**18) < WIN_START:
            print(f"pages {pages}: past window start, done", flush=True)
            break
        print(f"page {pages}: {len(sigs)} sigs, {len(inwin)} in-window non-error", flush=True)
        for s in inwin:
            if s["signature"] in done_sigs:
                continue
            tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                       "maxSupportedTransactionVersion": 1}])
            if not tx: continue
            keys = [a["pubkey"] for a in tx["transaction"]["message"]["accountKeys"]]
            if DJO not in keys: continue
            di = keys.index(DJO)
            pre, post = tx["meta"]["preBalances"][di], tx["meta"]["postBalances"][di]
            # Outflows: for each system transfer instruction from Djo in this tx.
            for ix in tx["transaction"]["message"]["instructions"]:
                if ix.get("program") != "system": continue
                pi = ix.get("parsed", {}).get("info", {})
                if pi.get("source") == DJO and pi.get("destination"):
                    outflows.append({"recipient": pi["destination"],
                                     "amount_sol": pi.get("lamports", 0) / 1e9,
                                     "ts": s["blockTime"], "sig": s["signature"]})
                    done_sigs.add(s["signature"])
            time.sleep(0.15)
        json.dump(outflows, open(out_path, "w"))
        time.sleep(0.4)
        if pages >= 60:
            print("page cap reached", flush=True)
            break
    json.dump(outflows, open(out_path, "w"))
    print(f"DONE outflows {len(outflows)}", flush=True)

if __name__ == "__main__":
    main()
