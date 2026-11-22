#!/usr/bin/env python3
"""Paper-mirror tests (pure logic + injected price feeds, no network).

Covers chain_costs (per-chain tx costs, the pre-registered cost gate)
and mirror.py (chain-aware $1 entries, the user's exit ladder, ledger,
idempotency, per-lane killswitch).
"""
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

import chain_costs
from daemon.launch_scanner import mirror as M

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- chain_costs ----------------
check("costs: ETH round-trip > 25% gate",
      chain_costs.round_trip_cost_pct("ethereum", 1.0) > 25.0)
blocked, why = chain_costs.cost_gate("ethereum", 1.0)
check("costs: ETH blocked by cost gate", blocked and "COST-BLOCKED" in why)
check("costs: ETH reason shows the math", "$" in why and "%" in why)

for ch in ("solana", "base", "hyperliquid"):
    blocked, _ = chain_costs.cost_gate(ch, 1.0)
    check("costs: %s passes cost gate" % ch, not blocked)

check("costs: base round-trip ≈ 12% + slippage",
      10.0 < chain_costs.round_trip_cost_pct("base", 1.0) < 20.0)
check("costs: HL round-trip tiny",
      chain_costs.round_trip_cost_pct("hyperliquid", 1.0) < 1.0)
check("costs: unknown chain -> 0 tx cost, gate open",
      chain_costs.tx_cost_usd("mystery") == 0.0)


# ---------------- mirror: test harness ----------------
TMPD = tempfile.mkdtemp(prefix="mirror-test-")
M.LEDGER_PATH = os.path.join(TMPD, "degen_mirror_ledger.json")


class FakeKS:
    halted = set()
    recorded = []

    @classmethod
    def reset(cls):
        cls.halted = set()
        cls.recorded = []

    def is_halted(self, lane):
        return lane in FakeKS.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        FakeKS.recorded.append((lane, pnl))

    def check_lane(self, lane):
        return {"lane": lane}


_RUNNER_TAPE = {"usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
                "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40}


def _patch_all(price=0.01, patch_quote=True):
    """Patch deps: veto passes, safety passes, fixed quote price, fake ks.

    patch_quote=False leaves the REAL quote_price in place so the
    entry-quote tests can drive it via _http_json/_fresh_tape stubs."""
    p1 = patch.object(M, "_veto")
    p2 = patch.object(M, "_safety")
    p3 = patch.object(M, "_ks", FakeKS())
    p4 = (patch.object(M, "quote_price", return_value=price)
          if patch_quote else None)
    p5 = patch.object(M, "_fresh_tape", return_value=dict(_RUNNER_TAPE))
    # Consecutive-confirmation gap zeroed: both reads still happen,
    # the suite just doesn't wait 5s between them.
    p6 = patch.object(M, "RUNNER_CONFIRM_GAP_S", 0.0)
    m_veto = p1.start()
    m_safety = p2.start()
    p3.start()
    if p4 is not None:
        p4.start()
    p5.start()
    p6.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = {"passed": True, "unknown": False}
    FakeKS.reset()
    # fresh ledger per test
    if os.path.exists(M.LEDGER_PATH):
        os.remove(M.LEDGER_PATH)
    return tuple(p for p in (p1, p2, p3, p4, p5, p6) if p is not None)


def _unpatch(ps):
    for p in ps:
        p.stop()


def _open(chain="solana", mint="MINT1", price=0.01, **kw):
    args = dict(chain=chain, source="test", mint=mint, symbol="TKN",
                score=70.0, tier="pick", mcap=500_000.0, liquidity=50_000.0)
    args.update(kw)
    return M.mirror_card(**args)


# entry: solana opens, quotes live, honest fill
ps = _patch_all(price=0.01)
ok, why = _open()
check("mirror: solana card opens $1 position", ok)
pos = M.open_positions().get("MINT1")
check("mirror: position recorded", pos is not None)
check("mirror: entry is adverse vs quoted (fills)",
      pos["entry_price"] >= 0.01)
check("mirror: gas deducted from notional",
      abs(pos["tokens"] * pos["entry_price"] - (1.0 - 0.001)) < 1e-6)
check("mirror: lane = degen_mirror", pos["lane"] == "degen_mirror")
check("mirror: entry snapshot kept",
      pos["entry_snapshot"]["score"] == 70.0
      and "cost_summary" in pos["entry_snapshot"])
# idempotent
ok2, why2 = _open()
check("mirror: duplicate mint rejected", not ok2)
_unpatch(ps)

# runner gate at entry (2026-10-21): Solana mirrors must be runner-grade
# on the live tape — thin liq / low mcap / falling / unknown all block.
ps = _patch_all(price=0.01)
with patch.object(M, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE, usd_liquidity=8_000.0)):
    ok, why = _open(mint="THIN1")
check("mirror: runner gate blocks thin liquidity",
      not ok and "runner gate" in why and "liq" in why)
with patch.object(M, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE,
                                    usd_market_cap=30_000.0)):
    ok, why = _open(mint="LOWMC")
check("mirror: runner gate blocks low mcap",
      not ok and "runner gate" in why and "mcap" in why)
with patch.object(M, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE, chg_m5=-3.0)):
    ok, why = _open(mint="FALL1")
check("mirror: runner gate blocks falling tape",
      not ok and "runner gate" in why and "not rising" in why)
with patch.object(M, "_fresh_tape", return_value=None):
    ok, why = _open(mint="DARK1")
check("mirror: runner gate fail-closed on unknown tape",
      not ok and "no live tape" in why)
check("mirror: gated entries open nothing",
      len(M.open_positions()) == 0)
ok, why = _open(mint="RUNNER1")
check("mirror: runner-grade tape opens", ok)
# Gate is Solana-only: a Base card never consults the tape.
with patch.object(M, "_fresh_tape", return_value=None) as mt:
    ok, why = _open(chain="base", mint="BASE1", price=0.02)
check("mirror: runner gate does not apply to base",
      ok and mt.call_count == 0)
# Consecutive confirmation (2026-10-21): the tape must pass on TWO
# consecutive fresh reads — a degraded or dark second read blocks.
with patch.object(M, "_fresh_tape",
                  side_effect=[dict(_RUNNER_TAPE),
                               dict(_RUNNER_TAPE, chg_m5=-2.0)]) as mt:
    ok, why = _open(mint="CONF1")
check("mirror: confirm read degraded (falling) blocks",
      not ok and "unconfirmed" in why and "not rising" in why
      and mt.call_count == 2)
with patch.object(M, "_fresh_tape",
                  side_effect=[dict(_RUNNER_TAPE), None]) as mt:
    ok, why = _open(mint="CONF2")
check("mirror: confirm read unknown blocks fail-closed",
      not ok and "unconfirmed" in why and "no live tape" in why
      and mt.call_count == 2)
with patch.object(M, "_fresh_tape",
                  side_effect=[dict(_RUNNER_TAPE),
                               dict(_RUNNER_TAPE)]) as mt:
    ok, why = _open(mint="CONF3")
check("mirror: two good reads confirm and open",
      ok and mt.call_count == 2)
# A first-read failure never pays for the confirmation read.
with patch.object(M, "_fresh_tape",
                  return_value=dict(_RUNNER_TAPE,
                                    usd_liquidity=8_000.0)) as mt:
    ok, why = _open(mint="CONF4")
check("mirror: first-read failure skips confirmation",
      not ok and "unconfirmed" not in why and mt.call_count == 1)
check("mirror: unconfirmed entries open nothing",
      "CONF1" not in M.open_positions()
      and "CONF2" not in M.open_positions()
      and "CONF4" not in M.open_positions())
_unpatch(ps)

# chain routing
ps = _patch_all()
ok, why = _open(chain="base", mint="MINTB", price=0.02)
check("mirror: base opens (base_mirror lane)",
      ok and M.open_positions()["MINTB"]["lane"] == "base_mirror")
posb = M.open_positions()["MINTB"]
check("mirror: base gas deducted ($0.03)",
      abs(posb["tokens"] * posb["entry_price"] - 0.97) < 1e-6)
ok, why = _open(chain="ethereum", mint="MINTE")
check("mirror: ethereum COST-BLOCKED (gas > edge room)",
      not ok and "COST-BLOCKED" in why)
check("mirror: blocked ETH opens nothing",
      "MINTE" not in M.open_positions())
ok, why = _open(chain="hyperliquid", mint="HLXYZ", price=0,
                extra={"pair": "XYZ/USDC", "spot_index": 42,
                       "mark_px": 1.25})
check("mirror: HL opens from listing mark price",
      ok and M.open_positions()["HLXYZ"]["lane"] == "hl_mirror")
check("mirror: HL stores spot_index",
      M.open_positions()["HLXYZ"]["spot_index"] == 42)
ok, why = _open(chain="doge", mint="MINTD")
check("mirror: unknown chain skipped", not ok)
_unpatch(ps)

# killswitch halt stops entries, not cards
ps = _patch_all()
FakeKS.halted.add("degen_mirror")
ok, why = _open(mint="MINTH")
check("mirror: halted lane refuses new entries",
      not ok and "halted" in why)
check("mirror: halted lane opens nothing",
      "MINTH" not in M.open_positions())
_unpatch(ps)

# veto re-check at entry
ps = _patch_all()
with patch.object(M, "_veto") as mv:
    mv.veto_check.return_value = {"passed": False,
                                  "vetoes": ["bundle: 80%"]}
    ok, why = _open(mint="MINTV")
check("mirror: veto at entry blocks",
      not ok and "veto" in why.lower())
_unpatch(ps)


# ---------------- exits: the user's ladder ----------------
def _open_at(price):
    ps = _patch_all(price=price)
    ok, why = _open(mint="MINTL", price=price)
    assert ok, why
    return ps


def _prices(seq):
    it = iter(seq)

    def fn(chain, mint, spot_index=None):
        try:
            return next(it)
        except StopIteration:
            return seq[-1]
    return fn


# leg A: 50% at 2x
ps = _open_at(0.01)
entry = M.open_positions()["MINTL"]["entry_price"]
closes = M.poll_exits(price_fn=_prices([entry * 2.0]))
pos = M.open_positions()["MINTL"]
check("exits: leg A closes at 2x", pos["legs"]["a"]["done"])
check("exits: leg A realized ≈ 50% x 2x minus costs",
      0.9 < pos["legs"]["a"]["realized_usd"] < 1.1)
check("exits: legs B/C still open",
      not pos["legs"]["b"]["done"] and not pos["legs"]["c"]["done"])
check("exits: no full close yet", closes == [])
_unpatch(ps)

# leg B: 25% at 3x (after A)
ps = _open_at(0.01)
entry = M.open_positions()["MINTL"]["entry_price"]
M.poll_exits(price_fn=_prices([entry * 2.0]))
closes = M.poll_exits(price_fn=_prices([entry * 3.0]))
pos = M.open_positions().get("MINTL")
check("exits: leg B closes at 3x",
      pos is None or pos["legs"]["b"]["done"])
if pos is not None:
    check("exits: leg B realized ≈ 25% x 3x ($0.75 gross)",
          0.65 < pos["legs"]["b"]["realized_usd"] < 0.85)
_unpatch(ps)

# HL mark-price fallback: no live quote -> uses the watcher's mark_px
ps = _patch_all(price=0.0)  # quote_price returns 0
ok, why = M.mirror_card(chain="hyperliquid", source="hl-listing",
                        mint="HLFALL", symbol="XYZ", score=70.0,
                        tier="listing",
                        extra={"pair": "XYZ/USDC", "spot_index": 7,
                               "mark_px": 1.25})
check("mirror: HL falls back to listing mark_px",
      ok and abs(M.open_positions()["HLFALL"]["entry_price"] - 1.25) < 0.2)
_unpatch(ps)

# leg C: 30% trailing stop
ps = _open_at(0.01)
entry = M.open_positions()["MINTL"]["entry_price"]
M.poll_exits(price_fn=_prices([entry * 2.0]))    # A done
M.poll_exits(price_fn=_prices([entry * 4.0]))    # B done, C peak=4x
closes = M.poll_exits(price_fn=_prices([entry * 2.7]))  # -32.5% off peak
pos = M.open_positions().get("MINTL")
check("exits: leg C trails off peak", pos is None or pos["legs"]["c"]["done"])
check("exits: ladder complete -> position closed", closes != [])
if closes:
    c = closes[0]
    check("exits: close record has entry snapshot + pnl",
          "entry_snapshot" in c and isinstance(c["pnl_usd"], float))
    check("exits: profitable ladder -> positive pnl", c["pnl_usd"] > 0)
    check("exits: lane trade recorded",
          any(lane == "degen_mirror" for lane, _ in FakeKS.recorded))
_unpatch(ps)

# hard stop: -50% closes everything
ps = _open_at(0.01)
entry = M.open_positions()["MINTL"]["entry_price"]
closes = M.poll_exits(price_fn=_prices([entry * 0.4]))
check("exits: hard stop closes all legs",
      closes != [] and closes[0]["closed_reason"] == "hard_stop_-50pct")
check("exits: hard stop pnl ≈ -50%+",
      -0.7 < closes[0]["pnl_usd"] < -0.3)
_unpatch(ps)

# time stop: 24h
ps = _open_at(0.01)
pos = M.open_positions()["MINTL"]
pos["entry_ts"] = time.time() - 25 * 3600
with open(M.LEDGER_PATH, "w") as f:
    json.dump({"positions": {"MINTL": pos}, "closes": []}, f)
closes = M.poll_exits(price_fn=_prices([0.011]))
check("exits: 24h time stop closes",
      closes != [] and closes[0]["closed_reason"] == "time_stop_24h")
_unpatch(ps)

# unknown price: skip cycle, never force-close
ps = _open_at(0.01)
closes = M.poll_exits(price_fn=_prices([0.0]))
check("exits: zero price skips (no force-close)",
      closes == [] and "MINTL" in M.open_positions())
_unpatch(ps)

# unknown price BUT past the 24h backstop: dead/delisted -> write off at 0
ps = _open_at(0.01)
pos = M.open_positions()["MINTL"]
pos["entry_ts"] = time.time() - 25 * 3600
with open(M.LEDGER_PATH, "w") as f:
    json.dump({"positions": {"MINTL": pos}, "closes": []}, f)
closes = M.poll_exits(price_fn=_prices([0.0]))
check("exits: unquotable + past 24h closes as full loss",
      closes != [] and closes[0]["closed_reason"] == "time_stop_24h_unquotable"
      and "MINTL" not in M.open_positions()
      and closes[0]["pnl_usd"] < -0.9)
_unpatch(ps)

# ledger stats
ps = _patch_all(price=0.01)
M.mirror_card(chain="solana", source="t", mint="S1", symbol="A",
              score=60, tier="pick", liquidity=100_000)
M.mirror_card(chain="solana", source="t", mint="S2", symbol="B",
              score=60, tier="pick", liquidity=100_000)
stats = M.lane_stats()
check("mirror: lane_stats aggregates closes", isinstance(stats, dict))
_unpatch(ps)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    raise SystemExit(1)
print("ALL MIRROR TESTS PASSED")

# ---------------- audit 2026-10-18: lock + single-gas hard stop ----------------
# ledger lock round-trips and serializes read-modify-write
with M._ledger_lock():
    d = M._read()
    d["positions"]["LOCKTEST"] = {"x": 1}
    assert M._write(d) is True
with M._ledger_lock():
    d2 = M._read()
check("audit: ledger lock read-modify-write round-trips",
      d2["positions"].get("LOCKTEST") == {"x": 1})
with M._ledger_lock():
    d3 = M._read()
    del d3["positions"]["LOCKTEST"]
    M._write(d3)

# hard stop on Base charges ONE tx gas ($0.03), not 3x ($0.09)
ps = _patch_all(price=0.01)
ok, why = M.mirror_card(chain="base", source="t", mint="MINTB2",
                        symbol="B", score=60, tier="pick",
                        liquidity=50000.0)
assert ok, why
entry = M.open_positions()["MINTB2"]["entry_price"]
closes = M.poll_exits(price_fn=_prices([entry * 0.4]))
assert closes != []
realized = sum(v["realized_usd"] for v in closes[0]["legs"].values())
# gross ≈ 0.384; single gas -> ~0.354; triple gas would be ~0.294
check("audit: hard stop charges single tx gas (not 3x)",
      0.33 < realized < 0.38)
check("audit: hard-stop legs share one exit reason",
      all(v["exit_reason"] == "hard_stop_-50pct"
          for v in closes[0]["legs"].values()))
_unpatch(ps)

# ---------------- peak_mult on closes (2026-10-20) ----------------
# Peaked path: rises to 2.6x, then hard-stops. The close must record
# the tracked peak — round-tripper leakage (peaked >=2x, closed <1x)
# is measured from this field; closes carried no peak before.
ps = _open_at(0.01)
entry = M.open_positions()["MINTL"]["entry_price"]
M.poll_exits(price_fn=_prices([entry * 2.6]))   # leg A banks at 2x
closes = M.poll_exits(price_fn=_prices([entry * 0.4]))  # hard stop rest
check("peak: peaked position finalized by hard stop", len(closes) == 1)
if closes:
    c = closes[0]
    check("peak: close records peak_price/peak_mult ~= 2.6x (tracked)",
          abs(c.get("peak_mult", 0) - 2.6) < 0.05
          and abs(c.get("peak_price", 0) - entry * 2.6) < entry * 0.05)
    check("peak: peak_source = tracked, no peak_unknown flag",
          c.get("peak_source") == "tracked" and "peak_unknown" not in c)
_unpatch(ps)

# Straight-to-stop: never rose. The peak is seeded at the entry fill,
# so peak_mult honestly reads 1.0 — the entry price was observed and
# nothing higher ever was.
ps = _open_at(0.01)
entry = M.open_positions()["MINTL"]["entry_price"]
M.poll_exits(price_fn=_prices([entry * 0.9]))
closes = M.poll_exits(price_fn=_prices([entry * 0.4]))
check("peak: straight-to-stop finalizes", len(closes) == 1)
if closes:
    check("peak: never-rose position peak_mult ~= 1.0",
          abs(closes[0].get("peak_mult", 0) - 1.0) < 0.02)
_unpatch(ps)

# Legacy position (opened before peak tracking): no peak_price key,
# only leg C's recorded peak (1.8x). The close must report that
# recorded value as a flagged lower bound — never an invented number.
ps = _open_at(0.01)
entry = M.open_positions()["MINTL"]["entry_price"]
with M._ledger_lock():
    d = M._read()
    pos = d["positions"]["MINTL"]
    del pos["peak_price"]
    pos["legs"]["c"]["peak"] = entry * 1.8
    M._write(d)
closes = M.poll_exits(price_fn=_prices([entry * 0.4]))
check("peak: legacy position finalizes", len(closes) == 1)
if closes:
    c = closes[0]
    check("peak: legacy close peak_mult = recorded leg-C peak (1.8x)",
          abs(c.get("peak_mult", 0) - 1.8) < 0.05)
    check("peak: legacy close flagged legacy_lower_bound",
          c.get("peak_source") == "legacy_lower_bound")
_unpatch(ps)

# ---------------- entry-quote repair (2026-10-21) ----------------
# Grok/Speed autopsy: gate confirmed x2, then "no live quote" — the
# quote read pairs[0] blindly on a single no-retry fetch while the
# gate selects the max-liquidity pair through a curl+retry fetcher.
_PAIRS_MIXED = [
    {"priceUsd": None, "liquidity": {"usd": 900_000.0}},
    {"priceUsd": "0.05", "liquidity": {"usd": 5_000.0}},
    {"priceUsd": "0.02", "liquidity": {"usd": 120_000.0}},
]
with patch.object(M, "_http_json", return_value=list(_PAIRS_MIXED)):
    _q = M.quote_price("solana", "MINTQ")
check("mirror quote: max-liquidity priced pair wins over pairs[0]",
      _q == 0.02)
check("mirror quote: detail trail records the pairs fetch",
      "pairs fetch: 3 pair(s), 2 priced" in M._LAST_QUOTE_DETAIL)

# Pairs endpoint down, tape priced: entry proceeds at the tape price,
# adverse fill still applied on top (honest fills unchanged).
ps = _patch_all(patch_quote=False)
with patch.object(M, "_http_json", return_value=None), \
        patch.object(M, "_fresh_tape",
                     return_value=dict(_RUNNER_TAPE, price_usd=0.02)), \
        patch.object(M, "QUOTE_RETRY_GAP_S", 0.0):
    ok, why = _open(mint="MINTTAPE")
pos = M.open_positions().get("MINTTAPE")
check("mirror quote: tape fallback opens the entry", ok and pos is not None)
check("mirror quote: quoted price is the tape price",
      pos is not None and pos["quoted_price"] == 0.02)
check("mirror quote: adverse fill still applied over the tape price",
      pos is not None and pos["entry_price"] >= 0.02)
_unpatch(ps)

# Every source dark: the skip stays — and the reason names each lookup.
ps = _patch_all(patch_quote=False)
with patch.object(M, "_http_json", return_value=None), \
        patch.object(M, "QUOTE_RETRY_GAP_S", 0.0):
    ok, why = _open(mint="MINTDARK")
check("mirror quote: all sources dark still skips", not ok)
check("mirror quote: skip reason names pairs fetch, tape fallback, retry",
      "no live quote" in why and "pairs fetch" in why
      and "tape fallback" in why and "pairs retry" in why)
_unpatch(ps)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    raise SystemExit(1)
print("ALL MIRROR TESTS PASSED (incl. audit fixes)")
