"""Bonding-curve math for a pump.fun-style launch (synthetic lab).

Pure stdlib, no network.  The curve is a constant-product market maker
over *virtual* reserves:  k = virtual_sol * virtual_tokens.  Virtual
reserves are larger than the real pool, which is what gives the curve
its starting price and its smooth price rise toward graduation.

Fee model (documented, so tests can hand-check it):
  * a buy pays ``fee_bps`` on the SOL side: only
    ``gross * (1 - fee)`` enters the virtual SOL reserve;
  * a sell receives ``net * (1 - fee)``: the fee is skimmed from the
    SOL that leaves the virtual reserve.
So a buy-then-sell round trip over the same size loses (about) the fee
twice and nothing else -- there is no hidden spread in this model.

Fail-closed: every public method raises ValueError on non-positive
amounts, non-positive reserves, out-of-range percentages, or a trade
that would drain the virtual token reserve.  Nothing returns a
plausible-looking zero for bad input.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def _require_positive(name: str, value: float) -> float:
    if value is None or not (value > 0):
        raise ValueError(f"{name} must be positive, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class Curve:
    """A virtual-reserve bonding curve.

    Defaults resemble a pump.fun launch: ~1.073B virtual tokens against
    30 virtual SOL, 1B total supply, graduation near 85 real SOL.
    """

    virtual_token_reserves: float = 1.073e9
    virtual_sol_reserves: float = 30.0
    total_supply: float = 1.0e9
    fee_bps: int = 100
    graduation_target_sol: float = 85.0

    def __post_init__(self) -> None:
        _require_positive("virtual_token_reserves", self.virtual_token_reserves)
        _require_positive("virtual_sol_reserves", self.virtual_sol_reserves)
        _require_positive("total_supply", self.total_supply)
        _require_positive("graduation_target_sol", self.graduation_target_sol)
        if not (0 <= self.fee_bps < 10_000):
            raise ValueError(f"fee_bps must be in [0, 10000), got {self.fee_bps!r}")
        # Note: virtual token reserves legitimately fall *below* total
        # supply as buys drain the curve, so no supply-vs-reserve check
        # belongs here -- only positivity is a real invariant.

    # -- basic quantities -------------------------------------------------
    @property
    def fee_rate(self) -> float:
        return self.fee_bps / 10_000.0

    @property
    def invariant_k(self) -> float:
        return self.virtual_token_reserves * self.virtual_sol_reserves

    def spot_price_sol(self) -> float:
        """SOL per token at the current virtual reserves (no trade)."""
        return self.virtual_sol_reserves / self.virtual_token_reserves

    def spot_price_tokens_per_sol(self) -> float:
        """Tokens one SOL would be quoted at spot (before impact/fees)."""
        return self.virtual_token_reserves / self.virtual_sol_reserves

    # -- buys ---------------------------------------------------------------
    def tokens_received_for_sol(self, sol_amount: float) -> float:
        """Tokens out for a gross SOL amount in, fee and impact included."""
        _require_positive("sol_amount", sol_amount)
        net_sol = sol_amount * (1.0 - self.fee_rate)
        new_sol = self.virtual_sol_reserves + net_sol
        new_tokens = self.invariant_k / new_sol
        return self.virtual_token_reserves - new_tokens

    def cost_to_buy_tokens(self, token_amount: float) -> float:
        """Gross SOL needed to receive exactly ``token_amount`` tokens."""
        _require_positive("token_amount", token_amount)
        if token_amount >= self.virtual_token_reserves:
            raise ValueError("token_amount would drain the virtual reserve")
        new_tokens = self.virtual_token_reserves - token_amount
        new_sol = self.invariant_k / new_tokens
        net_sol = new_sol - self.virtual_sol_reserves
        return net_sol / (1.0 - self.fee_rate)

    def cost_to_buy_percent_of_supply(self, percent: float) -> float:
        """Gross SOL needed to buy ``percent`` % of the total supply."""
        if percent is None or not (0 < percent <= 100):
            raise ValueError(f"percent must be in (0, 100], got {percent!r}")
        return self.cost_to_buy_tokens(self.total_supply * percent / 100.0)

    def curve_after_buy_sol(self, sol_amount: float) -> "Curve":
        """A new Curve whose virtual reserves reflect this buy."""
        tokens = self.tokens_received_for_sol(sol_amount)
        net_sol = sol_amount * (1.0 - self.fee_rate)
        return Curve(
            virtual_token_reserves=self.virtual_token_reserves - tokens,
            virtual_sol_reserves=self.virtual_sol_reserves + net_sol,
            total_supply=self.total_supply,
            fee_bps=self.fee_bps,
            graduation_target_sol=self.graduation_target_sol,
        )

    # -- sells --------------------------------------------------------------
    def sell_proceeds_sol(self, token_amount: float) -> float:
        """Gross SOL received for selling ``token_amount`` tokens."""
        _require_positive("token_amount", token_amount)
        new_tokens = self.virtual_token_reserves + token_amount
        new_sol = self.invariant_k / new_tokens
        net_sol = self.virtual_sol_reserves - new_sol
        if net_sol <= 0:
            raise ValueError("sell would not return positive SOL")
        return net_sol * (1.0 - self.fee_rate)

    def curve_after_sell_tokens(self, token_amount: float) -> "Curve":
        """A new Curve whose virtual reserves reflect this sell."""
        gross = self.sell_proceeds_sol(token_amount)
        net_sol = gross / (1.0 - self.fee_rate)
        return Curve(
            virtual_token_reserves=self.virtual_token_reserves + token_amount,
            virtual_sol_reserves=self.virtual_sol_reserves - net_sol,
            total_supply=self.total_supply,
            fee_bps=self.fee_bps,
            graduation_target_sol=self.graduation_target_sol,
        )

    # -- derived quotes -------------------------------------------------------
    def market_cap_sol(self) -> float:
        """Spot market cap in SOL: spot price times total supply."""
        return self.spot_price_sol() * self.total_supply

    def effective_buy_price_sol(self, sol_amount: float) -> float:
        """Average gross SOL paid per token for a buy of ``sol_amount``."""
        return sol_amount / self.tokens_received_for_sol(sol_amount)

    def effective_sell_price_sol(self, token_amount: float) -> float:
        """Average gross SOL received per token for this sell size."""
        return self.sell_proceeds_sol(token_amount) / token_amount

    def round_trip_loss_pct(self, sol_amount: float) -> float:
        """% lost buying with ``sol_amount`` then selling it all back.

        The sell is priced on the post-buy curve state, so at zero fee
        the loss is exactly 0 and otherwise it is the fee, twice.
        """
        tokens = self.tokens_received_for_sol(sol_amount)
        back = self.curve_after_buy_sol(sol_amount).sell_proceeds_sol(tokens)
        return (sol_amount - back) / sol_amount * 100.0

    def sol_to_reach_spot_price(self, target_price_sol: float) -> float:
        """Gross SOL of buys needed to push the spot price to a target.

        Only buys move the price up in this model, so the target must
        sit strictly above the current spot price; anything else raises.
        Derivation: at the target, vs' * vt' = k and vs' / vt' = target,
        so vs' = sqrt(k * target) and the net SOL in is vs' - vs.
        """
        _require_positive("target_price_sol", target_price_sol)
        if target_price_sol <= self.spot_price_sol():
            raise ValueError(
                f"target_price_sol must exceed spot "
                f"{self.spot_price_sol()!r}, got {target_price_sol!r}")
        new_sol = math.sqrt(self.invariant_k * target_price_sol)
        net_sol = new_sol - self.virtual_sol_reserves
        return net_sol / (1.0 - self.fee_rate)

    def simulate_buys(self, sol_amounts) -> tuple:
        """Apply buys in sequence; returns (total tokens, final curve).

        Each buy moves the curve for the next one.  An empty sequence
        is a no-op: (0.0, this curve).  Any non-positive leg raises.
        """
        if sol_amounts is None:
            raise ValueError("sol_amounts must be a sequence, got None")
        total = 0.0
        curve = self
        for amount in sol_amounts:
            total += curve.tokens_received_for_sol(amount)
            curve = curve.curve_after_buy_sol(amount)
        return total, curve

    def simulate_sells(self, token_amounts) -> tuple:
        """Apply sells in sequence; returns (total SOL, final curve)."""
        if token_amounts is None:
            raise ValueError("token_amounts must be a sequence, got None")
        total = 0.0
        curve = self
        for amount in token_amounts:
            total += curve.sell_proceeds_sol(amount)
            curve = curve.curve_after_sell_tokens(amount)
        return total, curve

    # -- impact / slippage ----------------------------------------------------
    def _avg_fill_price_sol(self, token_amount: float, gross_sol: float) -> float:
        return gross_sol / token_amount

    def price_impact_of_buy_tokens(self, token_amount: float) -> float:
        """% the average buy fill price sits above spot. Always >= 0."""
        gross = self.cost_to_buy_tokens(token_amount)
        avg = self._avg_fill_price_sol(token_amount, gross)
        return (avg / self.spot_price_sol() - 1.0) * 100.0

    def price_impact_of_sell_tokens(self, token_amount: float) -> float:
        """% the average sell fill price sits below spot. Always >= 0."""
        gross = self.sell_proceeds_sol(token_amount)
        avg = self._avg_fill_price_sol(token_amount, gross)
        return (1.0 - avg / self.spot_price_sol()) * 100.0

    def slippage_buy_tokens(self, token_amount: float) -> float:
        """% extra SOL paid vs the naive spot quote for the same tokens.

        Signed: positive means the fill was worse than the spot quote
        (the normal case on a bonding curve); it is exactly the buy
        price impact, exposed under the name traders use for it.
        """
        _require_positive("token_amount", token_amount)
        expected = token_amount * self.spot_price_sol()
        actual = self.cost_to_buy_tokens(token_amount)
        return (actual - expected) / expected * 100.0

    def slippage_sell_tokens(self, token_amount: float) -> float:
        """% SOL lost vs the naive spot quote. Positive = worse fill."""
        _require_positive("token_amount", token_amount)
        expected = token_amount * self.spot_price_sol()
        actual = self.sell_proceeds_sol(token_amount)
        return (expected - actual) / expected * 100.0

    # -- graduation -----------------------------------------------------------
    def completion_pct(self, real_sol_collected: float) -> float:
        """% of the graduation target collected, in [0, 100]."""
        if real_sol_collected is None or real_sol_collected < 0:
            raise ValueError(
                f"real_sol_collected must be >= 0, got {real_sol_collected!r}"
            )
        return min(100.0, real_sol_collected / self.graduation_target_sol * 100.0)

    def is_graduated(self, real_sol_collected: float) -> bool:
        """True iff the real SOL collected has reached the target."""
        if real_sol_collected is None or real_sol_collected < 0:
            raise ValueError(
                f"real_sol_collected must be >= 0, got {real_sol_collected!r}"
            )
        return real_sol_collected >= self.graduation_target_sol

    def remaining_sol_to_graduation(self, real_sol_collected: float) -> float:
        """SOL still needed to reach the target; 0 once graduated."""
        if real_sol_collected is None or real_sol_collected < 0:
            raise ValueError(
                f"real_sol_collected must be >= 0, got {real_sol_collected!r}"
            )
        return max(0.0, self.graduation_target_sol - real_sol_collected)
