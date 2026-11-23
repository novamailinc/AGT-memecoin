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
    non-transferable and default-frozen accounts are hard fails;
    transfer fees follow the >10% tax line (fail) / any fee (flag);
    mint close authority and confidential transfers flag. Any
    extension this parser cannot verify — unknown name, malformed
    entry, unreadable state — is flagged for review, never silently
    passed (Phase 2, directive §7.2). Classic SPL mints carry no
    extensions and are unaffected.
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

An unknown report carries passed=False (2026-11-22 contract
hardening): every current consumer checks unknown first and skips,
so the value is never read today — but a future consumer that reads
passed alone must never wave an unscored token through.

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
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from . import helius_ratelimit as _helius_ratelimit
except ImportError:  # top-level import context (tests import the file directly)
    import helius_ratelimit as _helius_ratelimit  # type: ignore

# Canonical Solana program IDs / mints (Phase 2): the single source
# of truth is daemon/solana_ids.py — never redefine these literals.
try:
    from daemon import solana_ids as _solana_ids
except ImportError:  # bare-file import without the repo root on path
    import os as _os, sys as _sys
    _root = _os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from daemon import solana_ids as _solana_ids

logger = logging.getLogger("forex_agent.daemon.launch_scanner.instant_safety")

_UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
       "Connection": "close"}

_HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")


def _shared_guard():
    """Fleet-wide Helius guard (patchable in tests)."""
    return _helius_ratelimit.shared_guard()


_pairs: Optional[List[Any]] = None


def _rpc_endpoint_pairs() -> List[Any]:
    """[(url, bucket)] — Helius keys first, public endpoint last.

    bucket is the shared-guard id for the key (sha256 prefix — never
    the key itself); None for the public endpoint, which is unguarded.
    _rpc_endpoints() is the URL-only view of this list."""
    global _pairs
    if _pairs is not None:
        return list(_pairs)
    pairs: List[Any] = []
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
            pairs.append(("https://mainnet.helius-rpc.com/?api-key=" + key,
                          _helius_ratelimit.bucket_id_for_key(key)))
    pairs.append(("https://api.mainnet-beta.solana.com", None))
    _pairs = pairs
    return list(pairs)


def _rpc_endpoints() -> List[str]:
    return [url for url, _bucket in _rpc_endpoint_pairs()]

_CACHE_TTL_S = 120.0
_CACHE_MAX = 512
_cache: Dict[str, Any] = {}

# Program ids whose token accounts are venues, not users.
PUMP_PROGRAM = _solana_ids.PUMP_PROGRAM_ID
ATA_PROGRAM = _solana_ids.ASSOCIATED_TOKEN_PROGRAM_ID
TOKEN_PROGRAM = _solana_ids.TOKEN_PROGRAM_ID
TOKEN2022_PROGRAM = _solana_ids.TOKEN2022_PROGRAM_ID

# Token-2022 extensions this parser recognises as safe with no
# transfer semantics (pure metadata / pointer extensions). Any other
# extension name the parser does not explicitly handle is flagged
# for REVIEW, never silently passed (Phase 2, directive §7.2).
# Deliberately narrow: fee-, interest-, memo- and pause-bearing
# extensions (transferFeeConfig aside, which is handled above)
# carry transfer/value semantics this parser does not evaluate,
# so they land in the unrecognized-review bucket instead.
_KNOWN_SAFE_EXTENSIONS = frozenset((
    "immutableOwner", "metadataPointer", "groupPointer",
    "groupMemberPointer", "tokenMetadata", "tokenGroup",
    "tokenGroupMember"))

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
    never logs a URL (the Helius URLs carry the key).

    2026-11-16 (RPC-burn fix, Lever 1): Helius legs whose key bucket
    is cooling down fleet-wide are SKIPPED (the guard is hot — the
    keys are already dead, so attempting them only burns the quota
    further). The public mainnet-beta leg is still tried — it is not
    Helius quota. Outcomes are reported to the pacer: a 429 engages
    progressive backoff + the single probe for that key, a success
    clears it (the pacer mirrors both into the shared guard, so the
    hot-bucket skip above keeps working off pacer state).
    Screening outcomes when RPC is healthy are unchanged — only
    which legs are attempted during exhaustion changes.

    2026-11-17 (pacing fix): a Helius leg is attempted only with a
    pacer token for its key's bucket. No token -> that leg is
    skipped (fail over to the next leg). Helius also spells rate
    limits as a JSON-RPC error (code -32005) with HTTP 200 — those
    report to the pacer as 429s too."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}).encode()
    guard = _shared_guard()
    for url, bucket in _rpc_endpoint_pairs():
        if bucket and guard.cooldown_remaining(bucket) > 0:
            logger.debug("instant_safety skipping hot Helius leg %s",
                         method)
            continue
        if bucket and not _helius_ratelimit.pacer_acquire(bucket):
            logger.debug("instant_safety skipping unpaced Helius leg %s",
                         method)
            continue
        req = urllib.request.Request(url, data=payload, headers=_UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8",
                                                     "replace"))
            if isinstance(data, dict) and data.get("error"):
                # Helius also spells rate limits as a JSON-RPC error
                # (code -32005) with HTTP 200 — engage the guard for
                # those too, so the fleet backs off either way.
                err = data.get("error") or {}
                if isinstance(err, dict) and err.get("code") in (
                        -32005, -32029) and bucket:
                    _helius_ratelimit.pacer_report(bucket, False)
                continue
            if bucket:
                _helius_ratelimit.pacer_report(bucket, True)
            return data.get("result") if isinstance(data, dict) \
                else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and bucket:
                _helius_ratelimit.pacer_report(bucket, False)
            continue
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
    unknown = {"passed": False, "score": 0, "flags": [], "fails": [],
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
    # Never cache an unknown verdict: a null/incomplete read must
    # not block later retrieval of confirmed data (Phase 2, tx-cache
    # correctness). Unknowns are re-fetched on the next call; only a
    # reached verdict is cached.
    if not rep.get("unknown"):
        if len(_cache) >= _CACHE_MAX:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])
            for k, _v in oldest[:len(_cache) - _CACHE_MAX + 1]:
                _cache.pop(k, None)
        _cache[mint] = (now, rep)
    return rep


def _check_inner(mint: str, timeout: float) -> Dict[str, Any]:
    unknown = {"passed": False, "score": 0, "flags": [], "fails": [],
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

    # 1b) Token-2022 extension traps (2026-10-20; fail-closed review
    # for unknown/malformed data, Phase 2 directive §7.2). Classic
    # SPL Token-program mints have no "extensions" list and skip this
    # entirely. Each entry is {"extension": <name>, "state": {...}}.
    # An extension this parser cannot verify — unknown name,
    # malformed entry, unreadable state — is NEVER silently passed:
    # it is flagged for REVIEW (score -5, passed unchanged) so the
    # verdict stays honest. The known trap names below still fail
    # outright. Extension data must never crash the check.
    def _ext_flag(msg):
        if msg not in flags:
            flags.append(msg)

    ext_names: List[str] = []
    transfer_fee_detail: Dict[str, Any] = {}
    extensions = info.get("extensions")
    if extensions is None:
        pass  # classic SPL mint: no extensions key at all
    elif not isinstance(extensions, list):
        _ext_flag("Token-2022 extensions data unreadable "
                  "(unverified — review)")
    else:
        for ext in extensions:
            try:
                if not isinstance(ext, dict):
                    _ext_flag("Token-2022 extension entry unreadable "
                              "(unverified — review)")
                    continue
                name = ext.get("extension")
                if not isinstance(name, str) or not name:
                    _ext_flag("Token-2022 extension entry unreadable "
                              "(unverified — review)")
                    continue
                state = ext.get("state")
                if not isinstance(state, dict):
                    _ext_flag("Token-2022 extension entry unreadable "
                              "(unverified — review)")
                    state = {}
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
                    st = state.get("state")
                    if not isinstance(st, str):
                        _ext_flag("Token-2022 default account state "
                                  "unreadable (unverified — review)")
                    elif st.lower() == "frozen":
                        fails.append(
                            "Token-2022 new accounts frozen by "
                            "default (defaultAccountState=frozen)")
                elif name == "transferFeeConfig":
                    bps = _transfer_fee_bps(state)
                    # Exact fee math is possible from mint data
                    # alone (UiTransferFee: epoch, maximumFee in
                    # RAW BASE UNITS, transferFeeBasisPoints):
                    # fee(x) = min(x * bps / 10000, maximumFee).
                    # The cap is token-denominated, so no price
                    # context is needed; expose the inputs so a
                    # consumer can compute the exact fee for any
                    # transfer amount.
                    def _maxfee():
                        vals = []
                        for k in ("newerTransferFee",
                                  "olderTransferFee"):
                            sub = state.get(k)
                            if isinstance(sub, dict) \
                                    and sub.get("maximumFee") \
                                    is not None:
                                try:
                                    vals.append(
                                        int(sub["maximumFee"]))
                                except (TypeError, ValueError):
                                    pass
                        return max(vals) if vals else None
                    mf = _maxfee()
                    if bps is not None:
                        transfer_fee_detail["bps"] = bps
                    if mf is not None:
                        transfer_fee_detail["maximum_fee_raw"] = mf
                        if bps:
                            transfer_fee_detail[
                                "cap_binds_above_raw"] = int(
                                    mf * 10000 // bps)
                    if bps is not None:
                        if bps > 1000:
                            fails.append(
                                "Token-2022 transfer fee %.1f%% "
                                "(>10%%)" % (bps / 100.0))
                        elif bps > 0:
                            flags.append(
                                "Token-2022 transfer fee %.1f%%"
                                % (bps / 100.0))
                    else:
                        _ext_flag("Token-2022 transfer fee unreadable "
                                  "(unverified — review)")
                    # The CURRENT fee is not the whole trap: a set
                    # transferFeeConfigAuthority can raise the
                    # schedule later (UiTransferFeeConfig.camelCase).
                    # Detection only, same REVIEW class.
                    auth = state.get("transferFeeConfigAuthority")
                    if auth:
                        _ext_flag("Token-2022 transfer fee is "
                                  "authority-mutable (can be raised "
                                  "later)")
                elif name == "mintCloseAuthority":
                    if state.get("mintCloseAuthority"):
                        flags.append(
                            "Token-2022 mint close authority set")
                elif name == "confidentialTransferMint":
                    flags.append(
                        "Token-2022 confidential transfer mint")
                elif name == "confidentialMintBurn":
                    flags.append(
                        "Token-2022 confidential mint/burn")
                # Canonical RPC jsonParsed names are the UiExtension
                # camelCase serde variants (agave account-decoder-
                # client-types token.rs): interestBearingConfig,
                # scaledUiAmountConfig, pausableConfig. Legacy/doc
                # aliases are accepted alongside.
                elif name in ("interestBearingConfig",
                              "interestBearingMint"):
                    _ext_flag(
                        "Token-2022 interest-bearing mint: displayed "
                        "balances drift from real balances "
                        "(unverified — review)")
                elif name == "memoTransfer":
                    _ext_flag(
                        "Token-2022 memo-gated transfers: venues "
                        "that omit memos cannot move the token "
                        "(unverified — review)")
                elif name in ("pausableConfig", "pausable"):
                    # Same power class as a freeze authority, which
                    # this checker also FLAGS rather than fails.
                    _ext_flag(
                        "Token-2022 pausable: an authority can halt "
                        "all transfers (unverified — review)")
                elif name in ("scaledUiAmountConfig",
                              "scaledUiAmount"):
                    _ext_flag(
                        "Token-2022 scaled UI amount: an issuer "
                        "multiplier rescales displayed amounts "
                        "(unverified — review)")
                elif name == "confidentialTransferFeeConfig":
                    flags.append(
                        "Token-2022 confidential transfer fee config")
                elif name == "unparseableExtension":
                    _ext_flag(
                        "Token-2022 extension the RPC decoder itself "
                        "could not parse (unverified — review)")
                elif name not in _KNOWN_SAFE_EXTENSIONS:
                    _ext_flag("Token-2022 extension %r unrecognized "
                              "(unverified — review)" % name)
            except Exception:
                _ext_flag("Token-2022 extension entry unreadable "
                          "(unverified — review)")
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
            "transfer_fee": transfer_fee_detail or None,
            "mint_authority": (str(mint_auth) if mint_auth else None),
            "freeze_authority": (str(freeze_auth)
                                 if freeze_auth else None),
            "top1_user_pct": round(top1, 2),
            "top10_user_pct": round(top10, 2),
            "program_held_pct": round(program_pct, 2),
        },
    }
