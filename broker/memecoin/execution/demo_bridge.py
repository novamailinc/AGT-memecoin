"""DemoExecutionBridge — adapter-level fills over SimulationExecutor.

Translates the MemecoinAdapter's fill vocabulary (SOL-denominated
volume, token quantities, price_sol) into SimulationExecutor
execute_swap calls (lamports in/out through a real Jupiter quote) and
back, so the adapter's demo mode runs the SAME order lifecycle as the
intended live path instead of an optimistic quote-price fill.

Skip/failure mapping is one-to-one and lossless: every BrokerError
from the executor reaches the caller with its original detail reason
(no_route, price_impact_over_cap, fee_over_cap, fee_uneconomic,
swap_failed_on_chain, confirm_timeout, zero_output, ...). The bridge
never converts a failure into a fill or a skip into a retry.

Decimals come from the caller's decimals_fn (market metadata), NOT
from a chain lookup — demo has no RPC. A missing decimals entry is a
config error, never a guessed 9.
"""

from typing import Any, Callable, Dict

from broker import BROKER_UNAVAILABLE, BrokerError

from .mainnet_executor import SOL_MINT
from .sim_executor import SimulationExecutor

LAMPORTS_PER_SOL = 1_000_000_000


class DemoExecutionBridge:
    def __init__(self, executor: SimulationExecutor,
                 decimals_fn: Callable[[str], int]):
        self.executor = executor
        self._decimals_fn = decimals_fn

    def decimals(self, mint: str) -> int:
        return self._decimals(mint)

    def _decimals(self, mint: str) -> int:
        try:
            dec = int(self._decimals_fn(mint))
        except Exception as exc:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "token decimals unavailable — refusing to size a "
                "simulated sell blind",
                detail={"reason": "decimals_unknown", "mint": mint}
            ) from exc
        if dec < 0 or dec > 18:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "implausible token decimals — refusing to size a "
                "simulated sell blind",
                detail={"reason": "decimals_unknown", "mint": mint})
        return dec

    def buy(self, mint: str, volume_sol: float,
            slippage_bps: int = 100,
            price_impact_cap_pct: float = 2.0) -> Dict[str, Any]:
        """Spend volume_sol of SOL for tokens. Returns the fill:
        qty_tokens (float), price_sol, fee_lamports, signature and the
        raw executor result. Raises the executor's skips unchanged."""
        lamports = int(round(volume_sol * LAMPORTS_PER_SOL))
        if lamports <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "demo buy volume below one lamport",
                              detail={"reason": "amount_too_small"})
        res = self.executor.execute_swap(
            SOL_MINT, mint, lamports, slippage_bps=slippage_bps,
            price_impact_cap_pct=price_impact_cap_pct)
        dec = self._decimals(mint)
        qty = res["out_amount"] / (10 ** dec)
        if qty <= 0:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "demo buy moved no tokens — reconcile before retrying",
                detail={"reason": "zero_output",
                        "signature": res["signature"]})
        return {
            "qty_tokens": qty,
            "price_sol": volume_sol / qty,
            "fee_lamports": res["priority_fee_lamports"],
            "signature": res["signature"],
            "latency_move_pct": res.get("latency_move_pct"),
            "price_impact_pct": res.get("price_impact_pct"),
            "raw": res,
        }

    def sell(self, mint: str, qty_tokens: float,
             slippage_bps: int = 100,
             price_impact_cap_pct: float = 2.0) -> Dict[str, Any]:
        """Sell qty_tokens of a token for SOL. Returns proceeds_sol,
        price_sol, fee_lamports, signature and the raw result. Raises
        the executor's skips unchanged."""
        dec = self._decimals(mint)
        base_units = int(round(qty_tokens * (10 ** dec)))
        if base_units <= 0:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "demo sell quantity below one base unit",
                              detail={"reason": "amount_too_small"})
        res = self.executor.execute_swap(
            mint, SOL_MINT, base_units, slippage_bps=slippage_bps,
            price_impact_cap_pct=price_impact_cap_pct)
        proceeds_sol = res["out_amount"] / LAMPORTS_PER_SOL
        if proceeds_sol <= 0:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "demo sell moved no SOL — reconcile before retrying",
                detail={"reason": "zero_output",
                        "signature": res["signature"]})
        return {
            "proceeds_sol": proceeds_sol,
            "price_sol": proceeds_sol / qty_tokens,
            "fee_lamports": res["priority_fee_lamports"],
            "signature": res["signature"],
            "latency_move_pct": res.get("latency_move_pct"),
            "price_impact_pct": res.get("price_impact_pct"),
            "raw": res,
        }
