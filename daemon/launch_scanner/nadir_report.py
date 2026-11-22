#!/usr/bin/env python3
"""Nadir-v0 reporting: daily snapshot + weekly verdict metrics.
Pure functions over a Ledger. No I/O."""
from typing import Dict, List


def daily_snapshot(ledger, day_label: str) -> Dict:
    m = ledger.expectancy()
    by_reason: Dict[str, int] = {}
    for p in ledger.closed:
        by_reason[p.exit_reason] = by_reason.get(p.exit_reason, 0) + 1
    return {
        "day": day_label,
        "strategy": "nadir_v0",
        "open": ledger.open_count(),
        "closed": m.get("n", 0),
        "net_pnl_sol": m.get("net", 0.0),
        "expectancy_per_trade": m.get("expectancy", 0.0),
        "win_rate": m.get("win_rate", 0.0),
        "profit_factor": m.get("profit_factor", 0.0),
        "worst_trade": m.get("max_dd", 0.0),
        "exits": by_reason,
    }


def weekly_verdict(snapshots: List[Dict]) -> Dict:
    """Answer the blueprint's 9 weekly questions from snapshots."""
    if not snapshots:
        return {"verdict": "sparse", "reason": "no data"}
    total_n = sum(s["closed"] for s in snapshots)
    total_net = sum(s["net_pnl_sol"] for s in snapshots)
    exp = total_net / total_n if total_n else 0.0
    # halves
    mid = len(snapshots) // 2 or 1
    h1 = sum(s["net_pnl_sol"] for s in snapshots[:mid])
    h2 = sum(s["net_pnl_sol"] for s in snapshots[mid:])
    n1 = sum(s["closed"] for s in snapshots[:mid]) or 1
    n2 = sum(s["closed"] for s in snapshots[mid:]) or 1
    wins = sum(s["win_rate"] * s["closed"] for s in snapshots)
    return {
        "closed_positions": total_n,
        "net_pnl_sol": round(total_net, 6),
        "expectancy": round(exp, 6),
        "win_rate": round(wins / (total_n or 1), 3),
        "first_half_exp": round(h1 / n1, 6),
        "second_half_exp": round(h2 / n2, 6),
        "both_halves_positive": (h1 / n1) > 0 and (h2 / n2) > 0,
        "verdict": ("green" if exp > 0 and total_n >= 60
                    else "promising" if exp > 0
                    else "red" if total_n >= 20
                    else "sparse"),
    }
