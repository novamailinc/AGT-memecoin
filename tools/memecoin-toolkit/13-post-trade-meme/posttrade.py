"""Post-trade analytics for the $1-shot memecoin paper ledger.

Pure stdlib, offline: every function consumes synthetic close records
of the form::

    {"lane": str, "mint": str, "realized_mult": float,
     "peak_mult": float, "exit_reason": str, "was_rug": bool}

Definitions (fixed, documented — do not silently move them):
  * WIN:  realized_mult >= 1.5  (win_rate uses this; breakeven_rate
    separately reports the share >= 1.0)
  * expectancy = mean(realized_mult) - 1, i.e. profit per $1 shot
  * round-tripper: peaked >= 2.0x but closed < 1.0x — the position saw
    a double and still lost money; that gap is exit leakage
  * rug rate: share of closes flagged was_rug

Fail-closed / empty-ledger behaviour: an empty ledger never crashes —
rates return 0.0, round_trippers returns count 0 with mean None,
leakage averages are None, the scorecard is {} and the rendered table
says so. A record missing a required field raises KeyError: a corrupt
ledger row must be loud, not silently averaged away.
"""
from __future__ import annotations

WIN_MULT = 1.5
PEAK_MULT = 2.0

_REQUIRED = ("lane", "mint", "realized_mult", "peak_mult", "exit_reason", "was_rug")


def _check(closes: list[dict]) -> None:
    for rec in closes:
        for key in _REQUIRED:
            if key not in rec:
                raise KeyError(f"close record missing required field {key!r}")


def win_rate(closes: list[dict]) -> float:
    """Share of closes with realized_mult >= 1.5. Empty -> 0.0."""
    _check(closes)
    if not closes:
        return 0.0
    return sum(1 for c in closes if c["realized_mult"] >= WIN_MULT) / len(closes)


def breakeven_rate(closes: list[dict]) -> float:
    """Share of closes with realized_mult >= 1.0. Empty -> 0.0."""
    _check(closes)
    if not closes:
        return 0.0
    return sum(1 for c in closes if c["realized_mult"] >= 1.0) / len(closes)


def expectancy(closes: list[dict]) -> float:
    """Mean realized multiple minus 1 (profit per $1 shot). Empty -> 0.0."""
    _check(closes)
    if not closes:
        return 0.0
    return sum(c["realized_mult"] for c in closes) / len(closes) - 1.0


def round_trippers(closes: list[dict]) -> dict:
    """Closes that peaked >= 2.0x yet realized < 1.0x.

    Returns {"count": int, "mean_realized": float | None} — the mean
    is None when there are no round-trippers (nothing to average).
    """
    _check(closes)
    rt = [c for c in closes if c["peak_mult"] >= PEAK_MULT and c["realized_mult"] < 1.0]
    return {
        "count": len(rt),
        "mean_realized": (sum(c["realized_mult"] for c in rt) / len(rt)) if rt else None,
    }


def leakage_report(closes: list[dict]) -> dict:
    """How much of the >=2x peak population survived to a win.

    Keys: n_peaked_2x, n_closed_win (realized >= 1.5 among all closes),
    n_round_tripped, avg_realized_round_trippers (None if none).
    """
    _check(closes)
    rt = round_trippers(closes)
    return {
        "n_peaked_2x": sum(1 for c in closes if c["peak_mult"] >= PEAK_MULT),
        "n_closed_win": sum(1 for c in closes if c["realized_mult"] >= WIN_MULT),
        "n_round_tripped": rt["count"],
        "avg_realized_round_trippers": rt["mean_realized"],
    }


def pnl_usd(closes: list[dict], shot_usd: float = 1.0) -> float:
    """Total profit in dollars at ``shot_usd`` per position. Empty -> 0.0."""
    _check(closes)
    if shot_usd <= 0:
        raise ValueError("shot_usd must be positive")
    return sum(c["realized_mult"] - 1.0 for c in closes) * float(shot_usd)


def median_realized(closes: list[dict]) -> float | None:
    """Median realized multiple; None on an empty ledger."""
    _check(closes)
    if not closes:
        return None
    values = sorted(c["realized_mult"] for c in closes)
    mid = len(values) // 2
    if len(values) % 2:
        return float(values[mid])
    return (values[mid - 1] + values[mid]) / 2.0


def best_close(closes: list[dict]) -> dict | None:
    """The close with the highest realized multiple (first on ties)."""
    _check(closes)
    return max(closes, key=lambda c: c["realized_mult"], default=None)


def worst_close(closes: list[dict]) -> dict | None:
    """The close with the lowest realized multiple (first on ties)."""
    _check(closes)
    return min(closes, key=lambda c: c["realized_mult"], default=None)


def capture_rate(closes: list[dict]) -> float | None:
    """Of the closes that peaked >= 2.0x, the share that closed as wins.

    None when nothing peaked at 2x -- there was nothing to capture,
    which is a different fact from capturing none of it (0.0).
    """
    _check(closes)
    peaked = [c for c in closes if c["peak_mult"] >= PEAK_MULT]
    if not peaked:
        return None
    won = sum(1 for c in peaked if c["realized_mult"] >= WIN_MULT)
    return won / len(peaked)


def profit_factor(closes: list[dict]) -> float | None:
    """Gross gains divided by gross losses, per $1 shot.

    Gains sum ``realized - 1`` over closes above 1.0x, losses sum
    ``1 - realized`` over closes below it; exactly-1.0 closes feed
    neither side. Returns ``inf`` when there are gains but no losses,
    and None when neither side exists (empty or perfectly flat).
    """
    _check(closes)
    gains = sum(c["realized_mult"] - 1.0 for c in closes if c["realized_mult"] > 1.0)
    losses = sum(1.0 - c["realized_mult"] for c in closes if c["realized_mult"] < 1.0)
    if losses == 0:
        return float("inf") if gains > 0 else None
    return gains / losses


def cumulative_pnl_curve(closes: list[dict], shot_usd: float = 1.0) -> list[float]:
    """Running total P/L in dollars after each close, in ledger order."""
    _check(closes)
    if shot_usd <= 0:
        raise ValueError("shot_usd must be positive")
    curve, total = [], 0.0
    for c in closes:
        total += (c["realized_mult"] - 1.0) * float(shot_usd)
        curve.append(round(total, 10))
    return curve


def max_drawdown_usd(closes: list[dict], shot_usd: float = 1.0) -> float:
    """Largest peak-to-trough fall of the cumulative P/L curve, in dollars.

    The curve starts at 0 before the first close, so an opening losing
    streak draws down from 0. Always >= 0; 0.0 on an empty ledger.
    """
    curve = [0.0] + cumulative_pnl_curve(closes, shot_usd)
    peak, worst = curve[0], 0.0
    for value in curve:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return round(worst, 10)


def _longest_run(closes: list[dict], predicate) -> int:
    best = run = 0
    for c in closes:
        run = run + 1 if predicate(c) else 0
        best = max(best, run)
    return best


def longest_win_streak(closes: list[dict]) -> int:
    """Longest run of consecutive wins (realized >= 1.5) in ledger order."""
    _check(closes)
    return _longest_run(closes, lambda c: c["realized_mult"] >= WIN_MULT)


def longest_loss_streak(closes: list[dict]) -> int:
    """Longest run of consecutive losers (realized < 1.0) in ledger order.

    A breakeven-ish close (1.0 <= realized < 1.5) breaks both streaks:
    it is neither a win nor a loss under the ledger's definitions.
    """
    _check(closes)
    return _longest_run(closes, lambda c: c["realized_mult"] < 1.0)


def exit_reason_breakdown(closes: list[dict]) -> dict:
    """Per-exit-reason stats: {reason: {n, wr, expectancy}}, keys sorted."""
    _check(closes)
    out: dict[str, dict] = {}
    for reason in sorted({c["exit_reason"] for c in closes}):
        rows = [c for c in closes if c["exit_reason"] == reason]
        out[reason] = {
            "n": len(rows),
            "wr": win_rate(rows),
            "expectancy": expectancy(rows),
        }
    return out


def filter_by_lane(closes: list[dict], lane: str) -> list[dict]:
    """The closes belonging to one lane, in ledger order ([] if none)."""
    _check(closes)
    return [c for c in closes if c["lane"] == lane]


def lane_ranking(closes: list[dict]) -> list[tuple[str, dict]]:
    """``(lane, scorecard-row)`` pairs: expectancy desc, then n desc,
    then lane name ascending -- deterministic for a given ledger."""
    card = per_lane_scorecard(closes)
    return sorted(
        card.items(),
        key=lambda item: (-item[1]["expectancy"], -item[1]["n"], item[0]),
    )


def verdict_ready(
    closes: list[dict], min_closes: int, min_expectancy: float = 0.0
) -> bool:
    """Pre-registered verdict gate: enough closes AND expectancy above bar.

    Both conditions are required -- a hot first week on five closes is
    not a verdict, and a hundred closes at negative expectancy is a
    verdict against, which this returns False for just the same.
    """
    _check(closes)
    if min_closes < 1:
        raise ValueError("min_closes must be >= 1")
    return len(closes) >= min_closes and expectancy(closes) > min_expectancy


def rug_rate(closes: list[dict]) -> float:
    """Share of closes flagged was_rug. Empty -> 0.0."""
    _check(closes)
    if not closes:
        return 0.0
    return sum(1 for c in closes if c["was_rug"]) / len(closes)


def per_lane_scorecard(closes: list[dict]) -> dict:
    """Per-lane stats: {lane: {n, wr, breakeven_rate, expectancy, rug_rate}}.

    ``wr`` uses the ledger-wide WIN definition (realized >= 1.5).
    Empty ledger -> {}.
    """
    _check(closes)
    card: dict[str, dict] = {}
    for lane in sorted({c["lane"] for c in closes}):
        rows = [c for c in closes if c["lane"] == lane]
        card[lane] = {
            "n": len(rows),
            "wr": win_rate(rows),
            "breakeven_rate": breakeven_rate(rows),
            "expectancy": expectancy(rows),
            "rug_rate": rug_rate(rows),
        }
    return card


def render_scorecard(closes: list[dict]) -> str:
    """Plain-text per-lane table (plus a TOTAL row). Empty ledger -> a
    one-line 'no closes recorded' string instead of a crash."""
    _check(closes)
    if not closes:
        return "post-trade scorecard: no closes recorded"
    card = per_lane_scorecard(closes)
    header = f"{'lane':<14} {'n':>3} {'wr':>6} {'be>=1x':>7} {'exp':>7} {'rug':>6}"
    lines = ["post-trade scorecard (win = realized >= 1.5x, $1 per shot)", header,
             "-" * len(header)]
    for lane, s in card.items():
        lines.append(f"{lane:<14} {s['n']:>3} {s['wr']:>6.2f} "
                     f"{s['breakeven_rate']:>7.2f} {s['expectancy']:>+7.3f} "
                     f"{s['rug_rate']:>6.2f}")
    lines.append(f"{'TOTAL':<14} {len(closes):>3} {win_rate(closes):>6.2f} "
                 f"{breakeven_rate(closes):>7.2f} {expectancy(closes):>+7.3f} "
                 f"{rug_rate(closes):>6.2f}")
    leak = leakage_report(closes)
    avg = leak["avg_realized_round_trippers"]
    avg_txt = f"{avg:.3f}x" if avg is not None else "n/a"
    lines.append(f"leakage: peaked>=2x {leak['n_peaked_2x']}, "
                 f"round-tripped {leak['n_round_tripped']} "
                 f"(avg realized {avg_txt})")
    return "\n".join(lines)


def demo_fixture() -> list[dict]:
    """Ten synthetic closes across two lanes (also used by the tests)."""
    def c(lane, mint, realized, peak, reason, rug=False):
        return {"lane": lane, "mint": mint, "realized_mult": realized,
                "peak_mult": peak, "exit_reason": reason, "was_rug": rug}
    return [
        c("degen", "MINT_A", 2.6, 3.1, "trail"),
        c("degen", "MINT_B", 0.4, 2.6, "hard_stop"),   # round-tripper
        c("degen", "MINT_C", 0.0, 1.1, "rug", rug=True),
        c("degen", "MINT_D", 1.8, 2.2, "tp"),
        c("degen", "MINT_E", 0.7, 1.3, "stop"),
        c("steady", "MINT_F", 1.6, 1.9, "tp"),
        c("steady", "MINT_G", 1.1, 1.4, "time_cap"),
        c("steady", "MINT_H", 0.9, 2.4, "trail"),      # round-tripper
        c("steady", "MINT_I", 0.0, 0.8, "rug", rug=True),
        c("steady", "MINT_J", 3.2, 4.0, "trail"),
    ]
