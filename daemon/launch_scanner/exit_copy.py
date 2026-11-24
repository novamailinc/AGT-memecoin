"""Exit-copying — MEASUREMENT ONLY (safe batch 2026-10-09).

For coins the bot tracks, record when a curated smart wallet EXITS vs
when our paper position exited. The weekly review answers: "would
copying their exits have helped?" — with exit ORDERING and our own
realized pnl. Counterfactual profits are never fabricated: we report
who left first and what we actually made, nothing more.

How it works:
  - record_close(...): at close time, stores our exit (mint, entry/
    close ts, close multiple, reason, pnl). One additive call at the
    existing close site.
  - daily_pass(state, degen): once per WAT calendar day, for recent
    closes lacking wallet-exit data, checks the wallets that BOUGHT
    this mint (state["smart_activity"][mint]) for sells after our
    entry. Helius calls go through the shared helius_ratelimit guard,
    are budget-capped (default 40 tx parses/day), and fail soft.

NO BEHAVIOR CHANGE: nothing reads these records except the weekly
review. There is no auto-copying, no signal, no gate — the module
cannot influence entries, exits, or alerts.

Dataset: runtime state ``<home>/launch_scanner/exit_copy.json``
(0600, atomic writes), bounded (500 closes), 7-day TTL.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Transaction-outcome guards (2026-11-17, Batch 3): meta.err
# gating (tx_guards.py). A reverted tx's balances never moved —
# its token delta is fiction, never a sale.
try:
    import tx_guards as _txg
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import tx_guards as _txg
    except Exception:
        raise

logger = logging.getLogger("exit_copy")

WAT = timezone(timedelta(hours=1))

_SUBDIR = "launch_scanner"
_FILENAME = "exit_copy.json"
_TTL_S = 7 * 24 * 3600
_MAX_CLOSES = 500
_SIG_LIMIT = 20
_TIMEOUT = 12.0

_STATE_DAY_KEY = "exit_copy_last_pass_day"


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def _resolve_path(path: Optional[str]) -> Optional[str]:
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        from daemon.common import get_home  # noqa: PLC0415
        return os.path.join(get_home(), _SUBDIR, _FILENAME)
    except Exception:
        logger.exception("exit_copy: could not resolve home")
        return None


def _load(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    p = _resolve_path(path)
    if not p:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    except Exception:
        logger.exception("exit_copy: load failed")
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = time.time() - _TTL_S
    return {str(m): r for m, r in data.items()
            if isinstance(r, dict) and _f(r.get("close_ts")) >= cutoff}


def _save(path: Optional[str], closes: Dict[str, Dict[str, Any]]) -> bool:
    p = _resolve_path(path)
    if not p:
        return False
    try:
        blob = json.dumps(closes, separators=(",", ":")).encode("utf-8")
    except Exception:
        logger.exception("exit_copy: serialize failed")
        return False
    tmp = "%s.tmp.%d" % (p, os.getpid())
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        return True
    except Exception:
        logger.exception("exit_copy: write failed")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _rpc(method: str, params: List[Any]) -> Any:
    """Helius RPC through the shared rate-limit guard (fail-soft)."""
    try:
        from . import helius_ratelimit  # noqa: PLC0415
        from . import smartmoney  # noqa: PLC0415
        key = smartmoney.api_key()
        if not key:
            return None
        if helius_ratelimit.cooldown_remaining() > 0:
            return None
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method,
             "params": params}).encode()
        req = urllib.request.Request(
            smartmoney._RPC_URL.format(key=key), data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0",
                     "Connection": "close"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                result = json.loads(
                    resp.read().decode("utf-8", "replace")).get("result")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = helius_ratelimit.report_ratelimited()
                logger.warning("exit_copy helius %s 429; cooldown %.0fs",
                               method, wait)
            else:
                logger.warning("exit_copy helius %s: HTTP %s", method,
                               exc.code)
            return None
        except Exception as exc:
            logger.warning("exit_copy helius %s failed: %s", method, exc)
            return None
        helius_ratelimit.report_success()
        return result
    except Exception:
        logger.exception("exit_copy _rpc failed")
        return None


def _token_delta(tx: Dict[str, Any], wallet: str,
                 mint: str) -> float:
    """Net token-balance change for (wallet, mint) in one transaction.
    Negative = the wallet sold (partially or fully)."""
    try:
        meta = tx.get("meta") or {}

        def _amts(key: str) -> Dict[Tuple[str, str], float]:
            out: Dict[Tuple[str, str], float] = {}
            for b in meta.get(key) or []:
                if not isinstance(b, dict):
                    continue
                amt = _f((b.get("uiTokenAmount") or {}).get("uiAmount"))
                out[(str(b.get("owner")), str(b.get("mint")))] = amt
            return out

        pre = _amts("preTokenBalances")
        post = _amts("postTokenBalances")
        k = (str(wallet), str(mint))
        return post.get(k, 0.0) - pre.get(k, 0.0)
    except Exception:
        return 0.0


def _wallet_sold_after(wallet: str, mint: str, after_ts: float,
                       budget: List[int]) -> Optional[float]:
    """Earliest blockTime at/after after_ts where the wallet's balance
    of mint decreased. None when no sell found (or on any failure).
    budget[0] caps getTransaction parses; decremented per parse."""
    try:
        sigs = _rpc("getSignaturesForAddress",
                    [wallet, {"limit": _SIG_LIMIT}]) or []
        earliest: Optional[float] = None
        for s in sigs:
            if not isinstance(s, dict):
                continue
            bt = _f(s.get("blockTime"))
            sig = s.get("signature")
            if not sig or bt < after_ts:
                continue
            if budget[0] <= 0:
                break
            budget[0] -= 1
            tx = _rpc("getTransaction",
                      [sig, {"encoding": "jsonParsed",
                             "maxSupportedTransactionVersion": 0}]) or {}
            # meta.err gate (2026-11-17, Batch 3): a reverted
            # tx's balances never moved — skip it, don't count
            # its delta as a sale.
            if not _txg.tx_succeeded(tx):
                continue
            if _token_delta(tx, wallet, mint) < 0:
                if earliest is None or bt < earliest:
                    earliest = bt
            time.sleep(0.15)
        return earliest
    except Exception:
        logger.exception("exit_copy wallet scan failed for %s", wallet[:8])
        return None


def record_close(degen: Any, mint: str, entry_ts: float, close_ts: float,
                 close_mult: float, reason: str, pnl_sol: float,
                 path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Store our exit for later wallet-exit comparison.

    Measurement only. Returns the record, or None when disabled.
    Never raises.
    """
    try:
        if not getattr(degen, "exit_copy_enabled", True):
            return None
        if not mint:
            return None
        mint = str(mint)
        closes = _load(path)
        rec = closes.get(mint) or {}
        rec.update({
            "close_ts": float(close_ts),
            "entry_ts": float(entry_ts),
            "close_mult": round(float(close_mult or 0.0), 4),
            "reason": str(reason or ""),
            "pnl_sol": round(float(pnl_sol or 0.0), 6),
            "wallet_exits": rec.get("wallet_exits") or {},
            "scanned_day": rec.get("scanned_day") or "",
        })
        closes[mint] = rec
        if len(closes) > _MAX_CLOSES:
            for m in sorted(closes,
                            key=lambda k: _f(closes[k].get("close_ts"))
                            )[:len(closes) - _MAX_CLOSES]:
                closes.pop(m, None)
        _save(path, closes)
        return rec
    except Exception:
        logger.exception("exit_copy record_close failed for %s", mint)
        return None


def daily_pass(state: Dict[str, Any], degen: Any,
               now: Optional[float] = None,
               path: Optional[str] = None) -> int:
    """Once per WAT calendar day: look for smart-wallet exits on our
    recent closes. Returns closes enriched. Never raises.

    Only wallets that BOUGHT the mint (state["smart_activity"][mint])
    are checked — bounded, throttled, fail-soft.
    """
    try:
        if not getattr(degen, "exit_copy_enabled", True):
            return 0
        now = time.time() if now is None else float(now)
        day = datetime.fromtimestamp(now, WAT).date().isoformat()
        if state.get(_STATE_DAY_KEY) == day:
            return 0
        state[_STATE_DAY_KEY] = day

        max_tx = int(getattr(degen, "exit_copy_max_tx_per_day", 40) or 40)
        budget = [max_tx]
        closes = _load(path)
        if not closes:
            return 0
        activity = state.get("smart_activity") or {}
        enriched = 0
        for mint, rec in closes.items():
            if not isinstance(rec, dict):
                continue
            if rec.get("scanned_day") == day:
                continue
            buyers = activity.get(mint) or []
            if not buyers:
                rec["scanned_day"] = day
                continue
            entry_ts = _f(rec.get("entry_ts"))
            exits = dict(rec.get("wallet_exits") or {})
            for wallet in buyers:
                if not isinstance(wallet, str) or wallet in exits:
                    continue
                if budget[0] <= 0:
                    break
                sold_ts = _wallet_sold_after(wallet, mint, entry_ts, budget)
                if sold_ts:
                    exits[wallet[:16]] = {"sold_ts": sold_ts,
                                          "after_our_close_h": round(
                                              (sold_ts - _f(rec.get("close_ts")))
                                              / 3600.0, 2)}
            rec["wallet_exits"] = exits
            rec["scanned_day"] = day
            enriched += 1
            if budget[0] <= 0:
                break
        _save(path, closes)
        if enriched:
            logger.warning("exit_copy daily pass: %d closes scanned, "
                           "%d tx budget left", enriched, budget[0])
        return enriched
    except Exception:
        logger.exception("exit_copy daily_pass failed")
        return 0


def weekly_section(path: Optional[str] = None) -> List[str]:
    """Weekly review: who exited first — us or the smart wallets?

    Honest measurement: ordering + our realized pnl. No fabricated
    counterfactual profits.
    """
    out = ["== EXIT-COPYING (last 7 days) =="]
    try:
        closes = _load(path)
    except Exception:
        out.append("exit-copy data unavailable (read failed).")
        return out
    with_exits = [(m, r) for m, r in closes.items()
                  if isinstance(r, dict) and r.get("wallet_exits")]
    if not with_exits:
        out.append("no smart-wallet exits observed on our closes yet.")
        return out
    n = len(with_exits)
    earlier = later = 0
    deltas: List[float] = []
    pnls: List[float] = []
    for _m, r in with_exits:
        exits = r.get("wallet_exits") or {}
        ours = _f(r.get("close_ts"))
        first = min((_f(e.get("sold_ts")) for e in exits.values()
                     if isinstance(e, dict) and _f(e.get("sold_ts")) > 0),
                    default=0.0)
        if first <= 0:
            continue
        dh = (first - ours) / 3600.0
        deltas.append(dh)
        pnls.append(_f(r.get("pnl_sol")))
        if dh < 0:
            earlier += 1
        else:
            later += 1
    out.append("closes with a tracked wallet exit: %d" % n)
    if deltas:
        out.append("wallet exited BEFORE us: %d | AFTER us: %d"
                   % (earlier, later))
        out.append("avg wallet-exit delta: %+.1fh (negative = they left first)"
                   % (sum(deltas) / len(deltas)))
        out.append("our avg pnl on those closes: %+.6f SOL"
                   % (sum(pnls) / len(pnls)))
        out.append("reading: when wallets leave first and we still "
                   "profit, our exits are fine; when they leave first "
                   "and we round-trip, their exits were the tell.")
    return out
