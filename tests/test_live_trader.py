#!/usr/bin/env python3
"""Step 4 tests for the live trader (plain script):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_live_trader.py

1. FILTER PARITY: grad_select.evaluate_candidate on the same
   recorded-style candidates returns identical verdicts (and
   reasons, and measured values) through the paper engine and the
   live engine. The filter data sources are stubbed identically
   for both, so the test isolates the engine parameter — and each
   candidate also asserts its EXPECTED verdict, so parity cannot
   pass vacuously (both engines rejecting everything would fail).
2. DEDUPE: with an armed fake executor, one scan_once opens
   exactly one live buy; a second scan, and a scan after the
   position closes, produce zero further buys (dedupe-ever in the
   live ledger); a candidate the wallet already holds on-chain
   produces zero buys (on-chain dedupe).
3. REFUSAL: the trader script exits 1 without a deployment
   marker (it cannot run on a machine without one — including
   this build VM).
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="live_trader_")
os.environ["FOREX_AGENT_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, SOL_MINT)
from daemon.launch_scanner import grad_raw as _raw
from daemon.launch_scanner import grad_select as _sel
from tests.fixture_guarantee import admit_in_script_process, fixture_proof  # noqa: E402
admit_in_script_process(_sel)
from daemon.launch_scanner import live_select
from daemon.launch_scanner import saturation as _sat

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAD_PY = os.path.join(REPO, "daemon", "launch_scanner", "grad.py")
ENTRY = 1e-6
SOL_PRICE = 100.0
DECIMALS = 6


def _mint(tag):
    return ("T" + tag + "x" * 30 + "pump")[:44]


class FakeLiveExecutor(MainnetExecutor):
    """Armed fake: perfect fills at current_px, balances tracked."""

    def __init__(self):
        self._dry_run = False
        self._wallet_pubkey = "testwallet11111111111111111111111111"
        self.current_px = ENTRY
        self.token_balances = {}
        self.sol_balance = 10.0
        self.swaps = []
        self._decimals_cache = {}

    def get_token_balance(self, mint):
        return self.token_balances.get(mint, 0)

    def get_balance_sol(self):
        return self.sol_balance

    def get_mint_decimals(self, mint):
        return DECIMALS

    def sol_price_usd(self):
        return SOL_PRICE

    def execute_swap(self, input_mint, output_mint, amount,
                     slippage_bps=100, price_impact_cap_pct=2.0,
                     priority_fee_lamports=None, confirm_timeout=60.0):
        self.swaps.append({"in": input_mint, "out": output_mint,
                           "amount": amount, "px": self.current_px})
        if self.current_px is None or self.current_px <= 0:
            raise BrokerError(
                "broker_unavailable", "jupiter has no route",
                detail={"reason": "no_route"})
        if input_mint == SOL_MINT:
            tokens_base = int(amount / 1e9 * SOL_PRICE
                              / self.current_px * (10 ** DECIMALS))
            sol_before = int(round(self.sol_balance * 1e9))
            tok_before = self.token_balances.get(output_mint, 0)
            self.sol_balance -= amount / 1e9
            self.token_balances[output_mint] = tok_before + tokens_base
            return {"signature": "fakebuy%d" % len(self.swaps),
                    "simulated": False, "in_amount": amount,
                    "out_amount": tokens_base,
                    "sol_before": sol_before,
                    "sol_after": int(round(self.sol_balance * 1e9)),
                    "in_before": sol_before,
                    "in_after": int(round(self.sol_balance * 1e9)),
                    "out_before": tok_before,
                    "out_after": tok_before + tokens_base,
                    "priority_fee_lamports": 0,
                    "price_impact_pct": 0.0,
                    "quote_out_amount": tokens_base}
        sol_out = int(amount / (10 ** DECIMALS)
                      * self.current_px / SOL_PRICE * 1e9)
        tok_before = self.token_balances.get(input_mint, 0)
        sol_before = int(round(self.sol_balance * 1e9))
        self.token_balances[input_mint] = tok_before - amount
        self.sol_balance += sol_out / 1e9
        return {"signature": "fakesell%d" % len(self.swaps),
                "simulated": False, "in_amount": amount,
                "out_amount": sol_out,
                "in_before": tok_before,
                "in_after": tok_before - amount,
                "out_before": sol_before,
                "out_after": int(round(self.sol_balance * 1e9)),
                "priority_fee_lamports": 0,
                "price_impact_pct": 0.0,
                "quote_out_amount": sol_out}


def _paper_engine(tmp):
    spec = importlib.util.spec_from_file_location(
        "grad_paper_engine2", GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = os.path.join(tmp, "paper",
                                   "grad_select_ledger.json")
    eng.LANE = "grad_select"
    eng.NOTIONAL_USD = 1.0
    eng.MAX_OPEN = 6
    eng.DAILY_ENTRY_CAP = None
    eng.MAX_GRAD_AGE_S = 1800.0
    eng.BANKROLL_START = 6.0
    eng._safety_ok = _raw._no_safety_gate
    eng._veto_ok = _raw._no_veto
    return eng


class _FakeSaturation:
    """Ticker-saturation tally stub: per-ticker launch timestamps."""

    def __init__(self, launches, updated, wm_ts):
        self._launches = launches
        self._updated = updated
        self._wm_ts = wm_ts

    def _refresh_if_stale(self):
        return {"updated": self._updated, "launches": self._launches,
                "wm_ts": self._wm_ts}

    @staticmethod
    def _norm(symbol):
        return str(symbol or "?").strip().upper() or "?"

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return _sat.rolling_count(state, symbol, window_s, now_s)


def main():
    tmp = _TMP
    now = time.time()
    fake = FakeLiveExecutor()

    # -- filter parity (shared stubs, expected verdicts) ------------
    # One creator per case; curve stats per mint; tally per symbol.
    launches = {"creator_ok": (1, now - 86400),
                "creator_serial": (5, now - 86400)}
    curves = {}   # mint -> {"trades", "created_ts", ...}
    births = {}   # mint -> {"trader", "ts"}
    migs = {}     # mint -> {"pool": pool_addr}
    tally_launches = {}

    _sel._index_refresh = lambda n: None
    _sel._INDEX = {"by_mint": births, "by_mig": migs,
                   "by_trader": {}, "files": {},
                   "event_ts": [now - 86400 + i * 300 for i in range(289)],
                   "coverage_start": now - 86400, "available": True}
    _sel._trader_launches_24h = lambda c, n: launches.get(c, (0, 0))
    # 2026-11-24 conservative contract: recorder-only sub-threshold
    # counts never admit; stub the authoritative-history hook with the
    # fixture's declared per-creator history (the `launches` table).
    _sel._AUTHORITATIVE_LAUNCHES_24H = lambda c, n: \
        fixture_proof(_sel, c, launches.get(c, (0, 0))[0], n)
    _sel._curve_stats = lambda addr: curves.get(
        addr, {"trades": None, "reason": "no curve"})
    _sel._creator_from_oldest_tx = lambda sig: ""
    _sel._saturation = _FakeSaturation(
        tally_launches, updated=now, wm_ts=now * 1000.0)

    def cand(mint, symbol="TEST", source="pumpportal-migration",
             created_offset=10, pool="pool1"):
        # P1-4 (2026-11-22): the fixture was 600s old, so Rule A
        # (30s, Nov 16) rejected the "pass" case as slow_entry and
        # the dedupe rows never opened. A fresh graduation (10s)
        # keeps every case inside the 30s bar with headroom for the
        # suite's own runtime.
        return {"mint": mint, "symbol": symbol, "source": source,
                "created_ts": now - created_offset,
                "pool_address": pool, "dex": "pumpswap"}

    def curve_for(mint, trades=100, created_offset=2400,
                  exhausted=True, oldest_sig="sig1"):
        curves["curve:" + mint] = {
            "trades": trades, "created_ts": now - created_offset,
            "exhausted": exhausted, "oldest_sig": oldest_sig,
            "capped": False}

    # _curve_address derives the PDA from the mint; stub it to a
    # stable per-mint address so _curve_stats keys line up.
    _sel._curve_address = lambda mint, birth: "curve:" + mint

    paper = _paper_engine(tmp)
    live = live_select.make_live_engine(tmp, fake)
    os.makedirs(os.path.dirname(paper.LEDGER_PATH), exist_ok=True)
    paper.ensure_ledger()
    live.ensure_ledger()

    cases = []
    # 1. pass
    m = _mint("pass1")
    births[m] = {"trader": "creator_ok", "ts": now - 2400}
    curve_for(m)
    cases.append((cand(m), "pass", ""))
    # 2. serial deployer
    m = _mint("serial1")
    births[m] = {"trader": "creator_serial", "ts": now - 2400}
    curve_for(m)
    cases.append((cand(m), "reject", "serial_deployer"))
    # 3. many trades on the curve
    m = _mint("manytr")
    births[m] = {"trader": "creator_ok", "ts": now - 2400}
    curve_for(m, trades=500)
    cases.append((cand(m), "reject", "many_trades_curve"))
    # 4. slow curve (TTG 7200s > 3600s)
    m = _mint("slow1")
    births[m] = {"trader": "creator_ok", "ts": now - 7800}
    curve_for(m, created_offset=7800)
    cases.append((cand(m), "reject", "slow_curve"))
    # 5. copycat ticker (5 launches in the last 24h -> 4 others)
    m = _mint("copy1")
    births[m] = {"trader": "creator_ok", "ts": now - 2400}
    curve_for(m)
    tally_launches["COPY"] = [int(now * 1000) - i * 3600 * 1000
                              for i in (1, 2, 3, 4, 5)]
    cases.append((cand(m, symbol="COPY"), "reject", "copycat_ticker"))
    # (The COPY tally stays set: it only affects the COPY symbol,
    # and every other case uses a different symbol.)
    # 6. non-canonical pool (backfill row, pool mismatch)
    m = _mint("noncan")
    births[m] = {"trader": "creator_ok", "ts": now - 2400}
    curve_for(m)
    migs[m] = {"pool": "otherpool"}
    cases.append((cand(m, source="geckoterminal-backfill",
                       pool="pool1"), "reject", "noncanonical_pool"))
    # 7. unknown canonical (backfill row, no migration record)
    m = _mint("unkcan")
    births[m] = {"trader": "creator_ok", "ts": now - 2400}
    curve_for(m)
    cases.append((cand(m, source="geckoterminal-backfill"),
                  "reject", "unknown_canonical"))
    # 8. unknown curve (no curve stats)
    m = _mint("unkcurve")
    births[m] = {"trader": "creator_ok", "ts": now - 2400}
    curves["curve:" + m] = {"trades": None, "reason": "rpc down"}
    cases.append((cand(m), "reject", "unknown_curve"))
    # 9. unknown serial (no creator anywhere)
    m = _mint("unkser")
    births[m] = {"trader": "", "ts": now - 2400}
    curve_for(m, exhausted=False, oldest_sig=None)
    cases.append((cand(m), "reject", "unknown_serial"))

    for c, exp_verdict, exp_reason in cases:
        pv = _sel.evaluate_candidate(paper, dict(c), now)
        lv = _sel.evaluate_candidate(live, dict(c), now)
        tag = c["mint"][:14]
        check("filter %s: paper verdict is %s/%s"
              % (tag, exp_verdict, exp_reason or "-"),
              pv["verdict"] == exp_verdict
              and pv.get("reason") == exp_reason)
        check("filter %s: live verdict matches paper exactly"
              % tag,
              lv["verdict"] == pv["verdict"]
              and lv.get("reason") == pv.get("reason")
              and lv.get("values") == pv.get("values"))

    # -- dedupe: one scan opens exactly one live buy ----------------
    mint_d = _mint("dedupe")
    births[mint_d] = {"trader": "creator_ok", "ts": now - 2400}
    curve_for(mint_d)
    live.quote_price = lambda chain, m: ENTRY
    live._fresh_tape = lambda m: {"usd_liquidity": 50000.0,
                                  "usd_market_cap": 100000.0,
                                  "tape_source": "test"}
    fake.current_px = ENTRY
    row = cand(mint_d)
    res = _sel.scan_once(live, feed=None, gt_rows=[dict(row)])
    check("dedupe: first scan opens the position",
          res.get("opened") == 1)
    check("dedupe: exactly one buy swap",
          len(fake.swaps) == 1)
    check("dedupe: position is open in the live ledger",
          mint_d in live._load()["positions"])
    pos = live._load()["positions"][mint_d]
    check("dedupe: position tokens are the executed tokens",
          abs(pos["tokens"] - 986500.0) < 1.0)
    check("dedupe: position carries the live signature",
          bool(pos.get("live_execution", {}).get("signature")))
    # Second scan: already open -> no second buy.
    res = _sel.scan_once(live, feed=None, gt_rows=[dict(row)])
    check("dedupe: second scan buys nothing more",
          len(fake.swaps) == 1 and res.get("opened") == 0)
    # Close the position (hard stop), then scan again: already
    # traded (dedupe-ever) -> still no second buy.
    fake.current_px = 0.4 * ENTRY
    closes = live._poll_exits(lambda c, m, s: 0.4 * ENTRY)
    check("dedupe: hard-stop close lands",
          len(closes) == 1
          and closes[0]["closed_reason"] == "hard_stop_-50pct")
    fake.current_px = ENTRY
    swaps_after_close = len(fake.swaps)  # 1 buy + 1 hard-stop sell
    res = _sel.scan_once(live, feed=None, gt_rows=[dict(row)])
    check("dedupe: scan after close buys nothing (dedupe-ever)",
          len(fake.swaps) == swaps_after_close
          and res.get("opened") == 0)

    # On-chain dedupe: wallet already holds the token -> no buy.
    mint_h = _mint("heldtok")
    births[mint_h] = {"trader": "creator_ok", "ts": now - 2400}
    curve_for(mint_h)
    fake.token_balances[mint_h] = 10 ** 12
    swaps_before = len(fake.swaps)
    res = _sel.scan_once(live, feed=None, gt_rows=[dict(cand(mint_h))])
    check("dedupe: on-chain holding produces no buy",
          len(fake.swaps) == swaps_before)
    check("dedupe: on-chain holding opens no position",
          mint_h not in live._load()["positions"])

    # -- refusal: trader exits without a deployment marker ----------
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = os.path.join(tmp, "nomarker")
    env["MEMECOIN_WALLET_PUBKEY"] = "testwallet11111111111111111111111111"
    env.pop("MEMECOIN_WALLET_KEY_FILE", None)
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts",
                                     "grad-select-live-trader")],
        env=env, capture_output=True, text=True, timeout=120,
        cwd=REPO)
    check("refusal: trader exits non-zero without a marker",
          proc.returncode != 0)
    check("refusal: message names the deployment marker refusal",
          "refused to connect" in proc.stderr + proc.stdout)

    print("ALL %d CHECKS PASSED" % _PASSED)
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
