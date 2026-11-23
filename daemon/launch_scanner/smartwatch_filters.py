#!/usr/bin/env python3
"""Staleness + convergence filters for the smart-wallet watcher (2026-10-17).

Two research-backed gates (field consensus, 2025-2026) that decide whether
a detected smart-wallet buy becomes a card:

1. STALENESS (50%-pump skip rule): skip the card when the token already
   pumped >=50% since the FIRST wallet's actual on-chain buy price. The
   buy price comes from the wallet's buy transaction itself
   (SOL spent / tokens received) — not from our (late) detection price.
   Fail-open: if the buy price can't be parsed, the filter doesn't block.

2. CONVERGENCE (multi-wallet confirmation): fire only when >=2
   INDEPENDENT wallets buy the same mint within 5 minutes. Independence
   = different wallet groups (label stem; the known pack-sniper pack
   counts as ONE actor). Single-wallet buys sit in the convergence ledger
   for 5 minutes awaiting confirmation, then expire silently (logged).

Measurement note: none of this grades, scores, or removes wallets — it
only decides card firing. Never raises.
"""

from __future__ import annotations

import json
import functools
import fcntl
import tempfile
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("forex_agent.daemon.launch_scanner.smartwatch_filters")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_LEDGER_FILE = _HOME / "launch_scanner" / "smart_convergence.json"

# Knobs (research values; window tightened 2026-10-17 on our own data:
# median co-buy gap 4 min; 5-min window keeps ~62% of historical
# convergences with the tightest "together" requirement. Reversible.)
CONVERGENCE_WALLETS = 2        # independent wallets required to fire
CONVERGENCE_WINDOW_S = 5 * 60   # sightings must fall within 5 min
STALE_PUMP_MULT = 1.5          # skip when price >= 1.5x first buy price


def wallet_group(wallet: str, registry: Dict[str, Dict[str, Any]]) -> str:
    """Actor group for a wallet: label stem with trailing -N stripped.

    pack-sniper-1/2/3 -> "pack-sniper" (one actor). Unknown wallet ->
    first 8 chars of the address (each unknown = its own actor).
    Never raises.
    """
    try:
        label = ""
        info = (registry or {}).get(wallet)
        if isinstance(info, dict):
            label = str(info.get("label") or "")
        stem = re.sub(r"-\d+$", "", label.strip().lower())
        if stem:
            return stem
        return (wallet or "")[:8]
    except Exception:
        return (wallet or "")[:8]


def is_stale(buy_price_sol: float, price_usd: float, sol_usd: float,
             stale_mult: float = STALE_PUMP_MULT) -> bool:
    """True when the token pumped >= stale_mult since the wallet's buy.

    buy_price_sol: SOL per token from the wallet's buy tx (0 = unknown).
    price_usd / sol_usd: current token price converted to SOL per token.
    Fail-open: unknown buy price or bad inputs -> False (don't block).
    Never raises.
    """
    try:
        buy_price_sol = float(buy_price_sol or 0)
        price_usd = float(price_usd or 0)
        sol_usd = float(sol_usd or 0)
        if buy_price_sol <= 0 or price_usd <= 0 or sol_usd <= 0:
            return False
        current_sol = price_usd / sol_usd
        return current_sol >= buy_price_sol * float(stale_mult)
    except Exception:
        return False


def eval_convergence(sightings: List[Dict[str, Any]],
                     window_s: float = CONVERGENCE_WINDOW_S,
                     required: int = CONVERGENCE_WALLETS) -> Tuple[int, bool]:
    """Count distinct actor groups among sightings in the window.

    Window is measured from the FIRST sighting's buy_ts. Returns
    (n_distinct_groups, converged). Never raises.
    """
    try:
        if not sightings:
            return 0, False
        first = min(float(s.get("buy_ts") or 0) for s in sightings)
        groups = {str(s.get("group") or "?")
                  for s in sightings
                  if float(s.get("buy_ts") or 0) - first <= float(window_s)}
        n = len(groups)
        return n, n >= int(required)
    except Exception:
        return 0, False


def is_expired(first_ts: float, now_ts: float,
               window_s: float = CONVERGENCE_WINDOW_S) -> bool:
    """True when a mint's convergence window lapsed with no confirmation."""
    try:
        return float(now_ts) - float(first_ts) > float(window_s)
    except Exception:
        return False


class ConvergenceUnavailable(ValueError):
    pass


def serialized_convergence(fn):
    """Serialize the entire production read/modify/write, not just save."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        _LEDGER_FILE.parent.mkdir(parents=True,exist_ok=True)
        with open(str(_LEDGER_FILE)+'.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return fn(*args,**kwargs)
    return wrapped


def load_convergence() -> Dict[str, Any]:
    """Missing starts empty; existing unavailable state never resets.

    Callers doing read/modify/write must hold serialized_convergence for
    the whole operation. The public load/save pair alone is not a transaction.
    """
    try:
        data = json.loads(_LEDGER_FILE.read_text())
    except FileNotFoundError:
        return {}
    except (OSError,ValueError) as exc:
        raise ConvergenceUnavailable('convergence state unavailable') from exc
    if not isinstance(data,dict):
        raise ConvergenceUnavailable('invalid convergence root')
    for mint,rec in data.items():
        if (not isinstance(mint,str) or not isinstance(rec,dict)
                or not isinstance(rec.get('sightings'),list)
                or any(not isinstance(item,dict) for item in rec['sightings'])):
            raise ConvergenceUnavailable('invalid convergence record')
    return data


def save_convergence(ledger: Dict[str, Any]) -> bool:
    tmp=None
    try:
        _LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=_LEDGER_FILE.parent,prefix='.convergence-')
        os.fchmod(fd,0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(ledger, f,allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,_LEDGER_FILE)
        directory_fd = os.open(_LEDGER_FILE.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except (OSError,ValueError,TypeError) as exc:
        logger.warning('could not save convergence ledger: %s',exc)
        return False
    finally:
        if tmp and os.path.exists(tmp):os.unlink(tmp)


def add_sighting(ledger: Dict[str, Any], mint: str, wallet: str,
                 group: str, buy_ts: float, buy_price_sol: float,
                 sol_spent: float, now_ts: float) -> Dict[str, Any]:
    """Append one wallet-buy sighting; returns the mint's record."""
    rec = ledger.setdefault(mint, {"sightings": [], "first_ts": now_ts,
                                   "card_fired": False})
    # Same actor buying again doesn't add convergence — but keep the
    # earliest buy price (strictest staleness anchor).
    for s in rec["sightings"]:
        if s.get("wallet") == wallet:
            return rec
    rec["sightings"].append({
        "wallet": wallet,
        "group": group,
        "buy_ts": float(buy_ts or now_ts),
        "detect_ts": float(now_ts),
        "buy_price_sol": float(buy_price_sol or 0),
        "sol_spent": float(sol_spent or 0),
    })
    if float(rec.get("first_ts") or 0) <= 0:
        rec["first_ts"] = float(now_ts)
    return rec
