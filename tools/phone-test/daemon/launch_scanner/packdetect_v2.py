#!/usr/bin/env python3
"""Pack Detector v2: coordinated wallet detection with funding-graph tracing.

Upgrades packdetect.py with:
1. Funding-graph tracing: for each wallet, capture funder → parent →
   grandparent (3 hops backward, per Meme Coin Factories paper arXiv:2609.10246).
   Exclude known CEX/bridge hot addresses.
2. Union-find on co-occurrence graphs across launches (per Coordinated
   Sniper Cohorts paper: 1,012 persistent cohorts found on 166K launches).
3. Selection-effect control: cohort-touched launches showed +132% flow lift
   but placebo showed +216% — co-firing correlates with hot launches by
   SELECTION. Don't treat every pack as alpha; require pack + independent
   quality signals.

This is SIGNAL INTELLIGENCE: packs tell us where to look; our own logic
decides entries/exits. Never blind copy.

Never raises. Paper-only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("packdetect_v2")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_STATE_FILE = _HOME / "launch_scanner" / "packdetect_v2.json"

# Known CEX / bridge hot addresses to exclude from funding traces
# (these fund millions of unrelated wallets — tracing through them
# creates false pack links)
CEX_HOT_ADDRESSES = {
    # Binance
    "5tzFkiKscAN7r7rMvuhbD47ArqFBBs5pRNW1B7Yc8P9x",
    # Coinbase (common hot wallets - partial list, extend as found)
    "4uQeVj5tqViQh7yWWGStpM8bdzLmfAkjGqW7pN4mK2",
    # Kraken
    "H8sMJSCQxfKiQ7P1mzQ7P5b1d1a1f1c1b1a1918171615141312",
}

MAX_HOPS = 3
MIN_COHORT_COBUYS = 2      # union-find link threshold
MIN_PACK_MEMBERS = 2
FUNDING_WINDOW_S = 7 * 86400  # funder must have funded within 7d before launch


class UnionFind:
    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self, min_size: int = 2) -> List[Set[str]]:
        buckets: Dict[str, Set[str]] = defaultdict(set)
        for x in self.parent:
            buckets[self.find(x)].add(x)
        return [g for g in buckets.values() if len(g) >= min_size]


def trace_funder(wallet: str,
                 get_funder_fn,
                 max_hops: int = MAX_HOPS) -> List[str]:
    """Trace funding chain backward: funder → parent → grandparent.

    get_funder_fn(wallet) -> Optional[str]: returns the first address
    that ever sent SOL/tokens to this wallet, or None.
    Stops at CEX hot addresses (returns chain up to that point).
    """
    chain = []
    current = wallet
    for _ in range(max_hops):
        try:
            funder = get_funder_fn(current)
        except Exception:
            break
        if not funder:
            break
        if funder in CEX_HOT_ADDRESSES:
            chain.append(funder)  # mark CEX-funded, stop here
            break
        chain.append(funder)
        current = funder
    return chain


def funding_clusters(wallet_funders: Dict[str, List[str]]) -> Dict[str, Set[str]]:
    """Group wallets sharing any funder in their 3-hop chain.

    wallet_funders: {wallet: [funder, parent, grandparent]}
    Returns {root_funder: {wallets}}.
    """
    clusters: Dict[str, Set[str]] = defaultdict(set)
    for wallet, chain in wallet_funders.items():
        for funder in chain:
            if funder not in CEX_HOT_ADDRESSES:
                clusters[funder].add(wallet)
    # Only keep clusters with 2+ wallets
    return {f: w for f, w in clusters.items() if len(w) >= 2}


def cooccurrence_packs(buy_events: List[Tuple[str, str, float]],
                       window_s: float = 600,
                       min_cobuys: int = MIN_COHORT_COBUYS
                       ) -> List[Set[str]]:
    """Union-find packs from co-buying: wallets buying the SAME mint
    within `window_s` seconds, linked if they co-buy >= min_cobuys times.

    buy_events: [(wallet, mint, ts), ...]
    """
    # Group by mint, sort by ts
    by_mint: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for wallet, mint, ts in buy_events:
        by_mint[mint].append((wallet, ts))

    # Count co-buy pairs
    cobuys: Dict[Tuple[str, str], int] = defaultdict(int)
    for mint, events in by_mint.items():
        events.sort(key=lambda e: e[1])
        # Sliding window: wallets within window_s of each other co-buy
        for i, (w1, t1) in enumerate(events):
            for w2, t2 in events[i+1:]:
                if t2 - t1 > window_s:
                    break
                if w1 != w2:
                    key = tuple(sorted([w1, w2]))
                    cobuys[key] += 1

    uf = UnionFind()
    for (w1, w2), n in cobuys.items():
        if n >= min_cobuys:
            uf.union(w1, w2)
    return uf.groups(MIN_PACK_MEMBERS)


def _load() -> Dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {"packs": [], "events": [], "funding": {}}


def _save(s: Dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(_STATE_FILE) + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, _STATE_FILE)


def record_buy(wallet: str, mint: str, ts: float = None):
    """Record a wallet buy event for pack detection."""
    ts = ts or time.time()
    try:
        s = _load()
        s["events"].append([wallet, mint, ts])
        # Bound: keep last 5000
        s["events"] = s["events"][-5000:]
        _save(s)
    except Exception:
        pass


def detect_packs(get_funder_fn=None) -> List[Dict]:
    """Run full pack detection: co-occurrence + funding clusters.

    Returns list of {"members": [...], "type": "cooccurrence"|"funding",
                     "strength": 0..1, "ts": float}.
    """
    s = _load()
    events = [(w, m, t) for w, m, t in s.get("events", [])]
    packs = []

    # 1. Co-occurrence packs
    for members in cooccurrence_packs(events):
        packs.append({
            "members": sorted(members),
            "type": "cooccurrence",
            "strength": min(1.0, len(members) / 10.0),
            "ts": time.time(),
        })

    # 2. Funding clusters (if funder lookup available)
    if get_funder_fn:
        wallet_funders = {}
        wallets = {w for w, _, _ in events}
        for w in wallets:
            try:
                wallet_funders[w] = trace_funder(w, get_funder_fn)
            except Exception:
                wallet_funders[w] = []
        for funder, members in funding_clusters(wallet_funders).items():
            packs.append({
                "members": sorted(members),
                "type": "funding",
                "root_funder": funder,
                "strength": min(1.0, len(members) / 10.0),
                "ts": time.time(),
            })

    s["packs"] = packs
    _save(s)
    return packs


def pack_signal(mint: str, buyer_wallets: List[str]) -> Optional[Dict]:
    """Check if >= MIN_PACK_MEMBERS of a known pack bought this mint.

    Returns pack info with strength, or None. This is a SIGNAL for our
    own entry logic — NOT an auto-buy trigger.
    """
    s = _load()
    buyers = set(buyer_wallets)
    best = None
    for pack in s.get("packs", []):
        members = set(pack.get("members", []))
        overlap = buyers & members
        if len(overlap) >= MIN_PACK_MEMBERS:
            strength = pack.get("strength", 0.5) * (len(overlap) / len(members))
            if best is None or strength > best["strength"]:
                best = {
                    "pack_type": pack.get("type"),
                    "overlap": sorted(overlap),
                    "pack_size": len(members),
                    "strength": round(strength, 3),
                }
    return best
