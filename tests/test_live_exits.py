#!/usr/bin/env python3
"""Step 3 tests for the live exit evaluator (plain script):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_live_exits.py

DIFF TEST: the live exit evaluator (live_select.make_live_engine,
real swaps via a fake executor) is driven poll-by-poll over the
same synthetic price series as paper grad.py, and the leg-firing
events must match exactly — same legs, same polls, same reasons.
Close PRICES are NOT compared: paper models an adverse fill +
gas per leg, live records the executed amounts; the test asserts
live realized == the fake's executed (perfect-fill) amounts and
paper realized < live (the modeled costs), which is the expected
difference.

Documented divergence (F2 dark-quote exit): paper banks the
position at its last real price the moment F2 fires; live cannot
sell an unquotable token, so the swap attempt fails with no_route
and the position stays open — the F2 decision re-fires on the
next dark poll and the sale executes when a route returns. The
same holds for the 24h-unquotable backstop: paper writes the
position off at $0, live keeps holding the tokens until a route
returns (booking a $0 close while still holding them would
corrupt the ledger). The test asserts exactly that behavior on
both sides.

Also covered (live-only): unconfirmed-send reconciliation (a
leg whose send timed out after executing on-chain is marked done
from measured balances, never re-sold); the entry fills adapter
(executed price, dry-run refusal, on-chain dedupe); live ledger
isolation (own subdir, own verdict chain).
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="live_exits_")
os.environ["FOREX_AGENT_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# MANDATORY-GUARD MIGRATION (disclosed test edit, published contract only; no live-hold behavior imported):
# isolate network/prices and enable the shared paper account with explicit TEST-FIXTURE gate limits.
import tests._guard_fixture as _GF
_GF.block_network()
_GF.install_price_feed(at_cost=True)
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
_GF.set_env()
from broker import BrokerError
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, SOL_MINT)
from daemon.launch_scanner import grad_raw as _raw
from daemon.launch_scanner import live_select

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAD_PY = os.path.join(REPO, "daemon", "launch_scanner", "grad.py")
ENTRY = 1e-6            # USD per token at entry
TOKENS = 1_000_000.0    # whole tokens for a $1.00 notional
DECIMALS = 6
SOL_PRICE = 100.0


def _mint(tag):
    # Graduation candidates must end in "pump".
    return ("T" + tag + "x" * 30 + "pump")[:44]


class FakeLiveExecutor(MainnetExecutor):
    """MainnetExecutor-shaped fake: perfect fills at current_px,
    balances tracked, dry-run simulates without moving anything,
    px<=0 means no Jupiter route."""

    def __init__(self):
        self._dry_run = False
        self._wallet_pubkey = "testwallet11111111111111111111111111"
        self.current_px = ENTRY
        self.token_balances = {}
        self.sol_balance = 10.0
        self.swaps = []
        self.fail_next = None
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
            return self._buy(output_mint, amount)
        return self._sell(input_mint, amount)

    def _buy(self, mint, lamports):
        tokens_base = int(lamports / 1e9 * SOL_PRICE
                          / self.current_px * (10 ** DECIMALS))
        sol_before = int(round(self.sol_balance * 1e9))
        tok_before = self.token_balances.get(mint, 0)
        if self._dry_run:
            return {"signature": None, "simulated": True,
                    "in_amount": lamports, "out_amount": tokens_base,
                    "sol_before": sol_before, "sol_after": sol_before,
                    "in_before": sol_before, "in_after": sol_before,
                    "out_before": tok_before, "out_after": tok_before,
                    "priority_fee_lamports": 0,
                    "price_impact_pct": 0.0,
                    "quote_out_amount": tokens_base}
        self.sol_balance -= lamports / 1e9
        self.token_balances[mint] = tok_before + tokens_base
        return {"signature": "fakebuy%d" % len(self.swaps),
                "simulated": False, "in_amount": lamports,
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

    def _sell(self, mint, base_units):
        sol_out = int(base_units / (10 ** DECIMALS)
                      * self.current_px / SOL_PRICE * 1e9)
        tok_before = self.token_balances.get(mint, 0)
        sol_before = int(round(self.sol_balance * 1e9))
        if self.fail_next == "confirm_timeout":
            self.fail_next = None
            # The chain executed it; confirmation timed out.
            self.token_balances[mint] = tok_before - base_units
            self.sol_balance += sol_out / 1e9
            raise BrokerError(
                "broker_unavailable", "send unconfirmed",
                detail={"reason": "confirm_timeout",
                        "signature": "fakeunk%d" % len(self.swaps)})
        self.token_balances[mint] = tok_before - base_units
        self.sol_balance += sol_out / 1e9
        return {"signature": "fakesell%d" % len(self.swaps),
                "simulated": False, "in_amount": base_units,
                "out_amount": sol_out,
                "in_before": tok_before,
                "in_after": tok_before - base_units,
                "out_before": sol_before,
                "out_after": int(round(self.sol_balance * 1e9)),
                "priority_fee_lamports": 0,
                "price_impact_pct": 0.0,
                "quote_out_amount": sol_out}


def _paper_engine(tmp):
    spec = importlib.util.spec_from_file_location(
        "grad_paper_engine", GRAD_PY)
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


def _pos(mint, entry_ts, armed=False, dark_ts=None):
    legs = {}
    for k, f in (("a", 0.5), ("b", 0.25), ("c", 0.25)):
        legs[k] = {"frac": f, "done": False, "exit_price": None,
                   "realized_usd": 0.0, "exit_reason": None,
                   "peak": ENTRY}
    p = {"mint": mint, "symbol": "TEST", "chain": "solana",
         "source": "test", "entry_ts": entry_ts,
         "entry_price": ENTRY, "entry_notional": 1.0,
         "notional_usd": 1.0, "tokens": TOKENS, "cost_usd": 1.0,
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
    if armed:
        p["legs"]["a"]["done"] = True
        p["legs"]["a"]["exit_price"] = 2.0 * ENTRY
        p["legs"]["a"]["realized_usd"] = 1.0
        p["legs"]["a"]["exit_reason"] = "leg_a_2x"
        p["realized_usd"] = 1.0
        p["peak_price"] = 2.0 * ENTRY
        p["peak_mult"] = 2.0
        p["legs"]["c"]["peak"] = 2.0 * ENTRY
    if dark_ts is not None:
        p["quote_journal"] = [{"ts": dark_ts, "px": 2.0 * ENTRY,
                               "src": "test"}]
    return p


def _seed(eng, mint, entry_ts, armed=False, dark_ts=None):
    with eng._ledger_lock():
        d = eng._read()
        d["positions"][mint] = _pos(mint, entry_ts, armed, dark_ts)
        eng._write(d)


def _leg_states(eng):
    """{mint: {leg: (done, exit_reason)}} for open positions AND
    closed ones (a close finalizes in the same poll its last leg
    fires, so the close record is the only place that transition
    is visible)."""
    d = eng._load()
    out = {}
    for m, p in d["positions"].items():
        out[m] = {k: (bool(v.get("done")),
                      v.get("exit_reason"))
                  for k, v in p["legs"].items()}
    for c in d["closes"]:
        out[c["mint"]] = {k: (bool(v["done"]), v.get("exit_reason"))
                          for k, v in c["legs"].items()}
    return out


def _closes(eng):
    return eng._load()["closes"]


def _run_series(paper, live, fake, mint, series, entry_ts):
    """Drive both engines poll-by-poll over px multiples of ENTRY.
    Returns (paper_events, live_events): lists of
    (poll_index, leg, done, reason) for legs that became done at
    that poll, in firing order."""
    paper_events, live_events = [], []
    prev_p, prev_l = _leg_states(paper), _leg_states(live)
    state = {"px": ENTRY}

    def price_fn(chain, m, spot_index):
        return state["px"]

    for i, mult in enumerate(series):
        px = mult * ENTRY if mult is not None else 0.0
        state["px"] = px
        fake.current_px = px
        paper._poll_exits(price_fn)
        live._poll_exits(price_fn)
        for eng, evs, prev in ((paper, paper_events, prev_p),
                               (live, live_events, prev_l)):
            cur = _leg_states(eng)
            old = prev.get(mint, {})
            new = cur.get(mint, {})
            for k in ("a", "b", "c"):
                if not old.get(k, (False, None))[0] \
                        and new.get(k, (False, None))[0]:
                    evs.append((i, k, new[k][1]))
            if mint in prev:
                prev[mint] = new
            else:
                prev.pop(mint, None)
    return paper_events, live_events


def _close_legs(eng):
    cs = _closes(eng)
    if not cs:
        return None
    c = cs[-1]
    return (c["closed_reason"],
            {k: (v["done"], v["exit_reason"])
             for k, v in c["legs"].items()})


def main():
    tmp = _TMP
    now = time.time()
    fake = FakeLiveExecutor()

    # -- scenario 1: the ladder --------------------------------------
    mint1 = _mint("ladder")
    paper = _paper_engine(tmp)
    live = live_select.make_live_engine(tmp, fake)
    os.makedirs(os.path.dirname(paper.LEDGER_PATH), exist_ok=True)
    paper.ensure_ledger()
    live.ensure_ledger()
    fake.token_balances[mint1] = int(TOKENS * (10 ** DECIMALS))
    _seed(paper, mint1, now - 600)
    _seed(live, mint1, now - 600)
    series = [1.0, 2.1, 3.1, 2.2, 2.1]
    pe, le = _run_series(paper, live, fake, mint1, series, now - 600)
    check("ladder: paper and live fire identical legs/polls",
          pe == le)
    check("ladder: events are A@1, B@2, C@4",
          pe == [(1, "a", "leg_a_2x"), (2, "b", "leg_b_3x"),
                 (4, "c", "leg_c_trail_30pct")])
    pc = _close_legs(paper)
    lc = _close_legs(live)
    check("ladder: both close ladder_complete",
          pc[0] == lc[0] == "ladder_complete")
    check("ladder: close leg reasons match",
          pc[1] == lc[1])
    # Live realized == perfect fill at the quote; paper < live by
    # its modeled adverse fill + gas (the expected difference).
    live_close = _closes(live)[-1]
    exp_a = 0.5 * TOKENS * 2.1 * ENTRY
    exp_b = 0.25 * TOKENS * 3.1 * ENTRY
    exp_c = 0.25 * TOKENS * 2.1 * ENTRY
    check("ladder: live leg A realized is the executed amount",
          abs(live_close["legs"]["a"]["realized_usd"] - exp_a) < 1e-6)
    check("ladder: live leg B realized is the executed amount",
          abs(live_close["legs"]["b"]["realized_usd"] - exp_b) < 1e-6)
    check("ladder: live leg C realized is the executed amount",
          abs(live_close["legs"]["c"]["realized_usd"] - exp_c) < 1e-6)
    paper_close = _closes(paper)[-1]
    check("ladder: paper realized is below live (modeled costs)",
          paper_close["realized_usd"] < live_close["realized_usd"])
    check("ladder: live token balance is empty after close",
          fake.token_balances.get(mint1, 0) == 0)

    # -- scenario 2: hard stop ---------------------------------------
    mint2 = _mint("hardstop")
    fake.token_balances[mint2] = int(TOKENS * (10 ** DECIMALS))
    _seed(paper, mint2, now - 600)
    _seed(live, mint2, now - 600)
    pe, le = _run_series(paper, live, fake, mint2, [0.49], now - 600)
    check("hard stop: identical events", pe == le)
    check("hard stop: all legs close at 0.49",
          pe == [(0, "a", "hard_stop_-50pct"),
                 (0, "b", "hard_stop_-50pct"),
                 (0, "c", "hard_stop_-50pct")])
    check("hard stop: both close hard_stop_-50pct",
          _close_legs(paper)[0] == _close_legs(live)[0]
          == "hard_stop_-50pct")
    check("hard stop: live realized is the executed amount",
          abs(_closes(live)[-1]["realized_usd"]
              - TOKENS * 0.49 * ENTRY) < 1e-6)

    # -- scenario 3: absurd print guard ------------------------------
    mint3 = _mint("absurd")
    fake.token_balances[mint3] = int(TOKENS * (10 ** DECIMALS))
    _seed(paper, mint3, now - 600)
    _seed(live, mint3, now - 600)
    pe, le = _run_series(paper, live, fake, mint3, [60.0, 2.1],
                         now - 600)
    check("absurd print: identical events (60x banks nothing)",
          pe == le)
    check("absurd print: only leg A fires, at the 2.1x poll",
          pe == [(1, "a", "leg_a_2x")])
    check("absurd print: position still open in both",
          mint3 in _leg_states(paper)
          and mint3 in _leg_states(live))
    check("absurd print: live made no swap on the absurd poll",
          not any(s["px"] == 60.0 * ENTRY for s in fake.swaps))

    # -- scenario 4: 24h time stop ------------------------------------
    mint4 = _mint("timestop")
    fake.token_balances[mint4] = int(TOKENS * (10 ** DECIMALS))
    _seed(paper, mint4, now - 25 * 3600)
    _seed(live, mint4, now - 25 * 3600)
    pe, le = _run_series(paper, live, fake, mint4, [1.1],
                         now - 25 * 3600)
    check("time stop: identical events", pe == le)
    check("time stop: both close time_stop_24h",
          _close_legs(paper)[0] == _close_legs(live)[0]
          == "time_stop_24h")

    # -- scenario 5: F2 dark-quote exit (documented divergence) ------
    mint5 = _mint("f2dark")
    fake.token_balances[mint5] = int(0.5 * TOKENS * (10 ** DECIMALS))
    _seed(paper, mint5, now - 3600, armed=True, dark_ts=now - 400)
    _seed(live, mint5, now - 3600, armed=True, dark_ts=now - 400)
    swaps_before = len(fake.swaps)
    pe, le = _run_series(paper, live, fake, mint5,
                         [0.0, 0.0, 1.2], now - 3600)
    check("F2: paper closes at the last real price on poll 0",
          _close_legs(paper)[0] == "f2_unquotable_last_price")
    check("F2: live does NOT close while dark (no route)",
          _close_legs(live) is None or
          _close_legs(live)[0] != "f2_unquotable_last_price")
    check("F2: live attempted the sale on both dark polls",
          [s["px"] for s in fake.swaps[swaps_before:swaps_before + 2]]
          == [0.0, 0.0])
    check("F2: live leg C fires on the lit 1.2x poll (trail)",
          (2, "c", "leg_c_trail_30pct") in le)
    check("F2: live position still open after the lit poll",
          mint5 in _leg_states(live))

    # -- scenario 6: unquotable, never armed, past 24h -----------------
    # (documented divergence: paper writes off at $0; live keeps
    # holding the tokens until a route returns)
    mint6 = _mint("unquot")
    fake.token_balances[mint6] = int(TOKENS * (10 ** DECIMALS))
    _seed(paper, mint6, now - 25 * 3600)
    _seed(live, mint6, now - 25 * 3600)
    swaps_before = len(fake.swaps)
    pe, le = _run_series(paper, live, fake, mint6, [0.0],
                         now - 25 * 3600)
    check("unquotable 24h: paper closes time_stop_24h_unquotable",
          _close_legs(paper)[0] == "time_stop_24h_unquotable")
    check("unquotable 24h: live does NOT write off at $0",
          mint6 in _leg_states(live))
    check("unquotable 24h: live attempted the sale (no route)",
          len(fake.swaps) - swaps_before == 1
          and fake.swaps[-1]["px"] == 0.0)

    # -- live-only: zero on-chain balance closes at 0, no swap -------
    mint6b = _mint("zerobal")
    _seed(live, mint6b, now - 25 * 3600)
    fake.token_balances.pop(mint6b, None)
    fake.current_px = 1.1 * ENTRY
    swaps_before = len(fake.swaps)
    live._poll_exits(lambda c, m, s: 1.1 * ENTRY)
    check("zero-balance: position closes at the time stop",
          _close_legs(live)[0] == "time_stop_24h")
    check("zero-balance: no swap attempted for this mint",
          not any(s["in"] == mint6b
                  for s in fake.swaps[swaps_before:]))
    check("zero-balance: realized is 0 (tokens already gone)",
          _closes(live)[-1]["realized_usd"] == 0.0)

    # -- live-only: unconfirmed send reconciles, never re-sells ------
    mint7 = _mint("reconcile")
    fake.token_balances[mint7] = int(TOKENS * (10 ** DECIMALS))
    _seed(live, mint7, now - 600)
    state = {"px": 2.1 * ENTRY}

    def price_fn7(chain, m, spot_index):
        return state["px"]

    fake.current_px = 2.1 * ENTRY
    fake.fail_next = "confirm_timeout"
    swaps_before = len(fake.swaps)
    live._poll_exits(price_fn7)
    st = _leg_states(live).get(mint7, {})
    check("reconcile: leg A marked done after unconfirmed send",
          st.get("a", (False, None))[0] is True)
    check("reconcile: leg A realized is the measured sale",
          abs(_load_pos(live, mint7)["legs"]["a"]["realized_usd"]
              - 0.5 * TOKENS * 2.1 * ENTRY) < 1e-6)
    check("reconcile: only one swap attempt for leg A",
          len(fake.swaps) - swaps_before == 1)
    # Next poll must NOT re-sell leg A (it is done) and must not
    # fire leg B at 2.1x.
    live._poll_exits(price_fn7)
    check("reconcile: no second sell of leg A on the next poll",
          len(fake.swaps) - swaps_before == 1)
    check("reconcile: leg B still open after the retry poll",
          _leg_states(live)[mint7]["b"][0] is False)

    # -- live-only: entry fills adapter -------------------------------
    mint8 = _mint("entry")
    fills = live._live_fills
    fills.set_entry_context(mint8)
    fake.current_px = ENTRY
    price = fills.honest_entry(ENTRY, 0.9865, 50000.0)
    check("entry adapter: executed price equals the quote "
          "(perfect fill in the fake)", abs(price - ENTRY) < 1e-12)
    check("entry adapter: last_entry carries the signature",
          bool(fills.last_entry
               and fills.last_entry["signature"]))
    check("entry adapter: tokens received match the stake",
          abs(fills.last_entry["tokens_received"] - 986500.0) < 1.0)
    # On-chain dedupe: wallet already holds the token.
    fake.token_balances[mint8] = int(TOKENS * (10 ** DECIMALS))
    fills.set_entry_context(mint8)
    check("entry adapter: refuses a duplicate on-chain holding",
          fills.honest_entry(ENTRY, 0.9865, 50000.0) == 0.0)
    del fake.token_balances[mint8]
    # Dry-run: simulates but never opens a position.
    fake._dry_run = True
    fills.set_entry_context(mint8)
    check("entry adapter: dry-run returns 0.0 (no position)",
          fills.honest_entry(ENTRY, 0.9865, 50000.0) == 0.0)
    fake._dry_run = False

    # -- live-only: full entry via consider_candidate + stamp --------
    mint9 = _mint("fullentry")
    fake.current_px = ENTRY
    live.quote_price = lambda chain, m: ENTRY
    live._fresh_tape = lambda m: {"usd_liquidity": 50000.0,
                                  "usd_market_cap": 100000.0,
                                  "tape_source": "test",
                                  # entry_guard.quote_age_refusal needs stamps; unstamped refuses fail-closed
                                  "fetched_at": now, "received_at": now}
    cand = {"mint": mint9, "symbol": "TEST", "dex": "pumpswap",
            "created_ts": now - 60, "pool_address": "pooladdr1",
            "fdv_usd": 100000.0, "reserve_usd": 50000.0}
    fills.set_entry_context(mint9)
    opened, why, final = live.consider_candidate(cand, now)
    check("consider_candidate opens a live position",
          opened is True)
    pos = _load_pos(live, mint9)
    check("consider_candidate: position tokens are the executed "
          "tokens", abs(pos["tokens"] - 986500.0) < 1.0)
    stamped = live_select.stamp_entry_execution(
        live, mint9, fills.last_entry)
    check("stamp_entry_execution succeeds", stamped is True)
    pos = _load_pos(live, mint9)
    check("stamp: live_execution recorded on the position",
          bool(pos.get("live_execution", {}).get("signature")))
    check("stamp: entry SOL price stored for exit fallback",
          pos.get("sol_price_usd") == SOL_PRICE)
    check("stamp: missing position fails loudly, returns False",
          live_select.stamp_entry_execution(
              live, _mint("ghost"), {}) is False)

    # -- live ledger isolation -----------------------------------------
    check("live ledger lives in its own subdir",
          live.LEDGER_PATH == os.path.join(
              tmp, "live", "live_grad_select_ledger.json"))
    check("live ledger header names the live lane",
          _load_ledger(live)["preregistered_verdict"]["lane"]
          == "grad_select_live")
    check("paper ledger header names the paper lane",
          _load_ledger(paper)["preregistered_verdict"]["lane"]
          == "grad_select")
    check("live verdict chain is separate from paper's",
          os.path.dirname(live.LEDGER_PATH)
          != os.path.dirname(paper.LEDGER_PATH))
    check("live ledger never wrote into the paper ledger",
          mint1 not in _load_ledger(paper)["positions"]
          and mint9 not in _load_ledger(paper)["positions"])

    print("ALL %d CHECKS PASSED" % _PASSED)
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0


def _load_pos(eng, mint):
    return eng._load()["positions"][mint]


def _load_ledger(eng):
    return eng._load()


if __name__ == "__main__":
    raise SystemExit(main())
