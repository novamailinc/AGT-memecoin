"""Wallet pack detection (feature-flagged: off -> measure -> on).

Thesis: one proven wallet buying a coin is a signal. Several wallets
that *habitually buy the same coins within minutes of each other* —
a pack, likely one operator or a coordinated group — buying together
is a much stronger signal. Helius sells raw transactions; it will
never tell you which wallets move as a pack. That takes private
history, which only this bot has.

How it works:
  1. Every smart-money poll's fresh buys {mint: [wallets]} are
     recorded as timestamped events (in-memory, bounded).
  2. Wallets that buy the SAME mint in the SAME poll window get a
     co-buy count bump. Pairs that co-buy >= ``packdetect_min_cobuys``
     (default 2) times are linked; linked wallets form packs via
     union-find.
  3. Each pack gets a grade-weighted strength from the smart-wallet
     grades (proven pickers lift the pack, unproven members don't
     drag it below neutral).
  4. At candidate scoring time, if >= ``packdetect_min_members``
     (default 2) members of a known pack bought this mint, that's a
     pack signal with strength 0..1.

MODES (knob ``packdetect_mode``, default "measure"):
  off     — nothing recorded, no signal, zero cost.
  measure — record + detect + log every pack sighting to
            state["pack_detections"] (bounded 500, 7-day TTL).
            NOTHING touches scoring: entries, alerts, caps and picks
            are untouched.
  on      — everything in measure, PLUS rank_candidate adds up to
            ``sniper_pack_bonus_max`` (default 8.0) points scaled by
            pack strength. The 50-point tier bar, the 1/100 safety
            floor and the 4/hour cap are untouched.

The ledger decides: after ~1 week, pack_detections joined with
outcomes answers whether pack buys win more than lone-wolf buys.
Only then may the user flip the switch to "on". Nothing
self-activates.

Never raises. No I/O in the hot path beyond the grade-weight cache
(smartgrade caches weights 60s).
"""

import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("packdetect")

EVENTS_KEY = "pack_buy_events"      # [[wallet, mint, ts], ...] bounded
COBUYS_KEY = "pack_cobuys"          # {"w1|w2": {"n": int, "ts": float}}
PACKS_KEY = "pack_packs"            # {pack_id: {members, strength, ts}}
DETECT_KEY = "pack_detections"      # [{mint, ts, pack_id, members, strength}]

_MAX_EVENTS = 3000
_MAX_COBUYS = 2000
_MAX_DETECT = 500
_DETECT_TTL_S = 7 * 24 * 3600
_RECOMPUTE_MIN_S = 600.0  # packs recomputed at most every 10 min


def _num(degen: Any, name: str, default: float) -> float:
    try:
        return float(getattr(degen, name, default))
    except (TypeError, ValueError):
        return default


def mode(degen: Any) -> str:
    """off | measure | on. Unknown values fail safe to measure."""
    m = str(getattr(degen, "packdetect_mode", "measure") or "measure")
    m = m.strip().lower()
    return m if m in ("off", "measure", "on") else "measure"


def _pair_key(a: str, b: str) -> str:
    return "%s|%s" % tuple(sorted((a, b)))


def record_fresh(state: Dict[str, Any], degen: Any,
                 fresh: Dict[str, List[str]],
                 now: Optional[float] = None) -> int:
    """Record one poll's fresh buys and bump co-buy counts.

    ``fresh`` is {mint: [wallet_addresses]} from
    smartmoney.poll_wallets(). Wallets buying the same mint in the
    same poll window count as co-buying once for that mint.
    Returns the number of events recorded. Never raises.
    """
    try:
        if mode(degen) == "off":
            return 0
        if not fresh:
            return 0
        now = now if now is not None else time.time()
        events = state.setdefault(EVENTS_KEY, [])
        cobuys = state.setdefault(COBUYS_KEY, {})
        added = 0
        for mint, wallets in (fresh or {}).items():
            if not mint:
                continue
            uniq = sorted({w for w in (wallets or []) if w})
            if not uniq:
                continue
            for w in uniq:
                events.append([w, mint, now])
                added += 1
            # Every pair that bought this mint together co-bought once.
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    key = _pair_key(uniq[i], uniq[j])
                    rec = cobuys.get(key)
                    if rec is None:
                        rec = {"n": 0, "ts": 0.0}
                        cobuys[key] = rec
                    rec["n"] = int(rec.get("n") or 0) + 1
                    rec["ts"] = now
        # Bounds: drop oldest events / stalest pairs first.
        if len(events) > _MAX_EVENTS:
            del events[:len(events) - _MAX_EVENTS]
        if len(cobuys) > _MAX_COBUYS:
            stale = sorted(cobuys.items(),
                           key=lambda kv: float((kv[1] or {}).get("ts") or 0))
            for key, _ in stale[:len(cobuys) - _MAX_COBUYS]:
                cobuys.pop(key, None)
        return added
    except Exception:
        logger.warning("packdetect.record_fresh failed", exc_info=True)
        return 0


def _union_find(pairs: List[Tuple[str, str]]) -> List[Set[str]]:
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: Dict[str, Set[str]] = {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    return [g for g in groups.values() if len(g) >= 2]


def recompute_packs(state: Dict[str, Any], degen: Any,
                    now: Optional[float] = None,
                    force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Rebuild packs from co-buy counts (throttled). Never raises."""
    try:
        now = now if now is not None else time.time()
        packs = state.setdefault(PACKS_KEY, {})
        last = float(state.get("pack_packs_ts") or 0)
        if not force and packs and now - last < _RECOMPUTE_MIN_S:
            return packs
        min_cobuys = int(_num(degen, "packdetect_min_cobuys", 2.0))
        min_cobuys = max(1, min_cobuys)
        cobuys = state.get(COBUYS_KEY) or {}
        pairs = []
        for key, rec in cobuys.items():
            try:
                if int((rec or {}).get("n") or 0) >= min_cobuys:
                    a, b = key.split("|", 1)
                    pairs.append((a, b))
            except (ValueError, AttributeError):
                continue
        groups = _union_find(pairs)
        try:
            from . import smartgrade  # noqa: PLC0415 (lazy: init order)
            weights = smartgrade.wallet_weights(degen)
        except Exception:
            weights = {}
        new_packs: Dict[str, Dict[str, Any]] = {}
        for i, members in enumerate(
                sorted(groups, key=lambda g: sorted(g)[0])):
            members = sorted(members)
            wsum = sum(float(weights.get(m, 1.0)) for m in members)
            strength = round(min(1.0, wsum / 2.0), 3)
            new_packs["pack-%d" % i] = {
                "members": members,
                "size": len(members),
                "weight_sum": round(wsum, 2),
                "strength": strength,
                "ts": now,
            }
        state[PACKS_KEY] = new_packs
        state["pack_packs_ts"] = now
        if new_packs:
            logger.info("packdetect: %d packs from %d linked pairs",
                        len(new_packs), len(pairs))
        return new_packs
    except Exception:
        logger.warning("packdetect.recompute_packs failed", exc_info=True)
        return state.get(PACKS_KEY) or {}


def pack_signal_for_mint(state: Dict[str, Any], degen: Any,
                         mint: str) -> Dict[str, Any]:
    """Pack signal for one candidate mint.

    Returns {"strength": 0..1, "packs": [...], "members": [...]}.
    A pack fires when >= packdetect_min_members of its members are
    among the tracked wallets that bought this mint
    (state["smart_activity"][mint]). Never raises.
    """
    out = {"strength": 0.0, "packs": [], "members": []}
    try:
        if not mint or mode(degen) == "off":
            return out
        packs = recompute_packs(state, degen)
        if not packs:
            return out
        buyers = set((state.get("smart_activity") or {}).get(mint) or [])
        if not buyers:
            return out
        min_members = int(_num(degen, "packdetect_min_members", 2.0))
        min_members = max(2, min_members)
        try:
            from . import smartgrade  # noqa: PLC0415 (lazy: init order)
            weights = smartgrade.wallet_weights(degen)
        except Exception:
            weights = {}
        fired = []
        for pid, pack in packs.items():
            members = [m for m in (pack.get("members") or [])
                       if m in buyers]
            if len(members) >= min_members:
                wsum = sum(float(weights.get(m, 1.0)) for m in members)
                fired.append({
                    "pack_id": pid,
                    "members": sorted(members),
                    "size": len(members),
                    "strength": round(min(1.0, wsum / 2.0), 3),
                })
        if not fired:
            return out
        fired.sort(key=lambda f: f["strength"], reverse=True)
        best = fired[0]["strength"]
        members = sorted({m for f in fired for m in f["members"]})
        out = {"strength": best, "packs": fired, "members": members}
        return out
    except Exception:
        logger.warning("packdetect.pack_signal_for_mint failed",
                       exc_info=True)
        return out


def measure(state: Dict[str, Any], degen: Any, mint: str,
            signal: Dict[str, Any],
            now: Optional[float] = None) -> bool:
    """Log a pack sighting (measure + on modes). Returns True if
    logged. Scoring-neutral by construction. Never raises."""
    try:
        if not mint or mode(degen) == "off":
            return False
        if not signal or float(signal.get("strength") or 0) <= 0:
            return False
        now = now if now is not None else time.time()
        log = state.setdefault(DETECT_KEY, [])
        entry = {
            "mint": mint,
            "ts": now,
            "strength": signal["strength"],
            "members": list(signal.get("members") or []),
            "packs": [p["pack_id"] for p in (signal.get("packs") or [])],
        }
        log.append(entry)
        if len(log) > _MAX_DETECT:
            del log[:len(log) - _MAX_DETECT]
        # TTL sweep, cheap: only when over 80% full.
        if len(log) > int(_MAX_DETECT * 0.8):
            cutoff = now - _DETECT_TTL_S
            state[DETECT_KEY] = [e for e in log
                                 if float(e.get("ts") or 0) >= cutoff]
        logger.info("PACK DETECT %s strength=%.2f members=%d (%s)",
                    mint[:12], signal["strength"],
                    len(entry["members"]),
                    ",".join(entry["packs"][:3]))
        return True
    except Exception:
        logger.warning("packdetect.measure failed", exc_info=True)
        return False


def pack_bonus_points(degen: Any, strength: float) -> float:
    """Score bonus for on-mode: strength 0..1 -> 0..max points."""
    try:
        if mode(degen) != "on":
            return 0.0
        s = max(0.0, min(1.0, float(strength or 0)))
        return round(s * _num(degen, "sniper_pack_bonus_max", 8.0), 2)
    except (TypeError, ValueError):
        return 0.0
