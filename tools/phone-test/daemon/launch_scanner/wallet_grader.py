#!/usr/bin/env python3
"""Wallet Grader v2: statistical wallet quality gates for signal intelligence.

From the 2026-10-14 sniper-wallet research (UCL/WWW'26 paper + Banana Gun
practitioner guide). This is SIGNAL INTELLIGENCE, not blind copy-trading:
wallets tell us where to look; our own logic decides entries and exits.

Gate sets implemented:

UCL academic gates (WWW'26, Luo et al.):
- t-statistic of returns > 1.645 (one-tailed 5% significance)
- average return > 0
- return std < 1.00
- positive returns across ALL horizons: last, 1-5, 6-10, 11-15
- trade count above 25th percentile of population
- recency (time since last trade) below 75th percentile
- longevity (time since first trade) above 25th percentile
- Bundle Bot = False (HARD requirement)
- Sniper Bot ideally False

Practitioner gates (Banana Gun 2026-10-09):
- WR >= 55% across >= 50 trades (62% over 300 = strong; 90% over 8 = noise)
- Active within last 30-90 days
- Positive realized PnL without single-trade dependence
- Accumulate-early-and-hold-days pattern (NOT first-block-dump = insider/bot)

Wash-trading detection (UCL):
- Per trader per coin: matched identical-amount buy/sell pairs / net position
- Flag if any trader's wash score > 50

Auto-demote: wallets failing gates get demoted from "active" to "watching"
or "removed". Human can always re-promote.

Never raises. Paper-only signals.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("wallet_grader")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_GRADE_FILE = _HOME / "launch_scanner" / "wallet_grades_v2.json"
_DEMOTE_LOG = _HOME / "launch_scanner" / "wallet_demotions.jsonl"

# ---- Gate thresholds (from research) ----
T_STAT_MIN = 1.645          # UCL: one-tailed 5%
RETURN_STD_MAX = 1.00       # UCL: return std < 1.00
WR_MIN = 0.55               # Practitioner: >=55% win rate
TRADES_MIN = 50             # Practitioner: >=50 trades
ACTIVE_DAYS_MIN = 30        # Active within 30-90 days
ACTIVE_DAYS_MAX = 90
WASH_SCORE_MAX = 50         # UCL wash-trading: flag if > 50
SINGLE_TRADE_MAX_SHARE = 0.40  # No single trade >40% of total PnL

# Status levels
STATUS_ACTIVE = "active"      # Passes all gates, signals trusted
STATUS_WATCHING = "watching" # Fails some gates, signals logged not acted on
STATUS_REMOVED = "removed"   # Fails hard gates (bundle bot, wash trading)


@dataclass
class WalletMetrics:
    """Computed metrics for one wallet."""
    wallet: str
    n_trades: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    return_std: float = 0.0
    t_stat: float = 0.0
    realized_pnl: float = 0.0
    horizons_positive: bool = False  # all horizons (last, 1-5, 6-10, 11-15)
    bundle_bot: bool = False
    sniper_bot: bool = False
    wash_score: float = 0.0
    single_trade_share: float = 0.0
    days_since_first: float = 0.0
    days_since_last: float = 0.0
    status: str = STATUS_WATCHING
    gate_results: Dict[str, bool] = field(default_factory=dict)
    graded_ts: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)


def t_statistic(returns: List[float]) -> float:
    """One-sample t-statistic of returns vs 0."""
    if len(returns) < 2:
        return 0.0
    try:
        mean = statistics.mean(returns)
        stdev = statistics.stdev(returns)
        if stdev <= 0:
            return 0.0
        return mean / (stdev / math.sqrt(len(returns)))
    except statistics.StatisticsError:
        return 0.0


def horizon_returns(returns: List[float]) -> Dict[str, float]:
    """Split returns into UCL horizons: last, 1-5, 6-10, 11-15.

    `returns` should be chronological (oldest first).
    """
    n = len(returns)
    horizons = {}
    if n >= 1:
        horizons["last"] = returns[-1]
    if n >= 5:
        horizons["h1_5"] = sum(returns[max(0, n-5):n-1]) / max(1, min(4, n-1))
    if n >= 10:
        horizons["h6_10"] = sum(returns[max(0, n-10):n-5]) / max(1, min(5, n-5))
    if n >= 15:
        horizons["h11_15"] = sum(returns[max(0, n-15):n-10]) / max(1, min(5, n-10))
    return horizons


def wash_trading_score(trades: List[Dict]) -> float:
    """UCL wash-trading score: matched identical-amount buy/sell pairs / net position.

    trades: list of {"side": "buy"|"sell", "amount": float, "mint": str}
    Returns max wash score across all mints.
    """
    from collections import defaultdict
    by_mint: Dict[str, List[Dict]] = defaultdict(list)
    for t in trades:
        by_mint[t.get("mint", "")].append(t)

    max_score = 0.0
    for mint, mints_trades in by_mint.items():
        buys = sorted([t["amount"] for t in mints_trades if t.get("side") == "buy"])
        sells = sorted([t["amount"] for t in mints_trades if t.get("side") == "sell"])
        if not buys or not sells:
            continue
        # Count matched identical-amount pairs (within 1% tolerance)
        matched = 0
        sell_remaining = sells[:]
        for b in buys:
            for i, s in enumerate(sell_remaining):
                if abs(b - s) / max(b, 1e-9) < 0.01:
                    matched += 1
                    sell_remaining.pop(i)
                    break
        total_volume = sum(buys) + sum(sells)
        net = abs(sum(buys) - sum(sells))
        if net < 1e-9 and matched > 0:
            # Fully washed: buys == sells in matched pairs = max score
            max_score = max(max_score, 100.0)
        elif total_volume > 1e-9:
            # UCL: ratio of matched pair volume to total volume, scaled
            matched_vol = matched * (sum(buys) / max(len(buys), 1))
            score = (matched_vol / total_volume) * 100
            max_score = max(max_score, score)
    return max_score


def grade_wallet(wallet: str, trades: List[Dict],
                 population_p25_trades: int = 25,
                 now: float = None) -> WalletMetrics:
    """Grade one wallet against UCL + practitioner gates.

    trades: list of {"ts": float, "pnl": float, "return": float,
                     "side": ..., "amount": ..., "mint": ...,
                     "bundle_bot": bool, "sniper_bot": bool}
    Sorted oldest-first internally.
    """
    now = now or time.time()
    m = WalletMetrics(wallet=wallet, graded_ts=now)

    if not trades:
        m.gate_results = {"has_data": False}
        m.status = STATUS_REMOVED
        return m

    trades = sorted(trades, key=lambda t: t.get("ts", 0))
    # None returns/pnls (e.g. unmatched buys from the nightly FIFO matcher)
    # are open positions, not 0% outcomes — exclude them from realized stats
    # rather than crashing statistics.mean or diluting the edge toward zero.
    returns = [r for t in trades
               for r in [t.get("return")]
               if isinstance(r, (int, float))]
    pnls = [p for t in trades
            for p in [t.get("pnl")]
            if isinstance(p, (int, float))]

    m.n_trades = len(trades)
    m.win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0
    m.realized_pnl = sum(pnls)
    m.avg_return = statistics.mean(returns) if returns else 0.0
    m.return_std = statistics.stdev(returns) if len(returns) >= 2 else 0.0
    m.t_stat = t_statistic(returns)

    # Horizons: all must be positive
    hz = horizon_returns(returns)
    required = {"last", "h1_5", "h6_10", "h11_15"}
    m.horizons_positive = (
        required.issubset(hz.keys()) and all(v > 0 for v in hz.values())
    )

    # Bot flags (hard from trade metadata)
    m.bundle_bot = any(t.get("bundle_bot") for t in trades)
    m.sniper_bot = sum(1 for t in trades if t.get("sniper_bot")) > len(trades) * 0.5

    # Wash trading
    m.wash_score = wash_trading_score(trades)

    # Single-trade dependence
    if m.realized_pnl > 0 and pnls:
        m.single_trade_share = max(pnls) / m.realized_pnl
    else:
        m.single_trade_share = 1.0 if pnls else 0.0

    # Activity windows
    first_ts = trades[0].get("ts", now)
    last_ts = trades[-1].get("ts", now)
    m.days_since_first = (now - first_ts) / 86400
    m.days_since_last = (now - last_ts) / 86400

    # ---- Evaluate gates ----
    g = m.gate_results
    g["has_data"] = True
    g["t_stat_ok"] = m.t_stat > T_STAT_MIN
    g["avg_return_ok"] = m.avg_return > 0
    g["std_ok"] = m.return_std < RETURN_STD_MAX
    g["horizons_ok"] = m.horizons_positive
    g["trades_p25_ok"] = m.n_trades > population_p25_trades
    g["trades_50_ok"] = m.n_trades >= TRADES_MIN
    g["wr_ok"] = m.win_rate >= WR_MIN
    g["active_ok"] = m.days_since_last <= ACTIVE_DAYS_MAX
    g["longevity_ok"] = m.days_since_first >= ACTIVE_DAYS_MIN
    g["bundle_bot_free"] = not m.bundle_bot
    g["wash_ok"] = m.wash_score <= WASH_SCORE_MAX
    g["no_single_dependence"] = m.single_trade_share <= SINGLE_TRADE_MAX_SHARE

    # ---- Status determination ----
    # HARD fails → removed
    if m.bundle_bot or m.wash_score > WASH_SCORE_MAX:
        m.status = STATUS_REMOVED
    # UCL core gates → active requires all
    elif (g["t_stat_ok"] and g["avg_return_ok"] and g["std_ok"]
          and g["horizons_ok"] and g["trades_50_ok"] and g["wr_ok"]
          and g["active_ok"] and g["no_single_dependence"]):
        m.status = STATUS_ACTIVE
    else:
        m.status = STATUS_WATCHING

    return m


def load_grades() -> Dict[str, Dict]:
    try:
        return json.loads(_GRADE_FILE.read_text())
    except Exception:
        return {}


def save_grades(grades: Dict[str, Dict]):
    _GRADE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(_GRADE_FILE) + ".tmp"
    json.dump(grades, open(tmp, "w"), indent=1)
    os.replace(tmp, _GRADE_FILE)
    try:
        os.chmod(_GRADE_FILE, 0o600)
    except Exception:
        pass


def auto_demote(wallet: str, old_status: str, new_status: str, reason: str):
    """Log auto-demotion. Human can re-promote via registry."""
    if old_status == new_status:
        return
    _DEMOTE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(), "wallet": wallet,
        "old": old_status, "new": new_status, "reason": reason,
    }
    with open(_DEMOTE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info("wallet %s demoted %s -> %s: %s", wallet[:8], old_status, new_status, reason)


def grade_all(wallet_trades: Dict[str, List[Dict]],
              population_p25: int = 25) -> Dict[str, WalletMetrics]:
    """Grade all wallets, persist results, auto-demote failures.

    wallet_trades: {wallet: [trade dicts]}
    Returns {wallet: WalletMetrics}.
    """
    stored = load_grades()
    results = {}
    for wallet, trades in wallet_trades.items():
        m = grade_wallet(wallet, trades, population_p25_trades=population_p25)
        old = stored.get(wallet, {}).get("status", STATUS_WATCHING)
        auto_demote(wallet, old, m.status,
                    "; ".join(k for k, v in m.gate_results.items() if not v))
        stored[wallet] = m.to_dict()
        results[wallet] = m
    save_grades(stored)
    return results


def active_wallets() -> List[str]:
    """Wallets currently passing all gates — safe for signal use."""
    return [w for w, g in load_grades().items()
            if g.get("status") == STATUS_ACTIVE]


def watching_wallets() -> List[str]:
    return [w for w, g in load_grades().items()
            if g.get("status") == STATUS_WATCHING]
