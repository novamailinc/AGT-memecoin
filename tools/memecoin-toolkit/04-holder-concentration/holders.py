"""Holder-concentration math over synthetic largest-account data.

Pure stdlib, no network. The point of this lab is a single subtlety:
the biggest account on-chain is usually NOT a user. Liquidity pools,
bonding curves and escrows are program-derived addresses (PDAs) --
they sit off the ed25519 curve. Counting them as "holders" makes a
healthy token look whale-dominated (or, read the other way, hides a
real whale behind a pool). So user concentration is computed only
over accounts classified as user wallets.

Classification is injected, never guessed: the caller passes a
registry mapping account address -> {"owner_program": bool,
"on_curve": bool}. An account is a user wallet iff on_curve is True.
Anything else (off-curve, program-owned, or missing from the
registry) is excluded from user concentration. Missing data fails
closed: the verdict is "unknown" and ``passed`` is False.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Verdict thresholds, in percent of total supply. Rules use strict
# greater-than, so a value exactly on a FAIL line is not a fail.
TOP1_FAIL_PCT = 50.0
TOP10_FAIL_PCT = 70.0
TOP1_FLAG_PCT = 25.0
TOP10_FLAG_PCT = 55.0


@dataclass
class HolderAnalysis:
    """Result of :func:`analyze`. Percentages are 0-100 floats."""

    top1_user_pct: float | None
    top10_user_pct: float | None
    verdict: str  # "PASS" | "FLAG" | "FAIL" | "unknown"
    passed: bool  # fail-closed: only PASS / FLAG pass; unknown does not
    user_holder_count: int = 0
    excluded_count: int = 0
    unknown_count: int = 0
    reason: str = ""
    user_holders: list = field(default_factory=list)


def _address_of(holder: dict) -> str | None:
    """Holder rows may key the account as 'address' or 'owner'."""
    if not isinstance(holder, dict):
        return None
    return holder.get("address") or holder.get("owner")


def _amount_of(holder: dict) -> float | None:
    try:
        value = holder.get("amount")
        if value is None:
            return None
        amount = float(value)
    except (TypeError, ValueError, AttributeError):
        return None
    # A negative, NaN or infinite balance is not a balance: the row is
    # malformed, never a (negative) contribution to concentration.
    if not math.isfinite(amount) or amount < 0:
        return None
    return amount


def _valid_supply(total_supply) -> float | None:
    """Return the supply as a float, or None when it is unusable."""
    try:
        supply = float(total_supply)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(supply) or supply <= 0:
        return None
    return supply


def classify_holder(address: str, registry: dict | None) -> str:
    """Return 'user', 'program', or 'unknown' for one account.

    Signature: classify_holder(address: str, registry: dict | None) -> str
    Fail-closed behaviour: an address absent from the registry (or a
    registry entry without a boolean ``on_curve``) is 'unknown',
    never silently treated as a user wallet.
    """
    if not registry or address not in registry:
        return "unknown"
    entry = registry[address]
    if not isinstance(entry, dict) or not isinstance(entry.get("on_curve"), bool):
        return "unknown"
    if entry["on_curve"] and not entry.get("owner_program", False):
        return "user"
    if entry["on_curve"] and entry.get("owner_program", False):
        # An on-curve account owned by a program is ambiguous; a real
        # user wallet is owned by the system program. Fail closed.
        return "program"
    return "program"


def is_user_wallet(address: str, registry: dict | None) -> bool:
    """Signature: is_user_wallet(address, registry) -> bool."""
    return classify_holder(address, registry) == "user"


def user_holders_sorted(holders: list, registry: dict | None) -> list:
    """Return [(address, amount), ...] for user wallets, biggest first.

    Signature: user_holders_sorted(holders: list, registry) -> list
    Rows with a missing address or a non-numeric amount are skipped;
    they are counted as unknown by :func:`analyze`, not here.
    """
    rows = []
    for holder in holders or []:
        address, amount = _address_of(holder), _amount_of(holder)
        if address is None or amount is None:
            continue
        if is_user_wallet(address, registry):
            rows.append((address, amount))
    # Biggest first; address breaks ties so output is deterministic
    # even when two wallets hold exactly the same amount.
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows


def verdict_for(top1_pct: float, top10_pct: float) -> str:
    """Map percentages to a verdict. Strict '>' on every threshold.

    Signature: verdict_for(top1_pct: float, top10_pct: float) -> str
    Exactly 50.0 / 70.0 is FLAG (not FAIL); exactly 25.0 / 55.0 is PASS.
    """
    if top1_pct > TOP1_FAIL_PCT or top10_pct > TOP10_FAIL_PCT:
        return "FAIL"
    if top1_pct > TOP1_FLAG_PCT or top10_pct > TOP10_FLAG_PCT:
        return "FLAG"
    return "PASS"


def analyze(holders: list, total_supply, registry: dict | None) -> HolderAnalysis:
    """Compute top-1 / top-10 USER-holder concentration.

    Signature:
        analyze(holders: list[dict], total_supply: float,
                registry: dict) -> HolderAnalysis

    ``holders`` rows look like {"address": str, "amount": number}
    (the address key may also be "owner"). ``total_supply`` is in
    the same raw units as ``amount``.

    Fail-closed behaviour: empty holders, a missing / non-positive
    supply, malformed rows, or zero classifiable user wallets all
    return verdict "unknown" with ``passed=False`` and percentages
    of None -- never a reassuring PASS built on no evidence.
    """
    if not holders:
        return HolderAnalysis(None, None, "unknown", False,
                              reason="no holder data")
    supply = _valid_supply(total_supply)
    if supply is None:
        return HolderAnalysis(None, None, "unknown", False,
                              reason="total supply missing or non-positive")

    excluded = unknown = malformed = 0
    for holder in holders:
        address, amount = _address_of(holder), _amount_of(holder)
        if address is None or amount is None:
            malformed += 1
            continue
        kind = classify_holder(address, registry)
        if kind == "program":
            excluded += 1
        elif kind == "unknown":
            unknown += 1

    users = user_holders_sorted(holders, registry)
    if not users:
        return HolderAnalysis(None, None, "unknown", False,
                              excluded_count=excluded,
                              unknown_count=unknown + malformed,
                              reason="no classifiable user wallets")

    top1_pct = users[0][1] / supply * 100.0
    top10_pct = sum(amount for _, amount in users[:10]) / supply * 100.0
    verdict = verdict_for(top1_pct, top10_pct)
    return HolderAnalysis(
        top1_user_pct=top1_pct,
        top10_user_pct=top10_pct,
        verdict=verdict,
        passed=verdict in ("PASS", "FLAG"),
        user_holder_count=len(users),
        excluded_count=excluded,
        unknown_count=unknown + malformed,
        reason=f"top1 user {top1_pct:.2f}%, top10 users {top10_pct:.2f}%",
        user_holders=users,
    )


def aggregate_user_balances(holders: list, registry: dict | None) -> list:
    """Sum user balances per address: [(address, total), ...].

    Signature: aggregate_user_balances(holders, registry) -> list
    The largest-accounts feed can list one wallet in several token
    accounts; concentration *per holder* must sum those rows first.
    Program / unknown / malformed rows are skipped exactly as in
    :func:`user_holders_sorted`. Output is biggest first, with the
    address breaking ties, so it is deterministic.
    """
    totals: dict = {}
    for holder in holders or []:
        address, amount = _address_of(holder), _amount_of(holder)
        if address is None or amount is None:
            continue
        if is_user_wallet(address, registry):
            totals[address] = totals.get(address, 0.0) + amount
    rows = sorted(totals.items(), key=lambda row: (-row[1], row[0]))
    return rows


def top_n_user_pct(holders: list, total_supply, registry: dict | None,
                   n: int) -> float | None:
    """Combined share (%) of the ``n`` largest user wallets.

    Signature: top_n_user_pct(holders, total_supply, registry, n)
    Generalises the top-1 / top-10 pair in :func:`analyze` (so
    ``n=1`` and ``n=10`` reproduce those numbers on the same input).
    Raises ValueError unless ``n`` is a positive int. Fail-closed:
    an unusable supply or zero user wallets returns None.
    """
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError(f"n must be a positive int, got {n!r}")
    supply = _valid_supply(total_supply)
    if supply is None:
        return None
    users = user_holders_sorted(holders, registry)
    if not users:
        return None
    return sum(amount for _, amount in users[:n]) / supply * 100.0


def herfindahl_index(holders: list, total_supply,
                     registry: dict | None) -> float | None:
    """Herfindahl-Hirschman index over aggregated user shares.

    Signature: herfindahl_index(holders, total_supply, registry)
    Sum of squared percentage shares, on the classic 0-10000 scale:
    one holder of everything scores 10000, ten equal holders score
    1000. Program accounts never enter the sum. Fail-closed: an
    unusable supply or zero user wallets returns None.
    """
    supply = _valid_supply(total_supply)
    if supply is None:
        return None
    aggregated = aggregate_user_balances(holders, registry)
    if not aggregated:
        return None
    return sum((amount / supply * 100.0) ** 2
               for _, amount in aggregated)


def effective_holder_count(holders: list, total_supply,
                           registry: dict | None) -> float | None:
    """The equal-sized-holder equivalent of the distribution.

    Signature: effective_holder_count(holders, total_supply, registry)
    The inverse Simpson index over shares *normalised among user
    holders*: two equal holders give exactly 2.0 however large (or
    small) their combined share of supply is, one holder gives 1.0,
    and ten equal holders give 10.0. Returns None when the HHI is
    unmeasurable or the user total is zero (all balances zero).
    """
    supply = _valid_supply(total_supply)
    if supply is None:
        return None
    aggregated = aggregate_user_balances(holders, registry)
    total_pct = sum(amount for _, amount in aggregated) / supply * 100.0
    hhi = herfindahl_index(holders, total_supply, registry)
    if hhi is None or hhi <= 0 or total_pct <= 0:
        return None
    return total_pct ** 2 / hhi


def nakamoto_coefficient(holders: list, total_supply,
                         registry: dict | None,
                         threshold_pct: float = 50.0) -> int | None:
    """Smallest number of top user holders exceeding a supply share.

    Signature: nakamoto_coefficient(holders, total_supply, registry,
                                     threshold_pct=50.0) -> int | None
    Aggregated holders are taken largest-first; the result is the
    first count whose cumulative share is *strictly greater* than
    ``threshold_pct`` (exactly 50.0% is not a majority). Raises
    ValueError unless ``0 < threshold_pct <= 100``. Returns None
    when the supply is unusable, there are no user wallets, or even
    all user wallets together never exceed the threshold.
    """
    if threshold_pct is None or not (0 < threshold_pct <= 100):
        raise ValueError(
            f"threshold_pct must be in (0, 100], got {threshold_pct!r}")
    supply = _valid_supply(total_supply)
    if supply is None:
        return None
    aggregated = aggregate_user_balances(holders, registry)
    if not aggregated:
        return None
    cumulative = 0.0
    for count, (_, amount) in enumerate(aggregated, start=1):
        cumulative += amount / supply * 100.0
        if cumulative > threshold_pct:
            return count
    return None
