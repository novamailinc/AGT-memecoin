#!/usr/bin/env python3
"""Funding-source clustering (2026-10-23): which wallets share one actor?

MEASUREMENT / DISPLAY ONLY. Nothing in this module gates, vetoes,
sizes, or blocks any trade, card, or entry. It is a registry other
code may READ for display (e.g. dossier surfacing); no caller may
turn a cluster into a decision input without a separate, explicit,
user-approved change.

Idea: a fresh wallet's FIRST funder — the wallet that first sent it
SOL — almost always belongs to whoever operates the wallet. Wallets
that share a first funder (directly, or through a funding chain) are
therefore probably one actor running many wallets. Clustering is
union-find over wallet -> funder mappings.

Registry: one JSON file, ~/.forex-agent/funding_clusters.json ::

    {"version": 1, "updated_ts": float,
     "funders": {wallet: {"funder": <addr|null>, "ts": float,
                          "source": "helius" | "seed" | ...}},
     "clusters": {cluster_id: {"funder": <addr|null>,
                               "members": [wallet, ...],
                               "size": int}},
     "wallet_cluster": {wallet: cluster_id}}

A wallet whose funder is null is ATTEMPTED-but-unknown (resolution
failed or the wallet has too much history to reach its first tx);
it forms no cluster. A cluster exists only where >= 2 TRACKED
wallets (wallets present in "funders") land in one union-find
component. Singles return None from cluster_of() — being tracked is
not the same as being clustered.

cluster_id is derived from the component's ultimate funder (walk
the funder chain to its root; cycle-safe), so adding a member does
not rename the cluster. When a component has no resolvable root
(pure cycle), the id falls back to a hash of its sorted members.

Resolution (resolve_first_funder): Helius RPC
getSignaturesForAddress walked newest -> oldest via `before`
cursors (bounded pages), then getTransaction (jsonParsed) on the
OLDEST signature, parsed for the earliest funding transfer — a
parsed system transfer / createAccount whose destination is the
wallet; its source is the funder (the bundle.py pattern). Fallback:
the tx fee payer when it differs from the wallet (the deployers.py
heuristic). Key loading prefers the DISCOVERY key
(HELIUS_API_KEY_DISCOVERY env / helius/api_key_discovery file) and
falls back to the main key — this is batch/background work and must
not contend with live signals; key values are never logged. Any
error, timeout, or malformed input resolves to None (unknown):
this module never raises into callers.

Storage follows the trials.py pattern: HOME from FOREX_AGENT_HOME,
REGISTRY_PATH module-level (tests reassign it to isolate), every
read-modify-write under an exclusive flock on a sibling lock file,
writes atomic tmp+rename. Reads never raise.
"""
import hashlib
import json
import logging
import os
import time
import urllib.request

# Transaction-outcome guards (2026-11-17, Batch 3): meta.err
# gating (tx_guards.py). A reverted tx funds nothing.
try:
    import tx_guards as _txg
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import tx_guards as _txg
    except Exception:
        raise

# Helius pacer (2026-11-17 pacing fix): every Helius URL takes a
# pacer token first (see helius_ratelimit.SharedPacer).
try:
    import helius_ratelimit as _helius_ratelimit
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import helius_ratelimit \
            as _helius_ratelimit
    except Exception:
        _helius_ratelimit = None  # type: ignore

# Canonical Solana program IDs / mints (Phase 2, D-05): the single
# source of truth is daemon/solana_ids.py — never redefine these
# literals here. The old Token-2022 literal in _NON_ACTORS was WRONG
# (``...QZWM1GjTbv...``), so the real Token-2022 program was not
# excluded as a non-actor.
try:
    from daemon import solana_ids as _solana_ids
except ImportError:  # bare-file import without the repo root on path
    import os as _os, sys as _sys
    _root = _os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from daemon import solana_ids as _solana_ids

logger = logging.getLogger("forex_agent.daemon.launch_scanner.funding_clusters")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
REGISTRY_PATH = os.path.join(HOME, "funding_clusters.json")

MIN_CLUSTER_SIZE = 2
_MEMBERS_SAMPLE = 10
_RPC_TIMEOUT = 12
_SIG_PAGE = 100
_MAX_PAGES = 10          # 1000 signatures of history is plenty for a
                         # "fresh" wallet; beyond that -> unknown,
                         # never a guessed funder.
_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

# Wallets that are infrastructure, not actors: a shared funder that is
# a system program would cluster the world. Never union these.
_NON_ACTORS = {
    _solana_ids.SYSTEM_PROGRAM_ID,           # System Program
    _solana_ids.TOKEN_PROGRAM_ID,
    _solana_ids.TOKEN2022_PROGRAM_ID,
}


def _home():
    """Evidence/seed files live beside the registry (which itself
    lives in the agent home), so reassigning REGISTRY_PATH in tests
    isolates everything this module reads."""
    return os.path.dirname(REGISTRY_PATH) or HOME


def is_plausible_wallet(w):
    """Cheap shape check: base58 string of 32..44 chars. Pure."""
    try:
        return (isinstance(w, str) and 32 <= len(w) <= 44
                and all(c in _B58 for c in w))
    except Exception:
        return False


# ---- storage (trials.py pattern: flock + atomic write) ---------------------

import fcntl


def _lock_path():
    return REGISTRY_PATH + ".lock"


class _registry_lock:
    """Exclusive inter-process lock spanning a read-modify-write."""

    def __enter__(self):
        parent = os.path.dirname(REGISTRY_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lf = open(_lock_path(), "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
        finally:
            self._lf.close()
        return False


def _blank():
    return {"version": 1, "updated_ts": 0.0, "funders": {},
            "clusters": {}, "wallet_cluster": {}}


def _read_registry():
    """Bare registry read. Hold _registry_lock if you will modify.
    Never raises: missing/corrupt reads as a blank registry."""
    try:
        with open(REGISTRY_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("funders"), dict):
            out = _blank()
            out.update(d)
            if not isinstance(out.get("clusters"), dict):
                out["clusters"] = {}
            if not isinstance(out.get("wallet_cluster"), dict):
                out["wallet_cluster"] = {}
            return out
    except Exception:
        pass
    return _blank()


def _write_registry(reg):
    """Bare registry write (atomic tmp+rename). Hold _registry_lock."""
    try:
        tmp = REGISTRY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(reg, f)
        os.replace(tmp, REGISTRY_PATH)
        return True
    except Exception:
        return False


def load_registry():
    """The full registry dict. Never raises."""
    try:
        with _registry_lock():
            return _read_registry()
    except Exception:
        return _read_registry()


# ---- union-find + cluster rebuild ------------------------------------------

class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        p = self.parent.setdefault(x, x)
        if p != x:
            self.parent[x] = p = self.find(p)
        return p

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _ultimate_funder(wallet, funders):
    """Walk wallet -> funder -> funder's funder ... to the root.
    Cycle-safe; None when the chain ends at an unknown funder."""
    seen = set()
    cur = wallet
    root = None
    while cur and cur not in seen:
        seen.add(cur)
        entry = funders.get(cur)
        f = entry.get("funder") if isinstance(entry, dict) else None
        if not f:
            break
        root = f
        cur = f
    return root


def rebuild_clusters(funders=None):
    """Pure rebuild: {wallet: {funder}} mappings -> (clusters,
    wallet_cluster). Only components holding >= MIN_CLUSTER_SIZE
    tracked wallets become clusters. Never raises."""
    try:
        funders = funders if funders is not None else {}
        uf = _UnionFind()
        for w, entry in funders.items():
            if not is_plausible_wallet(w):
                continue
            uf.find(w)
            f = entry.get("funder") if isinstance(entry, dict) else None
            if f and f != w and f not in _NON_ACTORS \
                    and is_plausible_wallet(f):
                uf.union(w, f)
        comps = {}
        for w in funders:
            if is_plausible_wallet(w):
                comps.setdefault(uf.find(w), []).append(w)
        clusters, wallet_cluster = {}, {}
        for _root, members in comps.items():
            if len(members) < MIN_CLUSTER_SIZE:
                continue
            members = sorted(members)
            rep = members[0]
            ultimate = _ultimate_funder(rep, funders)
            if ultimate:
                cid = "fc-" + hashlib.sha256(
                    ultimate.encode("utf-8")).hexdigest()[:16]
            else:
                cid = "fc-" + hashlib.sha256(
                    "|".join(members).encode("utf-8")).hexdigest()[:16]
            # The reported funder: the ultimate funder when the whole
            # component chains to one; else the most common direct
            # funder among members (honest best label, documented).
            direct = [(funders.get(m) or {}).get("funder") for m in members]
            direct = [d for d in direct if d]
            funder = ultimate if ultimate else (
                max(set(direct), key=direct.count) if direct else None)
            clusters[cid] = {"funder": funder, "members": members,
                             "size": len(members)}
            for m in members:
                wallet_cluster[m] = cid
        return clusters, wallet_cluster
    except Exception as e:
        logger.warning("funding rebuild_clusters failed: %s", e)
        return {}, {}


def _rebuild_in_place(reg):
    clusters, wallet_cluster = rebuild_clusters(reg.get("funders") or {})
    reg["clusters"] = clusters
    reg["wallet_cluster"] = wallet_cluster
    reg["updated_ts"] = time.time()
    return reg


# ---- mutation API -----------------------------------------------------------

def record_funder(wallet, funder, ts=None, source="helius"):
    """Record one wallet -> funder resolution (funder None = attempted,
    unknown) and rebuild clusters. Returns the stored entry or None
    on invalid input. Never raises."""
    try:
        wallet = str(wallet or "").strip()
        if not is_plausible_wallet(wallet):
            return None
        if funder is not None:
            funder = str(funder).strip()
            if not is_plausible_wallet(funder) or funder == wallet:
                funder = None
        entry = {"funder": funder,
                 "ts": float(ts) if ts else time.time(),
                 "source": str(source or "helius")}
        with _registry_lock():
            reg = _read_registry()
            reg.setdefault("funders", {})[wallet] = entry
            _rebuild_in_place(reg)
            _write_registry(reg)
        return dict(entry)
    except Exception as e:
        logger.warning("funding record_funder failed: %s", e)
        return None


def ingest_mappings(mappings, ts=None, source="seed"):
    """Bulk-ingest {wallet: funder|None}; existing HEURISTIC entries
    are only filled, never overwritten by a seed None. Rebuilds once.
    Returns how many entries are stored afterwards. Never raises."""
    n = 0
    try:
        with _registry_lock():
            reg = _read_registry()
            fund = reg.setdefault("funders", {})
            for w, f in (mappings or {}).items():
                w = str(w or "").strip()
                if not is_plausible_wallet(w) or w in fund:
                    continue
                if f is not None:
                    f = str(f).strip()
                    if not is_plausible_wallet(f) or f == w:
                        f = None
                fund[w] = {"funder": f,
                           "ts": float(ts) if ts else time.time(),
                           "source": str(source or "seed")}
            _rebuild_in_place(reg)
            _write_registry(reg)
            n = len(fund)
    except Exception as e:
        logger.warning("funding ingest_mappings failed: %s", e)
    return n


def refresh_clusters():
    """Rebuild clusters from stored mappings (e.g. after a seed
    import). Returns stats(). Never raises."""
    try:
        with _registry_lock():
            reg = _read_registry()
            _rebuild_in_place(reg)
            _write_registry(reg)
    except Exception as e:
        logger.warning("funding refresh_clusters failed: %s", e)
    return stats()


# ---- query API (display only) ----------------------------------------------

def cluster_of(wallet):
    """{cluster_id, size, members_sample, funder} for a clustered
    wallet, else None (unknown, untracked, or singleton). Never
    raises."""
    try:
        wallet = str(wallet or "").strip()
        if not wallet:
            return None
        reg = load_registry()
        cid = (reg.get("wallet_cluster") or {}).get(wallet)
        if not cid:
            return None
        c = (reg.get("clusters") or {}).get(cid) or {}
        members = c.get("members") or []
        return {"cluster_id": cid,
                "size": int(c.get("size") or len(members)),
                "members_sample": list(members)[:_MEMBERS_SAMPLE],
                "funder": c.get("funder")}
    except Exception:
        return None


def clusters_for_wallets(wallets):
    """{wallet: cluster_of(wallet)} for the clustered subset of a
    wallet list — the dossier-surfacing helper. Never raises."""
    out = {}
    try:
        for w in wallets or []:
            info = cluster_of(w)
            if info:
                out[str(w)] = info
    except Exception:
        pass
    return out


def known_funder(wallet):
    """The stored entry for a wallet ({funder, ts, source}) or None
    when never attempted. Never raises."""
    try:
        reg = load_registry()
        e = (reg.get("funders") or {}).get(str(wallet or "").strip())
        return dict(e) if isinstance(e, dict) else None
    except Exception:
        return None


def stats():
    """Registry headline numbers. Never raises."""
    try:
        reg = load_registry()
        fund = reg.get("funders") or {}
        return {
            "wallets_tracked": len(fund),
            "wallets_resolved": sum(
                1 for e in fund.values()
                if isinstance(e, dict) and e.get("funder")),
            "wallets_unknown": sum(
                1 for e in fund.values()
                if isinstance(e, dict) and not e.get("funder")),
            "clusters": len(reg.get("clusters") or {}),
            "clustered_wallets": len(reg.get("wallet_cluster") or {}),
        }
    except Exception:
        return {"wallets_tracked": 0, "wallets_resolved": 0,
                "wallets_unknown": 0, "clusters": 0,
                "clustered_wallets": 0}


# ---- first-funder resolver (batch/background; NOT a hot path) ---------------

def _helius_key():
    """Discovery key first (batch work must not contend with live
    signals), main key fallback — the reserves.py pattern. The value
    is never logged or returned to callers."""
    for env, fn in (("HELIUS_API_KEY_DISCOVERY", "api_key_discovery"),
                    ("HELIUS_API_KEY", "api_key")):
        v = os.environ.get(env)
        if v:
            return v.strip()
        try:
            p = os.path.join(_home(), "helius", fn)
            if os.path.exists(p):
                return open(p).read().strip()
        except Exception:
            pass
    return ""


def _rpc(method, params, timeout=_RPC_TIMEOUT):
    """One JSON-RPC call: Helius (keyed) first, public endpoint as
    fallback. Result or None. Never raises. Module-level so tests
    can monkeypatch the transport wholesale.

    Pacing (2026-11-17): the Helius leg takes a pacer token for
    its key's bucket first; no token -> the public leg carries the
    read. A 429 reports to the pacer (progressive backoff + single
    probe); a success clears the cooldown."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    urls = []
    k = _helius_key()
    bucket = ""
    if k:
        urls.append("https://mainnet.helius-rpc.com/?api-key=" + k)
        if _helius_ratelimit is not None:
            bucket = _helius_ratelimit.bucket_id_for_key(k)
    urls.append("https://api.mainnet-beta.solana.com")
    for url in urls:
        is_helius = "helius-rpc.com" in url
        if is_helius and bucket and _helius_ratelimit is not None:
            if not _helius_ratelimit.pacer_acquire(bucket):
                continue  # pacer: no token — public leg carries it
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0",
                         "Connection": "close"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if isinstance(d, dict) and d.get("result") is not None:
                if is_helius and bucket \
                        and _helius_ratelimit is not None:
                    _helius_ratelimit.pacer_report(bucket, True)
                return d["result"]
        except Exception as exc:
            if is_helius and bucket \
                    and _helius_ratelimit is not None \
                    and getattr(exc, "code", None) == 429:
                _helius_ratelimit.pacer_report(bucket, False)
            continue
    return None


def parse_funder_from_tx(tx, wallet):
    """Pure: the first funder of `wallet` visible in one parsed tx.

    Looks through top-level AND inner instructions for a parsed
    system transfer (or createAccount) INTO the wallet — that
    source is the funder (bundle.py pattern). Fallback: the fee
    payer (accountKeys[0]) when it is not the wallet itself
    (deployers.py heuristic). None when nothing qualifies.
    Never raises."""
    try:
        if not isinstance(tx, dict) or not wallet:
            return None
        # meta.err gate (2026-11-17, Batch 3): a reverted tx
        # funds nothing — its transfers never landed.
        if not _txg.tx_succeeded(tx):
            return None
        msg = ((tx.get("transaction") or {}).get("message") or {})
        ixs = list(msg.get("instructions") or [])
        for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
            if isinstance(inner, dict):
                ixs.extend(inner.get("instructions") or [])
        for ix in ixs:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            if parsed.get("type") == "transfer":
                if str(info.get("destination") or "") == wallet:
                    src = str(info.get("source") or "")
                    if src and src != wallet:
                        return src
            elif parsed.get("type") == "createAccount":
                if str(info.get("newAccount") or "") == wallet:
                    src = str(info.get("source") or "")
                    if src and src != wallet:
                        return src
        keys = msg.get("accountKeys") or []
        if keys:
            first = keys[0]
            payer = str((first or {}).get("pubkey") or first or "")
            if payer and payer != wallet:
                return payer
        return None
    except Exception:
        return None


def _oldest_signature(wallet, rpc, max_pages=_MAX_PAGES):
    """Oldest signature reachable by walking `before` cursors,
    bounded to max_pages pages. None when history exceeds the bound
    (we refuse to guess a 'first' funder we could not reach) or on
    any failure. Never raises."""
    try:
        before = None
        oldest = None
        for _ in range(max(1, int(max_pages))):
            opts = {"limit": _SIG_PAGE}
            if before:
                opts["before"] = before
            batch = rpc("getSignaturesForAddress", [wallet, opts])
            if not isinstance(batch, list) or not batch:
                break
            last = batch[-1]
            sig = last.get("signature") if isinstance(last, dict) else None
            if not sig:
                break
            oldest = sig
            before = sig
            if len(batch) < _SIG_PAGE:
                return oldest          # reached the true end of history
        return None                    # history deeper than the bound
    except Exception:
        return None


def resolve_first_funder(wallet, rpc=None, max_pages=_MAX_PAGES):
    """Resolve a wallet's first funder. `rpc` is an injectable
    callable(method, params) -> result (tests pass fakes; the live
    default is _rpc). Fail-soft: bad input, transport errors, and
    over-long histories all return None. Never raises."""
    try:
        wallet = str(wallet or "").strip()
        if not is_plausible_wallet(wallet):
            return None
        rpc = rpc or _rpc
        sig = _oldest_signature(wallet, rpc, max_pages=max_pages)
        if not sig:
            return None
        tx = rpc("getTransaction",
                 [sig, {"encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0}])
        funder = parse_funder_from_tx(tx, wallet)
        if funder and not is_plausible_wallet(funder):
            return None
        return funder
    except Exception:
        return None


def seed_from_deployer_funders(path=None):
    """Import KNOWN (non-null) funders from the legacy
    launch_scanner/deployer_funders.json cache. Its null entries are
    deliberately NOT imported: that cache's resolver never requested
    jsonParsed transactions, so its nulls mean 'not parsed', not
    'no funder' — those wallets stay eligible for real resolution.
    Returns the number of mappings imported. Never raises."""
    try:
        path = path or os.path.join(
            _home(), "launch_scanner", "deployer_funders.json")
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return 0
        known = {}
        for w, e in d.items():
            f = (e or {}).get("funder") if isinstance(e, dict) else None
            if f:
                known[w] = f
        if known:
            ingest_mappings(known, source="seed:deployer_funders")
        return len(known)
    except Exception:
        return 0
