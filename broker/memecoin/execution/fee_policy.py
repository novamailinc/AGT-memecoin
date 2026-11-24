"""Priority-fee policy + Jito-tip economics (Phase 2).

PLAIN TRUTH, READ FIRST: a 0.001-0.01 SOL Jito tip eats a $1 trade.
A $1 stake is ~0.01 SOL (at $100/SOL), so a 0.001 SOL tip is 10% of
the stake and a 0.01 SOL tip is 100% of it — the tip alone is several
times the whole expected edge of a $1 memecoin trade. Bundles only
make sense at stakes where the tip is a few percent or less. What
stake that is, and whether bundles land at all, is decided by the
live shakedown test (buy ~$0.50, sell it through one ladder leg),
NOT by this build. This module only does the math honestly and
refuses (SKIP, never pay) when the math says no.

What it does (pure logic — no network, no keys, no signing):
  * PriorityFeePolicy: picks a priority fee from recent
    prioritization-fee data (an injectable callable; by default it
    reads RPC getRecentPrioritizationFees through a passed rpc
    object — the policy NEVER makes a live RPC call itself) and
    sorts the result into tiers: calm / normal / hot.
  * Percentile fallback tiers (port of the solana-agent-kit
    feeTiers shape, min 0.01 / mid 0.5 / max 0.95): fee_tiers()
    returns the 1st / 50th / 95th percentiles of the same
    getRecentPrioritizationFees list. That list is the ONLY fee
    data the policy ever uses — there is no getPriorityFeeEstimate
    call anywhere in the policy path (Helius or otherwise), so the
    estimate endpoint is dead by construction, not by config.
    The fees list is cached ~10s so repeated fee decisions in one
    evaluation burst do not re-fetch.
  * Simulate-then-pad compute-unit sizing: safe_compute_units()
    returns max(estimated + 100_000, estimated * 1.2) from a
    simulation's unitsConsumed, else the 200_000 default. The
    executor wires it to the simulation it already runs (see
    MainnetExecutor.sized_compute_units): a missing or failed
    simulation falls back to the default and NEVER fails a trade
    on a missing estimate.
  * Two hard caps, both SKIP-never-pay: the existing 5%-of-stake cap
    (mirrors MainnetExecutor.PRIORITY_FEE_MAX_FRAC_OF_STAKE) plus an
    absolute lamports cap. A fee over either is a skip.
  * Jito-tip economics check: given a stake in SOL, an explicit
    verdict on whether a tip in the 0.001-0.01 SOL range eats the
    trade, with the math shown (tip_sol, stake_sol, tip_frac).

Phase 1 is UNPROVEN (one post-fix close, Helius quota spent, zero
new entries) and the user waived the Phase 1 deep-test gate to order
this build. Nothing here claims Phase 1 is proven or clean.

v1 audit (2026-11-17, Batch 3 — pumpfun-bonkfun-bot gotchas):
the fee this policy returns is TOTAL lamports, and the executor
passes it to Jupiter as prioritizationFeeLamports, which Jupiter
defines as total lamports — the policy's 5% cap is enforced in
the same units it is computed in. Our send path never hand-builds
a v1 message (Jupiter builds v0 swap txs; the Jito tip tx is
legacy), so the v1 traps cannot bite today: (a) a v1 Message.config
takes the fee as a TOTAL, not micro-lamports/CU — the solders
docstring saying micro-lamports is wrong, and writing a total
into setComputeUnitPrice (or a per-CU price into a v1 config)
misprices the fee by the CU limit; (b) a hand-built v1 tx must
state its loaded-accounts data-size limit or the limit is 0 and
the tx is rejected silently — v0/legacy txs have no such field.
Any future hand-built v1 path must write the TOTAL into the
config and state the data-size limit; tx.compute_budget_summary
reads a built tx's budget back so the model stays checkable.
Venue CU budgets live below (VENUE_CU_BUDGETS).
"""

import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("broker.memecoin.execution.fee_policy")

LAMPORTS_PER_SOL = 1_000_000_000

# Hard caps. A fee over either is a SKIP, never a paid fee.
# MAX_FEE_FRAC_OF_STAKE mirrors the executor's 5% cap — keep the two
# in sync; the policy is the stricter-or-equal gate, never looser.
MAX_FEE_FRAC_OF_STAKE = 0.05
DEFAULT_ABSOLUTE_FEE_CAP_LAMPORTS = 1_000_000  # 0.001 SOL

# Base priority fee when no fee data is available (mirrors the
# executor's DEFAULT_PRIORITY_FEE_LAMPORTS).
BASE_FEE_LAMPORTS = 10_000

# Tier boundaries on the chosen fee (lamports). Calm is the normal
# case on mainnet; hot is a contested window where paying more is
# still capped by the hard caps above, never uncapped.
TIER_CALM = "calm"
TIER_NORMAL = "normal"
TIER_HOT = "hot"
TIER_CALM_MAX_LAMPORTS = 10_000
TIER_NORMAL_MAX_LAMPORTS = 100_000

# Percentile fallback tiers (port of the solana-agent-kit feeTiers
# shape: min 0.01 / mid 0.5 / max 0.95). fee_tiers() reports the
# 1st / 50th / 95th percentiles of the getRecentPrioritizationFees
# list — the fallback that replaces any getPriorityFeeEstimate call.
FEE_TIERS_PCTS = {"min": 0.01, "mid": 0.5, "max": 0.95}

# Fees-list cache TTL (seconds). Repeated fee decisions inside one
# evaluation burst reuse the fetched list instead of re-calling
# getRecentPrioritizationFees every time.
FEES_CACHE_TTL_SECONDS = 10.0

# Compute-unit sizing (simulate-then-pad). DEFAULT is used whenever
# no simulation estimate exists; the pad is +100k units or x1.2,
# whichever is larger; MAX is Solana's per-transaction limit — a
# padded value above it is capped, never emitted as an invalid
# instruction.
DEFAULT_COMPUTE_UNITS = 200_000
COMPUTE_UNIT_PAD = 100_000
COMPUTE_UNIT_PAD_FACTOR = 1.2
MAX_COMPUTE_UNITS = 1_400_000

# Venue CU budgets (2026-11-17, Batch 3 v1 audit — from the
# pumpfun-bonkfun-bot reference: pump.fun v2 buy 180k / sell
# 120k; PumpSwap buy 250k / sell 200k; LaunchLab/CPMM 150k /
# 150k). These are the CONSUMPTION the venue's programs
# typically need — a tx's CU limit must cover them or it fails
# out-of-CU. They are the sanity check for a simulation's
# unitsConsumed (a sim far below its venue's budget likely
# failed early — check the sim err first) and the input the
# simulation harness compares headroom against. Our send path
# never hand-builds venue txs (Jupiter builds the swap), so
# these budgets bind the HARNESS and any future hand-built
# path, not the live executor. Retune from simulation
# unitsConsumed after program upgrades.
VENUE_CU_BUDGETS = {
    "pumpfun_v2": {"buy": 180_000, "sell": 120_000},
    "pumpswap": {"buy": 250_000, "sell": 200_000},
    "launchlab_cpmm": {"buy": 150_000, "sell": 150_000},
}
# A sim consuming under this fraction of its venue budget is
# suspiciously low (likely a partial/failed sim, not a cheap
# tx) — the harness flags it instead of trusting the headroom.
VENUE_CU_SUSPICIOUS_FRAC = 0.5


def venue_cu_budget(venue, side):
    """CU budget for (venue, side), or None when unknown.

    venue in {"pumpfun_v2", "pumpswap", "launchlab_cpmm"};
    side in {"buy", "sell"}. Case-insensitive; never raises."""
    try:
        table = VENUE_CU_BUDGETS.get(str(venue).lower())
        if not isinstance(table, dict):
            return None
        budget = table.get(str(side).lower())
        return int(budget) if budget is not None else None
    except Exception:
        return None


def check_cu_against_venue(venue, side, units_consumed):
    """Compare a simulation's unitsConsumed to its venue budget.

    Returns {"venue", "side", "budget", "units_consumed",
    "headroom_pct", "over_budget", "suspicious_low"}.
    headroom_pct is (budget - consumed) / budget in percent
    (negative when over). over_budget means the sim consumed
    more than the venue typically needs — the tx needs a limit
    above the budget (fine up to the 1.4M chain cap, but the
    table may be stale or the tx does extra work).
    suspicious_low means the sim consumed under half its budget
    — likely a partial sim; check the sim err before trusting
    the headroom. Unknown venue/side or unusable units give
    budget None and headroom None. Never raises."""
    budget = venue_cu_budget(venue, side)
    out = {"venue": venue, "side": side, "budget": budget,
           "units_consumed": None, "headroom_pct": None,
           "over_budget": False, "suspicious_low": False}
    try:
        if isinstance(units_consumed, bool):
            return out
        consumed = int(units_consumed)
    except (TypeError, ValueError):
        return out
    if consumed <= 0:
        return out
    out["units_consumed"] = consumed
    if budget is None or budget <= 0:
        return out
    out["headroom_pct"] = round(
        (budget - consumed) * 100.0 / budget, 1)
    out["over_budget"] = consumed > budget
    out["suspicious_low"] = consumed < \
        VENUE_CU_SUSPICIOUS_FRAC * budget
    return out
# uneconomic — same 5% bar as the priority-fee cap, because a tip
# is a fee by another name (paid only if the bundle lands, but
# still paid out of the trade's edge).
TIP_UNECONOMIC_FRAC = 0.05
# Default tip offered in a bundle: the low end of the documented
# Jito tip range (0.001-0.01 SOL). Even this low end eats a $1
# trade — see the module docstring.
DEFAULT_TIP_LAMPORTS = 1_000_000  # 0.001 SOL
TIP_RANGE_SOL = (0.001, 0.01)


def _as_lamports(sample: Any) -> Optional[int]:
    """One fee sample -> lamports, or None when unparseable.

    Accepts a bare int (lamports) or an RPC getRecentPrioritizationFees
    row {"slot": ..., "prioritizationFee": N}."""
    if isinstance(sample, bool):
        return None
    if isinstance(sample, int):
        return sample if sample >= 0 else None
    if isinstance(sample, dict):
        try:
            v = int(sample.get("prioritizationFee"))
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None
    return None


def _percentile(sorted_values: List[int], pct: float) -> int:
    """Nearest-rank percentile of an ascending list (pct in 0..1)."""
    if not sorted_values:
        return BASE_FEE_LAMPORTS
    rank = max(1, int(math.ceil(pct * len(sorted_values))))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def safe_compute_units(estimated_cu=None,
                       default: int = DEFAULT_COMPUTE_UNITS) -> int:
    """Simulate-then-pad compute-unit sizing (pure math, no I/O).

    With an estimate: max(estimated + 100_000, estimated * 1.2),
    capped at Solana's 1_400_000 per-transaction limit. Without one
    (None, <= 0, unparseable): the default (200_000). The default
    is also the fallback when the caller's default is unusable.
    Never raises — a missing estimate is a default, never an error.
    """
    try:
        fallback = int(default)
    except (TypeError, ValueError):
        fallback = DEFAULT_COMPUTE_UNITS
    if isinstance(default, bool) or fallback <= 0:
        fallback = DEFAULT_COMPUTE_UNITS
    if estimated_cu is None or isinstance(estimated_cu, bool):
        return fallback
    try:
        est = int(estimated_cu)
    except (TypeError, ValueError):
        return fallback
    if est <= 0:
        return fallback
    padded = max(est + COMPUTE_UNIT_PAD,
                 math.ceil(est * COMPUTE_UNIT_PAD_FACTOR))
    return min(padded, MAX_COMPUTE_UNITS)


def units_consumed_from_sim(sim: Any) -> Optional[int]:
    """unitsConsumed out of a simulateTransaction result, or None.

    Accepts the real RPC shape {"value": {"unitsConsumed": N}} and
    the flat shape {"unitsConsumed": N}. Returns None when there is
    no simulation (None / non-dict / no units), when the simulation
    itself errored (top-level "err" or value-level "err" — units
    from a failed sim are untrustworthy), or when the units value
    is <= 0 / unparseable. Callers treat None as "no estimate".
    """
    if not isinstance(sim, dict):
        return None
    if sim.get("err"):
        return None
    value = sim.get("value")
    if isinstance(value, dict) and value.get("err"):
        return None
    raw = None
    if isinstance(value, dict):
        raw = value.get("unitsConsumed")
    if raw is None:
        raw = sim.get("unitsConsumed")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        units = int(raw)
    except (TypeError, ValueError):
        return None
    return units if units > 0 else None


def sized_compute_units_from_sim(
        sim: Any,
        default: int = DEFAULT_COMPUTE_UNITS) -> Dict[str, Any]:
    """Simulate-then-pad sizing from an already-run simulation.

    Returns {"compute_units", "source", "units_consumed"} where
    source is "simulated" when the sim carried unitsConsumed and
    "default" otherwise (no sim, failed sim, unparseable units).
    Never raises and never fails a trade on a missing estimate —
    the default is the answer in every no-estimate case.
    """
    consumed = units_consumed_from_sim(sim)
    if consumed is None:
        return {"compute_units": safe_compute_units(None, default),
                "source": "default", "units_consumed": None}
    return {"compute_units": safe_compute_units(consumed, default),
            "source": "simulated", "units_consumed": consumed}


def jito_tip_verdict(stake_sol: float, tip_sol: float,
                     sol_price_usd: Optional[float] = None,
                     uneconomic_frac: float = TIP_UNECONOMIC_FRAC
                     ) -> Dict[str, Any]:
    """Is this Jito tip economic for this stake? Pure math, no I/O.

    Returns a dict with the verdict and the math shown:
    tip_sol, stake_sol, tip_frac (tip/stake), and USD values when a
    SOL price is given. Verdict is "uneconomic" when the tip is more
    than uneconomic_frac (default 5%) of the stake; "economic"
    otherwise. A zero tip is trivially economic (no tip paid).

    Example that must come back uneconomic: stake $1 (~0.01 SOL at
    $100/SOL) with a 0.001 SOL tip -> tip_frac 0.10 (10%).
    """
    if stake_sol is None or float(stake_sol) <= 0:
        raise ValueError("jito tip verdict needs a positive stake_sol")
    if tip_sol is None or float(tip_sol) < 0:
        raise ValueError("jito tip verdict needs tip_sol >= 0")
    stake = float(stake_sol)
    tip = float(tip_sol)
    frac = (tip / stake) if stake > 0 else float("inf")
    economic = frac <= float(uneconomic_frac)
    out: Dict[str, Any] = {
        "verdict": "economic" if economic else "uneconomic",
        "reason": None if economic else "tip_over_frac_of_stake",
        "tip_sol": tip,
        "stake_sol": stake,
        "tip_frac": frac,
        "tip_lamports": int(round(tip * LAMPORTS_PER_SOL)),
        "stake_lamports": int(round(stake * LAMPORTS_PER_SOL)),
        "uneconomic_frac": float(uneconomic_frac),
    }
    if sol_price_usd is not None and float(sol_price_usd) > 0:
        out["tip_usd"] = tip * float(sol_price_usd)
        out["stake_usd"] = stake * float(sol_price_usd)
    return out


class PriorityFeePolicy:
    """Priority-fee tiers + caps, fed by injectable fee data.

    fee_source: callable returning recent fee samples — a list of
    ints (lamports) or getRecentPrioritizationFees rows. If omitted
    and rpc is given, the source calls
    rpc.call("getRecentPrioritizationFees", []) through that object;
    the policy itself never opens a connection and never calls any
    fee-estimate endpoint (no getPriorityFeeEstimate anywhere in
    this module — the percentile tiers below are the fallback).
    If neither is given, the policy falls back to the base fee
    (calm tier) — it never invents fee data.

    The fetched fees list is cached for fee_cache_ttl_seconds
    (default 10s) so an evaluation burst that prices many trades
    does not re-fetch per decision; sample_fees(force_refresh=True)
    bypasses the cache. clock is an injectable monotonic clock
    (default time.monotonic), used only for the cache TTL.
    """

    def __init__(self, fee_source: Optional[Callable[[], Any]] = None,
                 rpc=None,
                 absolute_cap_lamports: int =
                 DEFAULT_ABSOLUTE_FEE_CAP_LAMPORTS,
                 max_frac_of_stake: float = MAX_FEE_FRAC_OF_STAKE,
                 base_fee_lamports: int = BASE_FEE_LAMPORTS,
                 default_tip_lamports: int = DEFAULT_TIP_LAMPORTS,
                 sol_price_usd: Optional[float] = None,
                 fee_cache_ttl_seconds: float = FEES_CACHE_TTL_SECONDS,
                 clock: Optional[Callable[[], float]] = None):
        if fee_source is not None:
            self._fee_source = fee_source
        elif rpc is not None:
            self._fee_source = self._rpc_fee_source(rpc)
        else:
            self._fee_source = lambda: []
        self.absolute_cap_lamports = int(absolute_cap_lamports)
        self.max_frac_of_stake = float(max_frac_of_stake)
        self.base_fee_lamports = int(base_fee_lamports)
        self.default_tip_lamports = int(default_tip_lamports)
        self.sol_price_usd = sol_price_usd
        if fee_cache_ttl_seconds is None:
            fee_cache_ttl_seconds = 0.0
        self.fee_cache_ttl_seconds = float(fee_cache_ttl_seconds)
        self._clock = clock or time.monotonic
        self._fees_cache: Optional[List[int]] = None
        self._fees_cache_ts: float = 0.0

    def invalidate_fee_cache(self) -> None:
        """Drop the cached fees list; the next sample re-fetches."""
        self._fees_cache = None
        self._fees_cache_ts = 0.0

    @staticmethod
    def _rpc_fee_source(rpc):
        def _source():
            try:
                return rpc.call("getRecentPrioritizationFees", []) or []
            except Exception:
                return []
        return _source

    def sample_fees(self, force_refresh: bool = False) -> List[int]:
        """Recent prioritization fees in lamports (newest data the
        source has; unparseable rows dropped, capped at 150).

        The fetched list is cached for fee_cache_ttl_seconds: calls
        inside the window return a copy of the cached list without
        touching the source. A source failure returns [] and is NOT
        cached, so the next call retries instead of serving a
        poisoned empty cache."""
        if not force_refresh and self._fees_cache is not None:
            now = self._clock()
            if now - self._fees_cache_ts < self.fee_cache_ttl_seconds:
                return list(self._fees_cache)
        try:
            raw = self._fee_source() or []
        except Exception:
            return []
        if isinstance(raw, dict):
            raw = raw.get("fees") or raw.get("result") or []
        fees = []
        for sample in list(raw)[-150:]:
            v = _as_lamports(sample)
            if v is not None:
                fees.append(v)
        self._fees_cache = list(fees)
        self._fees_cache_ts = self._clock()
        return fees

    def fee_tiers(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Percentile fallback tiers over the recent-fees list.

        Port of the solana-agent-kit feeTiers shape (min 0.01 / mid
        0.5 / max 0.95): min is the 1st percentile, mid the 50th,
        max the 95th (nearest-rank, same helper choose_fee uses).
        This is the fallback that kills the getPriorityFeeEstimate
        call — tiers come from getRecentPrioritizationFees only.
        Empty data falls back to the base fee on all three tiers
        (samples 0), mirroring choose_fee's base-fee fallback.
        """
        fees = sorted(self.sample_fees(force_refresh=force_refresh))
        if not fees:
            base = self.base_fee_lamports
            return {"min": base, "mid": base, "max": base,
                    "samples": 0}
        return {
            "min": _percentile(fees, FEE_TIERS_PCTS["min"]),
            "mid": _percentile(fees, FEE_TIERS_PCTS["mid"]),
            "max": _percentile(fees, FEE_TIERS_PCTS["max"]),
            "samples": len(fees),
        }

    @staticmethod
    def tier_for_fee(fee_lamports: int) -> str:
        """Tier for a fee value: calm / normal / hot."""
        fee = int(fee_lamports)
        if fee <= TIER_CALM_MAX_LAMPORTS:
            return TIER_CALM
        if fee <= TIER_NORMAL_MAX_LAMPORTS:
            return TIER_NORMAL
        return TIER_HOT

    def choose_fee(self, stake_lamports: int) -> Dict[str, Any]:
        """Pick the priority fee for a trade of stake_lamports.

        The fee is the 75th percentile of recent fees (pay enough to
        land in a contested window, not the median and not the max),
        floored at the base fee. Verdict "ok" carries the fee and
        tier; verdict "skip" carries reason "fee_over_cap" when the
        fee would breach the absolute cap or the 5%-of-stake cap.
        A skipped trade is never paid — the caller must not trade.
        The result also carries "fee_tiers" (the 1st/50th/95th
        percentile fallback tiers from fee_tiers(), same cached
        fees list, no extra fetch).
        """
        stake = int(stake_lamports or 0)
        if stake <= 0:
            return {"verdict": "skip", "reason": "fee_over_cap",
                    "fee_lamports": 0, "tier": None,
                    "stake_lamports": stake,
                    "detail": "stake must be positive to price a fee"}
        fees = sorted(self.sample_fees())
        fee = max(self.base_fee_lamports,
                  _percentile(fees, 0.75) if fees else
                  self.base_fee_lamports)
        tier = self.tier_for_fee(fee)
        out: Dict[str, Any] = {
            "verdict": "ok", "reason": None,
            "fee_lamports": int(fee), "tier": tier,
            "stake_lamports": stake,
            "fee_frac_of_stake": fee / stake,
            "absolute_cap_lamports": self.absolute_cap_lamports,
            "max_frac_of_stake": self.max_frac_of_stake,
            "samples": len(fees),
            "fee_tiers": self.fee_tiers(),
        }
        if fee > self.absolute_cap_lamports:
            out["verdict"] = "skip"
            out["reason"] = "fee_over_cap"
            out["detail"] = (
                "fee %d lamports over absolute cap %d lamports"
                % (fee, self.absolute_cap_lamports))
            return out
        if fee > self.max_frac_of_stake * stake:
            out["verdict"] = "skip"
            out["reason"] = "fee_over_cap"
            out["detail"] = (
                "fee %d lamports over %.0f%% of stake %d lamports"
                % (fee, self.max_frac_of_stake * 100, stake))
            return out
        return out

    def check_jito_tip(self, stake_sol: float, tip_sol: float,
                       sol_price_usd: Optional[float] = None
                       ) -> Dict[str, Any]:
        """Tip economics for this policy (see jito_tip_verdict)."""
        price = sol_price_usd if sol_price_usd is not None \
            else self.sol_price_usd
        verdict = jito_tip_verdict(stake_sol, tip_sol, price)
        if verdict["verdict"] == "uneconomic":
            verdict["reason"] = "fee_uneconomic"
            verdict["detail"] = (
                "Jito tip %.6f SOL is %.1f%% of a %.6f SOL stake — "
                "the tip eats the trade; sizing is decided by the "
                "shakedown test, not by this build"
                % (verdict["tip_sol"], verdict["tip_frac"] * 100,
                   verdict["stake_sol"]))
        return verdict
