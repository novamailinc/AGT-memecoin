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
program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA): creator @11,
mints @43/@75, lp_mint @107, vault pubkeys @139/@171,
lp_supply u64 @203. NOTE (2026-11-22): the authoritative pump_amm
IDL (pump-public-docs) names @43 base_mint, @75 quote_mint, @139
pool_BASE_token_account, @171 pool_QUOTE_token_account — the older
docstring had the base/quote NAMES swapped; only the vault-pubkey
offsets are used, and vault accounts are mint-matched (the vault
whose mint is the position mint is base; the other's mint IS the
quote asset), so the name swap never affected pricing. The quote
asset is NOT always WSOL: PUMP-quoted pools exist, and the quote
mint is read from the quote vault's token account (never assumed).
Token-2022 pools included (vault accounts may be 170 bytes). When
the offset parse fails a getProgramAccounts scan by vault-owner
finds the vaults layout-free.

Bonding curve (pre-graduation): PDA seeds ["bonding-curve", mint]
under the pump program. Authoritative layout = the pump-public-docs
IDL (idl/pump.json): virtual_token_reserves u64 @8,
virtual_QUOTE_reserves u64 @16, real_token_reserves @24,
real_quote_reserves @32, token_total_supply @40, complete bool @48,
creator pubkey @49, is_mayhem_mode @81, is_cashback_coin @82,
quote_mint pubkey @83 (so the schema needs >= 115 bytes; live
accounts observed 166 bytes with trailing fields the IDL does not
document — documented offsets only are parsed). The quote asset is
NOT always SOL: quote control mints are a program feature and live
PUMP-quoted curves exist (verified 2026-11-22). The repo's older
"virtual_sol_reserves / 81-byte" docstring was never the documented
layout (every pump-public-docs IDL since >= 2026-05-18 is
quote-aware); short accounts match no supported layout and are
refused.

Free-only: Helius RPC (discovery key first, main key fallback, then
public mainnet-beta). SOL/USD from DexScreener's SOL pairs (cached
30s — a cache, not a poller: refreshed only when a price is computed
and the cache is older than the TTL). Never raises.
"""
import base64
import fcntl
import hashlib
import json
import logging
import os
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

try:
    from . import helius_ratelimit as _helius_ratelimit
except ImportError:  # top-level import context (tests import the file directly)
    import helius_ratelimit as _helius_ratelimit  # type: ignore

# Canonical Solana program IDs / mints (Phase 2, D-01): the single
# source of truth is daemon/solana_ids.py — never redefine these
# literals here. The old TOKEN2022_PROGRAM literal in this file was
# WRONG (``...QZWM1GjTbv...``); it queried a nonexistent program, so
# Token-2022 pool vaults were never found by the scan_pool_vaults
# fallback.
try:
    from daemon import solana_ids as _solana_ids
except ImportError:  # bare-file import without the repo root on path
    import os as _os, sys as _sys
    _root = _os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from daemon import solana_ids as _solana_ids

# Switch journal (advanced build, piece 10, 2026-11-21): fail-soft
# import — a missing module leaves RPC behaviour byte-identical.
# JOURNAL-ONLY: the public-RPC fallback logs its engage/restore
# transitions (once per outage, not per call) to switch_log.jsonl.
try:
    from . import switches as _switches
except ImportError:  # top-level import context
    try:
        import switches as _switches  # type: ignore
    except Exception:
        _switches = None  # type: ignore

logger = logging.getLogger("forex_agent.daemon.launch_scanner.reserves")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")

PUMPSWAP_PROGRAM = _solana_ids.PUMPSWAP_PROGRAM_ID
PUMP_PROGRAM = _solana_ids.PUMP_PROGRAM_ID
TOKEN_PROGRAM = _solana_ids.TOKEN_PROGRAM_ID
TOKEN2022_PROGRAM = _solana_ids.TOKEN2022_PROGRAM_ID
WSOL = _solana_ids.WSOL_MINT

# Verified PumpSwap pool offsets (see module docstring).
POOL_QUOTE_VAULT_OFF = 139
POOL_BASE_VAULT_OFF = 171
# Signed virtual quote reserves (pump_amm IDL): boost_reserves
# - protocol_fees - creator_fees. The EXECUTABLE quote side is
# the vault amount PLUS this (official doc:
# docs/VIRTUAL_QUOTE_RESERVES_FEE_ADJUSTMENT.md): v2 trades keep
# fees inside the quote vault and boosts add reserves, so the
# raw vault amount is not the real liquidity. Absent on pools
# shorter than 261 bytes (pre-v2: virtual = 0).
POOL_VIRTUAL_QUOTE_OFF = 245

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


def _helius_keys():
    """All configured Helius keys, discovery first, distinct.

    The fleet shares one key today and 429s itself (20k+ 429s in
    the grad-select log alone). Rotating across both keys doubles
    the effective free quota. Free: both keys are already on disk
    for this project; nothing new is provisioned."""
    keys = []
    for env, fn in (("HELIUS_API_KEY_DISCOVERY", "api_key_discovery"),
                    ("HELIUS_API_KEY", "api_key")):
        v = os.environ.get(env)
        if not v:
            try:
                p = os.path.join(HOME, "helius", fn)
                if os.path.exists(p):
                    v = open(p).read().strip()
            except Exception:
                v = ""
        v = (v or "").strip()
        if v and v not in keys:
            keys.append(v)
    return keys


_URL_LOCK = threading.Lock()
_URL_SEQ = [0]

# 429 retry policy. 2026-11-16 (RPC-burn fix, Lever 2): the Helius
# legs NO LONGER retry — a 429 now engages the fleet-wide pacer
# backoff and the call fails fast, so a retry would only re-burn an
# exhausted bucket. 2026-11-18 (resilience build): the public leg
# does not retry either — _try_public is a single budgeted attempt
# with a hard stop on 429, never a retry into a 429. (The old
# _RETRY_* constants and the public fast-429 retry were removed
# with that change.)


def _shared_guard():
    """Fleet-wide Helius guard (patchable in tests).

    Still consulted by armed_watch (WSS gate) and instant_safety
    (hot-bucket skip); the pacer mirrors every outcome into it,
    so those readers see pacer cooldowns. reserves.rpc itself no
    longer gates on it — the pacer is the admission path."""
    return _helius_ratelimit.shared_guard()


def _pacer():
    """Fleet-wide Helius pacer (patchable in tests)."""
    return _helius_ratelimit.pacer()


# Vault-surrogate Helius key (2026-11-20, wired after the 200
# re-probe): the fresh vault key (custom.helius) is a third Helius
# source alongside the two disk keys. The surrogate rotates on every
# issuance, so the pacer bucket is keyed on the credential identity
# (stable) — NOT on the surrogate value, or every call would get a
# fresh bucket and the vault key would never be paced.
_VAULT_BUCKET = _helius_ratelimit.bucket_id_for_key("vault:custom.helius")


def _vault_url():
    """Vault-surrogate Helius URL, or None. Fail-soft: any failure
    (module missing, no helper, no credential, issuance error)
    returns None and the caller falls back to disk keys. The import
    is lazy so a broken vault module can never break the disk path.
    Never raises; never logs a URL (it carries the surrogate)."""
    try:
        try:
            from . import helius_vault as _hv
        except ImportError:
            import helius_vault as _hv  # type: ignore
        return _hv.vault_surrogate_url()
    except Exception:
        return None


# Alchemy primary tier (2026-11-23, §9 unified provider
# architecture): the ordered architecture makes Alchemy the PRIMARY
# routine acquisition leg (HTTP RPC, general transaction reads,
# normal scanning) and Helius SELECTIVE enrichment. This read path
# predates that order (Helius-first, built when Helius was the only
# keyed provider); the tier below wires the declared policy into the
# actual entry-critical reads. The key resolves through the SAME
# resolver the central router's adapter uses
# (providers/base.alchemy_key: env, then the two disk conventions) -
# one source of truth, no second credential path. The pacer bucket
# is namespaced ("alchemy:<key>") exactly like the vault bucket
# above, so the Alchemy leg gets the same per-key 429 pacing
# discipline, never a free pass.
_ALCHEMY_HTTPS_TMPL = "https://solana-mainnet.g.alchemy.com/v2/{key}"


def _alchemy_key():
    """The configured Alchemy key via the router's own resolver,
    or "" when unconfigured/unavailable. Never raises."""
    try:
        try:
            from daemon.intelligence.acquisition.providers import \
                base as _prov_base
        except ImportError:
            try:
                from intelligence.acquisition.providers import \
                    base as _prov_base  # type: ignore
            except ImportError:
                _prov_base = None
        if _prov_base is not None:
            return (_prov_base.alchemy_key(HOME) or "").strip()
        return os.environ.get("ALCHEMY_API_KEY", "").strip()
    except Exception:
        return ""


def _alchemy_url():
    """Alchemy primary-tier URL, or None. The saved value may be a
    bare key OR a full RPC URL (the ALCHEMY_API_KEY alias predates
    the key-only convention); accept both. Never logs either
    shape - both carry the credential."""
    raw = _alchemy_key()
    if not raw:
        return None
    if raw.startswith("https://") or raw.startswith("http://"):
        return raw
    return _ALCHEMY_HTTPS_TMPL.format(key=raw)


def rpc_url_pairs():
    """[(url, bucket)] with the Helius keys rotated per call.

    bucket is the shared-guard id for the key (sha256 prefix — never
    the key itself); None for the public endpoint, which is unguarded.
    rpc_urls() is the URL-only view of this list.

    The vault-surrogate key (helius_vault, wired 2026-11-20 after
    the 200 re-probe) is a third Helius source, appended after the
    disk keys. Fail-soft: when the surrogate is unavailable the
    pairs are exactly the disk keys + public, as before."""
    keys = _helius_keys()
    pairs = []
    # §9 order: Alchemy primary routine acquisition FIRST, then the
    # selective Helius keys, then the budgeted public leg (appended
    # below). Unconfigured Alchemy changes nothing for the existing
    # tiers.
    alchemy_url = _alchemy_url()
    if alchemy_url:
        # Bucket from the raw saved value (key OR full URL):
        # bucket_id_for_key sha256s it, so neither shape is ever
        # logged or persisted, and each distinct credential gets
        # its own pacing bucket.
        pairs.append((alchemy_url, _helius_ratelimit.bucket_id_for_key(
            "alchemy:" + _alchemy_key())))
    if keys:
        # Rotate the first key per call so consecutive calls start
        # on alternating keys and the load splits across both.
        with _URL_LOCK:
            _URL_SEQ[0] += 1
            rot = _URL_SEQ[0] % len(keys)
        ordered = keys[rot:] + keys[:rot]
        for k in ordered:
            pairs.append(("https://mainnet.helius-rpc.com/?api-key=" + k,
                          _helius_ratelimit.bucket_id_for_key(k)))
    vault_url = _vault_url()
    if vault_url:
        pairs.append((vault_url, _VAULT_BUCKET))
    pairs.append(("https://api.mainnet-beta.solana.com", None))
    return pairs


def rpc_urls():
    return [url for url, _bucket in rpc_url_pairs()]


# ---- public-RPC fallback budget (2026-11-18, resilience build) --------
# When Helius fails closed (429, or no pacer token on any key), the
# entry-critical reads fall through to the public mainnet-beta
# endpoint ONCE — but only under this budget: 1 call per 2s
# fleet-wide (cross-process, same flock + atomic-replace pattern as
# the pacer/guard), hard stop on 429. The budget is what keeps the
# fallback from becoming a second hammering path, and 2s is the
# 2026-11-18 evening fix for burst-throttling: when a fresh
# graduation arrives, 4 lanes evaluate it within seconds, but at
# 7s only the first got the budget and the rest failed fast
# ("budget spent") instead of trying — so a coin got one public
# attempt instead of up to four. At our demand (~60-100 calls/hr)
# the absolute load stays far below the public endpoint's
# documented limit (40 req/10s), so 2s is safe. Live probe
# 2026-11-18:
# getSignaturesForAddress on the pump program, 3 calls 7s apart,
# 3x HTTP 200 in ~0.7s — the leg is viable at low volume (it
# supersedes the 2026-11-16 measurement of 0/4 IncompleteReads
# while Helius 429d). Fail-soft: on any file/lock error the budget
# degrades to an in-process throttle, never to unpaced calls.
_PUBLIC_BUDGET_S = 2.0
_PUBLIC_BUDGET_FILE = "public_rpc_budget.json"
_PUBLIC_BUDGET_LOCK = "public_rpc_budget.lock"


class PublicRpcBudget:
    """Cross-process 1-call-per-2s budget for the public RPC leg."""

    def __init__(self, home=None):
        self._home = home or HOME
        self._state_file = os.path.join(self._home, _PUBLIC_BUDGET_FILE)
        self._lock_file = os.path.join(self._home, _PUBLIC_BUDGET_LOCK)
        self._local_last = 0.0

    def _read_last(self):
        try:
            with open(self._state_file, "r") as fh:
                return float((json.load(fh) or {}).get(
                    "last_call_ts", 0.0) or 0.0)
        except Exception:
            return 0.0

    def acquire(self, now=None):
        """True when a public-RPC call may proceed now (and record it).

        Never raises; never allows an unpaced call."""
        t = time.time() if now is None else now
        try:
            os.makedirs(self._home, exist_ok=True)
            lock_fh = open(self._lock_file, "a+")
        except Exception:
            return self._local_acquire(t)
        try:
            locked = False
            for _ in range(5):
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except Exception:
                    time.sleep(0.01)
            if not locked:
                return self._local_acquire(t)
            try:
                last = self._read_last()
                if t - last < _PUBLIC_BUDGET_S:
                    return False
                tmp = "%s.tmp.%d" % (self._state_file, os.getpid())
                with open(tmp, "w") as fh:
                    json.dump({"last_call_ts": t}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_file)
                self._local_last = t
                return True
            finally:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                except Exception:
                    pass
        except Exception:
            return self._local_acquire(t)
        finally:
            try:
                lock_fh.close()
            except Exception:
                pass

    def _local_acquire(self, t):
        if t - self._local_last < _PUBLIC_BUDGET_S:
            return False
        self._local_last = t
        return True


_PUBLIC_BUDGET = None


def _public_budget():
    """The fleet-wide public-RPC budget singleton (lazy)."""
    global _PUBLIC_BUDGET
    if _PUBLIC_BUDGET is None:
        _PUBLIC_BUDGET = PublicRpcBudget()
    return _PUBLIC_BUDGET


# ---- per-call source tracking -------------------------------------------
# rpc() stamps which provider answered the last call in THIS thread
# (Phase 1 evaluates in a 4-worker pool, so thread-local is the
# right scope). Grad-select's CurveStatsCheck reads it right after
# _curve_stats and stamps ctx.values["curve_trades_source"], which
# lands in the select journal — so the record shows whether Helius,
# the public fallback, or the shared cache answered each curve read.
_LAST_SOURCE = threading.local()


def last_rpc_source():
    """Source of the last rpc() call in this thread: "helius",
    "public-rpc", or None when the call failed / none was made."""
    return getattr(_LAST_SOURCE, "value", None)


def _rpc_impl_typed(method, params, timeout=12, exempt=False):
    """One JSON-RPC call with endpoint failover.

    Returns (result, kind): kind is "ok" (a result payload), "null"
    (an endpoint answered JSON null — a definitive empty answer, not
    a failure) or "error" (every endpoint failed). _rpc_impl keeps
    the historical result-or-None contract for the fleet.

    Failures are LOGGED per endpoint (host + exception type + HTTP
    code when present) instead of swallowed silently: the old bare
    `except: continue` turned every failure class into an identical
    bare None, which made the 2026-10-30 unknown_curve burst
    undiagnosable (every journal entry read curve_stats_error
    "None"). Return contract unchanged: result or None. Only the
    hostname is ever logged, never the URL: the Helius URL carries
    an api-key query parameter.

    2026-11-17 (pacing fix) — the Helius block is HTTP 429 rate
    limiting that FLAPS, not a spent monthly cap (measured: even
    1 req/5s/key succeeds only ~17%/8% under live load; 1 req/15s
    ~1/8 in a forced-quiet window). The old SharedGuard's 600s
    all-or-nothing lockout is REPLACED as the admission gate by
    the cross-process pacer (helius_ratelimit.SharedPacer): one
    token per 20s per key, capacity 2, so the fleet can never
    burst. Every Helius attempt first takes a pacer token for its
    key (rotation order); no token -> no HTTP attempt, and the
    next key is tried. A 429 engages progressive backoff (30s,
    doubling to 300s) with a SINGLE probe call at expiry to test
    recovery — one 429 no longer silences the bot for 10 minutes
    while Helius is actually answering. A success clears the
    cooldown fleet-wide at once. Non-429 failures still fail over
    to the next key / the public endpoint (a real outage, not a
    quota problem). When EVERY Helius key 429s, the public
    mainnet-beta endpoint is NOT tried: measured 0/4
    (IncompleteRead, 1.5-5.3s each) while Helius 429s, so the leg
    costs seconds for ~zero successes. It still runs when Helius
    fails with a non-429 error (a real outage). Callers fail
    closed on None either way; the verdict is identical, only
    faster.

    2026-11-18 (resilience build) — the public leg is now a REAL
    fallback, not a dead one: when Helius fails closed (429, or no
    pacer token on any key) the call tries api.mainnet-beta.solana.com
    ONCE under its own cross-process budget (1 call / 2s fleet-wide,
    hard stop on 429; one retry on a transport failure
    (IncompleteRead/disconnect/timeout), never on a 429 — the
    2026-11-18 evening probe measured first-try 6/8, with one
    retry 7/8, and the fleet-wide first-try rate is ~60%). Live
    probe 2026-11-18:
    getSignaturesForAddress, 3 calls 7s apart, 3x HTTP 200 in
    ~0.7s — viable at low volume; it supersedes the 2026-11-16
    0/4 IncompleteRead measurement that kept public out of the
    429 path. The vault-surrogate Helius key is WIRED (2026-11-20,
    after the re-probe returned HTTP 200 — the 2026-11-18 401 is
    gone; see helius_vault.py and rpc_url_pairs) as a third Helius
    source. 2026-11-23 (§9 wiring): the declared provider policy
    now orders the tiers here the way §9 orders them in the
    architecture - Alchemy (PRIMARY routine acquisition, when
    configured) -> Helius disk keys + vault key (selective) ->
    budgeted public RPC. The Alchemy leg takes one paced attempt
    and any failure falls through to the Helius tier unchanged.
    Every fallback attempt is logged with its source, and
    last_rpc_source() reports which provider answered, so the
    select journal records alchemy / helius / public-rpc per
    curve read.

    ``exempt`` (exit pricing only — see price_for_position): the
    call takes a PRIORITY pacer token (longer wait, may borrow
    one token of debt) — it never bypasses the pacer. A delayed
    armed-position exit is the one place throttling costs money,
    and exit-pricing volume is tiny (armed positions only, and
    only when the watcher push is stale), so the priority lane
    cannot reintroduce meaningful burn.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    pairs = rpc_url_pairs()
    alchemy_pairs = [(u, b) for (u, b) in pairs
                     if "g.alchemy.com" in u]
    helius_pairs = [(u, b) for (u, b) in pairs if "helius-rpc.com" in u]
    public_urls = [u for (u, _b) in pairs if "helius-rpc.com" not in u
                   and "g.alchemy.com" not in u]
    pacer = _pacer()
    _LAST_SOURCE.value = None
    # Alchemy primary tier (§9): one paced attempt; any failure falls
    # through to the Helius tier (controlled failover to the NEXT
    # tier, never a same-tier retry burn). A definitive null is
    # returned as-is (same index family, mainnet). Helius health
    # tracking is left untouched by this tier.
    for url, bucket in alchemy_pairs:
        if not bucket:
            continue
        if not pacer.acquire(bucket, priority=exempt):
            continue
        host = urllib.parse.urlparse(url).hostname or "unknown"
        result, kind, elapsed = _rpc_attempt(url, body, method,
                                             host, timeout)
        if kind == "ok":
            pacer.report(bucket, True)
            _LAST_SOURCE.value = "alchemy"
            return result, "ok"
        if kind == "null":
            pacer.report(bucket, True)
            _LAST_SOURCE.value = "alchemy"
            return None, "null"
        if kind == "429":
            pacer.report(bucket, False)
        break
    # Take ONE pacer token per logical call, first key (in rotation
    # order) that has one. No token anywhere -> Helius is throttled
    # or down: fall through to the public leg once (own budget,
    # _try_public) instead of giving up. (When no Helius keys are
    # configured at all, helius_pairs is empty and public is
    # still tried — the keyless fallback is unchanged.)
    acquired = None
    for url, bucket in helius_pairs:
        if not bucket:
            continue
        if pacer.acquire(bucket, priority=exempt):
            acquired = (url, bucket)
            break
    if acquired is None and helius_pairs:
        return _try_public_typed(body, method, public_urls, timeout)
    if acquired is not None:
        url, bucket = acquired
        host = urllib.parse.urlparse(url).hostname or "unknown"
        result, kind, elapsed = _rpc_attempt(url, body, method,
                                             host, timeout)
        if kind == "ok":
            pacer.report(bucket, True)
            _LAST_SOURCE.value = "helius"
            _note_helius_restored()
            return result, "ok"
        if kind == "null":
            # Definitive empty answer: the provider is healthy and
            # the same index backs every key, so failing over would
            # just re-ask the same question.
            pacer.report(bucket, True)
            _LAST_SOURCE.value = "helius"
            _note_helius_restored()
            return None, "null"
        if kind == "429":
            pacer.report(bucket, False)
            # Helius failed closed: one budgeted public attempt.
            return _try_public_typed(body, method, public_urls, timeout)
        # Non-429 failure: fall through to the next key below.
        # (The token for the failed key is spent; the next key
        # needs its own token.)
        for url2, bucket2 in helius_pairs:
            if url2 == url or not bucket2:
                continue
            if not pacer.acquire(bucket2, priority=exempt):
                continue
            host2 = urllib.parse.urlparse(url2).hostname or "unknown"
            result, kind, elapsed = _rpc_attempt(
                url2, body, method, host2, timeout)
            if kind == "ok":
                pacer.report(bucket2, True)
                _LAST_SOURCE.value = "helius"
                _note_helius_restored()
                return result, "ok"
            if kind == "null":
                pacer.report(bucket2, True)
                _LAST_SOURCE.value = "helius"
                _note_helius_restored()
                return None, "null"
            if kind == "429":
                pacer.report(bucket2, False)
                return _try_public_typed(body, method, public_urls, timeout)
            break
    return _try_public_typed(body, method, public_urls, timeout)


def _rpc_impl(method, params, timeout=12, exempt=False):
    """Back-compat wrapper: result or None (see _rpc_impl_typed)."""
    result, _kind = _rpc_impl_typed(method, params, timeout=timeout,
                                    exempt=exempt)
    return result


def rpc_typed(method, params, timeout=12, exempt=False):
    """(result, kind) variant of rpc() for callers that must tell a
    null ANSWER from a FAILURE (e.g. the provider adapter, which
    used to report every null payload as an outage). kind is "ok",
    "null" or "error". Same transport, pacer, failover and shadow
    observation as rpc().

    Phase 3 (shadow-first wiring): after the live call executes
    EXACTLY as before, the central router's routing decision for
    this call is recorded against the provider that actually served
    it (``daemon.intelligence.acquisition.shadow_observer``). The
    observer asks the router for a PURE decision (``route()`` — no
    transport, no network) and appends one JSONL line; it never
    sends, retries, or changes a request. The hook is best-effort
    and can never break the live path: any failure disables the
    observer for the process and the result is returned untouched.
    A null answer is journaled as "ok" (the provider answered),
    never as "error" (2026-11-22 null-vs-error fix).
    """
    import time as _time
    _start = _time.monotonic()
    result, kind = _rpc_impl_typed(method, params, timeout=timeout,
                                   exempt=exempt)
    try:
        from daemon.intelligence.acquisition import shadow_observer
        shadow_observer.observe_live_call(
            method, params, served=last_rpc_source(),
            kind=("error" if kind == "error" else "ok"),
            elapsed=_time.monotonic() - _start)
    except Exception:
        pass
    return result, kind


def rpc(method, params, timeout=12, exempt=False):
    """One JSON-RPC call with endpoint failover. Return contract
    unchanged: result or None (see _rpc_impl_typed). Null-vs-error
    callers use rpc_typed()."""
    result, _kind = rpc_typed(method, params, timeout=timeout,
                              exempt=exempt)
    return result


# Public-RPC fallback transition journal (advanced build, piece 10,
# 2026-11-21): the fallback switch decides for itself (Helius fails
# closed -> public mainnet-beta), but it is JOURNALED once per
# transition, not once per call — a sustained Helius outage would
# otherwise flood switch_log.jsonl with one line per curve read.
# _PUBLIC_FALLBACK_ON is True from the first _try_public entry
# until the next Helius success. Never gates a call; never raises.
_PUBLIC_FALLBACK_LOCK = threading.Lock()
_PUBLIC_FALLBACK_ON = [False]


def _note_public_fallback(method):
    try:
        if _switches is None:
            return
        with _PUBLIC_FALLBACK_LOCK:
            if _PUBLIC_FALLBACK_ON[0]:
                return
            _PUBLIC_FALLBACK_ON[0] = True
        _switches.log_switch(HOME, {
            "switch": "public_rpc_fallback", "action": "engage",
            "reason": ("helius failed closed, public mainnet-beta "
                       "attempted (%s)" % (method or "?"))})
    except Exception:
        pass


def _note_helius_restored():
    try:
        if _switches is None:
            return
        with _PUBLIC_FALLBACK_LOCK:
            if not _PUBLIC_FALLBACK_ON[0]:
                return
            _PUBLIC_FALLBACK_ON[0] = False
        _switches.log_switch(HOME, {
            "switch": "public_rpc_fallback", "action": "restore",
            "reason": "helius answered again, fallback stood down"})
    except Exception:
        pass


def _try_public_typed(body, method, public_urls, timeout):
    """(result, kind) variant of _try_public: kind is "ok", "null"
    or "error". One budgeted attempt at the public mainnet-beta
    endpoint.

    The last-resort leg when Helius fails closed: a single attempt
    under the cross-process 1-call-per-2s budget, hard stop on 429.
    A transport failure (IncompleteRead / disconnect / timeout) gets
    ONE retry on the same URL in the same budget slot — a truncated
    read is not a rate limit, and the 2026-11-18 probe measured
    first-try 6/8, with one retry 7/8 (fleet-wide first-try ~60%,
    so the retry lifts the fallback to ~85% per call). A 429 is
    never retried. Every attempt is
    logged with source=public-rpc, and a success stamps
    last_rpc_source() so the journal records which provider
    answered. Never raises."""
    _note_public_fallback(method)
    for url in public_urls:
        host = urllib.parse.urlparse(url).hostname or "unknown"
        if not _public_budget().acquire():
            logger.debug("reserves rpc %s: public-rpc budget spent, "
                         "skipping fallback", method)
            return None, "error"
        logger.warning("reserves rpc %s: Helius failed closed, "
                       "trying public-rpc (source=public-rpc)",
                       method)
        result, kind, _elapsed = _rpc_attempt(url, body, method,
                                              host, timeout)
        if kind == "ok":
            _LAST_SOURCE.value = "public-rpc"
            logger.warning("reserves rpc %s: public-rpc answered "
                           "(source=public-rpc)", method)
            return result, "ok"
        if kind == "null":
            # Definitive empty answer: no retry, nothing to fail
            # over to.
            _LAST_SOURCE.value = "public-rpc"
            return None, "null"
        if kind == "429":
            # Rate limit: hard stop, never retry a 429.
            logger.warning("reserves rpc %s: public-rpc failed (429), "
                           "no retry", method)
            return None, "error"
        # Transport failure: one retry, same URL, same budget slot.
        logger.warning("reserves rpc %s: public-rpc failed (%s), "
                       "one retry", method, kind)
        result, kind, _elapsed = _rpc_attempt(url, body, method,
                                              host, timeout)
        if kind == "ok":
            _LAST_SOURCE.value = "public-rpc"
            logger.warning("reserves rpc %s: public-rpc answered "
                           "on retry (source=public-rpc)", method)
            return result, "ok"
        if kind == "null":
            _LAST_SOURCE.value = "public-rpc"
            return None, "null"
        logger.warning("reserves rpc %s: public-rpc failed on retry "
                       "(%s), no further retry", method, kind)
        return None, "error"
    return None, "error"


def _try_public(body, method, public_urls, timeout):
    """Back-compat wrapper: result or None (see _try_public_typed)."""
    result, _kind = _try_public_typed(body, method, public_urls,
                                      timeout)
    return result


def _rpc_attempt(url, body, method, host, timeout):
    """One HTTP attempt. Returns (result, kind, elapsed_s) where
    kind is "ok" (result usable), "null" (the provider answered
    JSON null — a definitive empty answer, not a failure), "429"
    (rate-limited), or "fail" (anything else, already logged).
    Never raises.

    Transport is a curl subprocess (2026-11-20): urllib POSTs to
    these endpoints flap on this VM (HTTP 308, IncompleteRead,
    RemoteDisconnected) while curl succeeds — the same flake that
    starved the entry path during the Helius quiet period.
    curl's exit code and the HTTP status drive the kind mapping;
    any curl failure is "fail" (fail-closed). No redirects are
    followed: the Helius URL carries the api key in its query
    string, and a redirect would leak it. The URL itself is passed
    via a per-call 0600 curl config file (-K), never on the command
    line: argv is visible in ps / /proc/<pid>/cmdline, and the
    config file is deleted after the call. Never raises."""
    t0 = time.time()
    cfg_path = None
    try:
        fd, cfg_path = tempfile.mkstemp(prefix="reserves-curl-",
                                        suffix=".cfg")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write('url = "%s"\n' % url.replace("\\", "\\\\")
                     .replace('"', '\\"'))
        proc = subprocess.run(
            ["curl", "-sS", "-X", "POST", "-K", cfg_path,
             "-H", "Content-Type: application/json",
             "--data-binary", "@-",
             "--max-time", str(timeout),
             "-w", "\n__HTTP_STATUS__:%{http_code}"],
            input=body, capture_output=True, timeout=timeout + 5)
    except Exception as e:
        logger.warning("reserves rpc %s: %s curl failed (%s)",
                       method, host, type(e).__name__)
        return None, "fail", time.time() - t0
    finally:
        if cfg_path:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
    try:
        out = proc.stdout or b""
        if proc.returncode != 0:
            logger.warning("reserves rpc %s: %s curl exit %d (%s)",
                           method, host, proc.returncode,
                           (proc.stderr or b"")[:120].decode(
                               "utf-8", "replace"))
            return None, "fail", time.time() - t0
        text, sep, status = out.rpartition(b"\n__HTTP_STATUS__:")
        if not sep:
            logger.warning("reserves rpc %s: %s curl output "
                           "unparseable", method, host)
            return None, "fail", time.time() - t0
        try:
            http_code = int(status.strip())
        except ValueError:
            logger.warning("reserves rpc %s: %s curl bad HTTP "
                           "status %r", method, host, status[:20])
            return None, "fail", time.time() - t0
        if http_code == 429:
            return None, "429", time.time() - t0
        if http_code != 200:
            logger.warning("reserves rpc %s: %s HTTP %d",
                           method, host, http_code)
            return None, "fail", time.time() - t0
        d = json.loads(text)
        if isinstance(d, dict) and d.get("result") is not None:
            return d["result"], "ok", time.time() - t0
        if isinstance(d, dict) and d.get("error") is not None:
            err = d["error"] or {}
            code = err.get("code") if isinstance(err, dict) else None
            logger.warning("reserves rpc %s: %s returned JSON-RPC "
                           "error %s", method, host,
                           str(d["error"])[:160])
            # Helius also spells rate limits as a JSON-RPC error
            # (code -32005/-32029) with HTTP 200 — treat those as
            # 429s so the pacer backs off instead of failing over
            # and re-burning the next key (2026-11-17 pacing fix).
            if code in (-32005, -32029):
                return None, "429", time.time() - t0
            # Any other JSON-RPC error is a failure even when the
            # body also carries "result": null.
            return None, "fail", time.time() - t0
        if isinstance(d, dict) and "result" in d:
            # A completed call whose answer is JSON null (e.g.
            # getTransaction for a signature the node has not
            # indexed yet): the provider ANSWERED. Classifying it
            # as "fail" burned the second Helius key and the
            # public-RPC budget on every not-yet-indexed lookup
            # (2026-11-22 null-vs-error fix).
            return None, "null", time.time() - t0
        return None, "fail", time.time() - t0
    except Exception as e:
        logger.warning("reserves rpc %s: %s failed (%s)",
                       method, host, type(e).__name__)
        return None, "fail", time.time() - t0


def get_account(pubkey, exempt=False):
    """(owner, data_bytes) or (None, None). Never raises.

    ``exempt`` takes a PRIORITY pacer token (exit pricing only)
    — it never bypasses the pacer; see rpc()."""
    try:
        res = rpc("getAccountInfo", [pubkey, {"encoding": "base64"}],
                  exempt=exempt)
        v = (res or {}).get("value")
        if not v:
            return None, None
        return v.get("owner"), base64.b64decode(v["data"][0])
    except Exception:
        return None, None


def get_accounts(pubkeys, exempt=False):
    """{pubkey: (owner, data)} for existing accounts. Never raises."""
    out = {}
    try:
        res = rpc("getMultipleAccounts",
                  [list(pubkeys), {"encoding": "base64"}],
                  exempt=exempt)
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


def mint_decimals(mint, exempt=False):
    """Decimals for a mint (cached; pump tokens are 6)."""
    if mint in _DEC_CACHE:
        return _DEC_CACHE[mint]
    dec = None
    _owner, data = get_account(mint, exempt=exempt)
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


def parse_virtual_quote_reserves(data):
    """Pool::virtual_quote_reserves (signed i128 @245) from pool
    account bytes; 0 when the account predates the field (< 261
    bytes); None on parse failure. Per the official fee-adjustment
    doc the effective quote reserves = quote vault amount + this
    value (it carries the sign)."""
    try:
        if not data:
            return None
        if len(data) < POOL_VIRTUAL_QUOTE_OFF + 16:
            return 0
        return int.from_bytes(
            data[POOL_VIRTUAL_QUOTE_OFF:POOL_VIRTUAL_QUOTE_OFF + 16],
            "little", signed=True)
    except Exception:
        return None


def match_vaults(accounts, mint):
    """accounts: {pubkey: (owner, data)}. Returns (base_vault,
    quote_vault, quote_mint): base holds `mint`, quote is the OTHER
    token account — its mint IS the pool's quote asset. Usually
    WSOL, but PumpSwap pools can be quoted in other quote-control
    mints (PUMP-quoted pools exist: never assume SOL). Any
    ambiguity (duplicate base, a second quote mint, two candidate
    quote vaults) refuses: (None, None, None)."""
    base_v = quote_v = quote_mint = None
    for pk, (_owner, data) in (accounts or {}).items():
        ta = parse_token_account(data)
        if not ta:
            continue
        if ta["mint"] == mint:
            if base_v is not None:
                return None, None, None
            base_v = pk
        else:
            if quote_v is not None:
                return None, None, None
            quote_v = pk
            quote_mint = ta["mint"]
    if not base_v or not quote_v:
        return None, None, None
    return base_v, quote_v, quote_mint


def scan_pool_vaults(pool, mint, exempt=False):
    """Layout-free fallback: token accounts OWNED BY the pool
    account (vault authority = the pool itself on PumpSwap), via
    getProgramAccounts memcmp on both token programs."""
    for prog in (TOKEN_PROGRAM, TOKEN2022_PROGRAM):
        try:
            res = rpc("getProgramAccounts", [
                prog,
                {"encoding": "base64",
                 "filters": [{"memcmp": {"offset": 32,
                                         "bytes": pool}}]}],
                exempt=exempt)
            accts = {}
            for row in res or []:
                info = (row or {}).get("account") or {}
                d = info.get("data") or []
                if d:
                    accts[row["pubkey"]] = (
                        info.get("owner"), base64.b64decode(d[0]))
            b, q, _qm = match_vaults(accts, mint)
            if b and q:
                return b, q
        except Exception:
            continue
    return None, None


def resolve_vaults(pool, mint, exempt=False):
    """{base_vault, quote_vault, base_amount, quote_amount} for a
    PumpSwap pool, or None. Verifies by mint-matching; falls back to
    the program scan when the offset parse doesn't verify."""
    owner, data = get_account(pool, exempt=exempt)
    if owner != PUMPSWAP_PROGRAM or not data:
        return None
    vq = parse_virtual_quote_reserves(data)
    if vq is None:
        return None
    q_off, b_off = parse_pumpswap_vaults(data)
    if q_off and b_off:
        accts = get_accounts([q_off, b_off], exempt=exempt)
        b, q, qm = match_vaults(accts, mint)
        if b and q:
            return _vault_amounts(accts, b, q, qm, vq)
    b, q = scan_pool_vaults(pool, mint, exempt=exempt)
    if b and q:
        accts = get_accounts([b, q], exempt=exempt)
        _b, _q, qm = match_vaults(accts, mint)
        return _vault_amounts(accts, b, q, qm, vq)
    return None


def _vault_amounts(accts, base_v, quote_v, quote_mint=None,
                   quote_virtual=0):
    out = {"base_vault": base_v, "quote_vault": quote_v,
           "quote_mint": quote_mint, "quote_virtual": quote_virtual}
    for key, pk in (("base_amount", base_v), ("quote_amount", quote_v)):
        got = (accts or {}).get(pk)
        ta = parse_token_account(got[1]) if got else None
        if ta is None:
            return None
        out[key] = ta["amount"]
    # Effective quote reserves (official fee-adjustment formula):
    # vault amount + signed virtual. Non-positive means the pool
    # state is not priceable from these reads — refuse.
    out["quote_effective"] = out["quote_amount"] + quote_virtual
    if out["quote_effective"] <= 0:
        return None
    return out


def amounts_price(base_amount, quote_amount, base_decimals,
                  sol_usd=None, quote_mint=None):
    """USD price per token from raw vault amounts.
    price_quote = (quote/10^qdec) / (base/10^dec), converted at the
    quote asset's USD price. quote_mint None means the legacy
    SOL-quoted call (quote decimals 9); when a quote mint is given,
    its decimals are read (never assumed) and its USD price comes
    from quote_usd_price — for WSOL that is the SOL cache, where an
    injected sol_usd (the armed watcher's own cached source) takes
    precedence. None if any input is unusable."""
    try:
        if not base_amount or quote_amount is None or \
                base_decimals is None:
            return None
        if quote_mint is None or quote_mint == WSOL:
            qdec = 9
            qusd = sol_usd if sol_usd else sol_usd_price()
        else:
            qdec = quote_decimals(quote_mint)
            if qdec is None:
                return None
            qusd, _qts = quote_usd_price(quote_mint)
        if not qusd:
            return None
        price_quote = (quote_amount / (10 ** qdec)) / \
                      (base_amount / (10 ** base_decimals))
        return price_quote * qusd
    except Exception:
        return None


def pool_price_usd(pool, mint, exempt=False):
    """(price_usd, detail) from DIRECT PumpSwap vault balances.
    (0.0, why) when unreadable — callers fall back. Never raises."""
    try:
        if not pool or not mint:
            return 0.0, "no pool/mint"
        v = resolve_vaults(pool, mint, exempt=exempt)
        if not v:
            return 0.0, "vaults unresolved"
        dec = mint_decimals(mint, exempt=exempt)
        px = amounts_price(v["base_amount"], v["quote_effective"],
                           dec, quote_mint=v.get("quote_mint"))
        if not px:
            return 0.0, "amounts unusable (quote asset %s)" % \
                str(v.get("quote_mint") or "WSOL")[:8]
        return px, "pumpswap reserves %s/%s quote=%s eff=%+d" % (
            v["base_vault"][:8], v["quote_vault"][:8],
            str(v.get("quote_mint") or "WSOL")[:8],
            v["quote_virtual"])
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
    """{v_token, v_sol, r_token, r_sol, supply, complete, quote_mint}
    from curve account bytes, per the authoritative pump-public-docs
    IDL (quote-aware schema: quote_mint pubkey @83, so the account
    must be >= 115 bytes). `v_sol`/`r_sol` are the virtual/real
    QUOTE reserves, kept under the legacy key names for existing
    consumers — they are denominated in `quote_mint`, not always
    SOL. Accounts shorter than the documented schema match no
    supported layout and are refused (None) rather than priced from
    an assumed one."""
    try:
        if not data or len(data) < 115:
            return None
        return {"v_token": struct.unpack_from("<Q", data, 8)[0],
                "v_sol": struct.unpack_from("<Q", data, 16)[0],
                "r_token": struct.unpack_from("<Q", data, 24)[0],
                "r_sol": struct.unpack_from("<Q", data, 32)[0],
                "supply": struct.unpack_from("<Q", data, 40)[0],
                "complete": bool(data[48]),
                "quote_mint": b58encode(data[83:115])}
    except Exception:
        return None


def quote_usd_price(quote_mint, now=None):
    """(usd_price, observed_ts) of a curve's quote asset. WSOL maps
    to the SOL cache; any other quote mint is priced from
    DexScreener's token endpoint (deepest pair), cached 30s. Quote
    mints are configurable on-chain (QuoteControl), so there is no
    hardcoded allowlist — but a quote asset whose price cannot be
    established fails EXPLICITLY ((0.0, now)), never guessed."""
    now = time.time() if now is None else now
    if quote_mint == WSOL:
        return sol_usd_price(now), (_SOL_CACHE.get("ts") or now)
    ent = _QUOTE_USD_CACHE.get(quote_mint)
    if ent and ent["px"] > 0 and now - ent["ts"] < SOL_TTL_S:
        return ent["px"], ent["ts"]
    px = 0.0
    try:
        url = ("https://api.dexscreener.com/tokens/v1/solana/"
               + quote_mint)
        req = urllib.request.Request(
            url, headers={"User-Agent": "agt-reserves"})
        arr = json.loads(urllib.request.urlopen(req, timeout=8).read())
        best_liq = 0.0
        for pair in arr or []:
            try:
                p = float(pair.get("priceUsd") or 0)
                liq = float(((pair.get("liquidity") or {})
                             .get("usd")) or 0)
            except Exception:
                continue
            if p > 0 and liq > best_liq:
                best_liq, px = liq, p
    except Exception as e:
        logger.info("quote_usd_price %s failed: %s", quote_mint[:8], e)
    if px > 0:
        _QUOTE_USD_CACHE[quote_mint] = {"px": px, "ts": now}
    return px, now


# Quote-mint -> decimals cache: decimals are mint-immutable, so no
# TTL, but a lookup failure is NOT cached (retry next call).
_QUOTE_DEC_CACHE = {}


def quote_decimals(quote_mint, exempt=False):
    """Decimals of the quote asset: WSOL is canonically 9; other
    quote mints read their mint account (never assumed). None =
    unknown -> the caller refuses to price."""
    if quote_mint == WSOL:
        return 9
    if quote_mint in _QUOTE_DEC_CACHE:
        return _QUOTE_DEC_CACHE[quote_mint]
    dec = mint_decimals(quote_mint, exempt=exempt)
    if dec is not None:
        _QUOTE_DEC_CACHE[quote_mint] = dec
    return dec


def _curve_price_impl(mint, exempt=False):
    """(price_usd, observed_ts, detail). observed_ts is the OLDEST
    observation behind the number: the curve read happens at call
    time, but the quote-asset/USD conversion may come from a 30s
    cache — and, when its fetches fail, from an arbitrarily old
    cache entry. A caller that stamps freshness (account marks)
    must use observed_ts, never the wrap time."""
    read_ts = time.time()
    try:
        pda = derive_curve_pda(mint)
        if not pda:
            return 0.0, read_ts, "no curve pda"
        owner, data = get_account(pda, exempt=exempt)
        if owner != PUMP_PROGRAM:
            return 0.0, read_ts, ("curve account owner %s not the pump "
                                  "program; schema unverified"
                                  % str(owner)[:8])
        c = parse_bonding_curve(data)
        if not c or not c["v_token"]:
            return 0.0, read_ts, ("curve unreadable or unsupported "
                                  "layout (schema needs >= 115 bytes "
                                  "with quote_mint @83)")
        if c["complete"]:
            return 0.0, read_ts, "curve complete (graduated)"
        dec = mint_decimals(mint, exempt=exempt)
        if dec is None:
            return 0.0, read_ts, "decimals unknown"
        quote_mint = c["quote_mint"]
        qpx, qts = quote_usd_price(quote_mint)
        if not qpx:
            return 0.0, read_ts, ("no %s/usd (quote asset unpriceable; "
                                  "refusing to guess)" % quote_mint[:8])
        qdec = quote_decimals(quote_mint, exempt=exempt)
        if qdec is None:
            return 0.0, read_ts, ("quote asset %s decimals unknown; "
                                  "refusing to assume" % quote_mint[:8])
        price_usd = ((c["v_sol"] / (10 ** qdec))
                     / (c["v_token"] / (10 ** dec))) * qpx
        obs_ts = min(read_ts, qts)
        # Plausibility band, SECONDARY to the schema gate above (a
        # band cannot validate an unknown layout — 2026-11-22 live
        # evidence: a PUMP-quoted curve read as SOL-quoted landed
        # inside any reasonable SOL band while being ~27,778x off).
        # USD-denominated, generous: pump.fun spot prices run from
        # ~3e-6 USD at creation to ~7e-5 USD at graduation; the band
        # catches garbage decodes (zero/huge/negative), not wrong
        # layouts.
        if not (CURVE_PRICE_MIN_USD <= price_usd <= CURVE_PRICE_MAX_USD):
            return 0.0, read_ts, ("implausible curve price %.3g USD "
                                  "(band %.0g..%.0g); decode not "
                                  "trusted" % (price_usd,
                                               CURVE_PRICE_MIN_USD,
                                               CURVE_PRICE_MAX_USD))
        return price_usd, obs_ts, ("bonding curve %s quote=%s"
                                   % (pda[:8], quote_mint[:8]))
    except Exception as e:
        return 0.0, read_ts, "curve error: %s" % e


# Plausibility band for the curve spot price in USD (secondary to
# the schema gate; see _curve_price_impl): derived from the pump.fun
# creation/graduation price range with generous margin.
CURVE_PRICE_MIN_USD = 1e-9
CURVE_PRICE_MAX_USD = 1e-1


def curve_price_usd(mint, exempt=False):
    """(price_usd, detail) from the bonding curve's VIRTUAL reserves
    (the curve's own pricing inputs). (0.0, why) on failure."""
    px, _obs_ts, detail = _curve_price_impl(mint, exempt=exempt)
    return px, detail


def curve_price_observed(mint, exempt=False):
    """(price_usd, observed_ts, detail) — curve_price_usd with the
    true observation time of its oldest input (see _curve_price_impl).
    (0.0, read_ts, why) on failure."""
    return _curve_price_impl(mint, exempt=exempt)


# ---- SOL/USD ----------------------------------------------------------------

_SOL_CACHE = {"px": 0.0, "ts": 0.0}
# quote-mint -> {"px", "ts"}; same TTL as the SOL cache
_QUOTE_USD_CACHE = {}
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
    to DexScreener/GT. Never raises.

    Exit pricing takes a PRIORITY pacer token (exempt=True below):
    it waits longer and may borrow one token of debt, but it never
    bypasses the pacer — the old guard exemption that let exit
    pricing fire while the fleet was rate-limited (and re-trip the
    limit) is closed (2026-11-17 pacing fix). A delayed
    armed-position exit is the one place throttling costs money,
    and exit-pricing volume is tiny (armed positions only, only
    when the watcher push is stale). Every other caller of the
    pricing chain takes a normal token."""
    try:
        mint = str((pos or {}).get("mint") or "")
        if not mint:
            return 0.0, "no mint"
        pair = str((pos or {}).get("pair") or "")
        if pair:
            px, why = pool_price_usd(pair, mint, exempt=True)
            if px > 0:
                return px, "reserves:pumpswap"
            # Pair may be a Raydium pool or a stale curve address:
            # fall through to the curve only for un-graduated mints.
        px, why2 = curve_price_usd(mint, exempt=True)
        if px > 0:
            return px, "reserves:curve"
        return 0.0, "pool: %s; curve: %s" % (why if pair else "no pair",
                                             why2)
    except Exception as e:
        return 0.0, "reserves error: %s" % e
