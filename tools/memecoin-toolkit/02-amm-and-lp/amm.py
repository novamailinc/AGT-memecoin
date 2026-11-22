"""Constant-product AMM + LP lock checks (synthetic lab).

Pure stdlib, no network.  Models a Raydium / PumpSwap style x*y=k pool:

  amount_out = reserve_out * amount_in_after_fee
               / (reserve_in + amount_in_after_fee)

Two different "how bad is my fill" numbers are kept separate on purpose:
  * price_impact_pct -- curve movement only (fee excluded): how far the
    trade itself pushes the price;
  * slippage_pct -- spot quote vs actual fill, fee included: what the
    trader actually loses against the price they saw.

Fail-closed: non-positive reserves/amounts raise ValueError, a swap
whose output is below its ``min_out`` raises InsufficientOutputError,
and LP verification returns "unverifiable" (never "locked") when any
input it needs is missing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


class InsufficientOutputError(ValueError):
    """Raised when a swap's output is below the caller's min_out guard."""


def _require_positive(name: str, value: float) -> float:
    if value is None or not (value > 0):
        raise ValueError(f"{name} must be positive, got {value!r}")
    return float(value)


def get_amount_out(amount_in: float, reserve_in: float,
                   reserve_out: float, fee_bps: int = 25) -> float:
    """Output for ``amount_in`` given the reserves on each side."""
    _require_positive("amount_in", amount_in)
    _require_positive("reserve_in", reserve_in)
    _require_positive("reserve_out", reserve_out)
    if not (0 <= fee_bps < 10_000):
        raise ValueError(f"fee_bps must be in [0, 10000), got {fee_bps!r}")
    in_after_fee = amount_in * (1.0 - fee_bps / 10_000.0)
    return reserve_out * in_after_fee / (reserve_in + in_after_fee)


def get_amount_in(amount_out: float, reserve_in: float,
                  reserve_out: float, fee_bps: int = 25) -> float:
    """Exact input needed to receive exactly ``amount_out``.

    The algebraic inverse of get_amount_out:
    in_after_fee = reserve_in * amount_out / (reserve_out - amount_out),
    grossed back up by the fee.  Fail-closed: an output at or beyond
    the whole reserve can never be bought, so it raises.
    """
    _require_positive("amount_out", amount_out)
    _require_positive("reserve_in", reserve_in)
    _require_positive("reserve_out", reserve_out)
    if not (0 <= fee_bps < 10_000):
        raise ValueError(f"fee_bps must be in [0, 10000), got {fee_bps!r}")
    if amount_out >= reserve_out:
        raise ValueError("amount_out would drain reserve_out")
    in_after_fee = reserve_in * amount_out / (reserve_out - amount_out)
    return in_after_fee / (1.0 - fee_bps / 10_000.0)


def chained_quote(amount_in: float, hops) -> float:
    """Quote a multi-hop route: the output of each hop feeds the next.

    ``hops`` is a sequence of (reserve_in, reserve_out, fee_bps)
    triples, one per pool on the route.  Each hop charges its own fee
    and impact, so a route always fills worse than any single leg's
    spot quote suggests.  Fail-closed on an empty route.
    """
    _require_positive("amount_in", amount_in)
    if not hops:
        raise ValueError("hops must be a non-empty sequence of pools")
    amount = float(amount_in)
    for reserve_in, reserve_out, fee_bps in hops:
        amount = get_amount_out(amount, reserve_in, reserve_out, fee_bps)
    return amount


def _curve_only_out(amount_in: float, reserve_in: float,
                    reserve_out: float) -> float:
    """Output with no fee -- isolates pure curve (price) impact."""
    return reserve_out * amount_in / (reserve_in + amount_in)


@dataclass(frozen=True)
class Pool:
    """A two-sided constant-product pool. ``x`` is e.g. SOL, ``y`` token."""

    reserve_x: float
    reserve_y: float
    fee_bps: int = 25

    def __post_init__(self) -> None:
        _require_positive("reserve_x", self.reserve_x)
        _require_positive("reserve_y", self.reserve_y)
        if not (0 <= self.fee_bps < 10_000):
            raise ValueError(f"fee_bps must be in [0, 10000), got {self.fee_bps!r}")

    @property
    def invariant_k(self) -> float:
        return self.reserve_x * self.reserve_y

    def spot_price_y_per_x(self) -> float:
        return self.reserve_y / self.reserve_x

    def spot_price_x_per_y(self) -> float:
        return self.reserve_x / self.reserve_y

    # -- quotes (no state change) ------------------------------------------
    def quote_x_for_y(self, amount_x_in: float) -> float:
        """Tokens (y) out for x in."""
        return get_amount_out(amount_x_in, self.reserve_x, self.reserve_y,
                              self.fee_bps)

    def quote_y_for_x(self, amount_y_in: float) -> float:
        """X out for tokens (y) in."""
        return get_amount_out(amount_y_in, self.reserve_y, self.reserve_x,
                              self.fee_bps)

    def quote_x_in_for_y_out(self, amount_y_out: float) -> float:
        """Exact x in needed to receive exactly ``amount_y_out`` of y."""
        return get_amount_in(amount_y_out, self.reserve_x, self.reserve_y,
                             self.fee_bps)

    def quote_y_in_for_x_out(self, amount_x_out: float) -> float:
        """Exact y in needed to receive exactly ``amount_x_out`` of x."""
        return get_amount_in(amount_x_out, self.reserve_y, self.reserve_x,
                             self.fee_bps)

    # -- impact / slippage ----------------------------------------------------
    def price_impact_pct(self, amount_x_in: float) -> float:
        """% worse than spot from curve movement alone (fee excluded)."""
        _require_positive("amount_x_in", amount_x_in)
        spot_expected = amount_x_in * self.spot_price_y_per_x()
        curve_out = _curve_only_out(amount_x_in, self.reserve_x, self.reserve_y)
        return (spot_expected - curve_out) / spot_expected * 100.0

    def slippage_pct(self, amount_x_in: float) -> float:
        """% worse than the spot quote, fee included (what a buyer feels)."""
        _require_positive("amount_x_in", amount_x_in)
        spot_expected = amount_x_in * self.spot_price_y_per_x()
        actual = self.quote_x_for_y(amount_x_in)
        return (spot_expected - actual) / spot_expected * 100.0

    def price_impact_pct_y_in(self, amount_y_in: float) -> float:
        """% worse than spot from curve movement alone, selling y for x."""
        _require_positive("amount_y_in", amount_y_in)
        spot_expected = amount_y_in * self.spot_price_x_per_y()
        curve_out = _curve_only_out(amount_y_in, self.reserve_y,
                                    self.reserve_x)
        return (spot_expected - curve_out) / spot_expected * 100.0

    def slippage_pct_y_in(self, amount_y_in: float) -> float:
        """% worse than the spot quote, fee included, selling y for x."""
        _require_positive("amount_y_in", amount_y_in)
        spot_expected = amount_y_in * self.spot_price_x_per_y()
        actual = self.quote_y_for_x(amount_y_in)
        return (spot_expected - actual) / spot_expected * 100.0

    # -- executing a swap (returns a new Pool) --------------------------------
    def swap_x_for_y(self, amount_x_in: float,
                     min_out: float | None = None) -> tuple[float, "Pool"]:
        """Swap x -> y. Raises InsufficientOutputError if below min_out."""
        amount_out = self.quote_x_for_y(amount_x_in)
        ensure_min_out(amount_out, min_out)
        new_pool = Pool(self.reserve_x + amount_x_in,
                        self.reserve_y - amount_out, self.fee_bps)
        return amount_out, new_pool

    def swap_y_for_x(self, amount_y_in: float,
                     min_out: float | None = None) -> tuple[float, "Pool"]:
        """Swap y -> x. Raises InsufficientOutputError if below min_out."""
        amount_out = self.quote_y_for_x(amount_y_in)
        ensure_min_out(amount_out, min_out)
        new_pool = Pool(self.reserve_x - amount_out,
                        self.reserve_y + amount_y_in, self.fee_bps)
        return amount_out, new_pool


def ensure_min_out(amount_out: float, min_out: float | None) -> float:
    """The min-out guard: refuse (raise) when the fill is too small."""
    if min_out is None:
        return amount_out
    if min_out < 0:
        raise ValueError(f"min_out must be >= 0, got {min_out!r}")
    if amount_out < min_out:
        raise InsufficientOutputError(
            f"output {amount_out:.6f} below min_out {min_out:.6f}; swap refused")
    return amount_out


# ---------------------------------------------------------------------------
# LP burn / lock verification (synthetic fixtures only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LPVerdict:
    verdict: str            # "locked" | "unlocked" | "unverifiable"
    passed: bool            # True only for "locked"
    secured_pct: float | None   # burned + locked, % of total supply
    burned_pct: float | None
    locked_pct: float | None
    deployer_pct: float | None  # held by deployer: NOT counted as secured
    reasons: list = field(default_factory=list)


def verify_lp_lock(lp_total_supply: float | None,
                   burn_balance: float | None,
                   locker_balances: list | dict | None,
                   deployer_balance: float | None = None,
                   threshold_pct: float = 90.0) -> LPVerdict:
    """Verdict on whether LP tokens are burned/locked past the threshold.

    Burned and locker-held LP count as secured; LP sitting in the
    deployer wallet does not -- the deployer can pull it at any time.
    Missing data is fail-closed: "unverifiable", passed=False.
    """
    if threshold_pct is None or not (0 < threshold_pct <= 100):
        raise ValueError(f"threshold_pct must be in (0, 100], got {threshold_pct!r}")
    missing = []
    if lp_total_supply is None:
        missing.append("lp_total_supply")
    if burn_balance is None:
        missing.append("burn_balance")
    if locker_balances is None:
        missing.append("locker_balances")
    if missing:
        return LPVerdict("unverifiable", False, None, None, None, None,
                         [f"missing data: {', '.join(missing)}"])
    if lp_total_supply <= 0:
        return LPVerdict("unverifiable", False, None, None, None, None,
                         ["lp_total_supply is not positive"])
    for name, value in (("burn_balance", burn_balance),
                        ("deployer_balance", deployer_balance or 0.0)):
        if value < 0:
            raise ValueError(f"{name} must be >= 0, got {value!r}")
    if isinstance(locker_balances, dict):
        locked_values = list(locker_balances.values())
    else:
        locked_values = list(locker_balances)
    if any(v is None or v < 0 for v in locked_values):
        raise ValueError("locker balances must be non-negative numbers")

    locked_total = float(sum(locked_values))
    secured = float(burn_balance) + locked_total
    deployer = float(deployer_balance or 0.0)
    # Each balance alone, and all of them together, must fit inside the
    # total supply -- otherwise the fixture contradicts itself.
    if secured > lp_total_supply or deployer > lp_total_supply \
            or secured + deployer > lp_total_supply:
        return LPVerdict("unverifiable", False, None, None, None, None,
                         ["balances exceed total supply: inconsistent fixture"])
    secured_pct = secured / lp_total_supply * 100.0
    burned_pct = burn_balance / lp_total_supply * 100.0
    locked_pct = locked_total / lp_total_supply * 100.0
    deployer_pct = (deployer_balance or 0.0) / lp_total_supply * 100.0
    if secured_pct >= threshold_pct:
        return LPVerdict("locked", True, secured_pct, burned_pct, locked_pct,
                         deployer_pct,
                         [f"{secured_pct:.1f}% burned+locked >= {threshold_pct:.0f}%"])
    return LPVerdict("unlocked", False, secured_pct, burned_pct, locked_pct,
                     deployer_pct,
                     [f"only {secured_pct:.1f}% burned+locked "
                      f"< {threshold_pct:.0f}% threshold"])


# ---------------------------------------------------------------------------
# LP share math + impermanent loss
# ---------------------------------------------------------------------------

def lp_share_pct(lp_balance: float, lp_total_supply: float) -> float:
    """An LP balance as a % of the total LP supply.

    Fail-closed: a balance above the supply is a contradiction, not a
    share over 100%.
    """
    _require_positive("lp_total_supply", lp_total_supply)
    if lp_balance is None or lp_balance < 0:
        raise ValueError(f"lp_balance must be >= 0, got {lp_balance!r}")
    if lp_balance > lp_total_supply:
        raise ValueError("lp_balance exceeds lp_total_supply")
    return lp_balance / lp_total_supply * 100.0


def pro_rata_amounts(lp_balance: float, lp_total_supply: float,
                     reserve_x: float, reserve_y: float) -> tuple:
    """(x, y) a holder of ``lp_balance`` could redeem from the reserves."""
    _require_positive("reserve_x", reserve_x)
    _require_positive("reserve_y", reserve_y)
    share = lp_share_pct(lp_balance, lp_total_supply) / 100.0
    return share * reserve_x, share * reserve_y


def impermanent_loss_pct(price_ratio: float) -> float:
    """Impermanent loss % for a constant-product LP vs simply holding.

    ``price_ratio`` is new_price / old_price for the pair.  The loss is
    2*sqrt(r)/(1+r) - 1: always <= 0, exactly 0 at r == 1, and
    symmetric under r -> 1/r (a 4x rise and a 4x fall cost the same).
    """
    _require_positive("price_ratio", price_ratio)
    return (2.0 * math.sqrt(price_ratio) / (1.0 + price_ratio) - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Sandwich cost estimate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SandwichResult:
    victim_out_normal: float
    victim_out_sandwiched: float
    victim_extra_loss_pct: float
    attacker_profit_x: float    # in x terms; may be <= 0 if attack unprofitable


def estimate_sandwich(pool: Pool, victim_amount_x_in: float,
                      attacker_amount_x_in: float) -> SandwichResult:
    """Simulate front-run buy -> victim buy -> attacker sell-all.

    Sequence runs on successive pool states (each swap moves the price).
    The victim's extra loss is measured against the same victim trade
    executed alone on the untouched pool.
    """
    _require_positive("victim_amount_x_in", victim_amount_x_in)
    _require_positive("attacker_amount_x_in", attacker_amount_x_in)
    victim_normal = pool.quote_x_for_y(victim_amount_x_in)

    attacker_tokens, pool1 = pool.swap_x_for_y(attacker_amount_x_in)
    victim_sandwiched, pool2 = pool1.swap_x_for_y(victim_amount_x_in)
    attacker_back, _pool3 = pool2.swap_y_for_x(attacker_tokens)

    loss_pct = (victim_normal - victim_sandwiched) / victim_normal * 100.0
    return SandwichResult(
        victim_out_normal=victim_normal,
        victim_out_sandwiched=victim_sandwiched,
        victim_extra_loss_pct=loss_pct,
        attacker_profit_x=attacker_back - attacker_amount_x_in,
    )
