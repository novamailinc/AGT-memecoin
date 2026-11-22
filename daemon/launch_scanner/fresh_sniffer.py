#!/usr/bin/env python3
"""Fresh Wallet Sniffer: detect insider/sniper fresh wallets vs organic.

From the 2026-10-14 research:

INSIDER/SNIPER signatures:
- Pre-funded: wallet funded via CEX days before a launch, then dormant
- Funding-chain linkage: funded by deployer's funder cluster (short hop)
  or by a single address that funded dozens of sibling wallets
- First-transaction timing: wallet's FIRST-EVER tx is a buy within the
  first blocks/slots of a launch, especially pre-announcement
- Behavioral: first-block buys + quick exits across many tokens,
  identical buy sizes across sibling wallets, same-day creation
- Bundle participation: buys in same slot as creation (impossible outsider)

ORGANIC fresh wallet:
- Funded from personal CEX withdrawal, no deployer cluster link
- First buys AFTER public attention (social, trending)
- Varied sizes/timing, holds beyond first minutes

Implementation:
- seen_wallets set (persistent)
- For each new buyer: oldest-tx lookup → wallet age + first-tx vs launch
- 1-3 hop funder trace, checked against deployer clusters + CEX list
- Score = f(age, funder-link, buy-rank, size-vs-siblings)

This is SIGNAL INTELLIGENCE: insider wallets are a BEARISH signal
(they dump on us) or a timing signal (buy AFTER they buy, exit BEFORE
they dump). Never blind copy.

Never raises. Paper-only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("fresh_sniffer")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_SEEN_FILE = _HOME / "launch_scanner" / "seen_wallets.json"
_SCORES_FILE = _HOME / "launch_scanner" / "fresh_wallet_scores.json"

# Scoring weights
W_AGE = 25          # Young wallet (first tx near launch)
W_FUNDER_LINK = 30  # Funded by deployer cluster
W_BUY_RANK = 20     # Early buy rank (first 10 buyers)
W_SIZE_MATCH = 15   # Identical size to siblings
W_DORMANT = 10      # Funded then dormant until launch

INSIDER_THRESHOLD = 60   # score >= 60 = likely insider
SNIPER_THRESHOLD = 40    # score >= 40 = likely sniper


def _load_seen() -> Set[str]:
    try:
        return set(json.loads(_SEEN_FILE.read_text()))
    except Exception:
        return set()


def _save_seen(seen: Set[str]):
    _SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Bound: keep last 100k
    seen_list = list(seen)[-100000:]
    tmp = str(_SEEN_FILE) + ".tmp"
    json.dump(seen_list, open(tmp, "w"))
    os.replace(tmp, _SEEN_FILE)


def is_fresh(wallet: str) -> bool:
    """Check if wallet has never been seen before."""
    return wallet not in _load_seen()


def mark_seen(wallet: str):
    seen = _load_seen()
    seen.add(wallet)
    _save_seen(seen)


def score_fresh_wallet(
    wallet: str,
    wallet_age_s: float,          # time from first tx to now
    first_tx_ts: float,           # timestamp of wallet's first tx
    launch_ts: float,             # token launch timestamp
    funder_chain: List[str],      # 1-3 hop funder trace
    deployer_clusters: Set[str],  # known deployer funder addresses
    buy_rank: int,                # buy rank among first buyers (1 = first)
    buy_size_sol: float,          # this wallet's buy size
    sibling_sizes: List[float],   # other early buyers' sizes
    funded_ts: Optional[float] = None,  # when wallet was first funded
) -> Dict:
    """Score a fresh wallet 0-100 for insider/sniper likelihood.

    Returns {"score": int, "classification": str, "reasons": [...]}.
    classification: "insider" | "sniper" | "organic" | "unknown"
    """
    score = 0
    reasons = []

    # 1. Age: wallet created near launch = suspicious
    age_before_launch = launch_ts - first_tx_ts
    if age_before_launch < 0:
        # First tx AFTER launch — not a pre-positioned insider
        pass
    elif age_before_launch < 3600:  # created <1h before launch
        score += W_AGE
        reasons.append(f"+{W_AGE} created <1h before launch")
    elif age_before_launch < 86400:  # <1d before
        score += W_AGE * 0.6
        reasons.append(f"+{int(W_AGE*0.6)} created <1d before launch")
    elif age_before_launch < 7 * 86400:  # <7d before
        score += W_AGE * 0.3
        reasons.append(f"+{int(W_AGE*0.3)} created <7d before launch")

    # 2. Funder link: funded by deployer cluster = strong insider signal
    funder_set = set(funder_chain)
    if funder_set & deployer_clusters:
        score += W_FUNDER_LINK
        reasons.append(f"+{W_FUNDER_LINK} funded by deployer cluster")
    elif len(funder_chain) >= 2 and len(set(funder_chain)) == 1:
        # Same funder at multiple hops (unusual)
        score += W_FUNDER_LINK * 0.5
        reasons.append(f"+{int(W_FUNDER_LINK*0.5)} suspicious funder chain")

    # 3. Buy rank: earlier = more suspicious
    if buy_rank <= 3:
        score += W_BUY_RANK
        reasons.append(f"+{W_BUY_RANK} top-3 buyer")
    elif buy_rank <= 10:
        score += W_BUY_RANK * 0.6
        reasons.append(f"+{int(W_BUY_RANK*0.6)} top-10 buyer")

    # 4. Size match: identical size to siblings = coordinated
    if sibling_sizes and buy_size_sol > 0:
        matches = sum(
            1 for s in sibling_sizes
            if abs(s - buy_size_sol) / max(buy_size_sol, 1e-9) < 0.05
        )
        if matches >= 2:
            score += W_SIZE_MATCH
            reasons.append(f"+{W_SIZE_MATCH} size matches {matches} siblings")
        elif matches >= 1:
            score += W_SIZE_MATCH * 0.5
            reasons.append(f"+{int(W_SIZE_MATCH*0.5)} size matches 1 sibling")

    # 5. Dormant: funded then dormant until launch = pre-positioned
    if funded_ts and first_tx_ts:
        # If funded well before first tx (which is the launch buy)
        dormant_s = first_tx_ts - funded_ts
        if dormant_s > 86400:  # dormant >1d
            score += W_DORMANT
            reasons.append(f"+{W_DORMANT} dormant {dormant_s/86400:.1f}d before buy")

    score = min(100, int(score))

    if score >= INSIDER_THRESHOLD:
        classification = "insider"
    elif score >= SNIPER_THRESHOLD:
        classification = "sniper"
    elif score >= 15:
        classification = "unknown"
    else:
        classification = "organic"

    return {
        "score": score,
        "classification": classification,
        "reasons": reasons,
        "wallet": wallet,
        "ts": time.time(),
    }


def save_score(result: Dict):
    """Persist a fresh wallet score."""
    try:
        _SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            scores = json.loads(_SCORES_FILE.read_text())
        except Exception:
            scores = []
        scores.append(result)
        scores = scores[-2000:]  # bound
        tmp = str(_SCORES_FILE) + ".tmp"
        json.dump(scores, open(tmp, "w"), indent=1)
        os.replace(tmp, _SCORES_FILE)
    except Exception:
        pass


def get_insider_wallets(mint: str = None, min_score: int = INSIDER_THRESHOLD,
                        limit: int = 100) -> List[Dict]:
    """Get recent insider-classified wallets, optionally filtered by mint."""
    try:
        scores = json.loads(_SCORES_FILE.read_text())
    except Exception:
        return []
    result = [s for s in scores
              if s.get("score", 0) >= min_score
              and s.get("classification") == "insider"]
    if mint:
        result = [s for s in result if s.get("mint") == mint]
    return result[-limit:]
