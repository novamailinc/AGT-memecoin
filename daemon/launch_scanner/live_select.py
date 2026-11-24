#!/usr/bin/env python3
"""Live grad_select lane (mainnet, 2026-11-06 build).

The paper grad_select lane, but its fills are REAL: entries buy
$1 of the token on mainnet via Jupiter (MainnetExecutor) and exits
sell real token legs back to SOL. Everything else — the four
evidence filters, the ladder (50%@2x / 25%@3x / 25% trail 30% /
-50% stop / 24h), the killswitch lane, the verdict terms — mirrors
paper grad_select exactly, so the live ledger differs from paper
ONLY in that its fills are executed, not modeled.

Hard rules (see live-executor-plan.md):
- Dry-run by default. Live sends need the deployment marker armed
  AND executor.arm_live() — both deliberate, both reversible.
- This module REFUSES to run on the build VM: make_live_engine
  requires a deployment marker (FOREX_AGENT_HOME/live/
  deployment.json) — the build VM has none, so connect() fails
  closed there. The live trader ALSO refuses unless the marker
  names the deployment machine.
- Paper is untouched: the live ledger lives in its own subdir
  (FOREX_AGENT_HOME/live/live_grad_select_ledger.json) so its
  verdict chain never mixes with paper's, and the engine is a
  FRESH grad.py instance — paper's module instance is never
  modified.
- Exits execute swaps while _poll_exits holds the ledger flock
  (the paper decision code is reused untouched). Single-process
  trader, MAX_OPEN 6, legs fire rarely: a slow confirm blocks
  the loop for up to ~60s. Accepted v1 wart, documented here.
- An unconfirmed send (confirm_timeout / zero_output) is
  reconciled from on-chain balances BEFORE any retry: if the
  tokens demonstrably left, the leg is marked done with the
  measured amounts; if not, the leg stays open for the next
  poll. Never blind-retry a swap.
"""

import importlib.util
import json
import logging
import os
import time

from broker import BrokerError
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, SOL_MINT)

logger = logging.getLogger("live_select")

# -- lane terms (pre-registered; changing them means deleting the
#    ledger, which is itself visible) -------------------------------
LANE = "grad_select_live"
CHAIN = "solana"
BANKROLL_START = 6.00          # the user's $6 challenge, paper-mirrored
NOTIONAL_USD = 1.00            # $1/shot, same as the CEO account
MAX_OPEN = 6                   # $6 / $1 per coin
DAILY_ENTRY_CAP = None         # NO daily cap; bankroll is the limit
MAX_GRAD_AGE_S = 1800.0        # 30-min window, same as paper select
VERDICT_SAMPLE_CLOSES = 100
DAILY_LOSS_CAP_USD = 3.00      # plan Step 5: half the bankroll/day
FAIL_STREAK_HALT = 5           # plan Step 5: consecutive failures

LIVE_DIRNAME = "live"
LEDGER_FILENAME = "live_grad_select_ledger.json"
LAMPORTS_PER_SOL = 1_000_000_000

# On-chain dedupe: refuse an entry when the wallet already holds
# more than 1% of the expected buy in this token (a previous live
# buy of the same mint — ledger dedupe should have caught it, but
# the chain is the ground truth).
HELD_MAX_FRAC_OF_BUY = 0.01

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")
try:
    from daemon.launch_scanner import grad_raw as _raw
except Exception:  # pragma: no cover - import shape guard
    _raw = None


def live_home_dir(home):
    return os.path.join(os.path.abspath(home), LIVE_DIRNAME)


def live_ledger_path(home):
    return os.path.join(live_home_dir(home), LEDGER_FILENAME)


def today_pnl_usd(engine, now=None):
    """Realized P&L of closes whose close_ts falls on the current
    UTC date (same day boundary as grad.py's entries_day). Used by
    the trader's daily-loss-cap gate. Never raises — an unreadable
    ledger returns 0.0 (the trader's preflight treats an unreadable
    ledger as its own separate failure)."""
    try:
        d = engine._load()
    except Exception:
        return 0.0
    now = time.time() if now is None else now
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    total = 0.0
    for c in d.get("closes") or []:
        ts = c.get("close_ts") or 0
        if time.strftime("%Y-%m-%d", time.gmtime(ts)) == today:
            total += float(c.get("pnl_usd") or 0)
    return round(total, 4)


def _preregistration():
    """Verdict terms for the LIVE lane, stamped into its ledger
    header at creation. Same shape as paper select, live values."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'grad_select_live'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only. LIVE MAINNET: real $1 stakes, real fills, "
                 "real fees." % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,
            "max_grad_age_s": MAX_GRAD_AGE_S,
            "daily_loss_cap_usd": DAILY_LOSS_CAP_USD,
            "runner_rule_applies": False,
            "paper_equivalent": False,  # live carries mandatory safety/veto; paper grad_select does not
            "safety_gate": "grad._safety_ok (RugCheck / on-chain fallback; unknown is not a pass). MANDATORY (directive 11/25)",
            "veto": "grad._veto_ok, fail-closed when the veto engine is unavailable. MANDATORY",
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
            "execution": "mainnet Jupiter swaps via MainnetExecutor; "
                         "dry-run default; armed only on the "
                         "deployment machine",
        },
        "registered_ts": time.time(),
    }


# -- executor failure accounting --------------------------------------
# Skip reasons are NOT failures (the plan: quote rejections are
# skipped cycles, not fail-streak events). Everything else a swap
# raises — send failure, confirm timeout, failed simulate, RPC
# errors — counts. Step 5 halts entries at 5 consecutive.
SKIP_REASONS = frozenset({"no_route", "price_impact_over_cap",
                          "fee_over_cap", "price_impact_unknown"})


class FailStreak:
    """Consecutive executor failures. ok() resets on any cleanly
    executed swap; fail() counts one failure."""

    def __init__(self):
        self.count = 0

    def fail(self):
        self.count += 1
        return self.count

    def ok(self):
        self.count = 0


def _swap_failed(streak, exc):
    """Classify a swap BrokerError: False for skips (no streak
    change), True for failures (streak counted). Never raises."""
    det = getattr(exc, "detail", {}) or {}
    if det.get("reason") in SKIP_REASONS:
        return False
    try:
        streak.fail()
    except Exception:
        pass
    return True


# -- fills adapter ----------------------------------------------------
class LiveFills:
    """The grad.py fills adapter for the live lane.

    honest_entry executes the REAL buy swap through the executor
    and returns the executed USD/token price (so the position's
    token count equals the tokens actually received). Any failure
    — no route, over-cap impact, failed simulate, dry-run mode,
    unknown SOL price, unknown decimals, on-chain dedupe hit —
    returns 0.0, which the paper entry path treats as "bad fill
    price" and skips WITHOUT opening a position. The swap must
    never be followed by a modeled fill.

    honest_exit is identity: exits go through the live close
    overrides below, never through paper's modeled exit fill.
    """

    def __init__(self, executor, fail_streak=None):
        self._executor = executor
        self._fail_streak = fail_streak or FailStreak()
        self._entry_ctx = None      # {"mint": str} per candidate
        self.last_entry = None      # details of the last executed buy

    def set_entry_context(self, mint):
        self._entry_ctx = {"mint": mint}
        self.last_entry = None

    def clear_entry_context(self):
        self._entry_ctx = None

    def honest_entry(self, quoted, eff_notional, liquidity):
        ctx = self._entry_ctx
        if not ctx or not ctx.get("mint"):
            logger.warning("live fills: no entry context — refusing")
            return 0.0
        mint = ctx["mint"]
        ex = self._executor
        try:
            if ex.dry_run:
                # Dry-run still quotes + simulates the swap (validates
                # the route), but MUST NOT open a position: return 0.
                res = ex.execute_swap(SOL_MINT, mint,
                                      self._stake_lamports(
                                          eff_notional))
                self._fail_streak.ok()
                logger.info("live fills dry-run simulated buy %s "
                            "(no position opened)", mint[:12])
                return 0.0
            decimals = ex.get_mint_decimals(mint)
            held_base = ex.get_token_balance(mint)
            expected = eff_notional / quoted if quoted and quoted > 0 \
                else 0.0
            held_tokens = held_base / (10 ** decimals)
            if expected > 0 and held_tokens > \
                    HELD_MAX_FRAC_OF_BUY * expected:
                logger.warning(
                    "live fills: wallet already holds %.4g of %s "
                    "on-chain — refusing duplicate buy",
                    held_tokens, mint[:12])
                return 0.0
            stake = self._stake_lamports(eff_notional)
            if stake <= 0:
                return 0.0
            res = ex.execute_swap(SOL_MINT, mint, stake)
        except BrokerError as exc:
            det = getattr(exc, "detail", {}) or {}
            _swap_failed(self._fail_streak, exc)
            logger.warning("live fills: buy swap for %s refused/failed "
                           "(%s) — no position opened",
                           mint[:12], det.get("reason") or exc)
            return 0.0
        except Exception:
            self._fail_streak.fail()
            logger.exception("live fills: unexpected buy failure %s",
                             mint[:12])
            return 0.0
        try:
            tokens = res["out_amount"] / (10 ** decimals)
        except Exception:
            tokens = 0.0
        if tokens <= 0:
            logger.warning("live fills: buy of %s returned no tokens",
                           mint[:12])
            return 0.0
        self._fail_streak.ok()
        sol_price = ex.sol_price_usd()
        fee_sol = res["priority_fee_lamports"] / LAMPORTS_PER_SOL
        self.last_entry = {
            "mint": mint,
            "signature": res["signature"],
            "tokens_received": tokens,
            "stake_lamports": int(res["in_amount"]),
            "fee_sol": fee_sol,
            "fee_usd": (fee_sol * sol_price
                        if sol_price else None),
            "sol_price_usd": sol_price,
            "simulated": bool(res.get("simulated")),
        }
        return eff_notional / tokens

    def honest_exit(self, exit_price, leg_usd, liquidity):
        # Live exits never take this path (close overrides below);
        # identity is the safe fallback if one ever does.
        return exit_price

    def _stake_lamports(self, eff_notional):
        sol_price = self._executor.sol_price_usd()
        if not sol_price or sol_price <= 0:
            return 0
        return int(eff_notional / sol_price * LAMPORTS_PER_SOL)


# -- exit execution ---------------------------------------------------
def _usd_value(executor, pos, sol_lamports, tokens_sold,
               exit_price):
    """USD value of a confirmed sale. Prefer the live SOL price;
    fall back to the entry-time SOL price (flagged stale); last
    resort is the decision quote price. Never raises — a confirmed
    sale must always be markable, or the next poll would sell the
    same leg twice."""
    sol_price = None
    stale = False
    try:
        sol_price = executor.sol_price_usd()
    except Exception:
        sol_price = None
    if sol_price and sol_price > 0:
        return sol_lamports / LAMPORTS_PER_SOL * sol_price, False
    entry_price = pos.get("sol_price_usd")
    if entry_price and entry_price > 0:
        return sol_lamports / LAMPORTS_PER_SOL * entry_price, True
    return tokens_sold * exit_price, True


def _reconcile_sale(executor, mint, sol_before_lamports,
                    token_before_base, min_sold_base):
    """After an UNKNOWN send, read the balances again. Returns
    (sold_base, sol_received_lamports) when the sale demonstrably
    happened, else None (still unknown — retry next poll)."""
    try:
        token_now = executor.get_token_balance(mint)
        sol_now = int(round(executor.get_balance_sol()
                            * LAMPORTS_PER_SOL))
    except Exception:
        return None
    sold = token_before_base - token_now
    if sold >= min_sold_base:
        return sold, max(0, sol_now - sol_before_lamports)
    return None


def _mark_leg_sold(pos, leg, reason, sold_base, sol_lamports,
                   decimals, signature, fee_lamports, exit_price,
                   executor):
    """Mark one leg done with MEASURED amounts. Returns realized
    USD for the leg."""
    tokens_sold = sold_base / (10 ** decimals)
    gross_usd, stale = _usd_value(executor, pos, sol_lamports,
                                  tokens_sold, exit_price)
    px = gross_usd / tokens_sold if tokens_sold > 0 else exit_price
    leg.update(done=True, exit_price=round(px, 8),
               realized_usd=round(gross_usd, 6),
               exit_reason=reason, live_signature=signature,
               live_fee_sol=(fee_lamports / LAMPORTS_PER_SOL
                             if fee_lamports is not None else None),
               live_usd_price_stale=stale)
    pos["realized_usd"] = round(pos["realized_usd"] + gross_usd, 6)
    return gross_usd


def _make_close_leg(engine, executor, fail_streak):
    def _live_close_leg(pos, leg_key, exit_price, reason):
        leg = pos["legs"][leg_key]
        if leg.get("done"):
            return 0.0
        mint = pos["mint"]
        decimals = executor.get_mint_decimals(mint)
        leg_tokens = pos["tokens"] * leg["frac"]
        base_units = int(leg_tokens * (10 ** decimals))
        if base_units <= 0:
            leg.update(done=True, exit_price=exit_price,
                       realized_usd=0.0, exit_reason=reason)
            return 0.0
        sol_before = int(round(executor.get_balance_sol()
                               * LAMPORTS_PER_SOL))
        token_before = executor.get_token_balance(mint)
        try:
            res = executor.execute_swap(mint, SOL_MINT, base_units)
        except BrokerError as exc:
            det = getattr(exc, "detail", {}) or {}
            if det.get("reason") in ("confirm_timeout", "zero_output") \
                    and det.get("signature"):
                rec = _reconcile_sale(
                    executor, mint, sol_before, token_before,
                    int(0.9 * base_units))
                if rec is not None:
                    # The timeout still counts (the confirm path
                    # degraded), even though the sale reconciled.
                    _swap_failed(fail_streak, exc)
                    sold_base, sol_recv = rec
                    return _mark_leg_sold(
                        pos, leg, reason, sold_base, sol_recv,
                        decimals, det["signature"], None, exit_price,
                        executor)
                logger.warning(
                    "live close leg %s %s: send %s unconfirmed and "
                    "unreconciled — leg stays open for next poll",
                    mint[:12], leg_key, det["signature"][:12])
            _swap_failed(fail_streak, exc)
            raise  # nothing sent (or still unknown): retry next poll
        fail_streak.ok()
        sold_base = res["in_before"] - res["in_after"]
        return _mark_leg_sold(
            pos, leg, reason, sold_base, res["out_amount"], decimals,
            res["signature"], res["priority_fee_lamports"], exit_price,
            executor)
    return _live_close_leg


def _make_close_remaining(engine, executor, fail_streak):
    def _live_close_remaining(pos, exit_price, reason):
        legs = pos["legs"]
        open_legs = [k for k in ("a", "b", "c")
                     if not legs[k].get("done")]
        if not open_legs:
            return 0.0
        mint = pos["mint"]
        decimals = executor.get_mint_decimals(mint)
        # Sell the ENTIRE on-chain balance (leg fractions apply to
        # the balance at leg time; the last leg takes what is left,
        # including any rounding dust the fractions missed).
        token_before = executor.get_token_balance(mint)
        if token_before <= 0:
            # Tokens already gone (sold by an earlier leg or never
            # arrived): mark done with zero rather than attempting
            # a zero-amount swap.
            for k in open_legs:
                legs[k].update(done=True, exit_price=exit_price,
                               realized_usd=0.0, exit_reason=reason,
                               live_zero_balance=True)
            return 0.0
        sol_before = int(round(executor.get_balance_sol()
                               * LAMPORTS_PER_SOL))
        try:
            res = executor.execute_swap(mint, SOL_MINT, token_before)
        except BrokerError as exc:
            det = getattr(exc, "detail", {}) or {}
            if det.get("reason") in ("confirm_timeout", "zero_output") \
                    and det.get("signature"):
                rec = _reconcile_sale(
                    executor, mint, sol_before, token_before,
                    int(0.9 * token_before))
                if rec is not None:
                    _swap_failed(fail_streak, exc)
                    sold_base, sol_recv = rec
                    return _mark_remaining_sold(
                        pos, open_legs, reason, sold_base, sol_recv,
                        decimals, det["signature"], None, exit_price,
                        executor)
                logger.warning(
                    "live close remaining %s: send %s unconfirmed "
                    "and unreconciled — legs stay open",
                    mint[:12], det["signature"][:12])
            _swap_failed(fail_streak, exc)
            raise
        fail_streak.ok()
        sold_base = res["in_before"] - res["in_after"]
        return _mark_remaining_sold(
            pos, open_legs, reason, sold_base, res["out_amount"],
            decimals, res["signature"],
            res["priority_fee_lamports"], exit_price, executor)
    return _live_close_remaining


def _mark_remaining_sold(pos, open_legs, reason, sold_base,
                         sol_lamports, decimals, signature,
                         fee_lamports, exit_price, executor):
    """Mark all open legs done from one measured sale, splitting
    realized by token share (paper's formula). Returns total
    realized USD."""
    tokens_sold = sold_base / (10 ** decimals)
    gross_usd, stale = _usd_value(executor, pos, sol_lamports,
                                  tokens_sold, exit_price)
    px = gross_usd / tokens_sold if tokens_sold > 0 else exit_price
    total_tokens = pos["tokens"] or 0.0
    for k in open_legs:
        leg = pos["legs"][k]
        share = (pos["tokens"] * leg["frac"]) / total_tokens \
            if total_tokens > 0 else 0.0
        leg_real = gross_usd * share
        leg.update(done=True, exit_price=round(px, 8),
                   realized_usd=round(leg_real, 6),
                   exit_reason=reason, live_signature=signature,
                   live_fee_sol=(fee_lamports / LAMPORTS_PER_SOL
                                 if fee_lamports is not None else None),
                   live_usd_price_stale=stale)
    pos["realized_usd"] = round(pos["realized_usd"] + gross_usd, 6)
    return gross_usd


# -- post-entry stamp ---------------------------------------------------
def stamp_entry_execution(engine, mint, details):
    """After _consider_candidate opens a live position, stamp the
    real execution details into it and charge the entry priority
    fee against realized_usd so _finalize's pnl includes it.
    details: the fills adapter's last_entry dict. Returns True on
    success. Never raises — a failed stamp is logged loudly (the
    position is real and must be findable either way)."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = (d.get("positions") or {}).get(mint)
            if pos is None:
                logger.error(
                    "LIVE STAMP FAILED: no open position for %s "
                    "after executed buy sig=%s — position record "
                    "missing, reconcile from the chain",
                    mint[:12], str(details.get("signature"))[:12])
                return False
            fee_usd = details.get("fee_usd")
            pos["live_execution"] = {
                "signature": details.get("signature"),
                "tokens_received": details.get("tokens_received"),
                "stake_lamports": details.get("stake_lamports"),
                "fee_sol": details.get("fee_sol"),
                "fee_usd": fee_usd,
                "sol_price_usd": details.get("sol_price_usd"),
            }
            if details.get("sol_price_usd"):
                pos["sol_price_usd"] = details["sol_price_usd"]
            if fee_usd:
                pos["realized_usd"] = round(
                    pos.get("realized_usd", 0.0) - fee_usd, 6)
            engine._write(d)
        return True
    except Exception:
        logger.exception("LIVE STAMP FAILED for %s", mint[:12])
        return False


# -- engine factory -----------------------------------------------------

def _install_mandatory_gates(eng):
    """Keep the engine's real safety and veto, refuse the raw no-gate stubs, and make the veto fail CLOSED: grad._veto_ok
    treats an engine exception as "no evidence" and passes, which is acceptable for paper research but not for a lane that
    spends real money (directive section 25: never open positions when critical intelligence is unreliable)."""
    stubs = set()
    if _raw is not None:
        stubs = {_raw._no_safety_gate, _raw._no_veto}
    base_safety, base_veto = getattr(eng, "_safety_ok", None), getattr(eng, "_veto_ok", None)
    if not callable(base_safety) or not callable(base_veto) or base_safety in stubs or base_veto in stubs:
        raise BrokerError("config_invalid",
                          "live engine refused: mandatory safety/veto gates missing or replaced by no-op stubs")

    def _veto_fail_closed(mint, symbol, safety_report):
        ok, why = base_veto(mint, symbol, safety_report)
        if ok and str(why or "").startswith("veto unavailable"):
            return False, "live lane: veto engine unavailable, entry refused (fail closed): %s" % why
        return ok, why

    eng._veto_ok = _veto_fail_closed

def make_live_engine(home, executor):
    """Load grad.py as a fresh, isolated module and override its
    lane policy for the LIVE lane: live ledger/limits/
    preregistration, no safety gate / no veto (paper select's
    overrides), live fills + live close overrides. The paper
    lanes' own module instances are never touched.

    The executor must already be connected (connect() enforces
    the deployment marker), so this refuses on any machine
    without one — including the build VM.
    """
    if not isinstance(executor, MainnetExecutor):
        raise TypeError("live engine needs a MainnetExecutor")
    if not executor.wallet_pubkey:
        raise BrokerError("broker_unavailable",
                          "live engine needs a connected executor "
                          "(connect() first — deployment marker "
                          "required)")
    spec = importlib.util.spec_from_file_location(
        "grad_live_engine", _GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    # The live subdir may not exist on a fresh deployment machine
    # (the ledger lock file is opened before any ledger write).
    os.makedirs(live_home_dir(home), exist_ok=True)
    fail_streak = FailStreak()
    eng.LEDGER_PATH = live_ledger_path(home)
    eng.LANE = LANE
    eng.NOTIONAL_USD = NOTIONAL_USD
    eng.MAX_OPEN = MAX_OPEN
    eng.DAILY_ENTRY_CAP = DAILY_ENTRY_CAP
    eng.MAX_GRAD_AGE_S = MAX_GRAD_AGE_S
    eng.BANKROLL_START = BANKROLL_START
    # MANDATORY SAFETY (original owner directive sections 11 and 25, recovered from the owner's own message
    # 2026-11-22 14:25 PST): every proposed REAL-MONEY trade passes enforceable token-safety and veto checks, and the bot
    # never opens positions on unreliable critical intelligence. This lane therefore keeps grad.py's real, fail-closed
    # _safety_ok (RugCheck / on-chain fallback; unknown is not a pass) and _veto_ok. The raw no-gate stubs belong to the
    # experimental research lanes only and are refused here. NOTE: this makes the live lane NOT equivalent to paper
    # grad_select, so paper results must not be read as live evidence.
    _install_mandatory_gates(eng)
    eng._preregistration = _preregistration
    fills = LiveFills(executor, fail_streak)
    eng._fills = fills
    eng._live_fills = fills
    eng._close_leg = _make_close_leg(eng, executor, fail_streak)
    eng._close_remaining = _make_close_remaining(eng, executor,
                                                 fail_streak)
    eng._live_executor = executor
    eng._live_fail_streak = fail_streak
    # Wrap consider_candidate so the fills adapter knows which mint
    # each entry attempt is for (scan_once calls it internally, with
    # no hook) and so the executed buy is stamped onto the position
    # the moment it opens. The wrapper is the ONLY place a live
    # position is created; a position that opens without a recorded
    # executed buy is logged as a critical error (it would be
    # unbacked — no swap happened — and needs manual reconciliation).
    _orig_consider = eng.consider_candidate

    def _live_consider(cand, now=None):
        mint = str((cand or {}).get("mint") or "")
        fills.set_entry_context(mint)
        try:
            opened, why, final = _orig_consider(cand, now)
        finally:
            fills.clear_entry_context()
        if opened:
            if fills.last_entry is not None:
                stamp_entry_execution(eng, mint, fills.last_entry)
            else:
                logger.error(
                    "LIVE ENTRY WITHOUT EXECUTION: %s opened with no "
                    "recorded buy swap — position is UNBACKED, "
                    "reconcile from the chain before trusting it",
                    mint[:12])
        return opened, why, final

    eng.consider_candidate = _live_consider
    return eng
