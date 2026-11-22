"""Smart-wallet follower grading (lab, synthetic fixtures only).

Pure stdlib, no network. The question this module answers is not "did
the smart wallet win?" but "would a copier, arriving late, have won?"

Copier model ( deliberately simple, and documented here so grades can
be compared like-for-like ):

* Entry -- for a lag gain ``g`` in {0.15, 0.50, 1.00}, the copier only
  enters when the mint's price first reaches ``entry_price * (1 + g)``
  at or after the wallet's buy. If that level is never reached there
  is no follow at all (the signal is skipped, not chased).
* Exit -- the copier holds to the LAST point of the price path.
  ``realized_mult = exit_price / entry_price`` and
  ``realized_fraction = realized_mult - 1``.

Two honesty corrections sit on top of the raw simulation: wallets that
share a funder are ONE actor (pack independence), and grades are
shrunk toward a neutral prior so a wallet with two lucky follows does
not outrank one with fifty ordinary ones.
"""

from __future__ import annotations

LAG_GAINS = (0.15, 0.50, 1.00)
MUTE_MIN_FOLLOWS = 10
MUTE_MAX_AVG_REALIZED = -0.20  # average realised fraction, i.e. -20%


# ------------------------------------------------------------------- follow

def follow(event: dict, price_path: list, lag_gain: float) -> dict | None:
    """Simulate one copy of ``event`` at ``lag_gain``; None if no entry.

    ``event`` is ``{wallet, mint, t, price}`` and ``price_path`` is a
    list of ``(t, price)`` points for that mint, in time order.
    The copier's entry price is the first path price that is at or
    above ``event price * (1 + lag_gain)`` -- the path price itself,
    not the target, so overshoot is honestly charged to the copier.
    """
    if lag_gain not in LAG_GAINS:
        raise ValueError(f"lag_gain must be one of {LAG_GAINS}")
    if not price_path:
        return None
    target = float(event["price"]) * (1.0 + lag_gain)
    entry = None
    for t, price in price_path:
        if t >= event["t"] and float(price) >= target:
            entry = (t, float(price))
            break
    if entry is None:
        return None  # never reached: no follow, never a forced chase
    exit_t, exit_price = price_path[-1]
    entry_t, entry_price = entry
    realized_mult = float(exit_price) / entry_price
    return {
        "wallet": event["wallet"],
        "mint": event["mint"],
        "lag_gain": lag_gain,
        "entry_t": entry_t,
        "entry_price": entry_price,
        "exit_t": exit_t,
        "exit_price": float(exit_price),
        "realized_mult": realized_mult,
        "realized_fraction": realized_mult - 1.0,
    }


def follow_all_lags(event: dict, price_path: list) -> dict:
    """Follow one event at every documented lag: ``{lag_gain: result|None}``.

    Keys are exactly ``LAG_GAINS``, in order, so a caller can compare
    what each delay would have cost on the same signal.
    """
    return {lag: follow(event, price_path, lag) for lag in LAG_GAINS}


def wallet_outcome_fraction(event: dict, price_path: list) -> float | None:
    """The wallet's own realised fraction if it held to the last point.

    The baseline every copier is graded against: exit is the last path
    point at or after the wallet's buy. ``None`` when no such point
    exists (empty path, or the path ends before the buy).
    """
    later = [(t, float(p)) for t, p in price_path if t >= event["t"]]
    if not later:
        return None
    return later[-1][1] / float(event["price"]) - 1.0


def lag_cost_fraction(event: dict, price_path: list, lag_gain: float) -> float | None:
    """How much of the wallet's own outcome the copier's lag cost.

    ``wallet_outcome_fraction - copier realised_fraction``; positive
    means arriving late cost money. ``None`` when the copier never
    entered (a skipped signal has no lag cost to charge).
    """
    result = follow(event, price_path, lag_gain)
    own = wallet_outcome_fraction(event, price_path)
    if result is None or own is None:
        return None
    return own - result["realized_fraction"]


def follow_with_stop(
    event: dict, price_path: list, lag_gain: float, stop_fraction: float
) -> dict | None:
    """Like :func:`follow`, but the copier bails on a stop-loss.

    After entry, the first later point at or below
    ``entry_price * (1 - stop_fraction)`` becomes the exit (flagged
    ``stopped_out=True``); otherwise the copier holds to the last
    point as usual. ``stop_fraction`` must lie in (0, 1): a 0 stop
    would exit on any downtick and a >= 1 stop can never trigger, so
    both are rejected rather than silently modelling nonsense.
    """
    if not (0.0 < stop_fraction < 1.0):
        raise ValueError("stop_fraction must be in (0, 1)")
    result = follow(event, price_path, lag_gain)
    if result is None:
        return None
    stop_price = result["entry_price"] * (1.0 - stop_fraction)
    for t, price in price_path:
        if t <= result["entry_t"]:
            continue
        if float(price) <= stop_price:
            result = dict(result)
            result["exit_t"] = t
            result["exit_price"] = float(price)
            result["realized_mult"] = float(price) / result["entry_price"]
            result["realized_fraction"] = result["realized_mult"] - 1.0
            result["stopped_out"] = True
            return result
    result = dict(result)
    result["stopped_out"] = False
    return result


def simulate_follows(events: list, paths_by_mint: dict, lag_gain: float) -> list:
    """Run :func:`follow` over many events, dropping the skipped ones."""
    results = []
    for event in events:
        path = paths_by_mint.get(event["mint"], [])
        outcome = follow(event, path, lag_gain)
        if outcome is not None:
            results.append(outcome)
    return results


def wallet_stats(follow_results: list) -> dict:
    """Aggregate follow results into ``{wallet: (n, avg_realized_fraction)}``."""
    totals: dict[str, list[float]] = {}
    for result in follow_results:
        totals.setdefault(result["wallet"], []).append(
            float(result["realized_fraction"])
        )
    return {
        wallet: (len(values), sum(values) / len(values))
        for wallet, values in totals.items()
    }


# ---------------------------------------------------------- pack independence

def _normalise_pairs(wallets_with_funders) -> list[tuple[str, str]]:
    """Accept a dict, dict-rows, or (wallet, funder) pairs; return pairs.

    A wallet with a missing/empty funder is its own funder, so it can
    never be silently merged into somebody else's pack.
    """
    pairs: list[tuple[str, str]] = []
    if isinstance(wallets_with_funders, dict):
        items = list(wallets_with_funders.items())
    else:
        items = []
        for row in wallets_with_funders:
            if isinstance(row, dict):
                items.append((row.get("wallet"), row.get("funder")))
            else:
                wallet, funder = row
                items.append((wallet, funder))
    for wallet, funder in items:
        wallet = str(wallet)
        pairs.append((wallet, str(funder) if funder else wallet))
    return pairs


def group_wallets(wallets_with_funders) -> dict[str, list[str]]:
    """Group wallets by funder: ``{funder: [wallet, ...]}`` (sorted)."""
    groups: dict[str, list[str]] = {}
    for wallet, funder in _normalise_pairs(wallets_with_funders):
        groups.setdefault(funder, []).append(wallet)
    return {funder: sorted(set(w)) for funder, w in groups.items()}


def convergence_count(wallets_with_funders) -> int:
    """Number of independent actors: one per distinct funder, not wallet."""
    return len(group_wallets(wallets_with_funders))


def independence_ratio(wallets_with_funders) -> float:
    """Independent actors per wallet, in (0, 1]; 0.0 for no wallets.

    1.0 means every wallet is its own actor; 0.25 for four wallets
    behind one funder. A cheap pack-density gauge for a buy cluster.
    """
    pairs = _normalise_pairs(wallets_with_funders)
    wallets = {wallet for wallet, _ in pairs}
    if not wallets:
        return 0.0
    return convergence_count(wallets_with_funders) / len(wallets)


def meets_independence(wallets_with_funders, min_actors: int) -> bool:
    """True iff the cluster has at least ``min_actors`` distinct funders."""
    if min_actors < 1:
        raise ValueError("min_actors must be >= 1")
    return convergence_count(wallets_with_funders) >= min_actors


def largest_pack(wallets_with_funders) -> tuple[str, list[str]] | None:
    """The biggest funder group as ``(funder, wallets)``; None if empty.

    Ties go to the alphabetically first funder, so the answer is
    deterministic for a given registry.
    """
    groups = group_wallets(wallets_with_funders)
    if not groups:
        return None
    funder = sorted(groups, key=lambda f: (-len(groups[f]), f))[0]
    return funder, groups[funder]


# ------------------------------------------------------------ grade weighting

def grade_weight(
    n_follows: int, avg_realized: float, prior: float = 0.0, m: int = 10
) -> float:
    """Shrinkage grade weight, clamped to [0.5, 1.5].

    Formula::

        shrunk_grade = (n * avg_realized + m * prior) / (n + m)
        weight       = clamp(1.0 + shrunk_grade, 0.5, 1.5)

    The neutral prior (0.0 realised) maps to a neutral weight of 1.0.
    With few follows the ``m`` pseudo-observations of the prior pull
    the weight toward 1.0; with many follows the wallet's own record
    dominates. ``avg_realized`` is a fraction (0.5 means +50%).
    """
    if n_follows < 0 or m < 0:
        raise ValueError("n_follows and m must be non-negative")
    denom = n_follows + m
    shrunk = prior if denom == 0 else (n_follows * avg_realized + m * prior) / denom
    return max(0.5, min(1.5, 1.0 + shrunk))


def is_muted(n_follows: int, avg_realized: float) -> bool:
    """Auto-mute rule: at least 10 follows AND average realised <= -20%.

    Both boundaries are inclusive on the loss side by design: exactly
    10 follows averaging exactly -0.20 is muted; 9 follows never is,
    however bad the average, because the sample is too small to judge.
    """
    return n_follows >= MUTE_MIN_FOLLOWS and avg_realized <= MUTE_MAX_AVG_REALIZED


def grade_wallet(n_follows: int, avg_realized: float, prior: float = 0.0) -> dict:
    """Convenience bundle: weight plus mute flag for one wallet."""
    return {
        "n_follows": n_follows,
        "avg_realized": avg_realized,
        "weight": grade_weight(n_follows, avg_realized, prior=prior),
        "muted": is_muted(n_follows, avg_realized),
    }


def grade_all(stats: dict, prior: float = 0.0) -> dict:
    """Grade every wallet in a :func:`wallet_stats` mapping at once."""
    return {
        wallet: grade_wallet(n, avg, prior=prior)
        for wallet, (n, avg) in stats.items()
    }


def rank_wallets(stats: dict, prior: float = 0.0) -> list[tuple[str, dict]]:
    """``(wallet, grade)`` pairs, best first.

    Order: weight descending, then follow count descending (more
    evidence wins a tie), then wallet name ascending -- deterministic,
    so two runs over the same ledger rank identically.
    """
    graded = grade_all(stats, prior=prior)
    return sorted(
        graded.items(),
        key=lambda item: (-item[1]["weight"], -item[1]["n_follows"], item[0]),
    )


def median_realized(follow_results: list) -> float | None:
    """Median realised fraction over follow results; None when empty.

    The median is the honest centre for copier outcomes: one 20x
    follow must not average away nine rugs the way the mean would.
    """
    if not follow_results:
        return None
    values = sorted(float(r["realized_fraction"]) for r in follow_results)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0
