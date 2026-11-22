#!/usr/bin/env python3
"""Step 5 safety tests (plain script):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_live_safety.py

- Halted killswitch -> zero entries, exits still drain.
- 5 consecutive executor failures -> fail-streak halt (human-only
  re-enable); 4 do not halt; quote skips (no_route) do not count.
- Size caps: $1.00 stake, MAX_OPEN 6 enforced, $6.00 bankroll.
- Daily loss cap: today pnl <= -$3.00 blocks entries (exits unaffected).
- Dry-run: builds + simulates, never sends, never opens a position.
- Fail-closed: unreadable killswitch state = halted; unparseable
  live ledger blocks entries.
- Backup: the live key file is never archived; the live ledger is.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="live_safety_")
os.environ["FOREX_AGENT_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, SOL_MINT)
from daemon.launch_scanner import killswitch as _ks
from daemon.launch_scanner import live_select

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = 1e-6
SOL_PRICE = 100.0
DECIMALS = 6
LANE = live_select.LANE


def _mint(tag):
    return ("T" + tag + "x" * 30 + "pump")[:44]


class SafetyFakeExecutor(MainnetExecutor):
    """Armed/dry-run fake with a programmable failure mode."""

    def __init__(self, dry_run=False):
        self._dry_run = dry_run
        self._wallet_pubkey = "testwallet11111111111111111111111111"
        self.current_px = ENTRY
        self.token_balances = {}
        self.sol_balance = 10.0
        self.swaps = []        # sent swaps (real execution)
        self.simulations = []  # dry-run simulations
        self.fail_mode = None  # (reason, remaining)
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
        if self._dry_run:
            self.simulations.append(
                {"in": input_mint, "out": output_mint,
                 "amount": amount})
            return {"signature": None, "simulated": True,
                    "in_amount": amount, "out_amount": 999999,
                    "sol_before": 0, "sol_after": 0,
                    "in_before": 0, "in_after": 0,
                    "out_before": 0, "out_after": 0,
                    "priority_fee_lamports": 0,
                    "price_impact_pct": 0.0,
                    "quote_out_amount": 999999}
        if self.fail_mode is not None and self.fail_mode[1] > 0:
            reason, left = self.fail_mode
            self.fail_mode = (reason, left - 1)
            raise BrokerError(
                "broker_unavailable", "injected failure",
                detail={"reason": reason})
        self.swaps.append({"in": input_mint, "out": output_mint,
                           "amount": amount, "px": self.current_px})
        if input_mint == SOL_MINT:
            tokens_base = int(amount / 1e9 * SOL_PRICE
                              / self.current_px * (10 ** DECIMALS))
            tok_before = self.token_balances.get(output_mint, 0)
            self.sol_balance -= amount / 1e9
            self.token_balances[output_mint] = tok_before + tokens_base
            return {"signature": "safebuy%d" % len(self.swaps),
                    "simulated": False, "in_amount": amount,
                    "out_amount": tokens_base,
                    "sol_before": 0, "sol_after": 0,
                    "in_before": 0, "in_after": 0,
                    "out_before": tok_before,
                    "out_after": tok_before + tokens_base,
                    "priority_fee_lamports": 0,
                    "price_impact_pct": 0.0,
                    "quote_out_amount": tokens_base}
        sol_out = int(amount / (10 ** DECIMALS)
                      * self.current_px / SOL_PRICE * 1e9)
        tok_before = self.token_balances.get(input_mint, 0)
        self.token_balances[input_mint] = tok_before - amount
        self.sol_balance += sol_out / 1e9
        return {"signature": "safesell%d" % len(self.swaps),
                "simulated": False, "in_amount": amount,
                "out_amount": sol_out,
                "in_before": tok_before,
                "in_after": tok_before - amount,
                "out_before": 0, "out_after": 0,
                "priority_fee_lamports": 0,
                "price_impact_pct": 0.0,
                "quote_out_amount": sol_out}


def _load_trader():
    from importlib.machinery import SourceFileLoader
    spec = importlib.util.spec_from_file_location(
        "grad_select_live_trader",
        os.path.join(REPO, "scripts", "grad-select-live-trader"),
        loader=SourceFileLoader(
            "grad_select_live_trader",
            os.path.join(REPO, "scripts", "grad-select-live-trader")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cand(mint, now):
    return {"mint": mint, "symbol": "TEST",
            "source": "pumpportal-migration",
            "created_ts": now - 60, "pool_address": "pool1",
            "dex": "pumpswap", "fdv_usd": 100000.0,
            "reserve_usd": 50000.0}


def _seed_pos(engine, mint, entry_ts):
    legs = {}
    for k, f in (("a", 0.5), ("b", 0.25), ("c", 0.25)):
        legs[k] = {"frac": f, "done": False, "exit_price": None,
                   "realized_usd": 0.0, "exit_reason": None,
                   "peak": ENTRY}
    pos = {"mint": mint, "symbol": "TEST", "chain": "solana",
           "source": "test", "entry_ts": entry_ts,
           "entry_price": ENTRY, "entry_notional": 1.0,
           "notional_usd": 1.0, "tokens": 1_000_000.0, "cost_usd": 1.0,
           "gas_in_usd": 0.0, "realized_usd": 0.0,
           "entry_reason": "test",
           "entry_snapshot": {"entry_price": ENTRY, "mcap": 100000,
                              "liq": 50000},
           "entry_features": {}, "quote_journal": [],
           "peak_price": ENTRY, "peak_mult": 1.0, "peak_ts": entry_ts,
           "legs": legs, "last_liquidity": 50000.0,
           "transitions": [], "spot_index": None, "pair": None,
           "born_ts": entry_ts, "seen_ts": entry_ts,
           "decided_ts": entry_ts}
    with engine._ledger_lock():
        d = engine._read()
        d["positions"][mint] = pos
        engine._write(d)


def main():
    tmp = _TMP
    now = time.time()
    trader = _load_trader()
    fake = SafetyFakeExecutor()
    live = live_select.make_live_engine(tmp, fake)
    live.ensure_ledger()
    live.quote_price = lambda chain, m: ENTRY
    live._fresh_tape = lambda m: {"usd_liquidity": 50000.0,
                                  "usd_market_cap": 100000.0,
                                  "tape_source": "test"}
    fake.current_px = ENTRY

    # -- size caps are hard-coded -------------------------------------
    check("size cap: stake is $1.00",
          live_select.NOTIONAL_USD == 1.00)
    check("size cap: MAX_OPEN is 6", live_select.MAX_OPEN == 6)
    check("size cap: bankroll is $6.00",
          live_select.BANKROLL_START == 6.00)
    check("size cap: daily loss cap is $3.00",
          live_select.DAILY_LOSS_CAP_USD == 3.00)
    check("size cap: fail-streak halt is 5",
          live_select.FAIL_STREAK_HALT == 5)

    # -- MAX_OPEN enforced: 6 open, the 7th refused -------------------
    for i in range(6):
        m = _mint("cap%d" % i)
        live._live_fills.set_entry_context(m)
        opened, why, final = live.consider_candidate(_cand(m, now), now)
        check("size cap: entry %d opens" % (i + 1), opened is True)
    m7 = _mint("cap7")
    opened, why, final = live.consider_candidate(_cand(m7, now), now)
    check("size cap: 7th entry refused (bankroll $6 binds first)",
          opened is False and "bankroll exhausted" in why)
    check("size cap: no 7th swap was attempted",
          len(fake.swaps) == 6)
    # Drain the 6 (hard stop) so later sections start clean.
    fake.current_px = 0.4 * ENTRY
    closes = live._poll_exits(lambda c, m, s: 0.4 * ENTRY)
    check("size cap: all 6 drain on the hard stop",
          len(closes) == 6)
    fake.current_px = ENTRY
    # MAX_OPEN binds when the bankroll allows it: a second engine
    # with a topped-up ledger bankroll opens 6, and the 7th is
    # refused for MAX_OPEN, not bankroll.
    tmp2 = tempfile.mkdtemp(prefix="live_safety_maxopen_")
    live2 = live_select.make_live_engine(tmp2, fake)
    live2.ensure_ledger()
    live2.quote_price = lambda chain, m: ENTRY
    live2._fresh_tape = lambda m: {"usd_liquidity": 50000.0,
                                   "usd_market_cap": 100000.0,
                                   "tape_source": "test"}
    with live2._ledger_lock():
        d = live2._read()
        d["bankroll"] = 100.0
        live2._write(d)
    for i in range(6):
        m = _mint("mo%d" % i)
        live2._live_fills.set_entry_context(m)
        opened, why, final = live2.consider_candidate(
            _cand(m, now), now)
        check("size cap: topped-up entry %d opens" % (i + 1),
              opened is True)
    opened, why, final = live2.consider_candidate(
        _cand(_mint("mo7"), now), now)
    check("size cap: 7th entry refused (MAX_OPEN 6)",
          opened is False and "max open" in why)
    fake.current_px = 0.4 * ENTRY
    live2._poll_exits(lambda c, m, s: 0.4 * ENTRY)
    fake.current_px = ENTRY
    shutil.rmtree(tmp2, ignore_errors=True)

    # -- halted killswitch: zero entries, exits still drain ----------
    m_halt = _mint("halted")
    _seed_pos(live, m_halt, now - 600)
    fake.token_balances[m_halt] = int(1_000_000 * (10 ** DECIMALS))
    _ks.halt_lane(LANE, "test halt")
    check("halt: lane reports halted", _ks.is_halted(LANE) is True)
    check("halt: gate() is closed", _ks.gate(LANE) is False)
    ok, why = trader._preflight(fake, live)
    check("halt: preflight refuses entries while halted",
          ok is False and "killswitch" in why)
    opened, why, final = live.consider_candidate(
        _cand(_mint("haltentry"), now), now)
    check("halt: consider_candidate refuses while halted",
          opened is False and "halted" in why)
    fake.current_px = 0.4 * ENTRY
    closes = live._poll_exits(lambda c, m, s: 0.4 * ENTRY)
    check("halt: exits still drain while halted",
          len(closes) == 1
          and closes[0]["closed_reason"] == "hard_stop_-50pct")
    fake.current_px = ENTRY
    _ks.reenable(LANE, "test re-enable")
    check("halt: re-enable restores the gate",
          _ks.gate(LANE) is True)

    # -- fail-streak halt ----------------------------------------------
    fake.fail_mode = ("simulate_failed", 4)
    for i in range(4):
        m = _mint("fail%d" % i)
        live._live_fills.set_entry_context(m)
        live.consider_candidate(_cand(m, now), now)
    check("fail-streak: 4 failures counted",
          live._live_fail_streak.count == 4)
    check("fail-streak: 4 failures do NOT halt",
          trader._maybe_fail_streak_halt(live) is False)
    check("fail-streak: lane still open after 4",
          _ks.gate(LANE) is True)
    fake.fail_mode = ("simulate_failed", 1)
    live._live_fills.set_entry_context(_mint("fail4"))
    live.consider_candidate(_cand(_mint("fail4"), now), now)
    check("fail-streak: 5th failure counted",
          live._live_fail_streak.count == 5)
    check("fail-streak: 5 failures halt the lane",
          trader._maybe_fail_streak_halt(live) is True)
    check("fail-streak: lane is halted after 5",
          _ks.is_halted(LANE) is True)
    opened, why, final = live.consider_candidate(
        _cand(_mint("fail5"), now), now)
    check("fail-streak: entries refused after the halt",
          opened is False and "halted" in why)
    _ks.reenable(LANE, "test re-enable after fail-streak")
    # A clean success resets the streak.
    fake.fail_mode = None
    live._live_fills.set_entry_context(_mint("okafter"))
    opened, why, final = live.consider_candidate(
        _cand(_mint("okafter"), now), now)
    check("fail-streak: a clean success resets the streak",
          opened is True and live._live_fail_streak.count == 0)
    # Quote skips do NOT count toward the streak.
    fake.fail_mode = ("no_route", 5)
    for i in range(5):
        m = _mint("skip%d" % i)
        live._live_fills.set_entry_context(m)
        live.consider_candidate(_cand(m, now), now)
    check("fail-streak: 5 no-route skips do not count",
          live._live_fail_streak.count == 0)
    check("fail-streak: skips do not halt",
          trader._maybe_fail_streak_halt(live) is False)
    fake.fail_mode = None
    # Drain the one open position.
    fake.current_px = 0.4 * ENTRY
    live._poll_exits(lambda c, m, s: 0.4 * ENTRY)
    fake.current_px = ENTRY

    # -- daily loss cap --------------------------------------------------
    with live._ledger_lock():
        d = live._read()
        d["closes"] = [{
            "mint": _mint("lostoday"), "symbol": "TEST",
            "close_ts": now, "pnl_usd": -3.50,
            "realized_usd": 0.0, "realized_mult": 0.0,
            "closed_reason": "hard_stop_-50pct", "legs": {},
            "entry_price": ENTRY, "entry_ts": now - 600,
            "entry_notional_usd": 1.0, "entry_snapshot": {},
        }]
        live._write(d)
    check("daily cap: today pnl reads -$3.50",
          live_select.today_pnl_usd(live) == -3.50)
    ok, why = trader._preflight(fake, live)
    check("daily cap: entries blocked at -$3.50 today",
          ok is False and "daily loss cap" in why)
    with live._ledger_lock():
        d = live._read()
        d["closes"][0]["pnl_usd"] = -2.00
        live._write(d)
    ok, why = trader._preflight(fake, live)
    check("daily cap: entries allowed at -$2.00 today",
          ok is True)
    with live._ledger_lock():
        d = live._read()
        d["closes"] = []
        live._write(d)

    # -- dry-run: simulates, never sends, never opens -------------------
    dry_fake = SafetyFakeExecutor(dry_run=True)
    dry_live = live_select.make_live_engine(tmp, dry_fake)
    dry_live.quote_price = lambda chain, m: ENTRY
    dry_live._fresh_tape = lambda m: {"usd_liquidity": 50000.0,
                                      "usd_market_cap": 100000.0,
                                      "tape_source": "test"}
    dry_fake.current_px = ENTRY
    m_dry = _mint("dryrun")
    dry_live._live_fills.set_entry_context(m_dry)
    opened, why, final = dry_live.consider_candidate(
        _cand(m_dry, now), now)
    check("dry-run: no position opens",
          opened is False)
    check("dry-run: the swap was simulated",
          len(dry_fake.simulations) == 1)
    check("dry-run: nothing was ever sent",
          len(dry_fake.swaps) == 0)
    check("dry-run: ledger has no positions",
          dry_live._load()["positions"] == {})

    # -- fail-closed killswitch state ------------------------------------
    state_path = os.path.join(tmp, "killswitch", "state.json")
    with open(state_path, "w") as f:
        f.write("{invalid json")
    check("fail-closed: corrupt killswitch state reads as halted",
          _ks.is_halted(LANE) is True)
    check("fail-closed: gate() is closed on corrupt state",
          _ks.gate(LANE) is False)
    ok, why = trader._preflight(fake, live)
    check("fail-closed: preflight refuses on corrupt state",
          ok is False)
    opened, why, final = live.consider_candidate(
        _cand(_mint("corruptks"), now), now)
    check("fail-closed: entries refused on corrupt state",
          opened is False)
    os.remove(state_path)
    _ks.check_lane(LANE)  # re-materialize a valid state file
    check("fail-closed: valid state restores the gate",
          _ks.gate(LANE) is True)

    # -- fail-closed live ledger ------------------------------------------
    with open(live.LEDGER_PATH, "w") as f:
        f.write("{invalid json")
    ok, why = trader._preflight(fake, live)
    check("fail-closed: unparseable live ledger blocks entries",
          ok is False and "unparseable" in why)
    os.remove(live.LEDGER_PATH)
    live.ensure_ledger()
    ok, why = trader._preflight(fake, live)
    check("fail-closed: fresh ledger restores preflight",
          ok is True)

    # -- backup: key never archived, ledger is ---------------------------
    bhome = os.path.join(tmp, "bhome")
    os.makedirs(os.path.join(bhome, "live"), exist_ok=True)
    with open(os.path.join(bhome, "live", "keypair.json"), "w") as f:
        f.write("[1,2,3,4]")
    with open(os.path.join(
            bhome, "live", "live_grad_select_ledger.json"), "w") as f:
        json.dump({"bankroll": 6.0, "positions": {}}, f)
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = bhome
    proc = subprocess.run(
        [os.path.join(REPO, "scripts", "forex-backup")],
        env=env, capture_output=True, text=True, timeout=120,
        cwd=REPO)
    check("backup: script exits 0", proc.returncode == 0)
    archives = [f for f in os.listdir(os.path.join(bhome, "backups"))
                if f.endswith(".tar.gz")]
    check("backup: one archive produced", len(archives) == 1)
    with tarfile.open(os.path.join(bhome, "backups", archives[0])) as tf:
        names = tf.getnames()
    check("backup: live ledger IS archived",
          any(n.endswith("live/live_grad_select_ledger.json")
              for n in names))
    check("backup: live key file is NEVER archived",
          not any("keypair" in n or n.endswith(".key") for n in names))

    print("ALL %d CHECKS PASSED" % _PASSED)
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
