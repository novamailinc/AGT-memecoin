#!/usr/bin/env python3
"""Curve-direct pricing for the mimic lane (2026-11-01).

Why: DexScreener/tape do not price seconds-old pump.fun launches,
so a launch lane priced only from indexers starves (0/15 entered
in the mimic lane's first 5 minutes). The bonding curve account
exists from the very first second, so pricing from the curve itself
removes the indexer wait entirely.

Public mechanics only: the pump.fun bonding-curve PDA seeds
("bonding-curve", mint) under the pump program, and the anchor
BondingCurve layout (8-byte discriminator, then u64 virtual token
reserves, u64 virtual SOL reserves, u64 real token reserves, u64
real SOL reserves, u64 total supply, bool complete). The curve
account's OWNER must be the pump program, or the read is rejected
— a wrong PDA (or a non-curve account) can never price an entry.

Free-only: Helius RPC (the bot's own key files, never logged) with
public mainnet-beta fallback. Never raises. Never logs URLs or keys.
"""
import base64
import hashlib
import json
import logging
import os
import struct
import time
import urllib.parse
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.mimic_curve")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")

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

PUMP_PROGRAM = _solana_ids.PUMP_PROGRAM_ID
WSOL = _solana_ids.WSOL_MINT

# Helius rate-limit guard (the bot's own shared convention): only an
# HTTP 429 engages the brake (60s, doubling to a 600s cap, reset on
# success). While cooled, Helius calls are skipped silently and the
# public fallback carries the read — the lane must never hammer a
# rate-limited endpoint to keep its own quotes flowing. Fail-soft:
# a missing guard module leaves behavior unchanged.
try:
    import helius_ratelimit as _rl
    _GUARD = _rl.RateGuard()
except Exception:
    _rl = None  # type: ignore
    _GUARD = None

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + (s or "")


def b58decode(s):
    try:
        n = 0
        for c in s:
            n = n * 58 + _B58.index(c)
        b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
        return b"\0" * (len(s) - len(s.lstrip("1"))) + b
    except Exception:
        return b""


# ---- PDA derivation (standard Solana mechanics) -------------------------

_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _is_on_curve(b32):
    """True when the 32 bytes are a valid ed25519 point (so NOT a
    usable PDA). Standard Edwards-curve check."""
    try:
        y = int.from_bytes(b32, "little") & ((1 << 255) - 1)
        if y >= _P:
            return False
        yy = (y * y) % _P
        u = (yy - 1) % _P
        v = (_D * yy + 1) % _P
        x2 = u * pow(v, _P - 2, _P) % _P
        if x2 == 0:
            return True
        return pow(x2, (_P - 1) // 2, _P) == 1
    except Exception:
        return False


def derive_curve_pda(mint):
    """Bonding-curve PDA for a pump.fun mint. None on failure."""
    try:
        mint_b = b58decode(mint)
        prog = b58decode(PUMP_PROGRAM)
        if len(mint_b) != 32 or len(prog) != 32:
            return None
        for bump in range(255, -1, -1):
            h = hashlib.sha256()
            h.update(b"bonding-curve")
            h.update(mint_b)
            h.update(bytes([bump]))
            h.update(prog)
            h.update(b"ProgramDerivedAddress")
            cand = h.digest()
            if not _is_on_curve(cand):
                return b58encode(cand)
    except Exception:
        pass
    return None


# ---- RPC ------------------------------------------------------------------

def _helius_key():
    for env, fn in (("HELIUS_API_KEY_DISCOVERY", "api_key_discovery"),
                    ("HELIUS_API_KEY", "api_key")):
        v = os.environ.get(env)
        if v:
            return v.strip()
        try:
            p = os.path.join(HOME, "helius", fn)
            if os.path.exists(p):
                return open(p).read().strip()
        except Exception:
            pass
    return ""


def rpc_urls():
    k = _helius_key()
    urls = []
    if k:
        urls.append("https://mainnet.helius-rpc.com/?api-key=" + k)
    urls.append("https://api.mainnet-beta.solana.com")
    return urls


def rpc(method, params, timeout=12):
    """Kept offline lanes use the same section 9 routine policy."""
    from daemon.intelligence.acquisition.routine import read
    return read(method, params, timeout=timeout)



def get_account(pubkey):
    """(owner, data_bytes) or (None, None). Never raises."""
    try:
        res = rpc("getAccountInfo", [pubkey, {"encoding": "base64"}])
        v = (res or {}).get("value")
        if not v:
            return None, None
        return v.get("owner"), base64.b64decode(v["data"][0])
    except Exception:
        return None, None


# ---- account parsers (pure) ----------------------------------------------

def parse_bonding_curve(data):
    """{v_token, v_sol, real_token, real_sol, supply, complete}
    from curve account bytes. None if too short."""
    try:
        if not data or len(data) < 49:
            return None
        return {"v_token": struct.unpack_from("<Q", data, 8)[0],
                "v_sol": struct.unpack_from("<Q", data, 16)[0],
                "real_token": struct.unpack_from("<Q", data, 24)[0],
                "real_sol": struct.unpack_from("<Q", data, 32)[0],
                "supply": struct.unpack_from("<Q", data, 40)[0],
                "complete": bool(data[48])}
    except Exception:
        return None


_DEC_CACHE = {}


def mint_decimals(mint):
    """Decimals for a mint (cached). Pump mints default to 6 when
    the mint account cannot be read. None when unknown."""
    if mint in _DEC_CACHE:
        return _DEC_CACHE[mint]
    dec = None
    try:
        _owner, data = get_account(mint)
        if data and len(data) >= 45:
            dec = data[44]
    except Exception:
        dec = None
    if dec is None and str(mint or "").endswith("pump"):
        dec = 6
    if dec is not None:
        _DEC_CACHE[mint] = dec
    return dec


def curve_price_from_reserves(v_token, v_sol, decimals, sol_usd):
    """USD price per token from raw virtual reserves. Pure. None
    when unusable."""
    try:
        if not v_token or v_sol is None or decimals is None \
                or not sol_usd:
            return None
        price_sol = (v_sol / 1e9) / (v_token / (10 ** decimals))
        if price_sol <= 0:
            return None
        return price_sol * sol_usd
    except Exception:
        return None


# ---- SOL/USD ----------------------------------------------------------------

_SOL_CACHE = {"px": 0.0, "ts": 0.0}
SOL_TTL_S = 30.0


def sol_usd_price(now=None):
    """SOL/USD, cached 30s (cache refreshed on demand, not polled).
    DexScreener SOL pairs first, Jupiter lite fallback. 0.0 =
    unknown (callers fall back). Never raises."""
    now = time.time() if now is None else now
    if _SOL_CACHE["px"] > 0 and now - _SOL_CACHE["ts"] < SOL_TTL_S:
        return _SOL_CACHE["px"]
    try:
        url = ("https://api.dexscreener.com/tokens/v1/solana/" + WSOL)
        req = urllib.request.Request(
            url, headers={"User-Agent": "forex-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            pairs = json.loads(r.read().decode())
        best, best_liq = 0.0, -1.0
        for p in pairs or []:
            try:
                if (p.get("quoteToken") or {}).get("symbol") \
                        not in ("USDC", "USDT"):
                    continue
                px = float(p.get("priceUsd") or 0)
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                if px > 0 and liq > best_liq:
                    best, best_liq = px, liq
            except Exception:
                continue
        if best > 0:
            _SOL_CACHE.update(px=best, ts=now)
            return best
    except Exception:
        pass
    try:
        url = ("https://lite-api.jup.ag/price/v2?ids=" + WSOL)
        req = urllib.request.Request(
            url, headers={"User-Agent": "forex-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
        px = float(((d.get("data") or {}).get(WSOL) or {})
                   .get("price") or 0)
        if px > 0:
            _SOL_CACHE.update(px=px, ts=now)
            return px
    except Exception:
        pass
    return _SOL_CACHE["px"]


# ---- unified curve price ----------------------------------------------------

def curve_price_usd(mint):
    """(price_usd, detail) from the mint's bonding curve virtual
    reserves. (0.0, why) when unreadable, graduated, or unpriced —
    callers fall back to the live quote. Never raises."""
    try:
        pda = derive_curve_pda(mint)
        if not pda:
            return 0.0, "no curve pda"
        owner, data = get_account(pda)
        if owner != PUMP_PROGRAM or not data:
            return 0.0, "curve unreadable"
        c = parse_bonding_curve(data)
        if not c or not c["v_token"]:
            return 0.0, "curve unreadable"
        if c["complete"]:
            return 0.0, "curve complete (graduated)"
        dec = mint_decimals(mint)
        if dec is None:
            return 0.0, "decimals unknown"
        sol = sol_usd_price()
        if not sol:
            return 0.0, "no sol/usd"
        px = curve_price_from_reserves(c["v_token"], c["v_sol"],
                                       dec, sol)
        if not px:
            return 0.0, "curve price not positive"
        return px, "bonding curve %s" % pda[:8]
    except Exception as e:
        return 0.0, "curve error: %s" % e
