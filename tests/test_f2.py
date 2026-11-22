#!/usr/bin/env python3
"""F2 sustained-unquotability exit tests (2026-10-23).

Covers daemon/launch_scanner/f2.py + its wiring in the four ladder
engines (grad, grad_raw via the isolated grad engine, ceo, mirror,
momentum):

(a) an ARMED position (recorded peak >= 1.5x, leg A banked) dark
    across every source for > F2_SUSTAINED_S exits at its last real
    observed price, stamped f2_unquotable_last_price in the close
    record AND the verdict-ledger payload;
(b) an armed position with only a BRIEF quote gap does NOT fire;
(c) a NEVER-armed position dark for hours does NOT fire (pre-F2
    behavior, incl. no early finalization);
(d) quotable positions are untouched: hard stop and ladder legs
    behave exactly as before, no f2 stamp;
(e) dust guard: last real price < 10% of entry never F2-exits;
(f) hyperliquid is excluded (single pricing source);
(g) the no-price_fn backstop path: aggregator dark + reserves dark
    fires; aggregator dark + reserves ALIVE does not fire and the
    reserve price is what gets evaluated.

No network: every price source is stubbed. Temp ledgers throughout.
"""
import json
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import f2 as F2
from daemon.launch_scanner import grad as G
from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import ceo as C
from daemon.launch_scanner import mirror as M
from daemon.launch_scanner import momentum_lane as MO

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


class FakeKS:
    def __init__(self):
        self.halted = set()
        self.recorded = []

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append((lane, pnl))

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
          last_age_s=400.0, chain="solana", lane=None):
    """A position as the engines persist it: leg A banked at 2x,
    running peak 2.6x, last real quote `last_age_s` old."""
    pos = {
        "mint": "F2TESTpump", "symbol": "F2T", "chain": chain,
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
    if lane:
        pos["lane"] = lane
    return pos


def inject(mod, mint, pos):
    with mod._ledger_lock():
        d = mod._read()
        d["positions"][mint] = pos
        mod._write(d)


def load(mod):
    return mod._load()


NOW = time.time()
ENGINES = []


def fresh(mod, fname, lane=None):
    tmpd = tempfile.mkdtemp(prefix="f2-test-")
    boot(mod, tmpd, fname)
    return tmpd


# ---------- f2.check unit edges ----------
p = mkpos(NOW, last_age_s=F2.F2_SUSTAINED_S - 1)
check("unit: 299s silence does not fire",
      F2.check(p, NOW) is None)
p = mkpos(NOW, last_age_s=F2.F2_SUSTAINED_S + 1)
st = F2.check(p, NOW)
check("unit: 301s silence fires with stamp fields",
      st is not None and st["last_real_price"] == 1.8
      and abs(st["unquotable_seconds"] - (F2.F2_SUSTAINED_S + 1)) < 2
      and st["last_quote_ts"] > 0)
p = mkpos(NOW)
del p["peak_price"]  # peak provable only from banked leg prices
p["legs"]["c"]["peak"] = 0
check("unit: armed via recorded leg exit prices (2.0x leg A)",
      F2.check(p, NOW) is not None)
p = mkpos(NOW, chain="hyperliquid")
check("unit: hyperliquid excluded", F2.check(p, NOW) is None)
p = mkpos(NOW, last_px=0.05)
check("unit: dust last price (5% of entry) refused",
      F2.check(p, NOW) is None)
p = mkpos(NOW, leg_a_done=False)
check("unit: leg A not banked -> not armed",
      F2.check(p, NOW) is None)


# ---------- (a)+(b)+(c)+(d) per engine via price_fn (tape path) ----
def engine_matrix(mod, fname, label, lane=None):
    now = time.time()
    # (a) armed + sustained dark -> F2 at last real price, stamped
    fresh(mod, fname, lane)
    inject(mod, "F2TESTpump", mkpos(now, lane=lane))
    closes = mod.poll_exits(lambda c, m, s: 0.0)
    d = load(mod)
    ok = len(closes) == 1 and len(d["closes"]) == 1
    rec = (d["closes"] or [{}])[0]
    check(label + " (a) F2 fires on sustained dark armed position",
          ok and rec.get("closed_reason") == F2.F2_REASON
          and "F2TESTpump" not in d["positions"])
    f2s = rec.get("f2") or {}
    check(label + " (a) stamp: last price/ts/unquotable seconds",
          f2s.get("last_real_price") == 1.8
          and f2s.get("last_quote_ts", 0) > 0
          and f2s.get("unquotable_seconds", 0) >= F2.F2_SUSTAINED_S)
    legs = rec.get("legs") or {}
    check(label + " (a) remaining legs exit at ~last real price",
          legs.get("b", {}).get("exit_reason") == F2.F2_REASON
          and legs.get("c", {}).get("exit_reason") == F2.F2_REASON
          and 1.5 < legs.get("b", {}).get("exit_price", 0) <= 1.8)
    # (b) brief gap -> no fire
    fresh(mod, fname, lane)
    inject(mod, "F2TESTpump", mkpos(now, last_age_s=60.0, lane=lane))
    closes = mod.poll_exits(lambda c, m, s: 0.0)
    d = load(mod)
    check(label + " (b) brief 60s gap does NOT fire",
          closes == [] and "F2TESTpump" in d["positions"]
          and d["closes"] == [])
    # (c) never armed + hours dark -> no fire, still open
    fresh(mod, fname, lane)
    inject(mod, "F2TESTpump",
           mkpos(now, peak=1.3, leg_a_done=False, last_px=1.2,
                 last_age_s=7200.0, lane=lane))
    closes = mod.poll_exits(lambda c, m, s: 0.0)
    d = load(mod)
    check(label + " (c) never-armed dark position untouched",
          closes == [] and "F2TESTpump" in d["positions"])
    # (d1) quotable armed position at 0.4x -> hard stop, no f2 stamp
    fresh(mod, fname, lane)
    inject(mod, "F2TESTpump", mkpos(now, lane=lane))
    closes = mod.poll_exits(lambda c, m, s: 0.4)
    d = load(mod)
    rec = (d["closes"] or [{}])[0]
    check(label + " (d) quotable 0.4x still hard-stops, no F2",
          len(closes) == 1
          and rec.get("closed_reason") == "hard_stop_-50pct"
          and "f2" not in rec)
    # (d2) fresh position at 2.05x -> leg A banks, stays open
    fresh(mod, fname, lane)
    inject(mod, "F2TESTpump",
           mkpos(now, peak=1.0, leg_a_done=False, last_px=1.0,
                 last_age_s=5.0, lane=lane))
    closes = mod.poll_exits(lambda c, m, s: 2.05)
    d = load(mod)
    pos = d["positions"].get("F2TESTpump") or {}
    check(label + " (d) quotable 2.05x banks leg A, position open",
          closes == [] and pos.get("legs", {}).get("a", {}).get("done")
          and pos["legs"]["a"]["exit_reason"] == "leg_a_2x")


engine_matrix(G, "grad_ledger.json", "grad")
engine_matrix(C, "ceo_ledger.json", "ceo")
engine_matrix(M, "degen_mirror_ledger.json", "mirror",
              lane="degen_mirror")
engine_matrix(MO, "momentum_ledger.json", "momentum")


# ---------- grad_raw: the isolated grad engine instance ----------
now = time.time()
tmpd = tempfile.mkdtemp(prefix="f2-raw-")
GR.LEDGER_PATH = os.path.join(tmpd, "grad_raw_ledger.json")
eng = GR.make_engine()
eng._ks = FakeKS()
eng.ensure_ledger()
inject(eng, "F2TESTpump", mkpos(now))
closes = eng.poll_exits(lambda c, m, s: 0.0)
d = eng._load()
rec = (d["closes"] or [{}])[0]
check("grad_raw (a) F2 fires in the raw lane's own engine",
      len(closes) == 1 and rec.get("closed_reason") == F2.F2_REASON
      and (rec.get("f2") or {}).get("last_real_price") == 1.8
      and rec.get("lane") == "grad_raw")


# ---------- verdict-ledger stamp (grad chain) ----------
tmpd = tempfile.mkdtemp(prefix="f2-chain-")
boot(G, tmpd, "grad_ledger.json")
inject(G, "F2TESTpump", mkpos(time.time()))
G.poll_exits(lambda c, m, s: 0.0)
chain = os.path.join(tmpd, "verdict_ledger", "events.jsonl")
found = None
if os.path.exists(chain):
    for line in open(chain):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "close" and \
                (r.get("payload") or {}).get("mint") == "F2TESTpump":
            found = r
pl = (found or {}).get("payload") or {}
check("verdict chain: F2 close carries the stamp in its payload",
      pl.get("closed_reason") == F2.F2_REASON
      and (pl.get("f2") or {}).get("last_real_price") == 1.8
      and (pl.get("f2") or {}).get("unquotable_seconds", 0)
      >= F2.F2_SUSTAINED_S)


# ---------- (f) mirror hyperliquid exclusion, end to end ----------
fresh(M, "degen_mirror_ledger.json", "degen_mirror")
inject(M, "F2TESTpump",
       mkpos(time.time(), chain="hyperliquid", lane="hl_mirror"))
closes = M.poll_exits(lambda c, m, s: 0.0)
d = load(M)
check("mirror (f) armed HL position dark -> no F2 (single source)",
      closes == [] and "F2TESTpump" in d["positions"])


# ---------- (g) backstop path: reserves cross-probe (ceo) ----------
now = time.time()
fresh(C, "ceo_ledger.json")
inject(C, "F2TESTpump", mkpos(now))
_real_qp, _real_res = C.quote_price, C._reserves


class _ResStub:
    def __init__(self, px):
        self.px = px

    def price_for_position(self, pos):
        return self.px, "reserves:stub"


try:
    C.quote_price = lambda chain, mint, spot_index=None: 0.0
    C._reserves = _ResStub(1.7)  # reserves ALIVE at 1.7
    closes = C.poll_exits()
    d = load(C)
    pos = d["positions"].get("F2TESTpump") or {}
    jq = pos.get("quote_journal") or []
    check("ceo (g) reserves alive -> no F2, reserve price evaluated",
          closes == [] and "F2TESTpump" in d["positions"]
          and jq and abs(jq[-1]["px"] - 1.7) < 1e-9)
    C._reserves = _ResStub(0.0)  # now BOTH sources dark
    # The reserves-alive eval above journaled a FRESH real quote —
    # the sustained-silence clock rightly restarted there. Age the
    # journal to simulate 400s of subsequent total darkness.
    with C._ledger_lock():
        d = C._read()
        d["positions"]["F2TESTpump"]["quote_journal"] = [
            {"ts": now - 900.0, "px": 2.6, "src": "poll"},
            {"ts": now - 400.0, "px": 1.8, "src": "poll"}]
        C._write(d)
    closes = C.poll_exits()
    d = load(C)
    rec = (d["closes"] or [{}])[0]
    check("ceo (g) both sources dark -> F2 fires on backstop path",
          len(closes) == 1
          and rec.get("closed_reason") == F2.F2_REASON
          and (rec.get("f2") or {}).get("last_real_price") == 1.8)
finally:
    C.quote_price, C._reserves = _real_qp, _real_res


# ---------- (h) 2026-10-24 phantom: upside guards ----------
# The phantom (mint BVY5qMsi9G5f, CI): entry on a 4.73e-12 fiction
# print, then a 1.027e-4 print (21.7M-x entry) journaled as "real"
# via the external price_fn; legs A/B banked as 2x/3x on it and F2
# banked leg C at last_real_price=1.027e-4. Three new guards:
#  (h1) f2.check refuses a >50x last price on a dead tape
#       (flag-and-refuse: warning logged, None returned);
#  (h2) a sane 3x F2 exit still banks normally;
#  (h3) end to end (grad engine): dark cycle + absurd journaled
#       last price -> NO F2 close, position stays open;
#  (h4) end to end (grad engine): absurd LIVE print -> legs do NOT
#       bank, tracked peak NOT corrupted, journal carries "skipped".
import logging as _logging


class _FlagCapture(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


_cap = _FlagCapture()
_f2_logger = _logging.getLogger("forex_agent.daemon.launch_scanner.f2")
_f2_logger.addHandler(_cap)
try:
    # (h1) 16M-x last print on a dead tape: refused + flagged
    _cap.records.clear()
    p = mkpos(NOW, entry=1.0, peak=2.6, leg_a_done=True,
              last_px=16_000_000.0, last_age_s=400.0)
    st = F2.check(p, NOW)
    check("unit (h1) 16M-x last print on dead tape refused",
          st is None)
    check("unit (h1) refusal is flagged (warning logged)",
          any("upside refusal" in m for m in _cap.records))
    # boundary: 51x refused, 49x fires
    p = mkpos(NOW, entry=1.0, peak=60.0, leg_a_done=True,
              last_px=51.0, last_age_s=400.0)
    check("unit (h1) 51x last print refused",
          F2.check(p, NOW) is None)
    p = mkpos(NOW, entry=1.0, peak=60.0, leg_a_done=True,
              last_px=49.0, last_age_s=400.0)
    st = F2.check(p, NOW)
    check("unit (h1) 49x last print still fires",
          st is not None and st["last_real_price"] == 49.0)
    # (h2) sane 3x F2 exit banks normally
    _cap.records.clear()
    p = mkpos(NOW, entry=1.0, peak=3.4, leg_a_done=True,
              last_px=3.0, last_age_s=400.0)
    st = F2.check(p, NOW)
    check("unit (h2) sane 3x last print banks normally",
          st is not None and st["last_real_price"] == 3.0)
    check("unit (h2) no refusal flagged for sane print",
          not any("upside refusal" in m for m in _cap.records))
finally:
    _f2_logger.removeHandler(_cap)


def _mkpos_absurd(now, lane=None):
    """Armed position whose journaled last real price is the phantom
    row: 21.7M-x entry, 400s old (sustained dark)."""
    pos = mkpos(now, entry=1.0, peak=2.6, leg_a_done=True,
                last_px=21_700_000.0, last_age_s=400.0, lane=lane)
    return pos


# (h3) grad engine, dark cycle, absurd journaled last price -> no close
now = time.time()
fresh(G, "grad_ledger.json")
inject(G, "PHANTOMpump", _mkpos_absurd(now))
closes = G.poll_exits(lambda c, m, s: 0.0)
d = load(G)
check("grad (h3) F2 refuses absurd last price: no close, still open",
      closes == [] and "PHANTOMpump" in d["positions"]
      and d["closes"] == [])

# (h4) grad engine, absurd LIVE print -> legs do not bank, peak clean
now = time.time()
fresh(G, "grad_ledger.json")
inject(G, "PHANTOMpump",
       mkpos(now, entry=1.0, peak=1.0, leg_a_done=False,
             last_px=1.0, last_age_s=5.0))
closes = G.poll_exits(lambda c, m, s: 21_700_000.0)
d = load(G)
pos = d["positions"].get("PHANTOMpump") or {}
legs = pos.get("legs") or {}
jq = pos.get("quote_journal") or []
check("grad (h4) absurd live print banks no legs",
      closes == [] and "PHANTOMpump" in d["positions"]
      and not legs.get("a", {}).get("done")
      and not legs.get("b", {}).get("done"))
check("grad (h4) tracked peak not corrupted by absurd print",
      float(pos.get("peak_price") or 0) <= 1.0)
check("grad (h4) journal carries the skipped refusal",
      any("skipped" in str(e) for e in jq))

# (h4-m) momentum engine, same guard ported 2026-11-14: absurd
# LIVE print -> legs do NOT bank, tracked peak NOT corrupted,
# journal carries "skipped". Rocco (2026-11-07) banked legs A/B
# as 2x/3x on three $43.12 prints (43,703x) before the tape
# reverted; this is the hole the port closes.
now = time.time()
fresh(MO, "momentum_ledger.json")
inject(MO, "PHANTOMpump",
       mkpos(now, entry=1.0, peak=1.0, leg_a_done=False,
             last_px=1.0, last_age_s=5.0))
closes = MO.poll_exits(lambda c, m, s: 21_700_000.0)
d = load(MO)
pos = d["positions"].get("PHANTOMpump") or {}
legs = pos.get("legs") or {}
jq = pos.get("quote_journal") or []
check("momentum (h4) absurd live print banks no legs",
      closes == [] and "PHANTOMpump" in d["positions"]
      and not legs.get("a", {}).get("done")
      and not legs.get("b", {}).get("done"))
check("momentum (h4) tracked peak not corrupted by absurd print",
      float(pos.get("peak_price") or 0) <= 1.0)
check("momentum (h4) journal carries the skipped refusal",
      any("skipped" in str(e) for e in jq))

# (h4-r) Rocco replay: three consecutive absurd polls, as on
# 2026-11-07 05:38:23-05:38:42 WAT — nothing banks, peak stays
# clean, every poll journals its refusal.
now = time.time()
fresh(MO, "momentum_ledger.json")
inject(MO, "ROCCOpump",
       mkpos(now, entry=0.00073556, peak=0.00073556,
             leg_a_done=False, last_px=0.00073556, last_age_s=5.0))
for _ in range(3):
    closes = MO.poll_exits(lambda c, m, s: 43.12)
d = load(MO)
pos = d["positions"].get("ROCCOpump") or {}
legs = pos.get("legs") or {}
jq = pos.get("quote_journal") or []
check("momentum (h4-r) Rocco replay: 3 absurd polls bank nothing",
      closes == [] and "ROCCOpump" in d["positions"]
      and not legs.get("a", {}).get("done")
      and not legs.get("b", {}).get("done")
      and not legs.get("c", {}).get("done"))
check("momentum (h4-r) Rocco replay: peak uncorrupted, 3 refusals",
      float(pos.get("peak_price") or 0) <= 0.00073556
      and sum(1 for e in jq if "skipped" in str(e)) == 3)

# (h4-b) backstop: position older than 24h on a sustained absurd
# print finalizes at 0 like unquotable, never banks a leg.
now = time.time()
fresh(MO, "momentum_ledger.json")
_old = mkpos(now, entry=1.0, peak=1.0, leg_a_done=False,
             last_px=1.0, last_age_s=5.0)
_old["entry_ts"] = now - 90000.0
inject(MO, "STALEpump", _old)
closes = MO.poll_exits(lambda c, m, s: 21_700_000.0)
d = load(MO)
rec = (d["closes"] or [{}])[0]
check("momentum (h4-b) absurd print past 24h finalizes at 0",
      len(closes) == 1
      and rec.get("closed_reason") == "time_stop_24h_unquotable"
      and "STALEpump" not in d["positions"])


print()
if fails:
    print("FAILURES:", len(fails))
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL F2 TESTS PASS")
