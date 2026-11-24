"""LP-lock badge for EVM runner cards.

Free, stdlib-only, never raises. Answers one question for the card:
"is this pool's liquidity provably out of anyone's hands?"

How it checks (Uniswap-V2-style pools only — the pair contract IS the
ERC-20 LP token there):

    eth_call totalSupply()          on the pair contract
    eth_call balanceOf(0x...dEaD)    on the pair contract

    burned_share >= 90%  ->  locked      ("LP burned xx%")
    otherwise            ->  unverified  ("LP lock unverified")

Concentrated-liquidity pools (Uniswap V3/V4, Aerodrome Slipstream) have
no LP token at all, so there is nothing to verify on-chain without a
paid indexer — the badge says so honestly instead of guessing.

Design notes:
  - Display-only. This NEVER gates a card: "unverified" is not "bad",
    and locker-held LP (Unicrypt/TeamFinance/...) also counts as locked
    but is not auto-checked in v1 — the badge says "unverified", not
    "unlocked". Absence of a lock remains a safety-pipeline concern,
    not a card-blocker.
  - Runs at most ~1/day per feed (only for the winning candidate, after
    all gates pass), so free public RPCs are fine. curl-first through
    the VM egress proxy (same quirk as safety.py), urllib fallback.
  - Every failure mode returns {"status": "unverified"} — a card with
    no badge info is better than a crashed feed.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import urllib.request
from typing import Any, Dict, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.lp_lock")

# V2-style DEX ids whose pair contract doubles as the ERC-20 LP token.
_V2_DEXES = {
    "base": ("uniswap-v2-base", "uniswap_v2", "sushiswap", "base-swap",
             "baseswap"),
    "ethereum": ("uniswap_v2", "uniswap-v2-ethereum", "sushiswap",
                 "sushiswap-ethereum"),
}

# Free public RPCs. Only ~2 eth_calls per card-fire; rate limits are a
# non-issue at that volume. curl-first (egress-proxy quirk, see safety.py).
#
# Base endpoints are intermittently unreachable from this network
# (egress-level: on 2026-10-18 all 8 candidates below timed out on a
# live probe while api.hyperliquid.xyz stayed reachable). The list is
# deliberately wide — _v2_burned_share tries each with 1 retry and stays
# fail-soft, so a dead endpoint only costs its timeouts. Re-probe live
# before reordering "fastest-first".
_RPCS = {
    "base": ("https://mainnet.base.org",
             "https://base.publicnode.com",
             "https://base.llamarpc.com",
             "https://rpc.base.org",
             "https://base.meowrpc.com",
             "https://base-rpc.publicnode.com",
             "https://1rpc.io/base",
             "https://base.drpc.org"),
    "ethereum": ("https://ethereum.publicnode.com",
                 "https://rpc.ankr.com/eth"),
}

_RPC_RETRIES = 1  # one retry per endpoint before moving to the next

_DEAD = "0x000000000000000000000000000000000000dEaD"
_BURNED_PCT = 0.90  # >=90% of LP supply in the dead address = locked forever

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TOTAL_SUPPLY = "0x18160ddd"
_DECIMALS = "0x313ce567"
_BALANCE_OF_DEAD = ("0x70a08231"
                    "00000000000000000000000000000000000000000000000000000000"
                    "000000000000000000000000000000000000dEaD")

# Solana getTokenSupply is a cheap call public RPCs serve happily
# (unlike getTransaction, which 403s). Only used for card display.
_SOL_RPCS = ("https://api.mainnet-beta.solana.com",)

_UA = {"User-Agent": "forex-agent/1.0 (+paper-trading-bot)",
       "Accept": "application/json"}


def _post_json(url: str, payload: Dict[str, Any],
               timeout: float) -> Any:
    """POST JSON via curl (no shell), urllib fallback. None on failure."""
    body = json.dumps(payload).encode()
    if shutil.which("curl"):
        try:
            out = subprocess.run(
                ["curl", "-sS", "--max-time", str(int(timeout) + 2),
                 "-X", "POST", "-H", "Content-Type: application/json",
                 "-H", "User-Agent: %s" % _UA["User-Agent"],
                 "--data-binary", "@-",
                 url],
                input=body, capture_output=True, timeout=timeout + 5)
            if out.returncode == 0 and out.stdout:
                return json.loads(out.stdout.decode())
        except Exception:
            pass
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type":
                                              "application/json",
                                              **_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None
    return None


def _eth_call(rpc: str, to_addr: str, data: str,
              timeout: float) -> int | None:
    """One eth_call returning a uint256, or None. Never raises."""
    try:
        res = _post_json(rpc, {"jsonrpc": "2.0", "id": 1,
                               "method": "eth_call",
                               "params": [{"to": to_addr, "data": data},
                                          "latest"]},
                         timeout)
        val = (res or {}).get("result")
        if isinstance(val, str) and val.startswith("0x"):
            return int(val, 16)
    except Exception:
        pass
    return None


def _eth_call_retry(rpc: str, to_addr: str, data: str,
                    timeout: float) -> int | None:
    """eth_call with one retry per endpoint. Returns the uint256 (0 is a
    valid answer), or None when every attempt failed. Never raises."""
    for _ in range(_RPC_RETRIES + 1):
        try:
            val = _eth_call(rpc, to_addr, data, timeout)
        except Exception:
            val = None
        if val is not None:
            return val
    return None


def _v2_burned_share(chain: str, pair: str,
                     timeout: float) -> float | None:
    """Fraction of the V2 LP supply held by the dead address. None = unknown."""
    for rpc in _RPCS.get(chain, ()):  # try each RPC, first success wins
        total = _eth_call_retry(rpc, pair, _TOTAL_SUPPLY, timeout)
        if not total:
            continue
        burned = _eth_call_retry(rpc, pair, _BALANCE_OF_DEAD, timeout)
        if burned is None:
            continue
        return burned / total
    return None


def check_lp_lock(chain: str, dex_id: str, pair_address: str,
                  timeout: float = 10) -> Dict[str, str]:
    """Classify a pool's LP-lock state. Never raises.

    Returns {"status": "locked"|"unverified"|"na", "detail": <short str>}.
    """
    try:
        chain = (chain or "").lower()
        dex = (dex_id or "").lower()
        pair = (pair_address or "").strip()
        v2 = _V2_DEXES.get(chain, ())
        if dex and v2 and not any(d in dex for d in v2):
            # Concentrated-liquidity pool: no LP token exists, nothing to
            # verify without an indexer. Honest "n/a", not a guess.
            return {"status": "na",
                    "detail": "concentrated-liquidity pool — no LP token"}
        if not _ADDR_RE.match(pair):
            return {"status": "unverified",
                    "detail": "LP lock unverified (no pair address)"}
        share = _v2_burned_share(chain, pair, timeout)
        if share is None:
            return {"status": "unverified",
                    "detail": "LP lock unverified (RPC unavailable)"}
        if share >= _BURNED_PCT:
            return {"status": "locked",
                    "detail": "LP burned %.0f%%" % (100 * share)}
        # NOTE: locker-held LP (Unicrypt etc.) also counts as locked but is
        # not auto-checked in v1 — "unverified" is honest, not "unlocked".
        return {"status": "unverified",
                "detail": "LP lock unverified (%.0f%% burned)" % (100 * share)}
    except Exception as exc:  # fail-soft: never break card flow
        logger.warning("lp_lock check crashed: %s", exc)
        return {"status": "unverified", "detail": "LP lock unverified"}


def badge(report: Dict[str, str]) -> str:
    """One-line card badge. Empty string when there is nothing to show."""
    status = (report or {}).get("status", "unverified")
    detail = (report or {}).get("detail", "")
    if status == "locked":
        return "🔒 %s" % detail
    if status == "na":
        return "➖ LP n/a (%s)" % detail
    return "⚠️ %s" % detail


def _fmt_supply(n: float) -> str:
    """969499855 -> "969.5M". Pure function, unit-tested."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1e12:
        return "%.1fT" % (n / 1e12)
    if n >= 1e9:
        return "%.1fB" % (n / 1e9)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return "%d" % int(n)


def token_supply(chain: str, mint: str, timeout: float = 10) -> str:
    """Total token supply, preformatted for cards ("1.0B").

    Solana: getTokenSupply on a public RPC. Base/Ethereum: eth_call
    totalSupply + decimals on the token contract. "" when unknown.
    Display-only; never raises; only called for winning card candidates.
    """
    try:
        chain = (chain or "").lower()
        if chain == "solana":
            for rpc in _SOL_RPCS:
                res = _post_json(rpc, {"jsonrpc": "2.0", "id": 1,
                                       "method": "getTokenSupply",
                                       "params": [mint]}, timeout)
                val = ((res or {}).get("result") or {}).get("value") or {}
                amt = val.get("amount")
                if amt is None:
                    continue
                try:
                    dec = int(val.get("decimals", 0) or 0)
                    return _fmt_supply(int(amt) / (10 ** dec))
                except (TypeError, ValueError):
                    continue
        elif chain in _RPCS:
            if not _ADDR_RE.match(mint or ""):
                return ""
            for rpc in _RPCS[chain]:
                total = _eth_call_retry(rpc, mint, _TOTAL_SUPPLY, timeout)
                if not total:
                    continue
                dec = _eth_call_retry(rpc, mint, _DECIMALS, timeout)
                if dec is None:
                    continue
                return _fmt_supply(total / (10 ** dec))
    except Exception as exc:
        logger.warning("token_supply failed for %s: %s", chain, exc)
    return ""
