"""Realtime sync layer (rt_sync.py) — watched set, tape, price_fn,
exit-eval triggering. No network: stats come from fixtures/stubs."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import rt_sync as R  # noqa: E402

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


# ---------------- watched set ----------------
import tempfile  # noqa: E402

tmp = tempfile.mkdtemp()
home = os.path.join(tmp, "home")
os.makedirs(os.path.join(home, "launch_scanner"))
_write(os.path.join(home, "ceo_ledger.json"),
       {"positions": {"CEOMINT": {"chain": "solana", "symbol": "C"}},
        "closes": []})
_write(os.path.join(home, "degen_mirror_ledger.json"),
       {"positions": {"MIRMINT": {"chain": "base", "symbol": "M"}},
        "closes": []})
_write(os.path.join(home, "steady_ledger.json"),
       {"positions": {"STMINT": {"chain": "solana", "symbol": "S"}}})
now = time.time()
bus = os.path.join(home, "launch_scanner", "signal_bus.jsonl")
with open(bus, "w") as f:
    f.write(json.dumps({"ts": now - 60, "chain": "solana",
                        "mint": "BUSMINT"}) + "\n")
    f.write(json.dumps({"ts": now - 60, "chain": "hyperliquid",
                        "mint": "HLMINT"}) + "\n")
    f.write(json.dumps({"ts": now - 3 * 3600, "chain": "solana",
                        "mint": "OLDMINT"}) + "\n")
w = R.build_watched_set(home=home, now=now, bus_path=bus)
check("rt: ceo position watched", "ceo" in w.get("CEOMINT", {}).get("kinds", set()))
check("rt: mirror position watched", "mirror" in w.get("MIRMINT", {}).get("kinds", set()))
check("rt: tracked position watched", "tracked" in w.get("STMINT", {}).get("kinds", set()))
check("rt: recent bus mint watched", "bus" in w.get("BUSMINT", {}).get("kinds", set()))
check("rt: HL bus mint excluded (no DS tape)", "HLMINT" not in w)
check("rt: stale bus mint excluded", "OLDMINT" not in w)
check("rt: open_mints by kind", R.open_mints(w, "ceo") == {"CEOMINT"})

# ---------------- tape ----------------
tape = R.Tape()
stats = {"price_usd": 2.0, "usd_liquidity": 50_000.0,
         "usd_market_cap": 900_000.0}
check("rt: update lands", tape.update("M1", stats, 1000.0))
check("rt: fresh price served", tape.price("M1", 1005.0) == 2.0)
check("rt: stale price refused", tape.price("M1", 1000.0 + R.FRESH_S + 1) == 0.0)
tape.update("M1", {"price_usd": 3.5}, 1010.0)
check("rt: peak tracks up", tape.rows["M1"]["peak"] == 3.5)
tape.update("M1", {"price_usd": 1.0}, 1015.0)
check("rt: peak never falls", tape.rows["M1"]["peak"] == 3.5)
check("rt: empty stats refused", not tape.update("M2", None, 1000.0))
check("rt: zero price refused", not tape.update("M2", {"price_usd": 0}, 1000.0))
check("rt: unknown mint price 0", tape.price("NOPE", 1000.0) == 0.0)

# ---------------- price_fn ----------------
# Latest recorded price for M1 is 1.0 (t=1015); 3.5 is only the peak.
fn = R.make_price_fn(tape, clock=lambda: 1016.0)
check("rt: price_fn serves fresh", fn("solana", "M1", None) == 1.0)
check("rt: price_fn HL always 0", fn("hyperliquid", "M1", 3) == 0.0)
check("rt: price_fn unknown 0", fn("solana", "NOPE", None) == 0.0)

# ---------------- exit-eval triggering ----------------
changed = R.exit_eval_needed(tape, {"M1"}, {"M1": 1.0}, now=1016.0)
check("rt: no eval when price unchanged", changed == [])
changed = R.exit_eval_needed(tape, {"M1"}, {"M1": 3.5}, now=1016.0)
check("rt: eval when price moved", changed == ["M1"])
changed = R.exit_eval_needed(tape, {"M1"}, {"M1": 2.0}, now=2000.0)
check("rt: no eval on stale tape", changed == [])

# ---------------- refresh via stub ----------------
def stub_stats(mints, chain=None):
    return {m: {"price_usd": 7.0, "usd_liquidity": 10_000.0,
                "usd_market_cap": 70_000.0} for m in mints}

tape2 = R.Tape()
n = R.refresh_tape(tape2, {"A": {"chain": "solana", "kinds": {"bus"}},
                           "B": {"chain": "hyperliquid", "kinds": {"bus"}}},
                   500.0, stats_fn=stub_stats)
check("rt: refresh updates DS chains only", n == 1 and tape2.price("A", 500.0) == 7.0)

# ---------------- tape write ----------------
p = os.path.join(tmp, "live_tape.json")
check("rt: tape writes", R.write_tape(tape2, 500.0, path=p))
d = json.load(open(p))
check("rt: tape file has heartbeat + mints",
      d["heartbeat_ts"] == 500.0 and "A" in d["mints"])

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL RT_SYNC TESTS PASSED")
