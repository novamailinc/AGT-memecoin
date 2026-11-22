#!/usr/bin/env python3
"""Tap journal tests (Phase 0). Isolated HOME via env var before import."""
import json
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="tapj-")
os.environ["FOREX_AGENT_HOME"] = _tmp
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
import tap_journal
from tap_journal import journal_tap, tap_stats

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# journal three taps across two chain-sources
check("journal tap 1", journal_tap("MintA", chain="solana", source="degen-feed",
                                   entry_price=0.001, symbol="AAA") is True)
journal_tap("MintB", chain="base", source="base-runner",
            entry_price=0.002, symbol="BBB")
journal_tap("MintC", chain="base", source="base-runner",
            entry_price=0.003, symbol="CCC")
check("journal file written",
      os.path.exists(os.path.join(_tmp, "launch_scanner", "tap_journal.jsonl")))

# no positions yet -> taps counted, nothing closed
st = tap_stats()
check("solana taps counted", st[("solana", "degen-feed")]["n_taps"] == 1)
check("base taps counted", st[("base", "base-runner")]["n_taps"] == 2)
check("nothing closed yet", st[("base", "base-runner")]["n_closed"] == 0)
check("win rate 0 when undecided", st[("base", "base-runner")]["win_rate"] == 0.0)

# now close positions: MintA rugs (peak 1.0), MintB runs (peak 2.5x)
positions = [
    {"mint": "MintA", "status": "closed", "peak_mult": 1.0},
    {"mint": "MintB", "status": "closed", "peak_mult": 2.5},
    {"mint": "MintC", "status": "open", "peak_mult": 1.2},
]
with open(os.path.join(_tmp, "degen_positions.json"), "w") as f:
    json.dump(positions, f)
st2 = tap_stats()
sol = st2[("solana", "degen-feed")]
base = st2[("base", "base-runner")]
check("solana closed counted", sol["n_closed"] == 1 and sol["n_wins"] == 0)
check("solana win rate 0", sol["win_rate"] == 0.0)
check("base one closed one open", base["n_closed"] == 1 and base["n_taps"] == 2)
check("base winner counted", base["n_wins"] == 1)
check("base win rate 1.0", base["win_rate"] == 1.0)

# corrupt lines don't break stats
with open(os.path.join(_tmp, "launch_scanner", "tap_journal.jsonl"), "a") as f:
    f.write("not json\n")
st3 = tap_stats()
check("corrupt line tolerated", st3[("base", "base-runner")]["n_taps"] == 2)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
