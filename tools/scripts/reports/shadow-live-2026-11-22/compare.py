#!/usr/bin/env python3
"""Compare live shadow results across providers.

Reads results_*.json files (run_shadow.py output) and prints:
- per-method, per-provider outcome counts and latency stats
- exact-agreement verdicts on deterministic probes
- field-level diffs for mutable probes (slot, signatures, accounts)
- mismatch classification
Usage: python3 compare.py results_alchemy_chainstack.json [results_helius.json]
"""
import json
import math
import sys


def canon(v):
    if isinstance(v, float):
        return round(v, 9)
    if isinstance(v, dict):
        return {k: canon(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [canon(x) for x in v]
    return v


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(math.ceil(q * len(s)) - 1)))
    return s[i]


def load(path):
    with open(path) as fh:
        return json.load(fh)


def diff_paths(a, b, prefix=""):
    """Field paths where canonical forms differ (bounded)."""
    out = []
    if canon(a) == canon(b):
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(prefix + k + " (missing in A)")
            elif k not in b:
                out.append(prefix + k + " (missing in B)")
            else:
                out.extend(diff_paths(a[k], b[k], prefix + k + "."))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(prefix + "<len> %d vs %d" % (len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff_paths(x, y, prefix + "[%d]." % i))
    else:
        out.append(prefix.rstrip(".") + " (%r vs %r)" % (a, b))
    return out[:25]


def main(paths):
    by = {}  # (provider, method) -> [records]
    for path in paths:
        data = load(path)
        for r in data["results"]:
            by.setdefault((r["provider"], r["method"]), []).append(r)
    providers = sorted({p for p, _ in by})
    methods = ["getSlot", "getAccountInfo", "getTransaction",
               "getSignaturesForAddress", "getBlock", "getMultipleAccounts"]
    print("== outcomes + latency ==")
    for m in methods:
        for p in providers:
            recs = by.get((p, m), [])
            if not recs:
                print("%-24s %-22s no calls" % (m, p))
                continue
            kinds = {}
            for r in recs:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            lats = sorted(r["elapsed_s"] for r in recs if r["kind"] == "ok")
            print("%-24s %-22s n=%d kinds=%s lat n=%d p50=%s p95=%s max=%s" % (
                m, p, len(recs), kinds, len(lats),
                pct(lats, 0.5), pct(lats, 0.95),
                max(lats) if lats else None))
    print()
    print("== deterministic probes: exact agreement (round by round) ==")
    for m in ["getTransaction", "getBlock"]:
        for p in providers:
            for q in providers:
                if p >= q:
                    continue
                a = [r for r in by.get((p, m), []) if r["kind"] == "ok"]
                b = [r for r in by.get((q, m), []) if r["kind"] == "ok"]
                for x, y in zip(a, b):
                    same = canon(x["result"]) == canon(y["result"])
                    print("%s %s vs %s round %d: %s" % (
                        m, p, q, x["round"],
                        "AGREE" if same else "DIFFER"))
                    if not same:
                        for d in diff_paths(x["result"], y["result"]):
                            print("    " + d)
    print()
    print("== getAccountInfo (wSOL mint): field diffs ==")
    for p in providers:
        for q in providers:
            if p >= q:
                continue
            a = [r for r in by.get((p, "getAccountInfo"), []) if r["kind"] == "ok"]
            b = [r for r in by.get((q, "getAccountInfo"), []) if r["kind"] == "ok"]
            for x, y in zip(a, b):
                d = diff_paths(x["result"], y["result"])
                print("%s vs %s round %d: %d differing paths" % (p, q, x["round"], len(d)))
                for line in d:
                    print("    " + line)
    print()
    print("== getSlot: values + inter-provider lag ==")
    slots = {}
    for p in providers:
        vals = [(r["ts_utc"], r["result"]) for r in by.get((p, "getSlot"), [])
                if r["kind"] == "ok"]
        slots[p] = vals
        print("%s slots: %s" % (p, [v for _, v in vals]))
    print()
    print("== getSignaturesForAddress: overlap ==")
    for p in providers:
        recs = [r for r in by.get((p, "getSignaturesForAddress"), [])
                if r["kind"] == "ok"]
        for r in recs:
            sigs = [s["signature"][:12] for s in (r["result"] or [])]
            print("%s round %d: %s" % (p, r["round"], sigs))
    for p in providers:
        for q in providers:
            if p >= q:
                continue
            a = [r for r in by.get((p, "getSignaturesForAddress"), []) if r["kind"] == "ok"]
            b = [r for r in by.get((q, "getSignaturesForAddress"), []) if r["kind"] == "ok"]
            for x, y in zip(a, b):
                sa = {s["signature"] for s in x["result"]}
                sb = {s["signature"] for s in y["result"]}
                print("%s vs %s round %d: overlap %d/%d, only-%s %d, only-%s %d" % (
                    p, q, x["round"], len(sa & sb), len(sa | sb),
                    p, len(sa - sb), q, len(sb - sa)))
    print()
    print("== getMultipleAccounts: field diffs ==")
    for p in providers:
        for q in providers:
            if p >= q:
                continue
            a = [r for r in by.get((p, "getMultipleAccounts"), []) if r["kind"] == "ok"]
            b = [r for r in by.get((q, "getMultipleAccounts"), []) if r["kind"] == "ok"]
            for x, y in zip(a, b):
                d = diff_paths(x["result"], y["result"])
                print("%s vs %s round %d: %d differing paths" % (p, q, x["round"], len(d)))
                for line in d[:12]:
                    print("    " + line)
    print()
    print("== Helius answer sources ==")
    for r in by.get(("helius", "getSlot"), []) + by.get(("helius", "getTransaction"), []):
        pass
    for m in methods:
        for r in by.get(("helius", m), []):
            print("helius %s r%d kind=%s rpc_source=%s" % (
                m, r["round"], r["kind"], r.get("rpc_source")))


if __name__ == "__main__":
    main(sys.argv[1:])
