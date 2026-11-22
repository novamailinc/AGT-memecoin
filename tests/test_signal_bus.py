#!/usr/bin/env python3
"""Signal bus tests (Phase 0). Isolated HOME via env var before import."""
import json
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="sigbus-")
os.environ["FOREX_AGENT_HOME"] = _tmp
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
import signal_bus
from signal_bus import (make_signal, validate, publish, recent,
                        feed_enabled, set_feed, feed_status,
                        evaluate_feed_switches, CHAINS)

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# --- schema / validation ---
s = make_signal("solana", "degen-feed", "MintA", symbol="TST", score=75,
                age_min=5, mcap=100000, buys=20, sells=5,
                evidence={"smart_hit": True})
ok, errs = validate(s)
check("valid signal passes", ok and not errs)
check("chain normalized", s["chain"] == "solana")
check("schema version", s["v"] == 1)
bad = make_signal("mars", "x", "", symbol="?")
ok2, errs2 = validate(bad)
check("missing mint rejected (chain normalized to solana)",
      not ok2 and errs2 == ["missing-mint"] and bad["chain"] == "solana")
check("publish rejects invalid", publish(bad) is False)

# --- publish / recent round trip ---
check("publish ok", publish(s) is True)
s2 = make_signal("base", "base-runner", "MintB", symbol="BB", score=80)
publish(s2)
all_r = recent(limit=10)
check("recent returns newest first", all_r[0]["mint"] == "MintB")
check("recent chain filter", [r["mint"] for r in recent(chain="solana")] == ["MintA"])
check("recent source filter", [r["mint"] for r in recent(source="base-runner")] == ["MintB"])
check("bus file written", os.path.exists(os.path.join(_tmp, "launch_scanner", "signal_bus.jsonl")))

# --- feed kill switches ---
check("solana never switchable", feed_enabled("solana") is True)
check("base defaults on", feed_enabled("base") is True)
check("set_feed pauses base", set_feed("base", False, "test") is True)
check("base now off", feed_enabled("base") is False)
st = feed_status()
check("feed_status shape", st["base"]["enabled"] is False and st["ethereum"]["enabled"] is True)
check("solana not in status", "solana" not in st)
check("set_feed refuses solana", set_feed("solana", False) is False)
check("resume works", set_feed("base", True, "test resume") is True and feed_enabled("base") is True)

# --- evaluate_feed_switches ---
stats = {("base", "base-runner"): {"n_taps": 12, "win_rate": 0.0, "n_closed": 12, "n_wins": 0},
         ("ethereum", "eth-runner"): {"n_taps": 12, "win_rate": 0.5, "n_closed": 12, "n_wins": 6},
         ("solana", "degen-feed"): {"n_taps": 50, "win_rate": 0.0, "n_closed": 50, "n_wins": 0}}
rep = evaluate_feed_switches(stats)
check("cold base feed auto-paused", any(p["chain"] == "base" for p in rep["paused"]))
check("base actually off", feed_enabled("base") is False)
check("warm eth kept", any(p["chain"] == "ethereum" for p in rep["kept"]))
check("solana never evaluated", not any("solana" in str(p) for p in rep["paused"]))
# thin data -> untouched
rep2 = evaluate_feed_switches({("base", "x"): {"n_taps": 3, "win_rate": 0.0}})
check("thin data skipped", rep2["skipped_thin"] == ["base"])
# never resumes automatically
rep3 = evaluate_feed_switches({("base", "x"): {"n_taps": 20, "win_rate": 0.9, "n_closed": 20, "n_wins": 18}})
check("no auto-resume", feed_enabled("base") is False)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
