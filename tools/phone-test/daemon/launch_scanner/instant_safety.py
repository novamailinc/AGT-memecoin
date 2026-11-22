"""Instant on-chain safety verdict for Solana tokens (2026-10-20).

Why this exists: the safety gate is fail-closed on UNKNOWN, and the
usual source of "known" — RugCheck's report — only exists once
RugCheck indexes the token, minutes to hours after launch. Runners
live and die inside that window (DACITY ran to ~$1.6M mcap and died
before most lanes could ever see a report). This module computes the
rug vectors that are readable straight from chain, in seconds:

  * mint authority   — a wallet-held mint authority is an infinite-
                       mint rug. Null (revoked) is clean; the pump.fun
                       bonding-curve PDA is the normal pre-graduation
                       state (flag, not fail); any other program-held
                       authority is flagged as unverified.
  * freeze authority — flagged when present (same convention as the
                       RugCheck path in safety.py).
  * Token-2022 extension traps — permanent delegate, transfer hook,
                       non-transferable and default-frozen accounts
                       are hard fails; transfer fees follow the >10%
                       tax line (fail) / any fee (flag); mint close
                       authority and confidential transfers flag.
                       Classic SPL mints carry no extensions and are
                       unaffected.
  * holder concentration — top user holders from
                       getTokenLargestAccounts. Pool vaults, bonding
                       curves and other program accounts are excluded
                       by classifying each holder's OWNER: user wallets
                       are ed25519 points (on-curve); program accounts
                       are PDAs (off-curve) by construction.
  * LP lock state    — NOT verifiable from these reads; recorded as a
                       standing flag so the score stays honest.

The verdict is a safety-report dict in the exact shape safety.py
returns ({passed, score, flags, fails, unknown, ...}) so every gate
can consume it unchanged. Score uses the own-evidence convention from
safety.py: 100 - 25*fails - 5*flags. Hard fails block via `passed`,
never via the score.

Fail-closed contract: this module returns unknown=True whenever the
chain data itself cannot be fetched or parsed. It only ever replaces
"unknown because RugCheck hasn't indexed it" with a verdict computed
from primary data — never with a guess.

RugCheck remains the second layer: whenever its report exists it
governs (it adds insider graphs, creator history and sell-path
intelligence this module cannot see), because callers only invoke
this path when RugCheck has NO report.

RPC endpoints, in order: Helius with the discovery key (the main key
is at its monthly cap; the public endpoint 429s
getTokenLargestAccounts outright from this VM — both verified live
2026-10-20), then Helius with the main key, then the public endpoint.
Keys are read from their files at runtime and never logged. Results
are cached ~120s per mint: the gates re-check the same coin several
times inside one decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional

_UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
       "Connection": "close"}

_HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
_endpoints: Optional[List[str]] = None


def _rpc_endpoints() -> List[str]:
    global _endpoints
    if _endpoints is not None:
        return _endpoints
    urls: List[str] = []
    for env_name, fname in (
            ("HELIUS_API_KEY_DISCOVERY", "api_key_discovery"),
            ("HELIUS_API_KEY", "api_key")):
        key = (os.environ.get(env_name) or "").strip()
        if not key:
            try:
                with open(os.path.join(_HOME, "helius", fname)) as fh:
                    key = fh.read().strip()
            except Exception:
                key = ""
        if key:
            urls.append("https://mainnet.helius-rpc.com/?api-key=" + key)
    urls.append("https://api.mainnet-beta.solana.com")
    _endpoints = urls
    return urls

_CACHE_TTL_S = 120.0
_CACHE_MAX = 512
_cache: Dict[str, Any] = {}

# Program ids whose token accounts are venues, not users.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Concentration thresholds — mirrors of the RugCheck-path conventions
# in safety.py (top-holder fails/flags) extended to the top-10 line
# RugCheck itself marks danger (>70%).
_TOP1_FAIL = 50.0
_TOP1_FLAG = 25.0  # RugCheck lists a ~26% single holder as a risk;
                   # flag from 25% so the warning surfaces (fail stays
                   # at >50%: one holder who can zero the coin alone)
_TOP10_FAIL = 70.0
_TOP10_FLAG = 55.0


# ---------------------------------------------------------------- base58

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58_INDEX[ch]
    pad = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + (out or "")


# ------------------------------------------------------- ed25519 / PDA

_ED_P = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P


def _is_on_curve(pubkey: bytes) -> bool:
    """True when the 32-byte value is a valid ed25519 point (a wallet).

    PDAs are deliberately derived OFF the curve, so this is the
    classifier between user wallets (on) and program accounts (off).
    """
    if len(pubkey) != 32:
        return False
    y = int.from_bytes(pubkey, "little") & ((1 << 255) - 1)
    if y >= _ED_P:
        return False
    y2 = (y * y) % _ED_P
    # x^2 = (y^2 - 1) / (d*y^2 + 1); on-curve iff x^2 is a QR (or x==0)
    denom = (_ED_D * y2 + 1) % _ED_P
    if denom == 0:
        return False
    x2 = ((y2 - 1) * pow(denom, _ED_P - 2, _ED_P)) % _ED_P
    if x2 == 0:
        return True
    return pow(x2, (_ED_P - 1) // 2, _ED_P) == 1


def _find_pda(seeds: List[bytes], program_id: str) -> Optional[str]:
    """Derive a program-derived address (standard bump search)."""
    try:
        prog = _b58decode(program_id)
    except Exception:
        return None
    for bump in range(255, -1, -1):
        h = hashlib.sha256()
        for s in seeds:
            h.update(s)
        h.update(bytes([bump]))
        h.update(prog)
        h.update(b"ProgramDerivedAddress")
        cand = h.digest()
        if not _is_on_curve(cand):
            return _b58encode(cand)
    return None


def _ata(owner: str, mint: str, token_program: str) -> Optional[str]:
    """Associated token account address for (owner, mint)."""
    try:
        return _find_pda(
            [_b58decode(owner), _b58decode(token_program),
             _b58decode(mint)], ATA_PROGRAM)
    except Exception:
        return None


def _pump_curve_pda(mint: str) -> Optional[str]:
    try:
        return _find_pda([b"bonding-curve", _b58decode(mint)],
                         PUMP_PROGRAM)
    except Exception:
        return None


# ------------------------------------------------------------------- RPC

def _rpc(method: str, params: List[Any], timeout: float) -> Any:
    """One JSON-RPC call across the endpoint chain. Never raises,
    never logs a URL (the Helius URLs carry the key)."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}).encode()
    for url in _rpc_endpoints():
        req = urllib.request.Request(url, data=payload, headers=_UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8",
                                                     "replace"))
            if isinstance(data, dict) and data.get("error"):
                continue
            return data.get("result") if isinstance(data, dict) \
                else None
        except Exception:
            continue
    return None


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _transfer_fee_bps(state: Dict[str, Any]) -> Optional[int]:
    """Transfer-fee basis points from a transferFeeConfig extension
    state. jsonParsed nests the schedule under "newerTransferFee" /
    "olderTransferFee" (each {"transferFeeBasisPoints": n,
    "maximumFee": ..., "withheldFees": ...}); some shapes carry
    "transferFeeBasisPoints" at the top level. Check every spelling
    and take the max found; None when nothing parseable."""
    found: List[int] = []

    def _grab(d: Any) -> None:
        if isinstance(d, dict):
            v = d.get("transferFeeBasisPoints")
            if v is not None:
                try:
                    found.append(int(v))
                except (TypeError, ValueError):
                    pass

    _grab(state)
    _grab(state.get("newerTransferFee"))
    _grab(state.get("olderTransferFee"))
    return max(found) if found else None


# ----------------------------------------------------------------- check

def check(mint: str, timeout: float = 8.0) -> Dict[str, Any]:
    """Instant safety verdict for one Solana mint. Never raises.

    Returns a safety-report dict; unknown=True when the chain data
    itself could not be fetched (callers keep their fail-closed
    behavior for that case).
    """
    unknown = {"passed": True, "score": 0, "flags": [], "fails": [],
               "unknown": True, "source": "instant-onchain"}
    if not mint:
        return dict(unknown)
    hit = _cache.get(mint)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    try:
        rep = _check_inner(mint, max(2.0, float(timeout)))
    except Exception:
        rep = dict(unknown)
    if len(_cache) >= _CACHE_MAX:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])
        for k, _v in oldest[:len(_cache) - _CACHE_MAX + 1]:
            _cache.pop(k, None)
    _cache[mint] = (now, rep)
    return rep


def _check_inner(mint: str, timeout: float) -> Dict[str, Any]:
    unknown = {"passed": True, "score": 0, "flags": [], "fails": [],
               "unknown": True, "source": "instant-onchain"}
    per_call = max(1.0, timeout / 3.0)

    # 1) Mint account: authorities + supply + owning token program.
    res = _rpc("getAccountInfo",
               [mint, {"encoding": "jsonParsed"}], per_call)
    value = (res or {}).get("value") if isinstance(res, dict) else None
    if not value:
        return dict(unknown)
    parsed = ((value.get("data") or {}).get("parsed") or {})
    info = parsed.get("info") or {}
    if not info:
        return dict(unknown)
    token_program = str(value.get("owner") or TOKEN_PROGRAM)
    supply = _f(info.get("supply"))
    if supply <= 0:
        return dict(unknown)

    flags: List[str] = []
    fails: List[str] = []

    mint_auth = info.get("mintAuthority")
    freeze_auth = info.get("freezeAuthority")
    curve_pda = _pump_curve_pda(mint)
    if mint_auth:
        if curve_pda and str(mint_auth) == curve_pda:
            flags.append("mint authority = pump.fun curve "
                         "(pre-graduation)")
        elif _is_on_curve(_b58decode(str(mint_auth))):
            fails.append("mint authority enabled (infinite mint risk)")
        else:
            flags.append("mint authority held by a program "
                         "(unverified)")
    if freeze_auth:
        flags.append("freeze authority enabled")

    # 1b) Token-2022 extension traps (2026-10-20). Classic SPL
    # Token-program mints have no "extensions" list and skip this
    # entirely. Each entry is {"extension": <name>, "state": {...}}.
    # Malformed entries are ignored one by one — extension data must
    # never crash the check or turn a reached verdict into unknown.
    ext_names: List[str] = []
    extensions = info.get("extensions")
    if isinstance(extensions, list):
        for ext in extensions:
            try:
                if not isinstance(ext, dict):
                    continue
                name = str(ext.get("extension") or "")
                state = ext.get("state")
                if not isinstance(state, dict):
                    state = {}
                if not name:
                    continue
                ext_names.append(name)
                if name == "permanentDelegate":
                    fails.append(
                        "Token-2022 permanent delegate can seize/burn "
                        "any holder's tokens")
                elif name == "transferHook":
                    fails.append(
                        "Token-2022 transfer hook can block sells "
                        "arbitrarily")
                elif name == "nonTransferable":
                    fails.append(
                        "Token-2022 non-transferable tokens cannot "
                        "be sold or transferred")
                elif name == "defaultAccountState":
                    if str(state.get("state") or "").lower() \
                            == "frozen":
                        fails.append(
                            "Token-2022 new accounts frozen by "
                            "default (defaultAccountState=frozen)")
                elif name == "transferFeeConfig":
                    bps = _transfer_fee_bps(state)
                    if bps is not None:
                        if bps > 1000:
                            fails.append(
                                "Token-2022 transfer fee %.1f%% "
                                "(>10%%)" % (bps / 100.0))
                        elif bps > 0:
                            flags.append(
                                "Token-2022 transfer fee %.1f%%"
                                % (bps / 100.0))
                elif name == "mintCloseAuthority":
                    if state.get("mintCloseAuthority"):
                        flags.append(
                            "Token-2022 mint close authority set")
                elif name == "confidentialTransferMint":
                    flags.append(
                        "Token-2022 confidential transfer mint")
            except Exception:
                continue

    # 2) Largest token accounts + their owners.
    res = _rpc("getTokenLargestAccounts", [mint], per_call)
    accounts = (res or {}).get("value") if isinstance(res, dict) \
        else None
    if not accounts:
        return dict(unknown)
    addrs = [str(a.get("address") or "") for a in accounts]
    owners: List[Optional[str]] = [None] * len(addrs)
    res = _rpc("getMultipleAccounts",
               [addrs, {"encoding": "jsonParsed"}], per_call)
    values = (res or {}).get("value") if isinstance(res, dict) else None
    if values is None:
        return dict(unknown)
    for i, v in enumerate(values):
        if isinstance(v, dict):
            p = ((v.get("data") or {}).get("parsed") or {})
            o = (p.get("info") or {}).get("owner")
            owners[i] = str(o) if o else None

    # Venue accounts to exclude from user concentration: the pump
    # curve's own token account.
    venue_accts = set()
    if curve_pda:
        curve_ata = _ata(curve_pda, mint, token_program)
        if curve_ata:
            venue_accts.add(curve_ata)

    user_pcts: List[float] = []
    program_pct = 0.0
    for i, a in enumerate(accounts):
        # RAW amounts on both sides: mint info's `supply` is raw, and
        # uiAmount (decimal-adjusted) against it understates every
        # share by 10^decimals — the first live test read 0.0% for
        # every holder because of exactly that mismatch.
        amt = _f(a.get("amount"))
        if amt <= 0:
            continue
        pct = amt / supply * 100.0
        addr = addrs[i]
        owner = owners[i]
        if addr in venue_accts:
            program_pct += pct
            continue
        if owner is not None:
            try:
                if not _is_on_curve(_b58decode(owner)):
                    program_pct += pct  # PDA-owned: pool/escrow vault
                    continue
            except Exception:
                pass  # unparseable owner -> count as user (safer)
        user_pcts.append(pct)
    user_pcts.sort(reverse=True)

    top1 = user_pcts[0] if user_pcts else 0.0
    top10 = sum(user_pcts[:10])
    if user_pcts:
        if top1 > _TOP1_FAIL:
            fails.append("top holder owns %.0f%%" % top1)
        elif top1 > _TOP1_FLAG:
            flags.append("top holder owns %.0f%%" % top1)
        if top10 > _TOP10_FAIL:
            fails.append("top 10 holders own %.0f%%" % top10)
        elif top10 > _TOP10_FLAG:
            flags.append("top 10 holders own %.0f%%" % top10)
    else:
        flags.append("no user holders yet (instant path)")
    flags.append("LP lock unverified (instant path)")

    score = max(0, min(100, 100 - 25 * len(fails) - 5 * len(flags)))
    return {
        "passed": not fails,
        "score": score,
        "flags": flags,
        "fails": fails,
        "unknown": False,
        "source": "instant-onchain",
        # RugCheck-compatible holder list (user holders only) so
        # consumers like the watcher's bundle gate keep working.
        "topHolders": [{"pct": round(p, 2)} for p in user_pcts],
        "instant": {
            "token2022_extensions": ext_names,
            "mint_authority": (str(mint_auth) if mint_auth else None),
            "freeze_authority": (str(freeze_auth)
                                 if freeze_auth else None),
            "top1_user_pct": round(top1, 2),
            "top10_user_pct": round(top10, 2),
            "program_held_pct": round(program_pct, 2),
        },
    }
