#!/usr/bin/env python3
"""Exit price provenance labels (2026-11-22).

Covers daemon/launch_scanner/exit_provenance.py and its wiring in the
ladder engines (watcher, ceo, grad):

(a) unit: classify_reason maps close reasons to price kinds;
    aggregate collapses per-tranche kinds (single, mixed, empty and
    legacy-unlabeled -> unknown);
(b) watcher: an armed position whose leg A banks at a CURRENT quote
    and whose remainder later F2-exits at the last real price closes
    with a mixed summary; each leg carries its own kind;
(c) watcher: a never-armed position unquotable past the 24h stop
    closes write_down_zero;
(d) watcher: a normal hard stop at a current quote closes
    current_price_simulated_fill;
(e) ceo: the F2 close summary is labeled (real engine path);
(f) grad: poll_time_exits at a last-known price labels the close
    stale_price_estimate; with no price on record, write_down_zero.

No network: every price source is stubbed. Temp ledgers throughout.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import exit_provenance as PK
from daemon.launch_scanner import f2 as F2
from daemon.launch_scanner import watcher_lane as W
from daemon.launch_scanner import ceo as C
from daemon.launch_scanner import grad as G

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


class FakeKS:
    def __init__(self):
        self.halted = set()

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        pass

    def check_lane(self, lane):
        return {"action": "ok"}

    def halt_lane(self, lane, reason):
        self.halted.add(lane)
        return {"action": "HALTED"}


def boot(mod, tmpd, fname):
    mod.LEDGER_PATH = os.path.join(tmpd, fname)
    mod._ks = FakeKS()
    if hasattr(mod, "ensure_ledger"):
        mod.ensure_ledger()
    else:
        mod._load()
    return mod


def mkpos(now, entry=1.0, peak=2.6, leg_a_done=True, last_px=1.8,
          last_age_s=400.0, leg_a_kind=None):
    pos = {
        "mint": "PKTESTpump", "symbol": "PKT", "chain": "solana",
        "source": "test", "entry_ts": now - 2000.0,
        "quoted_price": entry, "entry_price": entry,
        "notional_usd": 1.0, "tokens": 1.0, "gas_in_usd": 0.004,
        "peak_price": peak, "peak_ts": now - 900.0,
        "legs": {
            "a": {"frac": 0.5, "target": 2.0, "done": leg_a_done,
                  "exit_price": 2.0 if leg_a_done else 0,
                  "realized_usd": 0.99 if leg_a_done else 0},
            "b": {"frac": 0.25, "target": 3.0, "done": False,
                  "exit_price": 0, "realized_usd": 0},
            "c": {"frac": 0.25, "trail": 0.30, "peak": peak,
                  "done": False, "exit_price": 0, "realized_usd": 0},
        },
        "realized_usd": 0.99 if leg_a_done else 0.0,
        "status": "open", "spot_index": None, "pair": "",
        "entry_snapshot": {"liquidity": 50000.0, "mcap": 100000.0},
        "entry_features": {},
        "quote_journal": [
            {"ts": now - 900.0, "px": peak, "src": "poll"},
            {"ts": now - last_age_s, "px": last_px, "src": "poll"},
        ],
    }
    if leg_a_kind:
        pos["legs"]["a"]["exit_price_kind"] = leg_a_kind
    return pos


def inject(mod, mint, pos):
    with mod._ledger_lock():
        d = mod._read()
        d["positions"][mint] = pos
        mod._write(d)


NOW = time.time()

# ---------- (a) unit ----------
check("unit: hard stop reason -> current",
      PK.classify_reason("hard_stop_-50pct") == PK.CURRENT)
check("unit: F2 reason -> stale",
      PK.classify_reason(F2.F2_REASON, F2.F2_REASON) == PK.STALE)
check("unit: f2-prefixed reason -> stale (no f2_reason arg)",
      PK.classify_reason("f2_unquotable_last_price") == PK.STALE)
check("unit: 24h unquotable -> write-down",
      PK.classify_reason("time_stop_24h_unquotable") == PK.WRITEDOWN)
check("unit: twap unquotable -> write-down",
      PK.classify_reason("twap_time_stop_60min_unquotable")
      == PK.WRITEDOWN)
check("unit: empty reason -> unknown",
      PK.classify_reason("") == PK.UNKNOWN
      and PK.classify_reason(None) == PK.UNKNOWN)
check("unit: aggregate single kind",
      PK.aggregate([PK.CURRENT, PK.CURRENT]) == PK.CURRENT)
check("unit: aggregate mixed",
      PK.aggregate([PK.STALE, PK.CURRENT])
      == "mixed:%s,%s" % (PK.CURRENT, PK.STALE))
check("unit: aggregate empty -> unknown",
      PK.aggregate([]) == PK.UNKNOWN)
check("unit: aggregate legacy unlabeled tranche -> unknown",
      PK.aggregate([None]) == PK.UNKNOWN)
check("unit: aggregate labeled + legacy -> mixed with unknown",
      PK.aggregate([PK.STALE, None])
      == "mixed:%s,%s" % (PK.STALE, PK.UNKNOWN))

# ---------- (b) watcher mixed close ----------
tmpd = tempfile.mkdtemp(prefix="pk-w-")
boot(W, tmpd, "watcher.json")
now = time.time()
# leg A banked at runtime through the real path (current quote), with
# the last real quote aged beyond the F2 sustain window.
pos = mkpos(now, leg_a_done=True, leg_a_kind=PK.CURRENT,
            last_age_s=F2.F2_SUSTAINED_S + 30)
inject(W, "PKTESTpump", pos)
closes = W.poll_exits(lambda c, m, s: 0.0)
d = W._read()
rec = (d["closes"] or [{}])[0]
legs = rec.get("legs") or {}
check("watcher: F2 close fires", rec.get("closed_reason") == F2.F2_REASON)
check("watcher: remaining legs carry stale estimate kind",
      legs.get("b", {}).get("exit_price_kind") == PK.STALE
      and legs.get("c", {}).get("exit_price_kind") == PK.STALE)
check("watcher: close summary is mixed current+stale",
      rec.get("exit_price_kind")
      == "mixed:%s,%s" % (PK.CURRENT, PK.STALE))

# legacy banked leg (no kind) is not promoted to current
tmpd = tempfile.mkdtemp(prefix="pk-w2-")
boot(W, tmpd, "watcher.json")
pos = mkpos(now, leg_a_done=True, leg_a_kind=None,
            last_age_s=F2.F2_SUSTAINED_S + 30)
inject(W, "PKTESTpump", pos)
W.poll_exits(lambda c, m, s: 0.0)
rec = (W._read()["closes"] or [{}])[0]
check("watcher: legacy banked leg keeps summary mixed with unknown",
      rec.get("exit_price_kind")
      == "mixed:%s,%s" % (PK.STALE, PK.UNKNOWN))

# ---------- (c) watcher 24h unquotable write-down ----------
tmpd = tempfile.mkdtemp(prefix="pk-w3-")
boot(W, tmpd, "watcher.json")
pos = mkpos(now, peak=1.3, leg_a_done=False, last_px=0.0,
            last_age_s=90000.0)
pos["entry_ts"] = now - 26 * 3600
pos["quote_journal"] = []
inject(W, "PKTESTpump", pos)
closes = W.poll_exits(lambda c, m, s: 0.0)
rec = (W._read()["closes"] or [{}])[0]
check("watcher: 24h unquotable close fires",
      rec.get("closed_reason") == "time_stop_24h_unquotable")
check("watcher: 24h unquotable close is a write-down",
      rec.get("exit_price_kind") == PK.WRITEDOWN)

# ---------- (d) watcher normal hard stop ----------
tmpd = tempfile.mkdtemp(prefix="pk-w4-")
boot(W, tmpd, "watcher.json")
pos = mkpos(now, peak=1.1, leg_a_done=False, last_px=1.1)
inject(W, "PKTESTpump", pos)
W.poll_exits(lambda c, m, s: 0.4)
rec = (W._read()["closes"] or [{}])[0]
check("watcher: hard stop fires", rec.get("closed_reason")
      == "hard_stop_-50pct")
check("watcher: hard stop close is a current-price simulated fill",
      rec.get("exit_price_kind") == PK.CURRENT)

# ---------- (e) ceo F2 label ----------
tmpd = tempfile.mkdtemp(prefix="pk-c-")
boot(C, tmpd, "ceo.json")
pos = mkpos(now, leg_a_done=True, leg_a_kind=PK.CURRENT,
            last_age_s=F2.F2_SUSTAINED_S + 30)
inject(C, "PKTESTpump", pos)
C.poll_exits(lambda c, m, s: 0.0)
rec = (C._read()["closes"] or [{}])[0]
check("ceo: F2 close summary labeled mixed current+stale",
      rec.get("closed_reason") == F2.F2_REASON
      and rec.get("exit_price_kind")
      == "mixed:%s,%s" % (PK.CURRENT, PK.STALE))

# ---------- (f) grad poll_time_exits ----------
tmpd = tempfile.mkdtemp(prefix="pk-g-")
boot(G, tmpd, "grad.json")
pos = mkpos(now, peak=1.1, leg_a_done=False, last_px=1.2,
            last_age_s=3600.0)
pos["entry_ts"] = now - 26 * 3600
inject(G, "PKTESTpump", pos)
closes = G.poll_time_exits()
rec = (G._read()["closes"] or [{}])[0]
check("grad: time exit at last-known price fires",
      len(closes) == 1 and rec.get("closed_reason") == "time_stop_24h")
check("grad: time exit at last-known price is a stale estimate",
      rec.get("exit_price_kind") == PK.STALE)

tmpd = tempfile.mkdtemp(prefix="pk-g2-")
boot(G, tmpd, "grad.json")
pos = mkpos(now, peak=1.1, leg_a_done=False, last_px=0.0)
pos["entry_ts"] = now - 26 * 3600
pos["quote_journal"] = []
inject(G, "PKTESTpump", pos)
closes = G.poll_time_exits()
rec = (G._read()["closes"] or [{}])[0]
check("grad: unquotable time exit is a write-down",
      len(closes) == 1
      and rec.get("closed_reason") == "time_stop_24h_unquotable"
      and rec.get("exit_price_kind") == PK.WRITEDOWN)

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL EXIT-PROVENANCE TESTS PASSED")
