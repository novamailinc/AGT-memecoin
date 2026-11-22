#!/usr/bin/env python3
"""Direct-reserve pricing for Solana pump venues (2026-10-23, P1).

Why this exists: the exit-leakage audit found paper exits filling off
DexScreener/GeckoTerminal REPUBLISHED quotes — a rugging pool's price
gaps from >2x to ~0 between republishes, so legs B/C exited on dust
prints. The fix is to price from the pool itself: PumpSwap vault
balances and pump.fun bonding-curve reserves, read directly from
chain state via Helius RPC (free tier). One account read is current
as of the last slot — there is no republisher in between.

Layout evidence (verified live 2026-10-23 against pool
52PfZrMW8XMcEdK86qSBFK4bArj2rCqAUgQH8quxhz4M, owner = PumpSwap
program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA):
creator @11, quote_mint (WSOL) @43, base_mint @75, lp_mint @107,
pool_quote_vault @139, pool_base_vault @171, lp_supply u64 @203.
Token-2022 pools included (vault accounts may be 170 bytes).
The reader NEVER trusts field order blindly: vault accounts are
mint-matched (the vault whose mint is WSOL is the quote vault; the
one whose mint is the position mint is the base vault), and when the
offset parse fails a getProgramAccounts scan by vault-owner finds
them layout-free.

Bonding curve (pre-graduation): PDA seeds ["bonding-curve", mint]
under the pump program; layout: virtual_token_reserves u64 @8,
virtual_sol_reserves u64 @16, real_token_reserves @24,
real_sol_reserves @32, token_total_supply @40, complete bool @48.

Free-only: Helius RPC (discovery key first, main key fallback, then
public mainnet-beta). SOL/USD from DexScreener's SOL pairs (cached
30s — a cache, not a poller: refreshed only when a price is computed
and the cache is older than the TTL). Never raises.
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

logger = logging.getLogger("forex_agent.daemon.launch_scanner.reserves")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")

PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN2022_PROGRAM = "TokenzQdBNbLQZWM1GjTbvYDbPknHBzqALDFpHHFmDWd"
WSOL = "So11111111111111111111111111111111111111112"

# Verified PumpSwap pool offsets (see module docstring).
POOL_QUOTE_VAULT_OFF = 139
POOL_BASE_VAULT_OFF = 171

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + (s or "")


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + b


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
    """One JSON-RPC call with endpoint failover. None on failure.

    Failures are LOGGED per endpoint (host + exception type + HTTP
    code when present) instead of swallowed silently: the old bare
    `except: continue` turned every failure class into an identical
    bare None, which made the 2026-10-30 unknown_curve burst
    undiagnosable (every journal entry read curve_stats_error
    "None"). Return contract unchanged: result or None. Only the
    hostname is ever logged, never the URL: the Helius URL carries
    an api-key query parameter.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    for url in rpc_urls():
        host = urllib.parse.urlparse(url).hostname or "unknown"
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if isinstance(d, dict) and d.get("result") is not None:
                return d["result"]
            if isinstance(d, dict) and d.get("error") is not None:
                logger.warning("reserves rpc %s: %s returned JSON-RPC "
                               "error %s", method, host,
                               str(d["error"])[:160])
        except Exception as e:
            code = getattr(e, "code", None)
            logger.warning("reserves rpc %s: %s failed (%s%s)",
                           method, host, type(e).__name__,
                           (" HTTP %s" % code) if code else "")
            continue
    return None


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


def get_accounts(pubkeys):
    """{pubkey: (owner, data)} for existing accounts. Never raises."""
    out = {}
    try:
        res = rpc("getMultipleAccounts",
                  [list(pubkeys), {"encoding": "base64"}])
        vals = (res or {}).get("value") or []
        for pk, v in zip(pubkeys, vals):
            if v:
                out[pk] = (v.get("owner"),
                           base64.b64decode(v["data"][0]))
    except Exception:
        pass
    return out


# ---- account parsers (pure) ----------------------------------------------

def parse_token_account(data):
    """SPL token account: mint @0, amount u64 @64. None if too short."""
    try:
        if not data or len(data) < 72:
            return None
        return {"mint": b58encode(data[0:32]),
                "amount": struct.unpack_from("<Q", data, 64)[0]}
    except Exception:
        return None


def parse_mint_decimals(data):
    """Mint account: decimals u8 @44. None if too short."""
    try:
        if not data or len(data) < 45:
            return None
        return data[44]
    except Exception:
        return None


_DEC_CACHE = {}


def mint_decimals(mint):
    """Decimals for a mint (cached; pump tokens are 6)."""
    if mint in _DEC_CACHE:
        return _DEC_CACHE[mint]
    dec = None
    _owner, data = get_account(mint)
    dec = parse_mint_decimals(data)
    if dec is None:
        dec = 6 if mint.endswith("pump") else None
    if dec is not None:
        _DEC_CACHE[mint] = dec
    return dec


def parse_pumpswap_vaults(data):
    """(quote_vault, base_vault) pubkeys from pool account bytes at
    the verified offsets. (None, None) if implausible."""
    try:
        if not data or len(data) < POOL_BASE_VAULT_OFF + 32:
            return None, None
        q = b58encode(data[POOL_QUOTE_VAULT_OFF:
                           POOL_QUOTE_VAULT_OFF + 32])
        b = b58encode(data[POOL_BASE_VAULT_OFF:
                           POOL_BASE_VAULT_OFF + 32])
        return q, b
    except Exception:
        return None, None


def match_vaults(accounts, mint):
    """accounts: {pubkey: (owner, data)}. Returns (base_vault,
    quote_vault) matched BY MINT — base holds `mint`, quote holds
    WSOL — or (None, None)."""
    base_v = quote_v = None
    for pk, (_owner, data) in (accounts or {}).items():
        ta = parse_token_account(data)
        if not ta:
            continue
        if ta["mint"] == mint:
            base_v = pk
        elif ta["mint"] == WSOL:
            quote_v = pk
    return base_v, quote_v


def scan_pool_vaults(pool, mint):
    """Layout-free fallback: token accounts OWNED BY the pool
    account (vault authority = the pool itself on PumpSwap), via
    getProgramAccounts memcmp on both token programs."""
    for prog in (TOKEN_PROGRAM, TOKEN2022_PROGRAM):
        try:
            res = rpc("getProgramAccounts", [
                prog,
                {"encoding": "base64",
                 "filters": [{"memcmp": {"offset": 32,
                                         "bytes": pool}}]}])
            accts = {}
            for row in res or []:
                info = (row or {}).get("account") or {}
                d = info.get("data") or []
                if d:
                    accts[row["pubkey"]] = (
                        info.get("owner"), base64.b64decode(d[0]))
            b, q = match_vaults(accts, mint)
            if b and q:
                return b, q
        except Exception:
            continue
    return None, None


def resolve_vaults(pool, mint):
    """{base_vault, quote_vault, base_amount, quote_amount} for a
    PumpSwap pool, or None. Verifies by mint-matching; falls back to
    the program scan when the offset parse doesn't verify."""
    owner, data = get_account(pool)
    if owner != PUMPSWAP_PROGRAM or not data:
        return None
    q_off, b_off = parse_pumpswap_vaults(data)
    if q_off and b_off:
        accts = get_accounts([q_off, b_off])
        b, q = match_vaults(accts, mint)
        if b and q:
            return _vault_amounts(accts, b, q)
    b, q = scan_pool_vaults(pool, mint)
    if b and q:
        accts = get_accounts([b, q])
        return _vault_amounts(accts, b, q)
    return None


def _vault_amounts(accts, base_v, quote_v):
    out = {"base_vault": base_v, "quote_vault": quote_v}
    for key, pk in (("base_amount", base_v), ("quote_amount", quote_v)):
        got = (accts or {}).get(pk)
        ta = parse_token_account(got[1]) if got else None
        if ta is None:
            return None
        out[key] = ta["amount"]
    return out


def amounts_price(base_amount, quote_amount, base_decimals,
                  sol_usd=None):
    """USD price per token from raw vault amounts + SOL/USD.
    price_sol = (quote/1e9) / (base/10^dec). None if unusable.
    sol_usd may be injected (the armed watcher carries its own
    cached source); otherwise the module cache/fetch is used."""
    try:
        if not base_amount or quote_amount is None or \
                base_decimals is None:
            return None
        sol = sol_usd if sol_usd else sol_usd_price()
        if not sol:
            return None
        price_sol = (quote_amount / 1e9) / (base_amount /
                                            (10 ** base_decimals))
        return price_sol * sol
    except Exception:
        return None


def pool_price_usd(pool, mint):
    """(price_usd, detail) from DIRECT PumpSwap vault balances.
    (0.0, why) when unreadable — callers fall back. Never raises."""
    try:
        if not pool or not mint:
            return 0.0, "no pool/mint"
        v = resolve_vaults(pool, mint)
        if not v:
            return 0.0, "vaults unresolved"
        dec = mint_decimals(mint)
        px = amounts_price(v["base_amount"], v["quote_amount"], dec)
        if not px:
            return 0.0, "amounts unusable"
        return px, "pumpswap reserves %s/%s" % (v["base_vault"][:8],
                                                v["quote_vault"][:8])
    except Exception as e:
        return 0.0, "reserves error: %s" % e


# ---- bonding curve ----------------------------------------------------------

_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _is_on_curve(b32):
    """True when the 32 bytes are a valid ed25519 point (so NOT a
    usable PDA). Standard Solana PDA check."""
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


def parse_bonding_curve(data):
    """{v_token, v_sol, complete} from curve account bytes."""
    try:
        if not data or len(data) < 49:
            return None
        return {"v_token": struct.unpack_from("<Q", data, 8)[0],
                "v_sol": struct.unpack_from("<Q", data, 16)[0],
                "complete": bool(data[48])}
    except Exception:
        return None


def curve_price_usd(mint):
    """(price_usd, detail) from the bonding curve's VIRTUAL reserves
    (the curve's own pricing inputs). (0.0, why) on failure."""
    try:
        pda = derive_curve_pda(mint)
        if not pda:
            return 0.0, "no curve pda"
        _owner, data = get_account(pda)
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
        price_sol = (c["v_sol"] / 1e9) / (c["v_token"] /
                                          (10 ** dec))
        return price_sol * sol, "bonding curve %s" % pda[:8]
    except Exception as e:
        return 0.0, "curve error: %s" % e


# ---- SOL/USD ----------------------------------------------------------------

_SOL_CACHE = {"px": 0.0, "ts": 0.0}
SOL_TTL_S = 30.0


def sol_usd_price(now=None):
    """SOL/USD, cached 30s (cache refreshed on demand, not polled).
    DexScreener SOL pairs first. 0.0 = unknown. Never raises."""
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
    try:  # Jupiter price API (free lite endpoint) as fallback.
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


# ---- unified position pricing ----------------------------------------------

def price_for_position(pos):
    """(price_usd, source) for an open Solana position, direct from
    chain state: PumpSwap pool reserves when the position's pair is
    a PumpSwap pool; otherwise the bonding curve (pre-graduation
    positions). (0.0, why) when neither reads — callers fall back
    to DexScreener/GT. Never raises."""
    try:
        mint = str((pos or {}).get("mint") or "")
        if not mint:
            return 0.0, "no mint"
        pair = str((pos or {}).get("pair") or "")
        if pair:
            px, why = pool_price_usd(pair, mint)
            if px > 0:
                return px, "reserves:pumpswap"
            # Pair may be a Raydium pool or a stale curve address:
            # fall through to the curve only for un-graduated mints.
        px, why2 = curve_price_usd(mint)
        if px > 0:
            return px, "reserves:curve"
        return 0.0, "pool: %s; curve: %s" % (why if pair else "no pair",
                                             why2)
    except Exception as e:
        return 0.0, "reserves error: %s" % e
