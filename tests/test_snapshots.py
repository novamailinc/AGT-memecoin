#!/usr/bin/env python3
"""Snapshot-test harness (2026-11-17, Batch 2).

Pins instruction bytes, parser outputs, curve-stat parsing,
and ladder exit decisions to committed JSON under
tests/snapshots/snapshots/. Runs fully OFFLINE on recorded
fixtures — no Helius, no Jupiter, no DexScreener — so the suite
keeps running through every quota drought.

Usage:
    python3 tests/test_snapshots.py            # compare
    python3 tests/test_snapshots.py --update   # rewrite snapshots

--update rewrites a snapshot ONLY when passed explicitly, for
an intended behaviour change (the pumpfun-bonkfun-bot rule).
A mismatch prints the FIRST difference path
(_first_difference), not a wall of JSON.

Groups:
  parsers       pumpportal parse outputs per raw fixture; every
                raw pumpportal_*.json fixture must have a parser
                snapshot.
  curve_stats   _curve_stats over the recorded signatures page
                (one-call budget asserted) + creator-from-tx +
                migration-tx pricing over the recorded tx.
  exit_decisions  the real grad.poll_exits ladder over scripted
                price series (temp HOME, price_fn prices every
                mint, Jupiter disabled): moon/fade, dump, bleed,
                exact-2x, just-below-2x, zero price, aged zero
                price, 24h stop, exact trail, phantom print.
                Volatile fields (close_ts, peak_ts) are never
                pinned — only legs, close reason, realized_mult,
                pnl_usd.
  instructions  Phase 2 signed-tx template + Jito bundle payload
                from the dry-run's canned swap, with a FIXED
                dummy keypair seed (deterministic Ed25519).
                The keypair is a test dummy, never a real key.

Bot modules are imported lazily inside the group functions,
after sys.path is set — the _fakes module itself imports
nothing from the bot at module level.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time

# Temp HOME first (tranche-test pattern): every bot import in
# this process must see the scratch ledger, never the live one.
_TMP_HOME = tempfile.mkdtemp(prefix="snapshot-test-")
os.environ["FOREX_AGENT_HOME"] = _TMP_HOME

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, os.path.join(REPO, "tests", "snapshots"))
sys.path.insert(0, REPO)

from _fakes import (  # noqa: E402  (path setup above is required)
    creator_and_tx_price_fixture, curve_stats_fixture,
    first_difference, load_fixture, load_snapshot,
    parse_migration_fixture, parse_new_token_fixture,
    save_snapshot)

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _compare_or_update(name, actual, update):
    """Compare actual against the committed snapshot, or rewrite
    it under --update. Returns True when the group passes."""
    snap = load_snapshot(name)
    if update:
        save_snapshot(name, actual)
        print("UPDATED %s" % name)
        return True
    if snap is None:
        check("snapshot %s exists" % name, False,
              "missing — run with --update to create it")
        return False
    diff = first_difference(actual, snap)
    check("snapshot %s matches" % name, diff is None,
          diff or "")
    return diff is None


# ---- group: parsers -------------------------------------------------

def group_parsers(update):
    new = parse_new_token_fixture()
    mig = parse_migration_fixture()
    # created_ts is receipt time (time.time() at parse), not
    # parser behaviour — never pinned; strip it from both.
    for item in (new, mig):
        item.pop("created_ts", None)
    actual = {
        "pumpportal_new_token.json": new,
        "pumpportal_migration.json": mig,
    }
    # Every raw pumpportal fixture must have a parser snapshot.
    raw = sorted(f for f in os.listdir(
        os.path.join(REPO, "tests", "snapshots", "fixtures"))
        if f.startswith("pumpportal_"))
    missing = [f for f in raw if f not in actual]
    check("every raw fixture has a parser snapshot",
          not missing, "missing: %s" % missing)
    # The listener must delegate to the parser (Batch 1 lesson):
    # a raw message through the feed must drain to the same item.
    from daemon.launch_scanner import pumpportal
    feed = pumpportal.PumpPortalFeed()
    feed._handle_message(json.dumps(
        load_fixture("pumpportal_new_token.json")))
    drained = feed.drain()
    # The feed stamps created_ts at receipt time, so compare
    # everything else: the flag + event fields must survive the
    # feed path intact (the full parse output is already pinned
    # in parsers.json above).
    same = False
    if len(drained) == 1:
        a = dict(drained[0])
        b = dict(new)
        a.pop("created_ts", None)
        b.pop("created_ts", None)
        same = (a == b)
    check("feed delegates to parser (new-token flag survives)",
          len(drained) == 1
          and drained[0].get("state_from_event") is True
          and same,
          "drained=%r" % (drained[:1],))
    return _compare_or_update("parsers.json", actual, update)


# ---- group: curve stats + creator + tx price -------------------------

def group_curve_stats(update):
    stats, calls = curve_stats_fixture()
    creator, info, why, ccalls = creator_and_tx_price_fixture()
    actual = {
        "curve_stats": stats,
        "curve_stats_rpc_calls": [[m, p] for m, p in calls],
        "creator": creator,
        "tx_price": {"info": info, "why": why},
        "creator_rpc_calls": [[m, p] for m, p in ccalls],
    }
    check("curve stats spends exactly one RPC call",
          len(calls) == 1 and calls[0][0] == "getSignaturesForAddress",
          "calls=%r" % (calls,))
    check("creator lookup spends exactly one RPC call",
          len(ccalls) == 1 and ccalls[0][0] == "getTransaction",
          "calls=%r" % (ccalls,))
    return _compare_or_update("curve_stats.json", actual, update)


# ---- group: exit decisions -------------------------------------------

ENTRY = 0.000025
TOKENS = 1_000_000.0
NOTIONAL = 25.0
MINT = "SnapShotMint1111111111111111111111111111111"


def _seed_position(age_s=3600.0):
    now = time.time()
    return {
        "mint": MINT, "symbol": "SNAP", "chain": "solana",
        "source": "graduation", "lane": "grad",
        "score": 0.0, "tier": "graduation",
        "entry_ts": now - age_s, "quoted_price": ENTRY,
        "entry_price": ENTRY, "notional_usd": NOTIONAL,
        "peak_price": ENTRY, "peak_ts": now - age_s,
        "born_ts": now - age_s - 30, "seen_ts": now - age_s,
        "decided_ts": now - age_s,
        "tokens": TOKENS, "gas_in_usd": 0.001,
        "legs": {
            "a": {"frac": 0.50, "target": 2.0, "done": False,
                  "exit_price": 0, "realized_usd": 0},
            "b": {"frac": 0.25, "target": 3.0, "done": False,
                  "exit_price": 0, "realized_usd": 0},
            "c": {"frac": 0.25, "trail": 0.30, "peak": ENTRY,
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


def _run_scenario(mults, age_s=3600.0):
    """Drive one seeded position through grad.poll_exits over a
    price series (multiples of entry). Returns the trace: one
    entry per poll step (legs done, close or None)."""
    from daemon.launch_scanner import grad as G
    old_jup = G._jup
    G._jup = None  # Jupiter disabled: price_fn prices everything.
    try:
        ledger_path = os.path.join(_TMP_HOME, "grad_ledger.json")
        trace = []
        for step, mult in enumerate(mults):
            price = ENTRY * mult
            if step == 0:
                with open(ledger_path, "w") as f:
                    json.dump({
                        "positions": {MINT: _seed_position(age_s)},
                        "closes": [], "bankroll": 10000.0}, f)
            else:
                # The previous poll already wrote the ledger;
                # stop the trace when the position closed.
                with open(ledger_path) as f:
                    d = json.load(f)
                if MINT not in d.get("positions", {}):
                    break
            state = {"px": price}

            def _price_fn(chain, mint, spot_index,
                          _s=state):
                return _s["px"]

            closes = G.poll_exits(price_fn=_price_fn)
            with open(ledger_path) as f:
                d = json.load(f)
            pos = d.get("positions", {}).get(MINT)
            legs = None
            if pos is not None:
                legs = {k: bool(v.get("done"))
                        for k, v in pos["legs"].items()}
            close = None
            for c in closes:
                if c.get("mint") == MINT:
                    close = {
                        "reason": c.get("closed_reason"),
                        "realized_mult": c.get("realized_mult"),
                        "pnl_usd": c.get("pnl_usd"),
                    }
            trace.append({
                "step": step, "mult": mult, "legs": legs,
                "close": close,
            })
            if close is not None:
                break
        return trace
    finally:
        G._jup = old_jup


def _run_time_only_scenario(journal_mults):
    """Drive one aged position through grad.poll_time_exits with
    pricing DISABLED (Jupiter off, no price_fn — the function
    takes none). journal_mults are the quote journal's px as
    multiples of entry, oldest first; [] means no real price on
    record (unquotable). Returns the pinned trace: close reason,
    realized multiple, pnl — or open when nothing fired."""
    from daemon.launch_scanner import grad as G
    old_jup = G._jup
    G._jup = None  # Jupiter disabled: the time-only exit reads
    # no price at all, so this also proves the stop fires with
    # pricing down.
    try:
        ledger_path = os.path.join(_TMP_HOME, "grad_ledger.json")
        now = time.time()
        pos = _seed_position(age_s=25 * 3600.0)
        pos["quote_journal"] = [
            {"ts": now - 60 * (len(journal_mults) - i),
             "px": ENTRY * m, "src": "dex"}
            for i, m in enumerate(journal_mults)]
        with open(ledger_path, "w") as f:
            json.dump({"positions": {MINT: pos},
                       "closes": [], "bankroll": 10000.0}, f)
        closes = G.poll_time_exits()
        with open(ledger_path) as f:
            d = json.load(f)
        close = None
        for c in closes:
            if c.get("mint") == MINT:
                close = {"reason": c.get("closed_reason"),
                         "realized_mult": c.get("realized_mult"),
                         "pnl_usd": c.get("pnl_usd")}
        return {"close": close,
                "open": MINT in d.get("positions", {})}
    finally:
        G._jup = old_jup


def group_exit_decisions(update):
    scenarios = {
        # Moon to 3x then fade: A @2x, B @3x, C trails from the
        # 3x peak and fires at 1.8x (below 3.0 * 0.7 = 2.1).
        "moon_then_fade": _run_scenario(
            [1.0, 1.5, 2.0, 2.5, 3.0, 2.2, 1.8]),
        # Hard stop, exactly at -50%.
        "straight_dump": _run_scenario([1.0, 0.8, 0.6, 0.5]),
        # Slow bleed: nothing fires, position stays open.
        "slow_bleed": _run_scenario([1.0, 0.9, 0.85, 0.8]),
        # Leg A fires exactly at its 2x target.
        "exactly_2x": _run_scenario([2.0]),
        # Just below the target: nothing fires.
        "just_below_2x": _run_scenario([1.99]),
        # Zero price, fresh position: unpriced, stays open.
        "zero_price": _run_scenario([0.0]),
        # Zero price, aged past 24h: unquotable time stop.
        "zero_price_aged": _run_scenario(
            [0.0], age_s=25 * 3600.0),
        # 24h hygiene backstop at a live price.
        "time_stop": _run_scenario([1.1], age_s=25 * 3600.0),
        # Trail fires exactly at 30% below the 2x peak.
        "trail_exact": _run_scenario([2.0, 1.4]),
        # Phantom print (>50x): guard skips the cycle, no leg
        # banks, position stays open.
        "phantom_print": _run_scenario([100.0]),
        # Time-only exit, pricing disabled: aged, last known
        # price 1.2x — fires on the journal, never the 3x peak.
        "time_only_known_price": _run_time_only_scenario(
            [2.0, 1.2]),
        # Time-only exit, pricing disabled, no real price on
        # record: unquotable close at 0.
        "time_only_unquotable": _run_time_only_scenario([]),
    }
    actual = {
        "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
        "notional_usd": NOTIONAL,
        "entry_price": ENTRY,
        "scenarios": scenarios,
    }
    return _compare_or_update("exit_decisions.json", actual, update)


# ---- group: instructions (Phase 2 templates + Jito bundle) -----------

def group_instructions(update):
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader(
        "phase2_dry_run",
        os.path.join(REPO, "scripts", "phase2-dry-run"))
    spec = importlib.util.spec_from_loader("phase2_dry_run", loader)
    dry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dry)
    from solders.keypair import Keypair
    from broker.memecoin.execution import jito as jito_mod
    from broker.memecoin.execution import signer as signer_mod
    from broker.memecoin.execution import tx as tx_mod
    from broker.memecoin.execution.tx_templates import (
        SignedTxTemplate)

    # FIXED dummy keypair (test only, never a real key): the
    # template bytes + signature are deterministic for it.
    kp = Keypair.from_seed(bytes(range(32)))
    wallet = str(kp.pubkey())
    sol = "So11111111111111111111111111111111111111112"
    tok = "H8DX2KJNV3c9Dg4L7sZ85qS5Mr9ZZ4y36tgsEFMopump"
    swap_b64 = dry._canned_swap_b64(kp, sol, tok)
    # created_at is metadata, not signed bytes: use "now" so the
    # template is valid for the run. The snapshot pins only the
    # bytes + signature + blockhash, which do not depend on it.
    template = SignedTxTemplate.build(
        swap_b64, kp, wallet, sol, tok, 1_000_000, 100,
        slot=100, created_at=time.monotonic())
    jito = jito_mod.JitoClient(
        http_post=lambda u, p: (_ for _ in ()).throw(
            AssertionError("snapshot harness must never POST")),
        dry_run=True)
    tip_account = jito.pick_tip_account()
    tip_tx = jito.build_tip_tx(wallet, 1_000_000,
                               template.blockhash)
    signer_mod.DevnetSigner.sign_transaction(
        tip_tx, kp, template.blockhash)
    tip_b64 = tx_mod.tx_to_base64(tip_tx)
    res = jito.send_bundle(
        [template.signed_base64, tip_b64], 1_000_000)
    actual = {
        "template": {
            "signed_base64": template.signed_base64,
            "signature": template.signature,
            "blockhash": template.blockhash,
            "wallet": template.wallet,
            "input_mint": template.input_mint,
            "output_mint": template.output_mint,
            "amount": template.amount,
            "slippage_bps": template.slippage_bps,
        },
        "tip_account": tip_account,
        "tip_tx_base64": tip_b64,
        "bundle_payload": res["payload"],
    }
    check("template has a signature and blockhash",
          bool(template.signature) and bool(template.blockhash))
    check("bundle payload is a 2-tx sendBundle",
          res["payload"].get("method") == "sendBundle"
          and len(res["payload"].get("params", [[]])[0]) == 2)
    return _compare_or_update("instructions.json", actual, update)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="rewrite snapshots (intended change only)")
    args = ap.parse_args()
    ok = True
    ok &= group_parsers(args.update)
    ok &= group_curve_stats(args.update)
    ok &= group_exit_decisions(args.update)
    ok &= group_instructions(args.update)
    print()
    if FAILURES:
        print("FAILURES: %d" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    if not ok:
        return 1
    print("ALL SNAPSHOT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
