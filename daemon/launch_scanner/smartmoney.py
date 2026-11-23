"""Smart-money wallet tracking (Helius).

Watches curated wallets via Helius RPC. When tracked wallets buy a token
we're watching as a candidate, the candidate earns a smart-money boost:
fewer rising polls needed to confirm, plus a line on the card.

API key: ``~/.forex-agent/helius/api_key`` (0600) or ``HELIUS_API_KEY`` env.
Helius free tier is enough for a small wallet list polled every few
minutes. Without a key the module idles — nothing breaks.

Buy detection: for each new wallet transaction, compare pre/post token
balances; a balance increase for (wallet, mint) means the wallet bought
that mint. Heuristic, read-only, never raises.

Wallet registry and activity live in daemon state::

    state["smart_wallets"]  = {wallet: {label, added_ts, picks, wins}}
    state["smart_activity"] = {mint: [wallets that bought it recently]}
    state["smart_last_sig"] = {wallet: last_seen_signature}
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import helius_ratelimit

logger = logging.getLogger("forex_agent.daemon.launch_scanner.smartmoney")

_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_KEY_FILE = _HOME / "helius" / "api_key"
# Second key, research-only: the wallet-discovery sweep and any other
# heavy one-off research use this key so they never contend with the
# live signals' budget on the main key. Absent -> discovery falls back
# to the main key with a warning (and its contention risk).
_DISCOVERY_KEY_FILE = _HOME / "helius" / "api_key_discovery"
_REGISTRY_FILE = _HOME / "launch_scanner" / "smart_wallets.json"
_TIMEOUT = 20
_SIG_LIMIT = 10
_MAX_WALLETS_PER_POLL = 20
_MAX_REGISTRY = 50

_key_cache: Optional[str] = None
_key_checked = False
_discovery_key_cache: Optional[str] = None
_discovery_key_checked = False
# Independent rate-limit bucket for the discovery key: research traffic
# must never trip (or be tripped by) the live signals' cooldown.
_discovery_guard = helius_ratelimit.new_guard()


def api_key() -> Optional[str]:
    """Helius key from the 0600 file or env. Cached; None when absent.

    2026-11-22 (dead-key fix): when the main key file is absent
    (quarantined after it started returning HTTP 401), fall back
    to the discovery key file directly (not via
    discovery_api_key(), keeping the two readers acyclic) so the
    wallet poll keeps working on the remaining live key instead
    of going dark. Restoring a dedicated main key takes a valid
    key back in the main key file plus a restart."""
    global _key_cache, _key_checked
    if _key_checked:
        return _key_cache
    _key_checked = True
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        try:
            key = _KEY_FILE.read_text().strip()
        except OSError:
            key = ""
    if not key:
        key = os.environ.get("HELIUS_API_KEY_DISCOVERY", "").strip()
        if not key:
            try:
                key = _DISCOVERY_KEY_FILE.read_text().strip()
            except OSError:
                key = ""
        if key:
            logger.warning(
                "smartmoney: main Helius key absent, using "
                "discovery key fallback")
    _key_cache = key or None
    return _key_cache


def discovery_api_key() -> Optional[str]:
    """Research-only Helius key: ``HELIUS_API_KEY_DISCOVERY`` env or the
    0600 ``~/.forex-agent/helius/api_key_discovery`` file. Cached; None
    when absent (the sweep then falls back to the main key)."""
    global _discovery_key_cache, _discovery_key_checked
    if _discovery_key_checked:
        return _discovery_key_cache
    _discovery_key_checked = True
    key = os.environ.get("HELIUS_API_KEY_DISCOVERY", "").strip()
    if not key:
        try:
            key = _DISCOVERY_KEY_FILE.read_text().strip()
        except OSError:
            key = ""
    _discovery_key_cache = key or None
    return _discovery_key_cache


def discovery_key_available() -> bool:
    """True when a dedicated research key is configured."""
    return bool(discovery_api_key())


def reset_key_caches() -> None:
    """Test hook: forget cached keys so env/file changes take effect."""
    global _key_cache, _key_checked
    global _discovery_key_cache, _discovery_key_checked
    _key_cache, _key_checked = None, False
    _discovery_key_cache, _discovery_key_checked = None, False
    _discovery_guard.reset()


def _rpc(method: str, params: List[Any]) -> Any:
    """One Helius call through the fleet-wide pacer (2026-11-17
    pacing fix). The old in-process-only guard let the scanner
    daemon poll wallets on its own private cooldown while the
    traders hammered the same key; now every attempt first takes
    a pacer token for the main-key bucket. Never raises."""
    key = api_key()
    if not key:
        return None
    bucket = helius_ratelimit.bucket_id_for_key(key)
    # Auto-shed (2026-11-18): smart-money background checks stop
    # attempting while the bucket is in backoff (logged once per
    # episode) instead of competing for entry-critical tokens.
    if not helius_ratelimit.background_call_allowed(bucket):
        helius_ratelimit.log_backoff_episode_once(
            bucket, "smartmoney", logger)
        return None
    # wait_s=0: take a token only if one is free right now.
    if not helius_ratelimit.pacer_acquire(bucket, wait_s=0):
        return None  # pacer: no token — fail-soft like any miss
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method,
         "params": params}).encode()
    req = urllib.request.Request(
        _RPC_URL.format(key=key), data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(
                resp.read().decode("utf-8", "replace")).get("result")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            helius_ratelimit.pacer_report(bucket, False)
            logger.warning("helius %s rate-limited (429); pacer "
                           "backoff engaged", method)
        else:
            logger.warning("helius %s failed: HTTP %s", method, exc.code)
        return None
    except Exception as exc:
        logger.warning("helius %s failed: %s", method, exc)
        return None
    helius_ratelimit.pacer_report(bucket, True)
    return result


def discovery_rpc(method: str, params: List[Any]) -> Any:
    """Helius RPC on the research key, with its own rate-limit bucket.

    This is the ``rpc`` to inject into ``wallet_discovery.run_discovery``:
    heavy one-off research that must never contend with (or be starved
    by) the live signals on the main key. Falls back to the main key's
    ``_rpc`` when no discovery key is configured, with a warning.
    Never raises.
    """
    key = discovery_api_key()
    if not key:
        logger.warning("discovery_rpc: no research key configured; "
                       "falling back to the main key (contention risk)")
        return _rpc(method, params)
    bucket = helius_ratelimit.bucket_id_for_key(key)
    # Auto-shed (2026-11-18): same as _rpc, research key.
    if not helius_ratelimit.background_call_allowed(bucket):
        helius_ratelimit.log_backoff_episode_once(
            bucket, "smartmoney-discovery", logger)
        return None
    # wait_s=0: take a token only if one is free right now.
    if not helius_ratelimit.pacer_acquire(bucket, wait_s=0):
        return None  # pacer: no token — fail-soft like any miss
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method,
         "params": params}).encode()
    req = urllib.request.Request(
        _RPC_URL.format(key=key), data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(
                resp.read().decode("utf-8", "replace")).get("result")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            helius_ratelimit.pacer_report(bucket, False)
            logger.warning("discovery helius %s rate-limited (429); "
                           "pacer backoff engaged", method)
        else:
            logger.warning("discovery helius %s failed: HTTP %s",
                           method, exc.code)
        return None
    except Exception as exc:
        logger.warning("discovery helius %s failed: %s", method, exc)
        return None
    helius_ratelimit.pacer_report(bucket, True)
    return result


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _bought_mints(wallet: str, signature: str):
    """None is unavailable/invalid, set() is a successful no-increase."""
    from .wallet_transaction import wallet_balance_evidence
    tx=_rpc('getTransaction',[signature,{'encoding':'jsonParsed','maxSupportedTransactionVersion':0}])
    evidence=wallet_balance_evidence(wallet,signature,tx)
    if evidence is None:return None
    return {mint for mint,amount in evidence['after'].items()
            if amount>evidence['before'].get(mint,0.0)}


def load_registry() -> Dict[str, Dict[str, Any]]:
    """Wallet registry from the shared JSON file (both daemons use it)."""
    try:
        data = json.loads(_REGISTRY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_registry(wallets: Dict[str, Dict[str, Any]]) -> bool:
    try:
        _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REGISTRY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(wallets))
        os.chmod(tmp, 0o600)
        tmp.replace(_REGISTRY_FILE)
        return True
    except OSError as exc:
        logger.warning("could not save smart wallet registry: %s", exc)
        return False


def _intake():
    from .wallet_intake import WalletIntake
    return WalletIntake(_HOME,filename='scanner_wallet_intake.json')


def poll_wallets(state: Dict[str, Any], poll_seconds: float = 180.0) -> Dict[str,List[str]]:
    """Existing-cadence discovery plus durable at-least-once handoff.

    The caller MUST acknowledge only after durable downstream acceptance.
    Restart can replay returned activity, never infer accepted from a cursor.
    """
    now=time.time()
    # A loaded committed batch survived downstream state persistence but
    # may have crashed before outbox acceptance. Consume only that batch.
    if state.get('smart_committed_refs'):
        intake=_intake()
        for wallet,signatures in state['smart_committed_refs'].items():
            intake.accept_deliveries(wallet,signatures)
        state['smart_committed_refs']={}
    state['smart_delivery_refs']={}
    if not api_key():return {}
    if now-float(state.get('smart_last_poll',0))<poll_seconds:return {}
    state['smart_last_poll']=now
    intake=_intake();fresh={};activity=state.setdefault('smart_activity',{})
    delivery_refs={}
    for wallet in list(load_registry())[:_MAX_WALLETS_PER_POLL]:
        old=intake.wallet(wallet)
        try:
            page=_rpc('getSignaturesForAddress',[wallet,{'limit':_SIG_LIMIT}])
            if page:
                if not isinstance(page,list) or any(not isinstance(item,dict) or not isinstance(item.get('signature'),str) or not item['signature'] for item in page):
                    raise ValueError('invalid signature page')
                previous=(old or {}).get('cursor') or state.get('smart_last_sig',{}).get(wallet)
                signatures=[]
                for item in page:
                    if item['signature']==previous:break
                    signatures.append(item['signature'])
                gap='limited_signature_window' if previous and previous not in [item['signature'] for item in page] else None
                intake.stage(wallet,(old or {}).get('cursor'),page[0]['signature'],list(reversed(signatures)),gap=gap)
        except (ValueError,TypeError,AttributeError):
            logger.warning('scanner wallet signature discovery unavailable: %s',wallet[:8])
        current=intake.wallet(wallet) or {}
        deliveries=current.get('deliveries',{})
        for signature in current.get('pending',[])[:_SIG_LIMIT]:
            if signature not in deliveries:
                mints=_bought_mints(wallet,signature)
                time.sleep(.15)
                if mints is None:
                    intake.defer(wallet,signature);continue
                intake.deliver(wallet,signature,sorted(mints))
        current=intake.wallet(wallet) or {}
        for signature,mints in current.get('deliveries',{}).items():
            delivery_refs.setdefault(wallet,[]).append(signature)
            for mint in mints:
                holders=activity.setdefault(mint,[])
                if wallet not in holders:holders.append(wallet)
                wallets=fresh.setdefault(mint,[])
                if wallet not in wallets:wallets.append(wallet)
    # References are handoff metadata, not acknowledgement. The journal
    # remains authoritative across scanner-state persistence/restart.
    state['smart_delivery_refs']=delivery_refs
    if len(activity)>500:
        for mint in list(activity)[:len(activity)-500]:activity.pop(mint,None)
    return fresh


def acknowledge_wallet_activity(state):
    intake=_intake()
    for wallet,signatures in (state.get('smart_delivery_refs') or {}).items():
        intake.accept_deliveries(wallet,signatures)
    state['smart_delivery_refs']={}
    state['smart_committed_refs']={}


def smart_buys_for(state: Dict[str, Any], mint: str) -> int:
    """How many tracked wallets recently bought this mint."""
    return len((state.get("smart_activity") or {}).get(mint, []))


def record_outcome(mint: str, won: bool,
                   wallets_involved: List[str]) -> None:
    """Score wallets that bought a closed runner: picks+1, wins+=won."""
    wallets = load_registry()
    changed = False
    for wallet in wallets_involved:
        w = wallets.get(wallet)
        if w is not None:
            w["picks"] = int(w.get("picks") or 0) + 1
            w["wins"] = int(w.get("wins") or 0) + (1 if won else 0)
            changed = True
    if changed:
        save_registry(wallets)


def add_wallet(wallet: str, label: str = "") -> Tuple[bool, str]:
    wallet = (wallet or "").strip()
    if not (32 <= len(wallet) <= 48):
        return False, "that doesn't look like a Solana wallet address"
    wallets = load_registry()
    if wallet in wallets:
        return False, "already tracked"
    if len(wallets) >= _MAX_REGISTRY:
        return False, "wallet list is full (%d)" % _MAX_REGISTRY
    wallets[wallet] = {"label": label[:32], "added_ts": time.time(),
                       "picks": 0, "wins": 0}
    if save_registry(wallets):
        return True, "tracking %s" % (label or wallet[:8] + "…")
    return False, "could not save the wallet list"


def remove_wallet(wallet: str) -> Tuple[bool, str]:
    wallet = (wallet or "").strip()
    wallets = load_registry()
    if wallet not in wallets:
        return False, "not in the tracked list"
    wallets.pop(wallet)
    if save_registry(wallets):
        return True, "removed"
    return False, "could not save the wallet list"


def wallet_score(w: Dict[str, Any]) -> float:
    picks = int(w.get("picks") or 0)
    if not picks:
        return 0.0
    return round(100.0 * int(w.get("wins") or 0) / picks, 1)


# ---------------------------------------------------------------------------
# Smart-buy fast-track
#
# When a tracked wallet buys a mint that is NEW to us, the normal pipeline
# waits for 2-3 rising polls before a card goes out. Fast-track skips the
# wait: the safety gate runs immediately and, on pass, an instant card is
# emitted. Hard fails (honeypot / can't-sell / insider-cluster / unknown
# report) ALWAYS block — a wallet buy never overrides the gate.
#
# evaluate_fasttrack() is pure decision logic: it returns a trigger payload
# or None. The integration step (daemon/launch_scanner/__init__.py) owns
# the publish path and MUST:
#   * call the EXISTING LaunchScanner._early_allowed() before publishing —
#     fast-track cards count toward the 4/hour instant cap exactly like
#     normal instant cards (never publish directly);
#   * register the new event fields in agent/events/bus.py EVENT_SCHEMAS
#     for "launch.early": "smart_money", "smart_wallets", "fasttrack";
#   * pass verdict_fn=LaunchScanner._instant_verdict so the single choke
#     point stays the single choke point (fail-closed).
# ---------------------------------------------------------------------------

_FASTTRACKED_KEY = "smart_fasttracked"
_FASTTRACK_PRUNE_DAYS = 7.0


def mint_is_new(state: Dict[str, Any], mint: str) -> bool:
    """Default ``is_new`` check for fast-track.

    A mint is new-to-us when it has never been carded or tracked: not in
    the scanner's seen entries, live candidates, milestone watch, and not
    already evaluated by fast-track. The integrator may pass its own
    callable instead for stricter semantics.
    """
    if not mint:
        return False
    if mint in (state.get(_FASTTRACKED_KEY) or {}):
        return False
    if mint in (state.get("seen") or {}):
        return False
    if mint in (state.get("candidates") or {}):
        return False
    if mint in (state.get("milestone_watch") or {}):
        return False
    return True


def wallet_labels_for(state: Dict[str, Any], mint: str) -> List[str]:
    """Registry labels of tracked wallets that recently bought this mint.

    Labels (never raw addresses) are what may go on a Telegram card.
    """
    addrs = (state.get("smart_activity") or {}).get(mint) or []
    reg = load_registry()
    out: List[str] = []
    for a in addrs:
        info = reg.get(a) or {}
        out.append(info.get("label") or (str(a)[:8] + "…"))
    return out


def evaluate_fasttrack(state: Dict[str, Any], degen: Any, mint: str,
                       wallet_names: List[str], is_new: Any = True,
                       verdict_fn: Any = None) -> Optional[Dict[str, Any]]:
    """Decide whether a first-seen wallet buy fast-tracks an instant card.

    Args:
        state: the launch_scanner daemon state dict (mutated: records
            every evaluated mint in ``state["smart_fasttracked"]`` for
            idempotency — a mint is NEVER evaluated twice).
        degen: config object; reads ``smart_fasttrack_enabled``
            (default True) and passes through to ``verdict_fn``.
        mint: the bought mint address.
        wallet_names: labels of the tracked wallets that bought it
            (e.g. ["sniper-1"]). ≥1 required.
        is_new: bool or callable ``(mint) -> bool``. The integrator must
            pass something meaningful here — e.g. the
            :func:`mint_is_new` default, or a stricter callable also
            checking digest/skipped buffers. A plain ``True`` is accepted
            but the internal fast-tracked set still prevents re-fires.
        verdict_fn: callable ``(degen, candidate) -> (allow, reason,
            report)`` — the integrator passes
            ``LaunchScanner._instant_verdict``. REQUIRED: with no verdict
            source the function returns None (fail-closed). The candidate
            passed to it is ``{"mint": mint, "chain": "solana"}``.

    Returns:
        A trigger payload on safety pass, else None::

            {"trigger": "smart_fasttrack", "mint": mint, "chain": "solana",
             "wallets": [...labels...], "reason": reason,
             "safety_score": score|None, "report": report,
             "card_source": "smart-fasttrack"}

        The payload is NOT a published event: the integrator builds the
        ``launch.early`` event from it (``smart_money`` string + the new
        ``smart_wallets`` / ``fasttrack`` fields), runs it through
        ``_early_allowed()`` for the 4/hour cap, then publishes. A blocked
        (held) mint is recorded in ``smart_fasttracked`` too, so a
        honeypot a wallet bought is never re-evaluated on the next poll —
        it stays eligible for the normal scan path.
    """
    now = time.time()
    try:
        if not getattr(degen, "smart_fasttrack_enabled", True):
            return None
        if not mint or not wallet_names:
            return None
        try:
            new = bool(is_new(mint)) if callable(is_new) else bool(is_new)
        except Exception:
            new = False
        fasttracked = state.setdefault(_FASTTRACKED_KEY, {})
        # Prune stale idempotency records (bounded growth).
        cutoff = now - _FASTTRACK_PRUNE_DAYS * 86400.0
        for m in [m for m, r in fasttracked.items()
                  if float((r or {}).get("ts") or 0) < cutoff]:
            fasttracked.pop(m, None)
        if mint in fasttracked:
            return None  # already evaluated — no double-trigger, ever
        if not new:
            return None
        if verdict_fn is None:
            # Fail-closed: no real safety verdict source, no fast-track.
            logger.warning("fast-track %s held: no verdict source", mint)
            return None
        candidate = {"mint": mint, "chain": "solana"}
        try:
            allow, reason, report = verdict_fn(degen, candidate)
        except Exception as exc:
            logger.warning("fast-track verdict crashed for %s: %s",
                           mint, exc)
            fasttracked[mint] = {"ts": now,
                                 "wallets": list(wallet_names),
                                 "verdict": "verdict-error"}
            return None
        fasttracked[mint] = {"ts": now, "wallets": list(wallet_names),
                             "verdict": str(reason)}
        if not allow:
            # Held by the gate — silent. The mint remains eligible for the
            # normal scan pipeline; fast-track will not retry it.
            logger.warning("FASTTRACK HELD %s: %s", mint, reason)
            return None
        score: Optional[float] = None
        try:
            score = float((report or {}).get("score"))
        except (TypeError, ValueError):
            score = None
        logger.warning("FASTTRACK %s bought by %s (%s)",
                       mint, ", ".join(wallet_names), reason)
        return {
            "trigger": "smart_fasttrack",
            "mint": mint,
            "chain": "solana",
            "wallets": list(wallet_names),
            "reason": str(reason),
            "safety_score": score,
            "report": report,
            "card_source": "smart-fasttrack",
        }
    except Exception as exc:
        logger.warning("evaluate_fasttrack failed for %s: %s", mint, exc)
        return None
