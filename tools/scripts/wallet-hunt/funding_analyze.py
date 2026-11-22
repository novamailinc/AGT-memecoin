#!/usr/bin/env python3
"""Join funding_trace.json + funding_candidates.json -> per-wallet table,
classification, and common-funder groups. Local computation only."""
import json, os
from collections import defaultdict

OUT = os.path.expanduser("~/workspace/wallet-hunt")
KNOWN = {
    "7JzH5xv82HKEK2kwitBiVi9CvGhu5dB8kui6tsbp962S": "finalist-7JzH",
    "7FPqg7MpJwvGneBPvkBrCDjAvZhFnAvoiUw9LF91nsN": "finalist-7FP",
    "J9E7jKgHsi1vubSwwi7qUXtD5hXNnHxpJDx4y7DAs5AL": "finalist-J9",
    "F9MxbtHQ8998RpMxUw7K7ED8qynTHQxcWuvvtEC7qqp1": "lead-F9MxbtHQ",
    "EedNgv1wGdCMayQ6znioCgVHjaqo7YBgdwq8GFUoMuRE": "lead-EedNgv1w",
    "88887QrRZPZmsstEXsoXXB8E7nbmUDde4Gp5s7jG3ENu": "trio-888",
    "EJX629cpqnoa7jNRUBsFeevfPLxFWoa4KWWfsX7teLHr": "trio-EJX",
    "5p1kt7GKideepmmC7DNNj1h2A5fRYLE8nn5CusVwTTL2": "trio-5p1 (launch-side)",
    "DYAKK4ui3hhBVariht6kF31QMWYnKZGAom67SzHnxncK": "KWH distribution artifact",
    "GZDRqVfpKLjTmDxf2Zh34WURCojSKbAoDJqxCi53m7ui": "cross-coin scalper GZDR",
}

def classify(row):
    if row.get("error") and not row.get("funder"):
        return "untraced: " + str(row["error"])[:40]
    age = row.get("age_at_first_buy_s")
    lag = row.get("funding_lag_s")
    if age is None:
        return "unknown age"
    if age <= 3600:
        return "FRESH just-in-time (born <=1h before first hunt buy)"
    if age <= 86400:
        return "fresh same-day (born <=24h before first buy)"
    if age <= 7 * 86400:
        return "young (born <=7d before first buy)"
    return "established (born >7d before first buy)"

def main():
    cands = {c["wallet"]: c for c in
             json.load(open(f"{OUT}/funding_candidates.json"))["candidates"]}
    trace = {t["wallet"]: t for t in json.load(open(f"{OUT}/funding_trace.json"))}
    rows = []
    for w, c in cands.items():
        t = trace.get(w, {"wallet": w, "error": "not traced"})
        first_buy = min(v["first_buy_ts"] for v in c["coins"].values())
        row = {"wallet": w,
               "coins": {k: {"gross": v["gross_buy"], "net": v["net"],
                             "first_buy_rel_s": v["first_buy_rel_s"]}
                         for k, v in c["coins"].items()},
               "first_buy_ts": first_buy,
               "funder": t.get("funder"),
               "fund_amount_sol": t.get("fund_amount_sol"),
               "funding_ts": t.get("funding_ts"),
               "fund_how": t.get("fund_how"),
               "txs_walked": t.get("txs_walked"),
               "wallet_first_tx_ts": t.get("wallet_first_tx_ts"),
               "lifetime_txs_exact": t.get("lifetime_txs_exact"),
               "trace_error": t.get("error")}
        if t.get("funding_ts"):
            row["funding_lag_s"] = first_buy - t["funding_ts"]
        if t.get("wallet_first_tx_ts"):
            row["age_at_first_buy_s"] = first_buy - t["wallet_first_tx_ts"]
        row["classification"] = classify(row)
        rows.append(row)
    rows.sort(key=lambda r: -max(v["gross"] for v in r["coins"].values()))
    # common funders
    by_funder = defaultdict(list)
    for r in rows:
        if r["funder"]:
            by_funder[r["funder"]].append(r)
    groups = {f: rs for f, rs in by_funder.items() if len(rs) >= 2}
    print(f"rows={len(rows)} traced_with_funder={sum(1 for r in rows if r['funder'])}")
    print(f"distinct funders={len(by_funder)}  common (>=2 candidates)={len(groups)}")
    for f, rs in sorted(groups.items(), key=lambda x: -len(x[1])):
        coins = sorted({c for r in rs for c in r["coins"]})
        tag = KNOWN.get(f, "")
        member = " FUNDER IS KNOWN/CANDIDATE" if (f in cands or f in KNOWN) else ""
        print(f"\nFUNDER {f} {tag}{member} — {len(rs)} candidates, coins {coins}")
        for r in rs:
            print(f"   {r['wallet'][:20]}.. coins={list(r['coins'])} "
                  f"amt={r['fund_amount_sol']} lag={r.get('funding_lag_s')}s "
                  f"how={r['fund_how']} class={r['classification'][:28]}")
    # classification histogram
    hist = defaultdict(int)
    for r in rows:
        hist[r["classification"].split(" (")[0]] += 1
    print("\nclassification histogram:", dict(hist))
    json.dump({"rows": rows,
               "common_funders": {f: [r["wallet"] for r in rs]
                                  for f, rs in groups.items()}},
              open(f"{OUT}/funding_table.json", "w"), indent=1)
    print("saved funding_table.json")

if __name__ == "__main__":
    main()
