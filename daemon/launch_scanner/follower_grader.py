#!/usr/bin/env python3
"""Follower-P&L grader for tracked smart-money wallets (2026-10-17).

The old grader measured each wallet's OWN peak multiples. A peer-reviewed
study of 6,000+ coins (Luo et al., WWW'26) found wallet-profitable models
still produced NEGATIVE copier returns — the only metric that predicts a
follower's profit is simulated follower P&L: buy at detection plus a
realistic manual delay, exit on YOUR rules, net of costs.

What this does (measurement only — never gates, scores, or removes):
  For every anchored wallet buy in smart_wallet_grades.json, simulate one
  manual follow and record the net outcome. Aggregates per-wallet
  expectancy over the trailing window.

Simulation model (all assumptions documented, all knobs overridable):
  - Entry: card anchor * (1 + entry_penalty). Default penalty 15% — the
    conservative end of the 15-40% manual-delay penalty measured in the
    field (tap -> open wallet -> paste CA -> confirm). peak_mult in the
    grades file is measured from OUR card anchor, which is ~the follower's
    reference price, so this is the right basis.
  - Exits: the user's own ladder — half at 2x, quarter at 3x, remainder on
    a 30% trailing stop off peak, hard floor at -50% of entry. With only
    the 24h peak known, the trail is modeled as exiting at 70% of peak.
  - Costs: 4% round trip (research figure), subtracted from every follow.

Storage: NEW file ``~/.forex-agent/launch_scanner/follower_grades.json``
(0600). Never touches smart_wallet_grades.json or any live path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.follower_grader")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_GRADES_FILE = _HOME / "launch_scanner" / "smart_wallet_grades.json"
_REPORT_FILE = _HOME / "launch_scanner" / "follower_grades.json"

DEFAULT_ENTRY_PENALTY = 0.15   # 15% above card anchor (manual delay)
DEFAULT_ROUND_TRIP = 0.04      # 4% round-trip friction
DEFAULT_TRAIL = 0.30           # 30% trailing stop on the runner portion
DEFAULT_HARD_EXIT = -0.50      # hard exit at -50% of entry
DEFAULT_WINDOW_DAYS = 30.0
MIN_GRADED = 5                 # fewer -> "thin data", not ranked


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def simulate_buy(peak_mult: float,
                 entry_penalty: float = DEFAULT_ENTRY_PENALTY,
                 round_trip: float = DEFAULT_ROUND_TRIP,
                 trail: float = DEFAULT_TRAIL,
                 hard_exit: float = DEFAULT_HARD_EXIT) -> float:
    """Net return of one manual follow, as a fraction of entry size.

    peak_mult: peak multiple of the card anchor within 24h (from the
    grades ledger). Entry is assumed at anchor*(1+entry_penalty).
    Returns net P&L fraction AFTER round-trip costs. Never raises.
    """
    try:
        peak = _f(peak_mult)
        entry = 1.0 + max(0.0, float(entry_penalty))
        if peak <= 0 or entry <= 0:
            return 0.0
        # Exit ladder on multiples of ENTRY (user's rules, peak-only model)
        if peak >= 3.0:
            gross = (0.50 * (2.0 / entry - 1.0)
                     + 0.25 * (3.0 / entry - 1.0)
                     + 0.25 * ((1.0 - trail) * peak / entry - 1.0))
        elif peak >= 2.0:
            gross = (0.50 * (2.0 / entry - 1.0)
                     + 0.50 * ((1.0 - trail) * peak / entry - 1.0))
        else:
            # Never reached first TP: trail off peak, floored at hard exit
            gross = max((1.0 - trail) * peak / entry - 1.0, hard_exit)
        return gross - max(0.0, float(round_trip))
    except Exception:
        return 0.0


def grade_followers(grades: Optional[Dict[str, Dict[str, Any]]] = None,
                    entry_penalty: float = DEFAULT_ENTRY_PENALTY,
                    round_trip: float = DEFAULT_ROUND_TRIP,
                    window_days: float = DEFAULT_WINDOW_DAYS,
                    now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Per-wallet follower-P&L stats over the trailing window.

    A buy counts only if anchored (anchor_mcap > 0) and it has a measured
    peak (peak_mult > 0). Never raises.
    """
    try:
        now = now if now is not None else time.time()
        if grades is None:
            try:
                grades = json.loads(_GRADES_FILE.read_text())
            except (OSError, ValueError):
                grades = {}
        cutoff = now - max(1.0, float(window_days)) * 86400.0
        out: Dict[str, Dict[str, Any]] = {}
        for wallet, rec in (grades or {}).items():
            if not isinstance(wallet, str) or wallet.startswith("WALLET"):
                continue  # skip test fixtures
            nets: List[float] = []
            for b in (rec or {}).get("buys") or []:
                if float(b.get("buy_ts") or 0) < cutoff:
                    continue
                if _f(b.get("anchor_mcap")) <= 0:
                    continue
                if _f(b.get("peak_mult")) <= 0:
                    continue
                nets.append(simulate_buy(
                    b.get("peak_mult"),
                    entry_penalty=entry_penalty,
                    round_trip=round_trip))
            n = len(nets)
            wins = sum(1 for x in nets if x > 0)
            out[wallet] = {
                "n_follows": n,
                "wins": wins,
                "win_rate": round(wins / n, 3) if n else 0.0,
                "avg_net": round(sum(nets) / n, 4) if n else 0.0,
                "total_net": round(sum(nets), 4) if n else 0.0,
                "median_net": round(statistics.median(nets), 4) if n else 0.0,
                "best_net": round(max(nets), 4) if n else 0.0,
                "worst_net": round(min(nets), 4) if n else 0.0,
                "thin_data": n < MIN_GRADED,
            }
        return out
    except Exception as exc:
        logger.warning("grade_followers failed: %s", exc)
        return {}


def save_report(per_wallet: Dict[str, Dict[str, Any]],
                entry_penalty: float,
                round_trip: float,
                window_days: float) -> bool:
    """Write the follower-grade report. Never raises."""
    try:
        ranked = sorted(
            ((w, s) for w, s in per_wallet.items()
             if not s.get("thin_data")),
            key=lambda kv: kv[1].get("avg_net", 0.0),
            reverse=True)
        report = {
            "ts": time.time(),
            "window_days": window_days,
            "assumptions": {
                "entry": "card anchor * (1 + %.2f) — manual delay penalty"
                         % entry_penalty,
                "exits": ("user ladder: 50%% at 2x, 25%% at 3x, rest on "
                          "30%% trail off 24h peak, -50%% hard floor"),
                "costs": "%.1f%% round trip subtracted per follow"
                         % (round_trip * 100),
                "basis": ("peak_mult = 24h peak multiple of OUR card "
                          "anchor (follower's reference price)"),
            },
            "per_wallet": per_wallet,
            "ranked": [w for w, _ in ranked],
        }
        _REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REPORT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(_REPORT_FILE)
        return True
    except OSError as exc:
        logger.warning("could not save follower grades: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Follower-graded scoring weights (2026-10-17 rebuild, replaces the old
# peak-multiple weights in smartgrade.wallet_weight).
#
# Weight = 1.0 + shrinkage-adjusted avg follower net, clamped [0.5, 1.5].
# A wallet whose follows average -44% scores ~0.6; one averaging +20%
# scores ~1.2. Shrinkage toward 0 net (k=10) keeps small samples near
# neutral; <5 follows -> 1.0 (thin data, never punished). Score-only.
# ---------------------------------------------------------------------------

_WEIGHT_SHRINK_K = 10.0
_weights_cache: Dict[str, Any] = {"ts": 0.0, "weights": {}}


def follower_weight(wallet: str,
                    per_wallet: Optional[Dict[str, Dict[str, Any]]] = None,
                    entry_penalty: float = DEFAULT_ENTRY_PENALTY,
                    round_trip: float = DEFAULT_ROUND_TRIP,
                    window_days: float = DEFAULT_WINDOW_DAYS,
                    now: Optional[float] = None) -> float:
    """Per-wallet scoring weight from simulated follower P&L.

    1.0 for unknown/thin-history wallets. Otherwise 1.0 plus the
    shrinkage-adjusted average follow net, clamped to [0.5, 1.5].
    Never raises.
    """
    try:
        if not wallet:
            return 1.0
        if per_wallet is None:
            per_wallet = grade_followers(entry_penalty=entry_penalty,
                                         round_trip=round_trip,
                                         window_days=window_days,
                                         now=now)
        s = per_wallet.get(wallet) or {}
        n = int(s.get("n_follows") or 0)
        if n < MIN_GRADED:
            return 1.0
        avg = float(s.get("avg_net") or 0.0)
        shrunk = (n * avg) / (n + _WEIGHT_SHRINK_K)
        return max(0.5, min(1.5, round(1.0 + shrunk, 3)))
    except Exception:
        logger.exception("follower_weight failed for %s", wallet)
        return 1.0


def follower_weights(degen: Any = None) -> Dict[str, float]:
    """{wallet: weight} for all wallets, cached 60s.

    Drop-in replacement for smartgrade.wallet_weights in scoring hot
    paths. Never raises.
    """
    try:
        now = time.time()
        cache = _weights_cache
        if now - float(cache.get("ts") or 0) < 60.0:
            return dict(cache.get("weights") or {})
        per_wallet = grade_followers()
        weights = {w: follower_weight(w, per_wallet) for w in per_wallet}
        cache["ts"] = now
        cache["weights"] = weights
        return dict(weights)
    except Exception:
        logger.exception("follower_weights failed")
        return {}


def format_report(per_wallet: Dict[str, Dict[str, Any]]) -> str:
    lines = ["🧠 Follower-P&L grades (simulated manual follows):"]
    rows = sorted(per_wallet.items(),
                  key=lambda kv: kv[1].get("avg_net", 0.0),
                  reverse=True)
    for wallet, s in rows:
        name = wallet[:10] + "…"
        if s.get("thin_data"):
            lines.append("• %s — %d follows (thin data, unranked)"
                         % (name, s["n_follows"]))
            continue
        lines.append(
            "• %s — %d follows · WR %.0f%% · avg %+.1f%% · "
            "total %+.2fR · best %+.0f%% / worst %+.0f%%" % (
                name, s["n_follows"], s["win_rate"] * 100,
                s["avg_net"] * 100, s["total_net"],
                s["best_net"] * 100, s["worst_net"] * 100))
    lines.append("")
    lines.append("Model: buy 15% above card anchor, user exit ladder, "
                 "4% costs. Measurement only — grades nothing live.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-penalty", type=float,
                    default=DEFAULT_ENTRY_PENALTY)
    ap.add_argument("--round-trip", type=float, default=DEFAULT_ROUND_TRIP)
    ap.add_argument("--window-days", type=float,
                    default=DEFAULT_WINDOW_DAYS)
    args = ap.parse_args()
    per_wallet = grade_followers(entry_penalty=args.entry_penalty,
                                round_trip=args.round_trip,
                                window_days=args.window_days)
    save_report(per_wallet, args.entry_penalty, args.round_trip,
                args.window_days)
    print(format_report(per_wallet))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
