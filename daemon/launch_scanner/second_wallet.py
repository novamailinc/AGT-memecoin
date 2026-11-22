"""Second-wallet trick detection — MEASUREMENT/DISPLAY ONLY (safe batch 2).

The trick: one operator funds several fresh wallets from the same
source; the fresh wallets then buy the same mint. On-chain it looks
like organic distribution — several "independent" early buyers — but
the shared first-funder betrays the single operator.

Detection: for a mint's early wallets (deployer + top holders from the
safety report, passed in by the caller), resolve each wallet's FIRST
funder via the cached ``deployers.funder_for`` (a wallet's first
funder never changes, so each wallet costs RPC at most once, ever).
A cluster = >= 2 wallets sharing one non-null funder.

DISPLAY ONLY: a "🔗 2nd-wallet: cluster (N→1 funder)" line on cards.
Never gates, never scores, never trades.

Rate discipline: uncached funder resolutions are throttled by a daily
budget (knob ``second_wallet_max_funders_per_day``, default 5) tracked
in the runtime file. Cached hits are free and never count. Everything
fail-soft — a dead module never affects the scan cycle.

Runtime state: ``<home>/launch_scanner/second_wallet.json`` (0600,
atomic writes), bounded (500 mints) with a 7-day TTL for verdicts;
the daily budget resets on WAT calendar day.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("second_wallet")

_SUBDIR = "launch_scanner"
_FILENAME = "second_wallet.json"
_TTL_S = 7 * 24 * 3600
_MAX_MINTS = 500
_MAX_WALLETS = 12


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def _knob(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def _resolve_path(path: Optional[str]) -> Optional[str]:
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        from daemon.common import get_home  # noqa: PLC0415
        return os.path.join(get_home(), _SUBDIR, _FILENAME)
    except Exception:
        logger.exception("second_wallet: could not resolve home")
        return None


def _blank() -> Dict[str, Any]:
    return {"verdicts": {}, "budget": {"day": "", "used": 0}}


def _load(path: Optional[str]) -> Dict[str, Any]:
    p = _resolve_path(path)
    if not p:
        return _blank()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return _blank()
    except Exception:
        logger.exception("second_wallet: load failed")
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    cutoff = time.time() - _TTL_S
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, dict):
        verdicts = {}
    verdicts = {str(m): r for m, r in verdicts.items()
                if isinstance(r, dict) and _f(r.get("ts")) >= cutoff}
    if len(verdicts) > _MAX_MINTS:
        for m in sorted(verdicts,
                        key=lambda k: _f(verdicts[k].get("ts"))
                        )[:len(verdicts) - _MAX_MINTS]:
            verdicts.pop(m, None)
    budget = data.get("budget")
    if not isinstance(budget, dict):
        budget = {"day": "", "used": 0}
    return {"verdicts": verdicts, "budget": budget}


def _save(path: Optional[str], data: Dict[str, Any]) -> bool:
    p = _resolve_path(path)
    if not p:
        return False
    try:
        blob = json.dumps(data, separators=(",", ":")).encode("utf-8")
    except Exception:
        logger.exception("second_wallet: serialize failed")
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
        logger.exception("second_wallet: write failed")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _wat_day(now: float) -> str:
    # Africa/Lagos = UTC+1, no DST. WAT calendar day for the budget.
    return time.strftime("%Y-%m-%d", time.gmtime(now + 3600))


def _budget_left(data: Dict[str, Any], degen: Any, now: float) -> int:
    budget = data.get("budget") or {}
    if budget.get("day") != _wat_day(now):
        return int(_knob(degen, "second_wallet_max_funders_per_day", 5) or 5)
    used = int(budget.get("used") or 0)
    cap = int(_knob(degen, "second_wallet_max_funders_per_day", 5) or 5)
    return max(0, cap - used)


def _spend_budget(data: Dict[str, Any], n: int, now: float) -> None:
    day = _wat_day(now)
    budget = data.get("budget") or {}
    if budget.get("day") != day:
        budget = {"day": day, "used": 0}
    budget["used"] = int(budget.get("used") or 0) + max(0, int(n))
    data["budget"] = budget


def _resolve_funders(wallets: List[str], budget_left: int
                     ) -> Tuple[Dict[str, Optional[str]], int]:
    """{wallet: funder|None}; second value = uncached RPC resolutions spent.

    Cached funders are free. Never raises; never touches the network
    directly — all RPC goes through deployers._rpc (shared guard).
    """
    out: Dict[str, Optional[str]] = {}
    spent = 0
    try:
        from . import deployers  # noqa: PLC0415
    except Exception:
        logger.exception("second_wallet: deployers import failed")
        return {w: None for w in wallets}, 0
    try:
        cached = deployers.load_funders()
    except Exception:
        cached = {}
    for w in wallets:
        w = str(w or "").strip()
        if not w or w in out:
            continue
        hit = cached.get(w)
        if isinstance(hit, dict) and "funder" in hit:
            out[w] = hit.get("funder")
            continue
        if spent >= budget_left:
            out[w] = None  # budget exhausted: honestly unresolved
            continue
        try:
            out[w] = deployers.funder_for(w, rpc=deployers._rpc)
        except Exception:
            out[w] = None
        spent += 1
    return out, spent


def analyze(degen: Any, mint: str, wallets: List[str],
            now: Optional[float] = None,
            path: Optional[str] = None) -> Dict[str, Any]:
    """Second-wallet cluster check. Measurement only. Never raises.

    wallets: early wallets for the mint (deployer + top holders).
    Returns {"verdict": cluster|clean|unknown, "wallets": n,
             "cluster_size": n, "detail": str}.
    """
    try:
        if not getattr(degen, "second_wallet_enabled", True):
            return {"verdict": "unknown", "wallets": 0,
                    "cluster_size": 0, "detail": "disabled"}
        mint = str(mint or "").strip()
        if not mint:
            return {"verdict": "unknown", "wallets": 0,
                    "cluster_size": 0, "detail": "no mint"}
        wallets = [str(w or "").strip() for w in (wallets or [])]
        wallets = [w for w in wallets if w][: _MAX_WALLETS]
        if len(wallets) < 2:
            return {"verdict": "unknown", "wallets": len(wallets),
                    "cluster_size": 0,
                    "detail": "need >=2 wallets to compare"}
        now = time.time() if now is None else float(now)
        data = _load(path)
        left = _budget_left(data, degen, now)
        funders, spent = _resolve_funders(wallets, left)
        if spent:
            _spend_budget(data, spent, now)
        # Cluster: >=2 wallets sharing one non-null funder.
        by_funder: Dict[str, List[str]] = {}
        for w, f in funders.items():
            if f:
                by_funder.setdefault(str(f), []).append(w)
        best: List[str] = []
        for ws in by_funder.values():
            if len(ws) > len(best):
                best = ws
        resolved = sum(1 for f in funders.values() if f)
        verdict: Dict[str, Any]
        if len(best) >= 2:
            verdict = {"verdict": "cluster", "wallets": len(wallets),
                       "cluster_size": len(best),
                       "detail": "%d wallets share 1 funder" % len(best)}
        elif resolved >= 2:
            verdict = {"verdict": "clean", "wallets": len(wallets),
                       "cluster_size": 0,
                       "detail": "%d funders resolved, no shared funder"
                                 % resolved}
        else:
            verdict = {"verdict": "unknown", "wallets": len(wallets),
                       "cluster_size": 0,
                       "detail": "funder data unavailable "
                                 "(budget or RPC miss)"}
        verdict["ts"] = now
        data["verdicts"][mint] = verdict
        _save(path, data)
        out = dict(verdict)
        out.pop("ts", None)
        return out
    except Exception:
        logger.exception("second_wallet analyze failed for %s", mint)
        return {"verdict": "unknown", "wallets": 0,
                "cluster_size": 0, "detail": "check error"}


def cached_verdict(mint: str,
                   path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Last analyzed verdict for a mint, or None. Never raises."""
    try:
        data = _load(path)
        rec = data.get("verdicts", {}).get(str(mint))
        return dict(rec) if isinstance(rec, dict) else None
    except Exception:
        return None


def weekly_section(degen: Any = None,
                   path: Optional[str] = None) -> List[str]:
    """Weekly review lines: cluster counts. Honest zeros. Never raises."""
    out = ["== SECOND-WALLET CLUSTERS (last 7 days) =="]
    try:
        data = _load(path)
        verdicts = data.get("verdicts", {})
        clusters = [m for m, r in verdicts.items()
                    if isinstance(r, dict)
                    and r.get("verdict") == "cluster"]
        out.append("mints analyzed: %d" % len(verdicts))
        out.append("clusters found: %d" % len(clusters))
        for m in clusters[:5]:
            r = verdicts[m]
            out.append("  %s…: %d wallets → 1 funder" % (
                str(m)[:8], int(r.get("cluster_size") or 0)))
        if not verdicts:
            out.append("no analyses yet — data accumulates from here.")
    except Exception:
        out.append("second-wallet data unavailable (read failed).")
    return out


def card_line(degen: Any, mint: str, wallets: Optional[List[str]] = None,
              path: Optional[str] = None) -> Optional[str]:
    """One-line card fragment, e.g. '🔗 2nd-wallet: cluster (3→1)'.

    Analyzes on demand (budget-throttled). Returns None when disabled,
    on error, or when the verdict is unknown — no line rather than a
    wrong line. Never gates.
    """
    try:
        if not getattr(degen, "second_wallet_enabled", True):
            return None
        if not mint:
            return None
        rec = analyze(degen, mint, wallets or [], path=path)
        if rec.get("verdict") == "cluster":
            return "🔗 2nd-wallet: cluster (%d→1 funder)" % int(
                rec.get("cluster_size") or 0)
        return None
    except Exception:
        return None
