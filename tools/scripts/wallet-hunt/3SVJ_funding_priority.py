#!/usr/bin/env python3
"""Priority birth-funding for the 12 key 3SVJ wallets:
5 migration-second whales + 5 loose fingerprint hits + 2 cross-coin
round-trippers. Imports birth() from 3SVJ_funding (public RPC)."""
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/workspace/wallet-hunt"))
from importlib import import_module

W = os.path.expanduser("~/workspace/wallet-hunt")
WHALES = ["BbQ23QTpbrvVJCYh14CcBnht2KrCqiCvGP54wxibyBLV",
          "6nU9ttFgM37QzbGD8qR1mZpCFwZLPnxT31dBTScmiThN",
          "4TbwARHGhNuUw3e8e694szG58of8rpeeW14QmbAsmXFf",
          "EwPdGA5qVWPmqRk9SQ36kySyADb9LSYckh6KTznUQHac",
          "EbF3tccfZg8okEbiwDziDtJ59JVPKJtDKsJxikRm4Hv5"]
LOOSE = ["9H27QM8fo4UJa3J4aCgtEdRN424eB6KvGNNYsuUsbn14",
         "H4emhJAeNjFB6mYN9K4R37mT5tZDrEq4omwkdtdpaUrY",
         "By26Nt715oZUL7pzi1Q2UECPhYPMURtGaTBhvfT2wadd",
         "GygoVKEzwJWw2xDmrPEuEwUCFZkmcWu9Q81NUan3TPUP",
         "94sHTFK5NkqwohmzoRDVPv5eKfxoV1iwfjPK2YfCJsMM"]
CROSS = ["mATtYiy3Cn1Bihg3aMaWDzWsWN33txxtnhDyJcFFiWZ",
         "J3Ez1WjZMpcnMua4xA9nirZwWTurAxY7wqhm4vPeJ8k5"]

def main():
    f = import_module("3SVJ_funding")
    refs = json.load(open(os.path.join(W, "DKYC_refsets.json")))
    lineage = {refs["treasury"], refs["seeder"], refs["dispatcher"]}
    farm, bundle = set(refs["farm_shells"]), set(refs["kimchi_bundle_28s"])
    out = os.path.join(W, "3SVJ_funding_priority.json")
    done = {}
    if os.path.exists(out):
        try: done = {d["wallet"]: d for d in json.load(open(out))}
        except Exception: done = {}
    for group, wallets in (("WHALE", WHALES), ("LOOSE", LOOSE), ("CROSS", CROSS)):
        for w in wallets:
            if w in done:
                print(f"skip {group} {w} (done)", flush=True)
                continue
            r = f.birth(w)
            r["group"] = group
            r["in_farm_shells"] = w in farm
            r["in_kimchi_bundle"] = w in bundle
            r["funder_in_lineage"] = r.get("funder") in lineage
            done[w] = r
            json.dump(list(done.values()), open(out, "w"))
            print(f"{group} {w}: funder={r.get('funder')} "
                  f"amt={r.get('fund_amount_sol')} how={r.get('fund_how')} "
                  f"lineage={r['funder_in_lineage']} err={r.get('error')}", flush=True)
    print("DONE priority funding", flush=True)

if __name__ == "__main__":
    main()
