#!/usr/bin/env python3
"""GENERATOR for the historical pre-account-epoch grad position fixture.

Runs the ACTUAL pre-guard grad lane (the tree it is executed against, which
must be commit 6b0aed8) with PAPER_ACCOUNT_ENABLED unset, so the lane opens a
position on its OWN bankroll and never debits the shared account: the state
old code really produced. It writes the lane ledger bytes UNMODIFIED.

Run ONLY inside the reviewed OS network-deny wrapper (agt-ns-run.sh) against a
clean worktree at 6b0aed8:
  AGT_ORPHAN_OUT=<out.json> agt-ns-run.sh /tmp/wt-pre6b <6b0aed8 hash> 60 <abs path to this file>
No network is used (all provider calls are patched with local values).
"""
import os, sys, tempfile, time, json, shutil
from unittest.mock import patch

OUT = os.environ["AGT_ORPHAN_OUT"]
home = tempfile.mkdtemp(prefix="orphan-gen-home-")
os.environ["FOREX_AGENT_HOME"] = home
os.environ.pop("PAPER_ACCOUNT_ENABLED", None)          # the pre-account epoch
sys.path.insert(0, os.environ["PYTHONPATH"].split(os.pathsep)[0])

import daemon.launch_scanner.grad as G
G.LEDGER_PATH = os.path.join(home, "grad_ledger.json")


class FakeKS:
    def is_halted(self, lane): return False
    def gate(self, lane): return True
    def record_trade(self, lane, pnl, ts=None): pass
    def check_lane(self, lane): return {"lane": lane}


TAPE_OK = {"usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007, "tape_source": "dexscreener"}
SAFETY_OK = {"passed": True, "unknown": False, "score": 7, "flags": [],
             "fails": [], "source": "rugcheck"}
ps = [patch.object(G, "_veto"), patch.object(G, "_safety"),
      patch.object(G, "_ks", FakeKS()),
      patch.object(G, "quote_price", return_value=0.00007),
      patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK)),
      patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)]
mv, ms = ps[0].start(), ps[1].start()
for p in ps[2:]:
    p.start()
mv.veto_check.return_value = {"passed": True, "vetoes": []}
ms.check_token.return_value = dict(SAFETY_OK)
G.ensure_ledger()
cand = {"mint": "ORPH11pump", "symbol": "TKN", "name": "TKN / SOL",
        "chain": "solana", "dex": "pumpswap", "created_ts": time.time() - 60.0,
        "pool_address": "POOL1", "reserve_usd": 6_000.0, "fdv_usd": 69_000.0,
        "price_usd": 0.00007, "source": "gecko-new-pool"}
opened, why, _f = G.consider_candidate(cand)
assert opened, why
d = json.load(open(G.LEDGER_PATH))
pos = d["positions"]["ORPH11pump"]
assert not pos.get("pa_pos_id"), "pre-guard entry must carry no shared-account position id"
assert not os.path.exists(os.path.join(home, "paper_account")), "shared account must be untouched"
shutil.copyfile(G.LEDGER_PATH, OUT)
print("GENERATED", OUT, "bytes=%d" % os.path.getsize(OUT))
