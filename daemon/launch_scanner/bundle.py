"""Bundle / insider detection via funding-graph clustering.

Public Solana RPC only, no key:

  1. getTokenLargestAccounts(mint) -> top holder token ACCOUNTS
  2. getAccountInfo on each account -> OWNER wallet (token accounts are
     usually ATAs; tracing the ATA itself misattributes funding)
  3. For each owner: getSignaturesForAddress(owner, limit=N). Fresh
     bundle wallets have few transactions, so the OLDEST signature
     (last in the list) is the funding transaction.
  4. getTransaction(oldest_sig, jsonParsed) -> find the SystemProgram
     transfer whose destination is the owner -> that source is the funder.
  5. Group owners by shared funder. Owners sharing a funder were created
     / funded by the same wallet = one coordinated cluster.

Verdict: cluster_pct = share of TOTAL token supply held by clusters of
size >= 2 (via getTokenSupply, not the top-N sum).
cluster_pct > max_cluster_pct -> bundled supply -> FAIL.

Hard wall-clock budget per call: a slow deep scan reports partial
results with unknown=True instead of stalling the scanner cycle.

Only runs as a DEEP scan when the cheap RugCheck gate already raised
insider/concentration flags — the full RPC walk is too slow to run on
every candidate. Never raises; unknown -> pass with a flag.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import tx_guards as _txg
except ImportError:  # direct scanner-module import
    import tx_guards as _txg

logger = logging.getLogger("forex_agent.daemon.launch_scanner.bundle")

_RPC_URL = "https://api.mainnet-beta.solana.com"
_TIMEOUT = 15
_MAX_HOLDERS = 10
_SIG_LIMIT = 200
_BUDGET_S = 45.0  # hard wall-clock budget per analyze() call
_OWNER_CACHE_TTL = 3600.0

_OWNER_CACHE: Dict[str, Tuple[str, float]] = {}


def _owner_of(token_account: str) -> Optional[str]:
    """Resolve a token ACCOUNT -> its OWNER wallet.

    getTokenLargestAccounts returns token accounts (usually ATAs), not
    wallets. Funding-graph analysis must trace the owner, otherwise every
    account trivially "traces" to whoever created its ATA and clusters
    never form (false negatives on real bundles).
    """
    now = time.time()
    hit = _OWNER_CACHE.get(token_account)
    if hit and now - hit[1] < _OWNER_CACHE_TTL:
        return hit[0]
    res = _rpc("getAccountInfo",
               [token_account, {"encoding": "jsonParsed"}])
    try:
        data = (res or {}).get("value") or {}
        parsed = (data.get("data") or {}).get("parsed") or {}
        owner = str((parsed.get("info") or {}).get("owner") or "")
    except Exception:
        owner = ""
    if owner:
        _OWNER_CACHE[token_account] = (owner, now)
        return owner
    return None


def _total_supply(mint: str) -> float:
    res = _rpc("getTokenSupply", [mint]) or {}
    val = (res.get("value") or {})
    return _f(val.get("uiAmount") if val.get("uiAmount") is not None
             else val.get("amount"))


def _rpc(method: str, params: List[Any]) -> Any:
    """Routine behavior/history reads use owner section 9 routing."""
    from daemon.intelligence.acquisition.routine import read
    return read(method, params, timeout=_TIMEOUT, background=True)



def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _oldest_signature(holder: str) -> Optional[str]:
    sigs = _rpc("getSignaturesForAddress",
                [holder, {"limit": _SIG_LIMIT}]) or []
    if not sigs:
        return None
    # Newest-first; last entry is the oldest we could see. If the wallet
    # has >= _SIG_LIMIT txs it is not a fresh bundle wallet — skip it.
    if len(sigs) >= _SIG_LIMIT:
        return None
    return (sigs[-1] or {}).get("signature")


def _funder_of(holder: str, signature: str) -> Optional[str]:
    """Who funded this holder? Scan the oldest tx for a system transfer
    into the holder; its source is the funder."""
    tx = _rpc("getTransaction",
              [signature, {"encoding": "jsonParsed",
                           "maxSupportedTransactionVersion": 0}])
    try:
        if not _txg.tx_succeeded(tx) or not _txg.response_signature_matches(tx, signature):
            return None
        ixs = ((tx or {}).get("transaction") or {}).get("message") or {}
        all_ixs = list(ixs.get("instructions") or [])
        for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
            all_ixs.extend(inner.get("instructions") or [])
        for ix in all_ixs:
            parsed = ix.get("parsed") or {}
            if parsed.get("type") != "transfer":
                continue
            info = parsed.get("info") or {}
            if str(info.get("destination")) == holder:
                src = str(info.get("source") or "")
                if src and src != holder:
                    return src
    except Exception:
        return None
    return None


def analyze(mint: str, max_cluster_pct: float = 30.0,
            max_holders: int = _MAX_HOLDERS,
            budget_s: float = _BUDGET_S) -> Dict[str, Any]:
    """Deep bundle scan for one mint. Returns verdict dict.

    Hard wall-clock budget (default 45s): when the budget is spent the
    scan stops and reports what it found with ``unknown=True`` — a slow
    deep scan must never stall the scanner cycle.
    """
    out = {"passed": True, "cluster_pct": 0.0, "clusters": [],
           "cluster_wallets": [],
           "flags": [], "fails": [], "unknown": False}
    deadline = time.time() + max(budget_s, 5.0)

    def _time_left() -> bool:
        return time.time() < deadline

    largest = _rpc("getTokenLargestAccounts", [mint]) or {}
    accounts = (largest.get("value") or [])[:max_holders]
    if not accounts:
        out["unknown"] = True
        out["flags"].append("holder data unavailable")
        return out

    total_supply = _total_supply(mint)
    holders: List[Tuple[str, float]] = []  # (owner_wallet, ui_amount)
    top_sum = 0.0
    for acc in accounts:
        if not _time_left():
            out["unknown"] = True
            out["flags"].append("bundle scan timed out (holder walk)")
            return out
        token_acct = str(acc.get("address") or "")
        amt = _f(acc.get("uiAmount") if acc.get("uiAmount") is not None
                 else acc.get("amount"))
        if not token_acct or amt <= 0:
            continue
        owner = _owner_of(token_acct) or token_acct  # fall back, flagged
        if owner == token_acct:
            out["flags"].append("owner resolution failed for a top holder")
        holders.append((owner, amt))
        top_sum += amt

    funder_of: Dict[str, str] = {}
    for owner, _amt in holders:
        if not _time_left():
            out["unknown"] = True
            out["flags"].append("bundle scan timed out (funding walk)")
            break
        sig = _oldest_signature(owner)
        if not sig:
            continue
        funder = _funder_of(owner, sig)
        if funder:
            funder_of[owner] = funder
        time.sleep(0.2)  # stay gentle on public RPC

    by_funder: Dict[str, List[Tuple[str, float]]] = {}
    for owner, amt in holders:
        funder = funder_of.get(owner)
        if funder:
            by_funder.setdefault(funder, []).append((owner, amt))

    denom = total_supply if total_supply > 0 else top_sum
    clustered_amt = 0.0
    clusters = []
    cluster_wallets: List[Dict[str, Any]] = []
    for funder, members in by_funder.items():
        if len(members) >= 2:
            amt = sum(a for _, a in members)
            clustered_amt += amt
            clusters.append({"funder": funder,
                             "wallets": len(members),
                             "members": [w for w, _a in members],
                             "pct_of_supply": round(100 * amt / denom, 2)
                             if denom else 0.0})
            for w, a in members:
                cluster_wallets.append({
                    "wallet": w, "amount": a,
                    "pct_of_supply": round(100 * a / denom, 2)
                    if denom else 0.0})
    cluster_wallets.sort(key=lambda e: e["amount"], reverse=True)
    out["cluster_wallets"] = cluster_wallets
    pct = round(100 * clustered_amt / denom, 2) if denom else 0.0
    out["cluster_pct"] = pct
    out["total_supply"] = total_supply
    out["clusters"] = sorted(clusters, key=lambda c: c["pct_of_supply"],
                             reverse=True)[:5]
    if pct > max_cluster_pct:
        out["passed"] = False
        out["fails"].append(
            "bundled supply: %.1f%% of total supply shares funders" % pct)
    elif pct > max_cluster_pct / 2:
        out["flags"].append("possible bundle: %.1f%% clustered" % pct)
    return out


# ------------------------------------------------------- cluster behavior
# Distribution-behavior check (user-approved change, 2026-10-20): a
# funding cluster's mere EXISTENCE is not a veto signal — in the
# largest launch dataset, bundled tokens were ~2x MORE likely to
# become large tokens. The signal is whether the cluster is DUMPING.
# cluster_distribution() samples the largest cluster wallets' recent
# activity for this token and reports net-selling vs holding.
# Fail-open doctrine: any data gap yields "unknown", never a
# veto-grade verdict — vetoes fire only on positive evidence.

_BEHAVIOR_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_BEHAVIOR_TTL_S = 120.0
_BEHAVIOR_MAX_WALLETS = 5
_BEHAVIOR_MAX_TXS = 25
_BEHAVIOR_BUDGET_S = 30.0


def _behavior_rpc(method: str, params: List[Any]) -> Any:
    """RPC for the behavior walk: Helius endpoint chain (discovery key
    -> main key -> public endpoint) via instant_safety's helper, which
    reads the same key files; falls back to this module's public-RPC
    _rpc when that import is unavailable. Never raises."""
    try:
        from . import instant_safety
        return instant_safety._rpc(method, params, 10.0)
    except Exception:
        return _rpc(method, params)


def _wallet_token_flow(wallet: str, mint: str,
                       deadline: float) -> Optional[Tuple[float, float]]:
    """(inflow, outflow) in raw token units for one wallet across its
    last ~25 token-related transactions, from pre/post token-balance
    deltas in parsed txs (exact direction — no DEX-program guessing).
    None when the wallet shows no activity in this token (or the data
    could not be fetched)."""
    sigs = _behavior_rpc("getSignaturesForAddress",
                         [wallet, {"limit": _BEHAVIOR_MAX_TXS}]) or []
    if not sigs:
        return None
    inflow = 0.0
    outflow = 0.0
    related = 0
    for entry in sigs:
        if time.time() > deadline:
            break
        sig = (entry or {}).get("signature")
        if not sig:
            continue
        tx = _behavior_rpc(
            "getTransaction",
            [sig, {"encoding": "jsonParsed",
                   "maxSupportedTransactionVersion": 0}])
        if not _txg.tx_succeeded(tx) or not _txg.response_signature_matches(tx, sig):
            continue
        meta = tx.get("meta") or {}
        pre: Dict[Any, float] = {}
        for b in meta.get("preTokenBalances") or []:
            if b.get("mint") == mint and b.get("owner") == wallet:
                pre[b.get("accountIndex")] = _f(
                    (b.get("uiTokenAmount") or {}).get("amount"))
        post_seen = False
        delta = 0.0
        for b in meta.get("postTokenBalances") or []:
            if b.get("mint") == mint and b.get("owner") == wallet:
                post_seen = True
                delta += _f((b.get("uiTokenAmount") or {}).get("amount")) \
                    - pre.get(b.get("accountIndex"), 0.0)
        if not post_seen and not pre:
            continue  # tx never touched this token for this wallet
        related += 1
        if not post_seen:
            outflow += sum(pre.values())  # balance gone: closed/sold out
        elif delta > 0:
            inflow += delta
        elif delta < 0:
            outflow += -delta
    if related == 0:
        return None
    return inflow, outflow


def cluster_distribution(mint: str,
                         wallets: List[str]) -> Dict[str, Any]:
    """Is this mint's funding cluster distributing (net selling)?

    Samples up to 5 of the largest cluster wallets (callers pass them
    largest-first, e.g. analyze()'s ``cluster_wallets``). A wallet is
    DISTRIBUTING when token outflows exceed inflows across its last
    ~25 token-related transactions; the cluster is distributing when
    >= half of the sampled wallets WITH any token activity are
    distributing. Returns {"verdict": "distributing"|"holding"|
    "unknown", "sampled", "distributing"} — "unknown" (RPC down, no
    activity) must fail open to a flag, never a veto. Cached 120s per
    mint. Never raises.
    """
    unknown = {"verdict": "unknown", "sampled": 0, "distributing": 0}
    if not mint or not wallets:
        return dict(unknown)
    now = time.time()
    hit = _BEHAVIOR_CACHE.get(mint)
    if hit and now - hit[0] < _BEHAVIOR_TTL_S:
        return hit[1]
    verdict = dict(unknown)
    try:
        deadline = now + _BEHAVIOR_BUDGET_S
        sampled = 0
        distributing = 0
        sample = [str(w) for w in wallets
                  if w][:_BEHAVIOR_MAX_WALLETS]
        for wallet in sample:
            if time.time() > deadline:
                break
            flow = _wallet_token_flow(wallet, mint, deadline)
            if flow is None:
                continue
            sampled += 1
            if flow[1] > flow[0]:
                distributing += 1
        if sampled > 0:
            verdict = {
                "verdict": "distributing"
                           if distributing * 2 >= sampled else "holding",
                "sampled": sampled,
                "distributing": distributing,
            }
    except Exception as exc:
        logger.warning("cluster_distribution %s failed: %s",
                       mint[:8], exc)
        verdict = dict(unknown)
    _BEHAVIOR_CACHE[mint] = (now, verdict)
    return verdict
