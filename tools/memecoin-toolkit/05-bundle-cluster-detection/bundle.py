"""Common-funder bundle / cluster detection (synthetic, stdlib only).

Wallets that were all funded from the same source address are often
one operator wearing many hats. This lab groups wallets by shared
funder with union-find (so funding links chain transitively), measures
each cluster's share of token supply, and -- crucially -- separates
*existence* of a cluster from its *behaviour*: a large cluster that is
holding is a flag; a large cluster that is distributing (net selling)
is a veto. Missing flow data can never manufacture a veto: in the
middle band it degrades to FLAG, not VETO.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EXTREME_SHARE_PCT = 50.0
ELEVATED_SHARE_PCT = 20.0


class UnionFind:
    """Classic union-find with path compression and union by size."""

    def __init__(self) -> None:
        self._parent: dict = {}
        self._size: dict = {}

    def add(self, item) -> None:
        if item not in self._parent:
            self._parent[item] = item
            self._size[item] = 1

    def find(self, item):
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]


def _wallet_id(wallet) -> str | None:
    if isinstance(wallet, str):
        return wallet
    if isinstance(wallet, dict):
        return wallet.get("wallet") or wallet.get("address") or wallet.get("owner")
    return None


def _funders_of(wallet) -> list:
    """A wallet row may carry 'funder' (str) or 'funders' (list)."""
    if not isinstance(wallet, dict):
        return []
    funders = []
    if wallet.get("funder"):
        funders.append(wallet["funder"])
    for key in ("funders", "funded_by"):
        value = wallet.get(key)
        if isinstance(value, (list, tuple, set)):
            funders.extend(v for v in value if v)
        elif value:
            funders.append(value)
    return funders


def find_clusters(wallets) -> list:
    """Group wallet ids that share a funder, transitively.

    Signature: find_clusters(wallets: list[dict]) -> list[list[str]]
    Each wallet dict is {"wallet": str, "funder": str} (a "funders"
    list is also accepted). Wallets sharing a funder -- directly or
    through a chain of shared funders -- land in one cluster.
    Wallets with no funder form singleton clusters. Output is
    deterministic: clusters sorted by (-size, first member), members
    sorted. Malformed rows are skipped, never guessed at.
    """
    uf = UnionFind()
    by_funder: dict = {}
    ids: list = []
    for wallet in wallets or []:
        wid = _wallet_id(wallet)
        if wid is None:
            continue
        uf.add(wid)
        ids.append(wid)
        for funder in _funders_of(wallet):
            by_funder.setdefault(funder, []).append(wid)
    for members in by_funder.values():
        for other in members[1:]:
            uf.union(members[0], other)
    groups: dict = {}
    for wid in dict.fromkeys(ids):  # de-duplicated, order-stable
        groups.setdefault(uf.find(wid), []).append(wid)
    clusters = [sorted(members) for members in groups.values()]
    clusters.sort(key=lambda c: (-len(c), c[0]))
    return clusters


def cluster_share(cluster, balances: dict, total_supply) -> float | None:
    """Cluster wallets' combined share of supply, in percent.

    Signature: cluster_share(cluster, balances: dict,
                              total_supply: float) -> float | None
    Fail-closed: returns None when supply is missing / non-positive;
    wallets absent from ``balances`` contribute 0 (unknown balance is
    not invented balance).
    """
    try:
        supply = float(total_supply)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(supply) or supply <= 0:
        return None
    total = 0.0
    for wid in cluster or []:
        try:
            balance = float((balances or {}).get(wid, 0) or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(balance):
            total += balance
    return total / supply * 100.0


def _flow_pair(flow) -> tuple[float, float] | None:
    """Normalise one flow record to (inflow, outflow) or None."""
    if not isinstance(flow, dict):
        return None
    inflow = flow.get("inflows", flow.get("in", flow.get("inflow")))
    outflow = flow.get("outflows", flow.get("out", flow.get("outflow")))
    if inflow is None and outflow is None:
        return None
    try:
        return float(inflow or 0), float(outflow or 0)
    except (TypeError, ValueError):
        return None


def classify_behavior(flows) -> str:
    """Classify a cluster's sampled wallets: distributing/holding/unknown.

    Signature: classify_behavior(flows) -> str
    ``flows`` is either {wallet: {"in": x, "out": y}} or a list of
    {"wallet": str, "in": x, "out": y}. A wallet has *activity* when
    in + out > 0, and is *distributing* when outflows > inflows.
    The cluster is "distributing" iff distributing_count * 2 >=
    active_count and distributing_count > 0; otherwise, when at
    least one sampled wallet has activity, it is "holding".
    Fail-closed: flows is None / empty, or no wallet has activity,
    returns "unknown" -- missing RPC data is not evidence of holding.
    """
    if not flows:
        return "unknown"
    if isinstance(flows, dict):
        records = list(flows.values())
    elif isinstance(flows, (list, tuple)):
        records = flows
    else:
        return "unknown"
    active = distributing = 0
    for record in records:
        pair = _flow_pair(record)
        if pair is None:
            continue
        inflow, outflow = pair
        if inflow + outflow <= 0:
            continue
        active += 1
        if outflow > inflow:
            distributing += 1
    if active == 0:
        return "unknown"
    if distributing > 0 and distributing * 2 >= active:
        return "distributing"
    return "holding"


@dataclass
class ClusterVerdict:
    """Result of :func:`evaluate` for the largest cluster found."""

    verdict: str  # "VETO" | "FLAG" | "PASS" | "UNKNOWN"
    vetoed: bool
    share_pct: float | None
    behavior: str
    cluster: list = field(default_factory=list)
    reason: str = ""


def verdict_for_share(share_pct: float, behavior: str) -> tuple[str, str]:
    """Pure band rule. Signature: verdict_for_share(share, behavior).

    share >= 50            -> VETO ("extreme share"), any behaviour
    20 <= share < 50 and
      behaviour distributing -> VETO
      behaviour otherwise    -> FLAG (including "unknown")
    share < 20             -> PASS
    """
    if share_pct >= EXTREME_SHARE_PCT:
        return "VETO", "extreme share"
    if share_pct >= ELEVATED_SHARE_PCT:
        if behavior == "distributing":
            return "VETO", "elevated share and distributing"
        return "FLAG", f"elevated share, behaviour={behavior}"
    return "PASS", "share below elevated band"


def evaluate_cluster(cluster, balances, total_supply, flows=None) -> ClusterVerdict:
    """Evaluate one cluster. Signature:
    evaluate_cluster(cluster, balances, total_supply, flows=None)
    Fail-closed: an unmeasurable share returns verdict "UNKNOWN",
    which callers must treat as not-a-pass (never as PASS).
    """
    share = cluster_share(cluster, balances, total_supply)
    behavior = classify_behavior(flows)
    if share is None:
        return ClusterVerdict("UNKNOWN", False, None, behavior,
                              list(cluster or []),
                              "supply missing or non-positive")
    verdict, reason = verdict_for_share(share, behavior)
    return ClusterVerdict(verdict, verdict == "VETO", share, behavior,
                          list(cluster or []), reason)


def evaluate(wallets, balances, total_supply, flows=None) -> ClusterVerdict:
    """Cluster all wallets and evaluate the largest-share cluster.

    Signature:
        evaluate(wallets: list[dict], balances: dict,
                 total_supply: float, flows=None) -> ClusterVerdict
    ``flows``, when given, is scoped to the winning cluster's wallets
    before classification. Fail-closed: no wallets or an
    unmeasurable supply returns verdict "UNKNOWN".
    """
    clusters = find_clusters(wallets)
    if not clusters:
        return ClusterVerdict("UNKNOWN", False, None,
                              classify_behavior(flows), [],
                              "no wallets supplied")
    best: ClusterVerdict | None = None
    for cluster in clusters:
        result = evaluate_cluster(cluster, balances, total_supply,
                                  _scoped_flows(flows, cluster))
        if best is None or (result.share_pct or -1) > (best.share_pct or -1):
            best = result
    return best


def _scoped_flows(flows, cluster):
    """Restrict a flows payload to one cluster's wallets."""
    if isinstance(flows, dict):
        return {w: flows[w] for w in cluster if w in flows}
    if isinstance(flows, (list, tuple)):
        return [f for f in flows
                if isinstance(f, dict) and f.get("wallet") in cluster]
    return None


def cluster_shares(clusters, balances, total_supply) -> list:
    """Share of every cluster: [(cluster, share_pct | None), ...].

    Signature: cluster_shares(clusters, balances, total_supply)
    Sorted by share descending; clusters whose share is unmeasurable
    (bad supply) sort last, and ties break on the first member so the
    output is deterministic.
    """
    rows = [(list(cluster), cluster_share(cluster, balances, total_supply))
            for cluster in clusters or []]
    rows.sort(key=lambda row: (row[1] is None, -(row[1] or 0.0),
                               row[0][0] if row[0] else ""))
    return rows


def summarize_clusters(wallets, balances, total_supply, flows=None) -> list:
    """A ClusterVerdict for *every* cluster, largest share first.

    Signature: summarize_clusters(wallets, balances, total_supply,
                                  flows=None) -> list[ClusterVerdict]
    :func:`evaluate` answers only for the largest cluster; this is
    the full picture -- a small distributing cluster still shows up
    here even when a bigger holding cluster takes the headline.
    Flows are scoped per cluster exactly as in :func:`evaluate`.
    Clusters with unmeasurable share sort last.
    """
    verdicts = [evaluate_cluster(cluster, balances, total_supply,
                                 _scoped_flows(flows, cluster))
                for cluster in find_clusters(wallets)]
    verdicts.sort(key=lambda v: (v.share_pct is None, -(v.share_pct or 0.0),
                                 v.cluster[0] if v.cluster else ""))
    return verdicts


_VERDICT_SEVERITY = {"PASS": 0, "FLAG": 1, "UNKNOWN": 2, "VETO": 3}


def worst_verdict(verdicts) -> str:
    """The most severe verdict name in ``verdicts``.

    Signature: worst_verdict(verdicts) -> str
    Accepts ClusterVerdict objects or plain verdict strings.
    Severity order: VETO > UNKNOWN > FLAG > PASS -- UNKNOWN outranks
    FLAG because unmeasurable is worse than measured-and-elevated.
    Fail-closed: an empty input, or any unrecognised verdict, yields
    "UNKNOWN" rather than a reassuring default.
    """
    worst = None
    for item in verdicts or []:
        name = getattr(item, "verdict", item)
        if name not in _VERDICT_SEVERITY:
            return "UNKNOWN"
        if worst is None or _VERDICT_SEVERITY[name] > _VERDICT_SEVERITY[worst]:
            worst = name
    return worst if worst is not None else "UNKNOWN"
