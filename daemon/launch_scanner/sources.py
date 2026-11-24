"""Launch-radar data sources: pump.fun, DexScreener boosts, Hyperliquid listings.

All functions are stdlib-only, never raise (return [] on any failure),
and normalize to dicts:

    {
        "mint": str | None,      # contract address (None for CEX listings)
        "symbol": str,           # display ticker (may be "")
        "name": str,
        "chain": str,            # solana | base | ethereum | robinhood | hyperliquid
        "source": str,           # pumpfun | dexscreener-boost | hyperliquid-listing
        "created_ts": float,     # epoch seconds (0 when unknown)
        "usd_market_cap": float, # 0 when unknown
        "extra": dict,           # source-specific fields
        "url": str,              # best link for the alert card
    }
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.sources")

# Canonical Solana program IDs / mints (Phase 2): the single source
# of truth is daemon/solana_ids.py — never redefine these literals.
try:
    from daemon import solana_ids as _solana_ids
except ImportError:  # bare-file import without the repo root on path
    import sys as _sys
    _root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from daemon import solana_ids as _solana_ids

_HTTP_TIMEOUT = 15
_UA = {"User-Agent": "forex-agent/1.0 (+paper-trading-bot)",
       "Connection": "close"}

# Bounded retry for flaky upstreams (DexScreener / Hyperliquid sometimes
# reset connections or truncate bodies). Never raises — returns None after
# the budget is spent so a bad poll can't stall the scanner cycle.
_RETRYABLE = (
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
)
try:
    import http.client as _http_client
    _RETRYABLE = _RETRYABLE + (
        _http_client.IncompleteRead,
        _http_client.RemoteDisconnected,
        _http_client.BadStatusLine,
    )
except Exception:  # pragma: no cover — stdlib always has http.client
    _http_client = None

_MAX_ATTEMPTS = 3
_BACKOFF_S = (1.0, 2.0)  # sleeps between attempts 1->2, 2->3

_PUMPFUN_URL = ("https://frontend-api-v3.pump.fun/coins?offset=0&limit=50"
                "&sort=created_timestamp&order=DESC&includeNsfw=false")
_DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
_DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
_DEX_BATCH_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{mints}"
_HL_URL = "https://api.hyperliquid.xyz/info"
_GECKO_NEW_POOLS_URL = ("https://api.geckoterminal.com/api/v2/networks/"
                       "{network}/new_pools")

_BOOST_CHAINS = {"solana", "base", "ethereum", "robinhood"}

# GeckoTerminal network id -> our chain name, plus the USDC mint/address on
# each network. USDC-quoted pools are matched by address (the new_pools
# payload carries no token symbols in `included`), never by name.
# GeckoTerminal network id -> our canonical chain name. The safety
# gate keys EVM checks off "base"/"ethereum" (see safety._CHAIN_IDS),
# so "eth" must map to "ethereum" — a raw "eth" chain would come back
# safety-unknown and be held silently by the fail-closed gate.
_GECKO_NETWORK_TO_CHAIN = {"solana": "solana", "base": "base",
                           "eth": "ethereum"}

_GECKO_USDC_NETWORKS = (
    ("solana", "solana",
     _solana_ids.USDC_MINT),
    ("base", "base",
     "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA4a966"),
    ("eth", "ethereum",
     "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
)


def _curl_bytes(url: str, data: Optional[bytes] = None,
                extra_headers: Optional[Dict[str, str]] = None,
                timeout: float = _HTTP_TIMEOUT) -> Optional[bytes]:
    """Fetch raw body bytes via curl (no shell — arg list only).

    VM egress quirk: Python's urllib through the MITM proxy intermittently
    fails against DexScreener (IncompleteRead / connection reset — currently
    ~100% failure) while curl succeeds consistently (10/10). Prefer curl
    when available; the urllib loop below is the fallback. Returns None on
    any failure (never raises).
    """
    if not shutil.which("curl"):
        return None
    curl = shutil.which("curl")
    cmd = [curl, "-sS", "--max-time", str(timeout)]
    headers = dict(_UA)
    headers.pop("Connection", None)  # curl manages its own connections
    if extra_headers:
        headers.update(extra_headers)
    for key, val in headers.items():
        cmd += ["-H", "%s: %s" % (key, val)]
    if data is not None:
        cmd += ["-X", "POST", "--data-binary", "@-"]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, input=data, capture_output=True,
                             timeout=_HTTP_TIMEOUT + 5, check=False)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    return out.stdout


def _get_json(url: str) -> Optional[Any]:
    raw = _curl_bytes(url)
    if raw is not None:
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            pass  # garbage payload — fall through to urllib
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_S[attempt])
        except Exception as exc:
            logger.warning("source fetch failed %s: %s", url.split("?")[0][:60], exc)
            return None
    logger.warning("source fetch failed after %d attempts %s: %s",
                   _MAX_ATTEMPTS, url.split("?")[0][:60], last_exc)
    return None


def _post_json(url: str, payload: Dict[str, Any]) -> Optional[Any]:
    raw = _curl_bytes(url, data=json.dumps(payload).encode(),
                      extra_headers={"Content-Type": "application/json"})
    if raw is not None:
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            pass  # garbage payload — fall through to urllib
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=body, headers=dict(
                _UA, **{"Content-Type": "application/json"}))
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_S[attempt])
        except Exception as exc:
            logger.warning("source post failed %s: %s", url.split("?")[0][:60], exc)
            return None
    logger.warning("source post failed after %d attempts %s: %s",
                   _MAX_ATTEMPTS, url.split("?")[0][:60], last_exc)
    return None


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------
# pump.fun — newest launches on Solana
# ----------------------------------------------------------------------
def pumpfun_new() -> List[Dict[str, Any]]:
    data = _get_json(_PUMPFUN_URL)
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        if not isinstance(c, dict):
            continue
        mint = str(c.get("mint") or "").strip()
        if not mint:
            continue
        out.append({
            "mint": mint,
            "symbol": str(c.get("symbol") or "").strip(),
            "name": str(c.get("name") or "").strip(),
            "chain": "solana",
            "source": "pumpfun",
            "created_ts": _f(c.get("created_timestamp")) / 1000.0,
            "usd_market_cap": _f(c.get("usd_market_cap") or c.get("market_cap_usd")),
            "extra": {
                "complete": bool(c.get("complete")),
                "reply_count": int(c.get("reply_count") or 0),
                "banned": bool(c.get("is_banned")),
                "creator": str(c.get("creator") or ""),
            },
            "url": "https://pump.fun/coin/%s" % mint,
        })
    return out


# ----------------------------------------------------------------------
# DexScreener — top boosted tokens (multi-chain runner visibility)
# ----------------------------------------------------------------------
def dexscreener_boosts() -> List[Dict[str, Any]]:
    data = _get_json(_DEX_BOOSTS_URL)
    if not isinstance(data, list):
        return []
    out = []
    for b in data:
        if not isinstance(b, dict):
            continue
        chain = str(b.get("chainId") or "").lower()
        if chain not in _BOOST_CHAINS:
            continue
        addr = str(b.get("tokenAddress") or "").strip()
        if not addr:
            continue
        links = b.get("links") or {}
        url = str(b.get("url") or "")
        if not url:
            url = "https://dexscreener.com/%s/%s" % (chain, addr)
        out.append({
            "mint": addr,
            "symbol": "",
            "name": str(b.get("description") or "")[:60],
            "chain": chain,
            "source": "dexscreener-boost",
            "created_ts": 0.0,
            "usd_market_cap": 0.0,
            "extra": {"boost_amount": b.get("totalAmount")},
            "url": url,
        })
    return out


def _pair_to_stats(top: Dict[str, Any]) -> Dict[str, Any]:
    from daemon.launch_scanner import provider_inputs as provenance
    evidence=provenance.begin('dexscreener',top)
    base = top.get("baseToken") or {}
    dex = str(top.get("dexId") or "")
    chain = str(top.get("chainId") or "")
    pair_addr = str(top.get("pairAddress") or "")
    txns_m5 = (top.get("txns") or {}).get("m5") or {}
    txns_h1 = (top.get("txns") or {}).get("h1") or {}
    _chg = top.get("priceChange") or {}
    result = {
        "symbol": str(base.get("symbol") or ""),
        "name": str(base.get("name") or ""),
        "usd_liquidity": _f((top.get("liquidity") or {}).get("usd")),
        "usd_market_cap": _f(top.get("marketCap") or top.get("fdv")),
        "price_usd": _f(top.get("priceUsd")),
        "vol_m5": _f((top.get("volume") or {}).get("m5")),
        "buys_m5": int(txns_m5.get("buys") or 0),
        "sells_m5": int(txns_m5.get("sells") or 0),
        # h1 windows (2026-10-21): the sniper tape fallback divides the
        # m5 rate by the h1 rate to measure acceleration from ONE
        # snapshot — additive fields, existing consumers unaffected.
        "vol_h1": _f((top.get("volume") or {}).get("h1")),
        "buys_h1": int(txns_h1.get("buys") or 0),
        "sells_h1": int(txns_h1.get("sells") or 0),
        "chg_m5": _f(_chg.get("m5")),
        "chg_h1": _f(_chg.get("h1")),
        # pair_created_ms (2026-11-23): pair creation time from the
        # DexScreener payload, needed by the entry-time manipulation
        # veto (bot-swarm fingerprint). None when the payload lacks
        # it - never guessed.
        "pair_created_ms": (_f(top.get("pairCreatedAt"))
                            if top.get("pairCreatedAt") is not None
                            else None),
        "url": ("https://dexscreener.com/%s/%s" % (chain, pair_addr)
                if chain and pair_addr else ""),
        "dex": dex,
        "chain": chain,
        # tape_source (2026-10-21): every tape dict names the venue it
        # was read from so journaling can say whether an entry/exit was
        # priced off dexscreener or the geckoterminal fallback.
        "tape_source": "dexscreener",
    }
    result[provenance.KEY]=provenance.finish(evidence,result)
    return result


def dexscreener_batch_stats(mints: List[str],
                            chain: str = "solana") -> Dict[str, Dict[str, Any]]:
    """Batch pair stats: one HTTP call for up to 30 mints.

    GET /tokens/v1/{chain}/{m1,m2,...} — free, no key. Returns
    {mint: stats}; mints with no indexed pair are simply absent.
    Highest-liquidity pair wins per mint.
    """
    out: Dict[str, Dict[str, Any]] = {}
    addrs = [m for m in dict.fromkeys(mints) if m][:30]
    if not addrs:
        return out
    data = _get_json(_DEX_BATCH_URL.format(chain=chain,
                                           mints=",".join(addrs)))
    if not isinstance(data, list):
        return out
    best: Dict[str, Dict[str, Any]] = {}
    for pair in data:
        base = pair.get("baseToken") or {}
        addr = str(base.get("address") or "")
        if not addr:
            continue
        stats = _pair_to_stats(pair)
        prev = best.get(addr.lower())
        if prev is None or stats["usd_liquidity"] > prev["usd_liquidity"]:
            best[addr.lower()] = stats
    for m in addrs:
        hit = best.get(m.lower())
        if hit:
            from daemon.launch_scanner import provider_inputs as provenance
            out[m] = provenance.resolved(hit,m,chain,'highest-liquidity-base-token-pair')
    return out


def dexscreener_token_stats(mint: str, chain: str = "solana") -> Optional[Dict[str, Any]]:
    """Resolve a mint -> {symbol, name, usd_liquidity, usd_market_cap, url}."""
    data = _get_json(_DEX_TOKEN_URL.format(mint=mint))
    pairs = (data or {}).get("pairs") or []
    if not pairs:
        return None
    pairs = sorted(pairs,
                   key=lambda p: _f((p.get("liquidity") or {}).get("usd")),
                   reverse=True)
    from daemon.launch_scanner import provider_inputs as provenance
    stats=_pair_to_stats(pairs[0])
    return provenance.resolved(stats,mint,chain,'highest-liquidity-single-token-pair')


# ----------------------------------------------------------------------
# GeckoTerminal — fallback tape when DexScreener is unreachable
# ----------------------------------------------------------------------
# 2026-10-21: DexScreener went fully dark from this VM (empty replies /
# timeouts) while RugCheck stayed green — and DexScreener is the single
# live-price source for the realtime tape, the CEO/mirror runner gates
# and entry quotes. All those paths fail-closed (safe, but blind).
# This layer gives them a second venue, with the SAME tape dict shape
# so every consumer works unchanged, and with fail-closed semantics
# preserved: fields GT cannot provide are None (never 0 or guessed),
# and if both venues are dark the mints are simply absent — identical
# to dexscreener_batch_stats-alone behavior.
_GT_TOKENS_MULTI_URL = ("https://api.geckoterminal.com/api/v2/networks/"
                        "{network}/tokens/multi/{addrs}")
_GT_NETWORK = {"solana": "solana", "base": "base", "ethereum": "eth"}
_GT_MAX_BATCH = 30            # GT multi-token cap per call
_GT_TIMEOUT_S = 10            # short: this VM's egress is flaky
_GT_MIN_GAP_S = 2.0           # per-process floor between GT calls
_GT_BUDGET_PER_MIN = 25       # shared cross-process budget (flock file)
_GT_DARK_AFTER = 3            # consecutive empty DS batches -> DS dark
_GT_DARK_TTL_S = 300.0        # dark latch TTL (per-process)

_GT_LAST_CALL = 0.0
_GT_DS_FAILS = 0              # consecutive empty DS results (per-process)
_GT_DS_DARK_UNTIL = 0.0       # per-process skip-DS latch deadline


def _gt_throttle_path() -> str:
    home = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
    return os.path.join(home, "gt_throttle.json")


def _gt_budget_ok() -> bool:
    """Claim one GT call from the shared 25/min cross-process budget.

    rt_sync, ceo-trader, mirror-exits and the scanner are separate
    processes — the budget lives in a flock-guarded state file so they
    share it. False = spend nothing (the fetch is skipped; callers
    degrade to DS-only behavior, i.e. today's behavior). Never raises.
    """
    import fcntl as _fcntl
    path = _gt_throttle_path()
    now = time.time()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a+") as f:
            _fcntl.flock(f, _fcntl.LOCK_EX)
            try:
                f.seek(0)
                st = json.load(f) or {}
            except Exception:
                st = {}
            win = float(st.get("win") or 0)
            used = int(st.get("used") or 0)
            if now - win >= 60.0:
                win, used = now, 0
            if used >= _GT_BUDGET_PER_MIN:
                return False
            f.seek(0)
            f.truncate()
            json.dump({"win": win, "used": used + 1}, f)
            return True
    except Exception:
        return False


def _gt_get_json(url: str) -> Optional[Any]:
    """Single-bounded GT fetch: curl-first (10s), one urllib shot, no
    retry loop — callers already have their own cadences. Never raises;
    None on any failure."""
    global _GT_LAST_CALL
    now = time.time()
    gap = _GT_MIN_GAP_S - (now - _GT_LAST_CALL)
    if gap > 0:
        time.sleep(gap)
    _GT_LAST_CALL = time.time()
    raw = _curl_bytes(url, timeout=_GT_TIMEOUT_S)
    if raw is not None:
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None  # garbage payload — not worth a urllib shot
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req,
                                    timeout=_GT_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _gt_token_to_stats(token: Dict[str, Any], chain: str,
                       network: str) -> Optional[Dict[str, Any]]:
    """GT token resource -> _pair_to_stats-shaped tape dict.

    The GT token endpoint carries NO transaction/buy-sell/volume
    windows — those fields are None (unknown), never 0-guessed.
    Consumers treat unknown txn data as fail-closed (tape_composite
    returns None without txn data; gates read only liq/mcap/momentum,
    where unknown momentum reads as 0 and blocks).
    """
    if not isinstance(token, dict):
        return None
    from daemon.launch_scanner import provider_inputs as provenance
    evidence=provenance.begin('geckoterminal',token)
    attrs = token.get("attributes") or {}
    addr = str(attrs.get("address") or "").strip()
    if not addr:
        return None
    price = _f(attrs.get("price_usd"))
    chg = attrs.get("price_change_percentage") or {}
    chg_m5 = chg.get("m5")
    chg_h1 = chg.get("h1")
    result = {
        "symbol": str(attrs.get("symbol") or ""),
        "name": str(attrs.get("name") or ""),
        "usd_liquidity": _f(attrs.get("total_reserve_in_usd")),
        "usd_market_cap": _f(attrs.get("market_cap_usd")
                             or attrs.get("fdv_usd")),
        "price_usd": price if price > 0 else None,
        "vol_m5": None,
        # pair_created_ms: GT's token payload carries no pair creation
        # time here - None (unknown), never guessed.
        "pair_created_ms": None,
        "buys_m5": None,
        "sells_m5": None,
        "vol_h1": None,
        "buys_h1": None,
        "sells_h1": None,
        "chg_m5": _f(chg_m5) if chg_m5 is not None else None,
        "chg_h1": _f(chg_h1) if chg_h1 is not None else None,
        "url": ("https://www.geckoterminal.com/%s/tokens/%s"
                % (network, addr)),
        "dex": "",
        "chain": chain,
        "tape_source": "geckoterminal",
    }
    result[provenance.KEY]=provenance.finish(evidence,result)
    return result


def geckoterminal_token_stats(mints: List[str],
                              chain: str = "solana"
                              ) -> Dict[str, Dict[str, Any]]:
    """Token-level tape from GeckoTerminal (price, liquidity, mcap,
    m5/h1 momentum). Up to 30 mints per call; respects the shared
    25/min budget (returns {} when the budget is spent). Same dict
    shape as dexscreener_batch_stats. Never raises.

    Known limitation, documented not hidden: GT's liquidity proxy is
    ``total_reserve_in_usd`` AGGREGATED across all pools (verified
    live: SOL reads ~1.6e10), while DexScreener's is the single best
    pair's. It can only make the liq leg of an entry gate PASS, never
    fail — so GT-sourced entries lean on the mcap + rising-momentum
    legs for selectivity, and momentum is frequently absent on GT
    (reads None -> the gate blocks). The DS latch keeps GT as the
    exception, not the rule."""
    out: Dict[str, Dict[str, Any]] = {}
    network = _GT_NETWORK.get((chain or "").lower())
    addrs = [m for m in dict.fromkeys(mints) if m][:_GT_MAX_BATCH]
    if not network or not addrs:
        return out
    if not _gt_budget_ok():
        logger.warning("GT fallback skipped: rate budget spent")
        return out
    data = _gt_get_json(_GT_TOKENS_MULTI_URL.format(
        network=network, addrs=",".join(addrs)))
    items = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out
    by_addr = {a.lower(): a for a in addrs}
    for tok in items:
        if not isinstance(tok, dict):
            continue
        addr = str(((tok.get("attributes") or {}).get("address"))
                   or "").strip().lower()
        orig = by_addr.get(addr)
        if not orig:
            continue
        st = _gt_token_to_stats(tok, (chain or "").lower(), network)
        if st:
            from daemon.launch_scanner import provider_inputs as provenance
            out[orig] = provenance.resolved(st,orig,chain,'token-resource-no-pair-selection')
    return out


def _ds_dark(now: float) -> bool:
    """True when DexScreener has failed repeatedly and the TTL latch
    says skip it (per-process). While dark, tape_batch_stats goes
    straight to GT instead of wasting calls on a dead venue."""
    return now < _GT_DS_DARK_UNTIL


def tape_batch_stats(mints: List[str],
                     chain: str = "solana") -> Dict[str, Dict[str, Any]]:
    """Batch tape with a GeckoTerminal fallback per mint.

    DexScreener is authoritative: it is tried first (unless the
    per-process dark latch is set after repeated failures), and the GT
    fallback fills ONLY mints DS did not return. Every dict carries
    ``tape_source`` ("dexscreener" | "geckoterminal"). Both venues dark
    -> those mints are absent, exactly like dexscreener_batch_stats
    alone. Unknown GT fields are None, never guessed. Never raises.
    """
    global _GT_DS_FAILS, _GT_DS_DARK_UNTIL
    out: Dict[str, Dict[str, Any]] = {}
    addrs = [m for m in dict.fromkeys(mints) if m][:30]
    if not addrs:
        return out
    now = time.time()
    if not _ds_dark(now):
        try:
            ds = dexscreener_batch_stats(addrs, chain=chain) or {}
        except Exception:
            ds = {}
        if ds:
            _GT_DS_FAILS = 0
        else:
            _GT_DS_FAILS += 1
            if _GT_DS_FAILS >= _GT_DARK_AFTER:
                _GT_DS_DARK_UNTIL = now + _GT_DARK_TTL_S
                logger.warning(
                    "DexScreener dark %d consecutive batches — "
                    "tape on GeckoTerminal fallback for %.0fs",
                    _GT_DS_FAILS, _GT_DARK_TTL_S)
        out.update(ds)
    missing = [m for m in addrs if m not in out]
    if missing:
        try:
            gt = geckoterminal_token_stats(missing, chain=chain) or {}
        except Exception:
            gt = {}
        out.update(gt)
    return out


# ----------------------------------------------------------------------
# GeckoTerminal — trending pools (established-coin momentum discovery)
# ----------------------------------------------------------------------
_GECKO_TRENDING_URL = ("https://api.geckoterminal.com/api/v2/networks/"
                       "{network}/trending_pools")


def geckoterminal_trending_pools(network: str = "solana"
                                 ) -> List[Dict[str, Any]]:
    """Pools people are actually trading right now (free endpoint).

    This is the established-coin momentum feed: unlike new_pools (birth
    hunting), trending_pools surfaces coins mid-run — the FWOG/NEET/
    ANTFUN-shaped moves. Each pool carries 1h/24h price change, 24h
    volume and liquidity so the momentum scanner can qualify runners
    and skip exhausted tops. Never raises.
    """
    out: List[Dict[str, Any]] = []
    try:
        data = _get_json(_GECKO_TRENDING_URL.format(network=network))
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for pool in data.get("data") or []:
        if not isinstance(pool, dict):
            continue
        try:
            attrs = pool.get("attributes") or {}
            rels = pool.get("relationships") or {}
            base_id = ((rels.get("base_token") or {}).get("data")
                       or {}).get("id")
            mint = _gecko_token_addr(base_id)
            if not mint:
                continue
            name = str(attrs.get("name") or "")
            symbol = (name.split("/")[0].strip() if "/" in name else "")
            chg = attrs.get("price_change_percentage") or {}
            vol = attrs.get("volume_usd") or {}
            pool_addr = str(attrs.get("address") or "")
            out.append({
                "pool_address": pool_addr,
                "mint": mint,
                "symbol": symbol[:20],
                "name": name[:60],
                "chain": _GECKO_NETWORK_TO_CHAIN.get(network, network),
                "chg_1h_pct": _f(chg.get("h1")),
                "chg_24h_pct": _f(chg.get("h24")),
                "vol_24h_usd": _f(vol.get("h24")),
                "liq_usd": _f(attrs.get("reserve_in_usd")),
                "usd_market_cap": _f(attrs.get("market_cap_usd")
                                     or attrs.get("fdv_usd")),
                "url": (("https://www.geckoterminal.com/%s/pools/%s"
                         % (network, pool_addr)) if pool_addr else ""),
            })
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------
# GeckoTerminal — newest pools quoted in USDC (organic launch discovery)
# ----------------------------------------------------------------------
def _gecko_token_addr(token_id: Any) -> str:
    """'solana_<mint>' / 'base_0x..' / 'eth_0x..' -> bare token address."""
    tid = str(token_id or "").strip()
    if "_" in tid:
        return tid.split("_", 1)[1].strip()
    return tid


def _gecko_created_ts(value: Any) -> float:
    try:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return 0.0
        import datetime as _dt
        return _dt.datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def geckoterminal_usdc_pools() -> List[Dict[str, Any]]:
    """Newest USDC-quoted pools per network (free GeckoTerminal endpoint).

    Covers the launch venues pump.fun never sees: Raydium/Meteora/Orca
    USDC pools on Solana, Aerodrome/Uniswap USDC pools on Base, Uniswap
    USDC pools on Ethereum. Candidates flow through the same safety
    gates and score bar as every other source.
    """
    out: List[Dict[str, Any]] = []
    for gecko_net, chain, usdc_addr in _GECKO_USDC_NETWORKS:
        data = _get_json(_GECKO_NEW_POOLS_URL.format(network=gecko_net))
        if not isinstance(data, dict):
            continue
        for pool in data.get("data") or []:
            if not isinstance(pool, dict):
                continue
            attrs = pool.get("attributes") or {}
            rels = pool.get("relationships") or {}
            quote_id = ((rels.get("quote_token") or {}).get("data")
                        or {}).get("id")
            if (_gecko_token_addr(quote_id).lower()
                    != usdc_addr.lower()):
                continue
            base_id = ((rels.get("base_token") or {}).get("data")
                       or {}).get("id")
            mint = _gecko_token_addr(base_id)
            if not mint:
                continue
            # Never alert on USDC itself (or a mislabeled quote leg).
            if mint.lower() == usdc_addr.lower():
                continue
            name = str(attrs.get("name") or "")
            symbol = name.split("/")[0].strip() if "/" in name else ""
            pool_addr = str(attrs.get("address") or "")
            out.append({
                "mint": mint,
                "symbol": symbol[:20],
                "name": name[:60],
                "chain": chain,
                "source": "gecko-usdc-pool",
                "created_ts": _gecko_created_ts(
                    attrs.get("pool_created_at")),
                "usd_market_cap": _f(attrs.get("market_cap_usd")
                                     or attrs.get("fdv_usd")),
                "extra": {
                    "pool_address": pool_addr,
                    "dex": ((rels.get("dex") or {}).get("data")
                            or {}).get("id"),
                },
                "url": ("https://www.geckoterminal.com/%s/pools/%s"
                        % (gecko_net, pool_addr)) if pool_addr else "",
            })
    return out
# ----------------------------------------------------------------------
# GeckoTerminal — raw newest pools (graduation detection, 2026-10-21)
# ----------------------------------------------------------------------
def geckoterminal_new_pools(network: str = "solana",
                            pages: int = 2) -> List[Dict[str, Any]]:
    """Newest pools on one network, ALL quote tokens, newest first.

    Unlike geckoterminal_usdc_pools (USDC-quoted discovery only), this
    returns every fresh pool with its dex id and creation time so
    callers can detect venue events — the grad lane (grad.py) watches
    for pump.fun graduations: a `pump`-suffixed mint whose FIRST real
    pool (PumpSwap / Raydium) was created moments ago. The bonding
    curve itself shows up here too (dex id "pump-fun") and is the
    caller's job to exclude. Never raises.
    """
    out: List[Dict[str, Any]] = []
    for page in range(1, max(1, int(pages)) + 1):
        url = _GECKO_NEW_POOLS_URL.format(network=network)
        if page > 1:
            url += "?page=%d" % page
        data = _get_json(url)
        if not isinstance(data, dict):
            break
        pools = data.get("data") or []
        if not pools:
            break
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            try:
                attrs = pool.get("attributes") or {}
                rels = pool.get("relationships") or {}
                base_id = ((rels.get("base_token") or {}).get("data")
                           or {}).get("id")
                mint = _gecko_token_addr(base_id)
                if not mint:
                    continue
                name = str(attrs.get("name") or "")
                symbol = (name.split("/")[0].strip()
                          if "/" in name else "")
                pool_addr = str(attrs.get("address") or "")
                out.append({
                    "pool_address": pool_addr,
                    "mint": mint,
                    "symbol": symbol[:20],
                    "name": name[:60],
                    "chain": _GECKO_NETWORK_TO_CHAIN.get(network,
                                                         network),
                    "dex": str(((rels.get("dex") or {}).get("data")
                                or {}).get("id") or ""),
                    "created_ts": _gecko_created_ts(
                        attrs.get("pool_created_at")),
                    "reserve_usd": _f(attrs.get("reserve_in_usd")),
                    "fdv_usd": _f(attrs.get("fdv_usd")
                                  or attrs.get("market_cap_usd")),
                    "price_usd": _f(attrs.get("base_token_price_usd")),
                    "source": "gecko-new-pool",
                })
            except Exception:
                continue
    return out


# ----------------------------------------------------------------------
# Hyperliquid — new spot listings
# ----------------------------------------------------------------------
def hyperliquid_listings() -> List[Dict[str, Any]]:
    data = _post_json(_HL_URL, {"type": "spotMeta"})
    tokens = (data or {}).get("tokens") or []
    out = []
    for t in tokens:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name or name in ("USDC", "USDT"):
            continue
        out.append({
            "mint": None,
            "symbol": name,
            "name": name,
            "chain": "hyperliquid",
            "source": "hyperliquid-listing",
            "created_ts": 0.0,
            "usd_market_cap": 0.0,
            "extra": {},
            "url": "https://app.hyperliquid.xyz/trade/%s-SPOT" % name,
        })
    return out
