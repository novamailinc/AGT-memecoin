"""SimulationExecutor — demo-mode execution with live-order parity.

Implements the SAME execute_swap contract as MainnetExecutor (same
signature, same result-dict shape, same skip reasons, same UNKNOWN
semantics) so the demo path runs the identical order lifecycle as the
intended live path: quote -> price-impact cap -> fee economics ->
fill -> confirm -> measured amounts. Live migration swaps this
executor for MainnetExecutor; strategy, risk and order code above the
executor do not change.

What is REAL here (same sources the live path would use):
  * the quote: fetched through the same JupiterClient against the live
    Jupiter API — live market data, including the route's own
    priceImpactPct;
  * the priority-fee economics: the same PriorityFeePolicy samples and
    caps when a fee policy is attached;
  * every skip decision (no_route / price_impact_unknown /
    price_impact_over_cap / fee_over_cap / fee_uneconomic /
    bundle_stake_unknown / jito_not_configured / fee_policy_required):
    evaluated on the real quote, identical thresholds.

What is SIMULATED (clearly labeled, never presented as measured):
  * the fill price moves between quote and simulated confirmation by a
    latency draw: rng gaussian(0, hourly_vol * sqrt(latency_hours)),
    clipped so an adverse move beyond slippage_bps is a LANDED-BUT-
    FAILED swap (fee paid, no fill) — the same way a real tx that
    moves past its slippage tolerance fails on-chain. Both
    latency_seconds and hourly_vol are CONFIGURED ASSUMPTIONS;
    estimate_volatility_from_series() can ground hourly_vol in an
    observed price series. They are not measurements.
  * confirmation: a simulated signature (deterministic hash) confirms
    after latency_seconds. An injected outage (inject_confirm_outage)
    raises reason "confirm_timeout" WITH the signature — the send is
    UNKNOWN, never failed, exactly the live rule: reconcile from
    balances before retrying.
  * balances: held in a BalanceBook the caller supplies (the demo
    account ledger). Executed amounts are measured from book deltas,
    mirroring the live executor's on-chain balance measurement,
    including the zero_output check.

No keys, no RPC, no send, no signing — this executor can never touch
funds or a chain. Dry-run is not a mode here; simulation is the whole
class.
"""

import hashlib
import math
import random
import time
from typing import Any, Callable, Dict, List, Optional

from broker import BROKER_UNAVAILABLE, BrokerError

from .fee_policy import PriorityFeePolicy
from .jupiter import JupiterClient
from .mainnet_executor import (
    DEFAULT_PRIORITY_FEE_LAMPORTS,
    PRIORITY_FEE_MAX_FRAC_OF_STAKE,
    SOL_MINT,
)

LAMPORTS_PER_SOL = 1_000_000_000

# Solana runtime base fee for a single-signature transaction. The live
# executor never names it because it MEASURES the fee from balance
# deltas; the simulated chain must charge it explicitly, base +
# priority, or every simulated fill is systematically undercharged.
BASE_FEE_LAMPORTS = 5_000

# Configured-assumption defaults (NOT measurements):
DEFAULT_LATENCY_SECONDS = 2.0     # quote->confirm assumption
DEFAULT_HOURLY_VOL = 0.12         # 12%/h, the validation generator's vol


def estimate_volatility_from_series(prices: List[float],
                                    step_seconds: float) -> float:
    """Ground the latency draw's volatility in an observed price series.
    Returns per-hour standard deviation of log returns. Callers should
    prefer this over the configured default whenever real feed history
    is available."""
    if len(prices) < 3 or step_seconds <= 0:
        raise BrokerError(BROKER_UNAVAILABLE,
                          "volatility estimate needs >= 3 prices and a "
                          "positive step")
    if any(isinstance(p, bool) or not isinstance(p, (int, float))
           or not math.isfinite(p) or p <= 0 for p in prices):
        raise BrokerError(
            BROKER_UNAVAILABLE,
            "volatility estimate got a nonpositive price — a bad tick "
            "must fail the estimate, never be silently dropped")
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(3600.0 / step_seconds)


class BalanceBook:
    """Minimal balance protocol the executor measures fills against.
    The demo account ledger provides the real implementation; tests use
    this in-memory one. Amounts are integer base units."""

    def __init__(self, sol_lamports: int = 0):
        self._balances: Dict[str, int] = {SOL_MINT: int(sol_lamports)}

    def balance_of(self, mint: str) -> int:
        return self._balances.get(mint, 0)

    def credit(self, mint: str, amount: int) -> None:
        self._balances[mint] = self.balance_of(mint) + int(amount)

    def debit(self, mint: str, amount: int) -> None:
        amount = int(amount)
        if self.balance_of(mint) < amount:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "simulated balance insufficient — refusing to overdraw "
                "the demo ledger",
                detail={"reason": "sim_insufficient_balance",
                        "mint": mint})
        self._balances[mint] = self.balance_of(mint) - amount


class SimulationExecutor:
    """Demo execution adapter with MainnetExecutor contract parity."""

    def __init__(self,
                 jupiter: Optional[JupiterClient] = None,
                 fee_policy: Optional[PriorityFeePolicy] = None,
                 jito=None,
                 book: Optional[BalanceBook] = None,
                 latency_seconds: float = DEFAULT_LATENCY_SECONDS,
                 hourly_vol: float = DEFAULT_HOURLY_VOL,
                 seed: Optional[int] = None,
                 now_fn: Callable[[], float] = time.time,
                 chain_state_path: Optional[str] = None):
        if latency_seconds < 0 or hourly_vol < 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "latency and volatility must be "
                              "non-negative")
        self._jupiter = jupiter if jupiter is not None else JupiterClient()
        self._fee_policy = fee_policy
        self._jito = jito
        self._book = book if book is not None else BalanceBook()
        self._latency = float(latency_seconds)
        self._vol = float(hourly_vol)
        self._rng = random.Random(seed)
        self._now = now_fn
        self._nonce = 0
        self._outage_next = False
        self._outage_mode = "unlanded"
        self._preallocated = None
        # Durable simulated-chain memory: the TRUE outcome of every swap
        # attempt, recorded before the caller-visible result. This is
        # what reconciliation reads — a chain remembers even when the
        # caller crashed between send and confirm.
        self._chain_state_path = chain_state_path
        self._chain: Dict[str, Any] = {"version": 1, "signatures": {}}
        if chain_state_path is not None:
            import json as _json
            import os as _os
            if _os.path.exists(chain_state_path):
                with open(chain_state_path) as fh:
                    loaded = _json.load(fh)
                if (not isinstance(loaded, dict)
                        or loaded.get("version") != 1
                        or not isinstance(loaded.get("signatures"), dict)):
                    raise ValueError("corrupt simulated-chain state")
                self._chain = loaded
            self._nonce = len(self._chain["signatures"])

    # -- book access (scratch mirror of the caller's authoritative ledger)
    def set_book_balance(self, mint: str, amount: int) -> None:
        """Sync the scratch book from the caller's authoritative state
        before a swap. The book is measurement scratch, never a ledger."""
        self._book._balances[mint] = int(amount)

    def book_balance(self, mint: str) -> int:
        return self._book.balance_of(mint)

    # -- simulated-chain truth ------------------------------------------
    def _chain_record(self, sig: str, record: Dict[str, Any]) -> None:
        """Persist the true outcome BEFORE the caller sees anything."""
        self._chain["signatures"][sig] = record
        if self._chain_state_path is not None:
            import json as _json
            import os as _os
            tmp = self._chain_state_path + ".tmp"
            with open(tmp, "w") as fh:
                _json.dump(self._chain, fh, sort_keys=True)
                fh.flush()
                _os.fsync(fh.fileno())
            _os.replace(tmp, self._chain_state_path)

    def allocate_signature(self, input_mint: str, output_mint: str,
                           amount: int) -> str:
        """Reserve the next swap's signature WITHOUT executing — lets the
        caller seal an unresolved-execution record BEFORE the chain can
        settle anything, closing the crash window between chain effect
        and caller bookkeeping. Verified against the actual swap.

        The reservation itself is durable: a crash after sealing but
        before execution leaves an "allocated" chain record, which is
        PROVABLY unlanded (no send ever happened) rather than a guess."""
        self._nonce += 1
        sig = hashlib.sha256(
            ("sim|%s|%s|%d|%d" % (input_mint, output_mint, int(amount),
                                  self._nonce)).encode()
        ).hexdigest()[:64]
        self._preallocated = (input_mint, output_mint, int(amount), sig)
        self._chain_record(sig, {
            "status": "allocated", "input_mint": input_mint,
            "output_mint": output_mint, "amount": int(amount),
            "fee_lamports": 0})
        return sig

    def reconcile(self, signature: str) -> Dict[str, Any]:
        """The true outcome of a signature: settled (with the recorded
        result), failed (landed, fee lost), unlanded (nothing
        happened, no fee), or allocated (reserved but provably never
        executed — a pre-send crash; treat as unlanded). An unknown
        signature is a reconciliation error, never a guessed outcome."""
        record = self._chain["signatures"].get(signature)
        if record is None:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "signature unknown to the simulated chain — cannot "
                "reconcile; treat as unresolved, not as failure",
                detail={"reason": "unknown_signature",
                        "signature": signature})
        return dict(record)

    # -- test/fault hooks (deterministic, explicit) --------------------
    def inject_confirm_outage(self, landed: str = "unlanded") -> None:
        """The next swap's confirmation times out: UNKNOWN semantics —
        signature attached, never counted as a clean failure. `landed`
        selects the hidden chain truth: "unlanded" (nothing happened,
        no fee), "settled" (the swap actually filled), or "failed"
        (landed but reverted: fee lost, no fill). The caller cannot
        observe which; only reconcile() reveals it."""
        if landed not in ("unlanded", "settled", "failed"):
            raise BrokerError(BROKER_UNAVAILABLE,
                              "outage landing must be "
                              "unlanded|settled|failed")
        self._outage_next = True
        self._outage_mode = landed

    # -- parity contract -------------------------------------------------
    def execute_swap(self, input_mint: str, output_mint: str,
                     amount: int, slippage_bps: int = 100,
                     price_impact_cap_pct: float = 2.0,
                     priority_fee_lamports: int = None,
                     confirm_timeout: float = 60.0,
                     use_bundle: bool = False,
                     jito_tip_lamports: int = None
                     ) -> Dict[str, Any]:
        """One simulated swap through the live order lifecycle.
        Skip reasons and economics identical to MainnetExecutor;
        settlement is simulated per the module docstring."""
        bundle_mode = bool(use_bundle)
        if bundle_mode:
            if self._jito is None:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "use_bundle=True but no Jito client configured — "
                    "refusing to bundle",
                    detail={"reason": "jito_not_configured"})
            if self._fee_policy is None:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "use_bundle=True but no fee policy configured — "
                    "refusing to bundle blind",
                    detail={"reason": "fee_policy_required"})
        if int(amount or 0) <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "swap amount must be positive")
        fee = (DEFAULT_PRIORITY_FEE_LAMPORTS
               if priority_fee_lamports is None
               else int(priority_fee_lamports))
        if fee < 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "priority fee must not be negative")
        # The chain always charges base + priority.
        total_fee = BASE_FEE_LAMPORTS + fee

        in_is_sol = input_mint == SOL_MINT
        out_is_sol = output_mint == SOL_MINT

        # -- quote (REAL, same client the live path uses) ---------------
        try:
            quote = self._jupiter.get_quote(
                input_mint, output_mint, int(amount), slippage_bps)
        except BrokerError as exc:
            det = getattr(exc, "detail", {}) or {}
            if det.get("http_status") == 400:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "jupiter has no route for this swap — skipped",
                    detail={"reason": "no_route"}) from exc
            raise
        if not isinstance(quote, dict) or not quote.get("outAmount"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote missing outAmount — no trade",
                detail={"reason": "no_route"})
        try:
            out_expected = int(quote["outAmount"])
        except (TypeError, ValueError):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote outAmount unparseable — no trade",
                detail={"reason": "no_route"})
        impact_raw = quote.get("priceImpactPct")
        if impact_raw is None:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote has no priceImpactPct — refusing to "
                "trade blind",
                detail={"reason": "price_impact_unknown"})
        try:
            impact = float(impact_raw)
        except (TypeError, ValueError):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter quote priceImpactPct unparseable — "
                "refusing to trade blind",
                detail={"reason": "price_impact_unknown"})
        if impact > float(price_impact_cap_pct):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "jupiter price impact %.2f%% over cap %.2f%% — "
                "skipped" % (impact, price_impact_cap_pct),
                detail={"reason": "price_impact_over_cap",
                        "price_impact_pct": impact})

        trade_sol = int(amount) if in_is_sol else (
            out_expected if out_is_sol else None)
        fee_tier = None
        tip = None
        if bundle_mode:
            if trade_sol is None:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "bundle mode needs a SOL-denominated stake to "
                    "price the fee and tip — token-to-token swaps "
                    "cannot be bundled",
                    detail={"reason": "bundle_stake_unknown"})
            decision = self._fee_policy.choose_fee(trade_sol)
            if decision.get("verdict") != "ok":
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "priority fee %d lamports rejected by fee policy "
                    "(%s) — skipped"
                    % (int(decision.get("fee_lamports") or 0),
                       decision.get("detail") or decision.get("reason")),
                    detail={"reason": decision.get("reason")
                            or "fee_over_cap",
                            "priority_fee_lamports":
                                int(decision.get("fee_lamports") or 0)})
            fee = int(decision["fee_lamports"])
            fee_tier = decision.get("tier")
            tip = (int(jito_tip_lamports)
                   if jito_tip_lamports is not None
                   else int(getattr(self._fee_policy,
                                    "default_tip_lamports", 0)))
            if tip <= 0:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "bundle mode needs a positive Jito tip — skipped",
                    detail={"reason": "fee_uneconomic"})
            tip_check = self._fee_policy.check_jito_tip(
                trade_sol / LAMPORTS_PER_SOL, tip / LAMPORTS_PER_SOL)
            if tip_check.get("verdict") != "economic":
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "jito tip %d lamports (%.1f%% of stake) eats the "
                    "trade — skipped"
                    % (tip, tip_check.get("tip_frac", 0) * 100),
                    detail={"reason": "fee_uneconomic",
                            "tip_lamports": tip,
                            "tip_frac": tip_check.get("tip_frac")})
        if trade_sol is not None and fee > \
                PRIORITY_FEE_MAX_FRAC_OF_STAKE * trade_sol:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "priority fee %d lamports exceeds 5%% of the "
                "trade's SOL value — skipped" % fee,
                detail={"reason": "fee_over_cap",
                        "priority_fee_lamports": fee})

        # -- simulated send: deterministic signature --------------------
        pre = self._preallocated
        self._preallocated = None
        if pre is not None and pre[:3] == (input_mint, output_mint,
                                           int(amount)):
            # allocate_signature() already advanced the nonce and wrote
            # the durable "allocated" record this execution settles.
            sig = pre[3]
        else:
            self._nonce += 1
            sig = hashlib.sha256(
                ("sim|%s|%s|%d|%d" % (input_mint, output_mint,
                                      int(amount), self._nonce)).encode()
            ).hexdigest()[:64]

        sol_before = self._book.balance_of(SOL_MINT)
        in_before = self._book.balance_of(input_mint)
        out_before = self._book.balance_of(output_mint)

        # Consume a one-shot injected outage BEFORE any branch below can
        # raise: a landed-failure or insolvency raise must never leak
        # the armed flag into the next swap.
        outage = self._outage_next
        outage_mode = self._outage_mode
        self._outage_next = False

        # Fail closed on insolvency: the real chain never partially
        # pays a fee, and a clamped fee would hide an empty wallet.
        if sol_before < total_fee:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "insufficient SOL for the network fee: have %d "
                "lamports, need %d — refusing to send"
                % (sol_before, total_fee),
                detail={"reason": "insufficient_funds_for_fee",
                        "have_lamports": sol_before,
                        "need_lamports": total_fee})
        if in_is_sol and sol_before < int(amount) + total_fee:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "insufficient SOL for swap + fee: have %d lamports, "
                "need %d — refusing to send"
                % (sol_before, int(amount) + total_fee),
                detail={"reason": "insufficient_funds",
                        "have_lamports": sol_before,
                        "need_lamports": int(amount) + total_fee})

        # -- simulated confirmation window ------------------------------
        # The price moves during quote->confirm by a seeded draw. An
        # adverse move beyond the slippage tolerance is a landed-but-
        # failed swap: fee lost, no fill — never a silent retry.
        latency_hours = self._latency / 3600.0
        move = self._rng.gauss(0.0, self._vol * math.sqrt(latency_hours))
        slip_frac = slippage_bps / 10_000.0
        if move < -slip_frac:
            self._book.debit(SOL_MINT, total_fee)
            self._chain_record(sig, {
                "status": "failed", "input_mint": input_mint,
                "output_mint": output_mint, "amount": int(amount),
                "fee_lamports": total_fee,
                "base_fee_lamports": BASE_FEE_LAMPORTS,
                "latency_move_pct": move * 100.0})
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "simulated swap landed failed: price moved %.2f%% "
                "against the quote beyond the %d bps tolerance — "
                "network fee lost, no fill"
                % (move * 100.0, slippage_bps),
                detail={"reason": "tx_failed",
                        "signature": sig,
                        "latency_move_pct": move * 100.0,
                        "fee_lamports": total_fee,
                        "priority_fee_lamports": fee})
        if outage and outage_mode == "unlanded":
            self._chain_record(sig, {
                "status": "unlanded", "input_mint": input_mint,
                "output_mint": output_mint, "amount": int(amount),
                "fee_lamports": 0})
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "simulated swap unconfirmed (UNKNOWN, not failed) — "
                "reconcile from balances before retrying",
                detail={"reason": "confirm_timeout",
                        "signature": sig})
        if outage and outage_mode == "failed":
            # Landed but reverted: the fee is gone, no fill. The caller
            # sees only confirm_timeout; reconcile() reveals the loss.
            self._book.debit(SOL_MINT, total_fee)
            self._chain_record(sig, {
                "status": "failed", "input_mint": input_mint,
                "output_mint": output_mint, "amount": int(amount),
                "fee_lamports": total_fee,
                "base_fee_lamports": BASE_FEE_LAMPORTS,
                "latency_move_pct": move * 100.0})
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "simulated swap unconfirmed (UNKNOWN, not failed) — "
                "reconcile from balances before retrying",
                detail={"reason": "confirm_timeout",
                        "signature": sig})
        # (outage "settled" falls through: settle, record, THEN raise.)

        # -- settle on the book, measure like the live executor ---------
        out_amount = int(out_expected * (1.0 + move))
        self._book.debit(input_mint, int(amount))
        # Network fee (base + priority) is always paid in SOL, in both
        # swap directions; insolvency was refused above, never clamped.
        self._book.debit(SOL_MINT, total_fee)
        self._book.credit(output_mint, out_amount)

        sol_after = self._book.balance_of(SOL_MINT)
        in_after = self._book.balance_of(input_mint)
        out_after = self._book.balance_of(output_mint)
        measured_out = out_after - out_before
        if measured_out <= 0:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "confirmed swap moved no output tokens — "
                "reconcile before retrying",
                detail={"reason": "zero_output", "signature": sig})
        if measured_out < 0.5 * out_expected:
            # Same warning the live executor logs; not a failure.
            pass
        if in_is_sol:
            fee_paid = max(0, (sol_before - sol_after) - int(amount))
        elif out_is_sol:
            fee_paid = max(0, out_expected - measured_out)
        else:
            fee_paid = fee

        result = {
            "signature": sig, "simulated": True,
            "input_mint": input_mint, "output_mint": output_mint,
            "in_amount": int(amount),
            "out_amount": measured_out,
            "sol_before": sol_before, "sol_after": sol_after,
            "in_before": in_before, "in_after": in_after,
            "out_before": out_before, "out_after": out_after,
            "priority_fee_lamports": fee_paid,
            "base_fee_lamports": BASE_FEE_LAMPORTS,
            "price_impact_pct": impact,
            "quote_out_amount": out_expected,
            "latency_move_pct": move * 100.0,
            "latency_seconds": self._latency,
            "compute_units": None,
            "compute_units_source": "simulated",
            "units_consumed": None,
        }
        if bundle_mode:
            result["bundle"] = {
                "dry_run": True, "bundle_id": None,
                "payload": None,
                "tip_lamports": tip, "fee_tier": fee_tier,
            }
            result["bundle_id"] = None
            result["jito_tip_lamports"] = tip
            result["fee_tier"] = fee_tier
        self._chain_record(sig, {"status": "settled", "result": result})
        if outage:
            # The swap LANDED and settled; only the confirmation was
            # lost. The caller sees UNKNOWN; reconcile() adopts the
            # recorded result.
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "simulated swap unconfirmed (UNKNOWN, not failed) — "
                "reconcile from balances before retrying",
                detail={"reason": "confirm_timeout",
                        "signature": sig})
        return result
