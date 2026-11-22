"""Bot-footprint check on the 59 fetched oldest STONKS txs (local only,
no new RPC calls). Prints: per-tx fee/success/signer/programs, priority
fees, failed-tx senders, per-wallet repeat stats, and token deltas."""
import json, sys
from collections import Counter, defaultdict

MINT = "22r9JGoWRBYc4Z1pkSFw8p2tdjefS2ZZshw1vr8bBC2K"
d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else
                   "22r9_oldest_txs.json"))
txs = sorted(d.values(), key=lambda t: (t.get("blockTime") or 0))
print("txs:", len(txs))
signers = Counter()
progs = Counter()
failed = []
fees = []
cuprice = Counter()
for t in txs:
    r = t.get("result") or t
    tx = r.get("transaction", {}).get("message", {})
    meta = r.get("meta", {}) or {}
    keys = tx.get("accountKeys", [])
    signer = keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else (keys[0] if keys else "?")
    signers[signer] += 1
    for ix in tx.get("instructions", []):
        pid = ix.get("programId", "?")
        progs[pid] += 1
        if pid == "ComputeBudget111111111111111111111111111111":
            data = ix.get("data", "")
            cuprice[data[:24]] += 1
    fees.append(meta.get("fee") or 0)
    if meta.get("err") is not None:
        failed.append((t.get("blockTime"), signer,
                       str(meta.get("err"))[:80]))
print("signers:", len(signers))
for s, n in signers.most_common(12):
    print("  signer", s, "x", n)
print("top programs:")
for p, n in progs.most_common(10):
    print("  ", p, n)
print("fee lamports: min", min(fees), "max", max(fees),
      "median", sorted(fees)[len(fees) // 2])
print("failed:", len(failed))
for bt, s, e in failed:
    print("  failed bt", bt, "signer", s, e)
print("compute-budget datas:", dict(list(cuprice.items())[:8]))
# per-signer intervals (bot regularity) for repeat signers
by_signer = defaultdict(list)
for t in txs:
    r = t.get("result") or t
    keys = r.get("transaction", {}).get("message", {}).get("accountKeys", [])
    s = keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else (keys[0] if keys else "?")
    by_signer[s].append(t.get("blockTime") or 0)
for s, times in sorted(by_signer.items(), key=lambda kv: -len(kv[1]))[:6]:
    times = sorted(times)
    gaps = [b - a for a, b in zip(times, times[1:])]
    print("  wallet", s, "n", len(times), "gaps", gaps)
