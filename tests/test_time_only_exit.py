#!/usr/bin/env python3
"""Time-only exit tests (Batch 3, 2026-11-17).

The 24h stop must fire against the LAST KNOWN price with NO
price read at all — even when every pricing source is down
(Helius spent, Jupiter dark, quote_price raising). A 0.0 curve
price never floors an exit: no real price on record means the
position closes unquotable at 0, never at its peak or entry.

Plain check()-style script (repo convention): run with the venv
python, temp HOME via FOREX_AGENT_HOME so the live ledgers are
never touched.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CHECKS = []


def check(name, cond, extra=""):
    CHECKS.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""))


def _fresh_grad():
    """Fresh isolated grad.py against a temp HOME."""
    home = tempfile.mkdtemp(prefix="time-exit-test-")
    os.environ["FOREX_AGENT_HOME"] = home
    spec = importlib.util.spec_from_file_location(
        "grad_time_exit_test",
        os.path.join(REPO, "daemon", "launch_scanner", "grad.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, home


ENTRY = 0.000025
TOKENS = 1_000_000.0
NOTIONAL = 25.0
MINT = "TimeExitMint111111111111111111111111111111"


def _seed(age_s=25 * 3600.0, journal=None):
    """Seeded open position (same shape as the snapshot harness
    _seed_position), with an optional quote journal."""
    now = time.time()
    pos = {
        "mint": MINT, "symbol": "TIME", "chain": "solana",
        "source": "graduation", "lane": "grad",
        "score": 0.0, "tier": "graduation",
        "entry_ts": now - age_s, "quoted_price": ENTRY,
        "entry_price": ENTRY, "notional_usd": NOTIONAL,
        "peak_price": ENTRY * 3.0, "peak_ts": now - age_s,
        "born_ts": now - age_s - 30, "seen_ts": now - age_s,
        "decided_ts": now - age_s,
        "tokens": TOKENS, "gas_in_usd": 0.001,
        "legs": {
            "a": {"frac": 0.50, "target": 2.0, "done": False,
                  "exit_price": 0, "realized_usd": 0},
            "b": {"frac": 0.25, "target": 3.0, "done": False,
                  "exit_price": 0, "realized_usd": 0},
            "c": {"frac": 0.25, "trail": 0.30, "peak": ENTRY * 3.0,
                  "done": False, "exit_price": 0,
                  "realized_usd": 0},
        },
        "realized_usd": 0.0, "status": "open",
        "spot_index": None, "pair": "Pair111111111111111111111",
        "entry_snapshot": {
            "score": 0.0, "tier": "graduation", "mcap": 25000.0,
            "liquidity": 50000.0, "quoted_price": ENTRY,
            "filled_price": ENTRY, "cost_summary": {},
            "graduation": {},
            "safety": {"score": 0, "flags": [], "source": ""},
        },
    }
    if journal is not None:
        pos["quote_journal"] = journal
    return pos


def _write(G, positions):
    with open(G.LEDGER_PATH, "w") as f:
        json.dump({"positions": positions, "closes": [],
                   "bankroll": 10000.0}, f)


def _read(G):
    with open(G.LEDGER_PATH) as f:
        return json.load(f)


def _raise(*a, **k):
    raise RuntimeError("pricing is down (test stub)")


def main():
    G, home = _fresh_grad()
    now = time.time()

    # ---- pure predicate: should_exit_on_time -------------------
    aged = _seed(25 * 3600.0)
    fire, reason = G.should_exit_on_time(aged, now)
    check("aged 25h fires time_stop_24h",
          fire is True and reason == "time_stop_24h", (fire, reason))
    young = _seed(3600.0)
    fire, reason = G.should_exit_on_time(young, now)
    check("young 1h does not fire",
          fire is False and reason is None, (fire, reason))
    edge = _seed(24 * 3600.0)
    fire, _r = G.should_exit_on_time(edge, edge["entry_ts"] + 24 * 3600.0)
    check("exactly 24h does not fire (strict >)",
          fire is False, fire)
    fire, _r = G.should_exit_on_time(
        edge, edge["entry_ts"] + 24 * 3600.0 + 1.0)
    check("24h + 1s fires", fire is True, fire)
    fire, reason = G.should_exit_on_time({}, now)
    check("missing entry_ts never fires",
          fire is False and reason is None, (fire, reason))
    fire, reason = G.should_exit_on_time({"entry_ts": 0}, now)
    check("zero entry_ts never fires",
          fire is False and reason is None, (fire, reason))

    # TWAP mode: 60-minute max hold, own reason.
    old_slices, old_hold = G.TWAP_SLICES, G.TWAP_MAX_HOLD_S
    try:
        G.TWAP_SLICES = [0.25, 0.25, 0.25, 0.25]
        G.TWAP_MAX_HOLD_S = 3600
        fire, reason = G.should_exit_on_time(_seed(3700.0), now)
        check("TWAP 61min fires twap_time_stop_60min",
              fire is True and reason == "twap_time_stop_60min",
              (fire, reason))
        fire, _r = G.should_exit_on_time(_seed(3500.0), now)
        check("TWAP 58min does not fire", fire is False, fire)
    finally:
        G.TWAP_SLICES, G.TWAP_MAX_HOLD_S = old_slices, old_hold

    # ---- pure helper: last_known_price -------------------------
    journal = [
        {"ts": now - 300, "px": ENTRY * 2.0, "src": "dex"},
        {"ts": now - 60, "px": ENTRY * 1.2, "src": "dex"},
    ]
    px = G.last_known_price(_seed(25 * 3600.0, journal=journal))
    check("last known price is the LATEST quote, not the max",
          abs(px - ENTRY * 1.2) < 1e-12, px)
    # A 0.0 journal entry (corrupt / dark read) is ignored, never
    # taken as the price.
    journal0 = journal + [{"ts": now, "px": 0.0, "src": "dex"}]
    px = G.last_known_price(_seed(25 * 3600.0, journal=journal0))
    check("0.0 journal entry never floors the price",
          abs(px - ENTRY * 1.2) < 1e-12, px)
    px = G.last_known_price(_seed(25 * 3600.0, journal=[]))
    check("empty journal -> 0.0 (unquotable)", px == 0.0, px)
    px = G.last_known_price(_seed(25 * 3600.0))
    check("missing journal -> 0.0 (unquotable)", px == 0.0, px)
    # The peak (3x) is NOT a fallback: last real quote is 1.2x.
    pos = _seed(25 * 3600.0, journal=journal)
    check("peak is never the fallback",
          G.last_known_price(pos) < pos["peak_price"],
          (G.last_known_price(pos), pos["peak_price"]))

    # ---- poll_time_exits with pricing DISABLED -----------------
    # Every pricing source raises: Jupiter stub, quote_price,
    # and any price_fn a caller might pass. The stop must still
    # fire — it performs no price read at all.
    G._jup = None
    old_quote = G.quote_price
    G.quote_price = _raise
    try:
        # Case A: aged, last real quote 1.2x (peak 3x — must NOT
        # bank the peak).
        _write(G, {MINT: _seed(25 * 3600.0, journal=journal)})
        closes = G.poll_time_exits()
        d = _read(G)
        check("pricing-down: aged position closes",
              len(closes) == 1 and MINT not in d["positions"],
              (len(closes), list(d["positions"])))
        rec = closes[0] if closes else {}
        check("pricing-down: reason is time_stop_24h",
              rec.get("closed_reason") == "time_stop_24h",
              rec.get("closed_reason"))
        mult = rec.get("realized_mult")
        check("pricing-down: exits at last known 1.2x, not peak 3x",
              mult is not None and 1.05 < mult < 1.30, mult)
        check("pricing-down: profitable at 1.2x",
              (rec.get("pnl_usd") or 0) > 0, rec.get("pnl_usd"))

        # Case B: aged, NO real price on record -> unquotable at 0.
        _write(G, {MINT: _seed(25 * 3600.0, journal=[])})
        closes = G.poll_time_exits()
        d = _read(G)
        rec = closes[0] if closes else {}
        check("unquotable: closes time_stop_24h_unquotable",
              rec.get("closed_reason") == "time_stop_24h_unquotable",
              rec.get("closed_reason"))
        check("unquotable: pnl is the full -$25 notional",
              abs((rec.get("pnl_usd") or 0) + 25.0) < 0.01,
              rec.get("pnl_usd"))
        check("unquotable: position left the book",
              MINT not in d["positions"], list(d["positions"]))

        # Case C: young position stays open, nothing returned.
        _write(G, {MINT: _seed(3600.0, journal=journal)})
        closes = G.poll_time_exits()
        d = _read(G)
        check("young position stays open",
              closes == [] and MINT in d["positions"],
              (closes, list(d["positions"])))

        # Case D: poll_exits with a RAISING price_fn still closes
        # nothing new here (already closed by the time-only poll),
        # but the time-only poll itself never called the fn: prove
        # by calling poll_time_exits with a raising price_fn arg
        # is impossible (it takes none) — instead prove the
        # function signature takes no price_fn at all.
        import inspect
        sig = inspect.signature(G.poll_time_exits)
        check("poll_time_exits takes no price_fn argument",
              len(sig.parameters) == 0, str(sig))
    finally:
        G.quote_price = old_quote

    # ---- trader wiring ------------------------------------------
    for script in ("grad-trader", "grad-select-trader",
                   "grad-raw-trader", "tranche-trader",
                   "tranche-protect-trader"):
        path = os.path.join(REPO, "scripts", script)
        with open(path) as f:
            src = f.read()
        check("trader %s calls poll_time_exits before poll_exits"
              % script,
              "poll_time_exits() + " in src
              and "poll_time_exits" in src, script)

    failed = [c for c in CHECKS if not c[1]]
    print("TIME-ONLY EXIT: %d checks, %d failed"
          % (len(CHECKS), len(failed)))
    if failed:
        for name, _ok, extra in failed:
            print("  FAILED:", name, extra)
        return 1
    print("TIME-ONLY EXIT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
