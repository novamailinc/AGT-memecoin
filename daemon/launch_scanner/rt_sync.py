"""Realtime sync layer (2026-10-21, user directive: "build a realtime
advance syncing into the system").

Why this exists: every hop in the pipeline used to run on a slow poll —
the CEO lane checks the bus every 120s, mirror/CEO exits quote every
120s. Day-1's ugliest closes (positions realized at 0.003x-0.008x) were
coins that rugged BETWEEN exit polls, straight through the -50% hard
stop: the ladder was right, the clock was wrong.

What this layer does:
  * Maintains a single LIVE TAPE over the watched set — every mint the
    system currently cares about: open CEO/mirror positions, open
    steady/stacker positions (tape only), and bus mints from the last
    2h (digest/promotion candidates, so their peaks are tick-grade).
  * Refreshes the tape every ~5s from DexScreener's batch endpoint
    (one call covers 30 mints; ~12 calls/min — inside the free tier),
    with a GeckoTerminal fallback per mint when DexScreener is dark
    (sources.tape_batch_stats; both dark = skip, as before).
  * Drives the lanes' OWN exit machinery event-style: whenever an open
    position's tape price moves, it calls mirror.poll_exits /
    ceo.poll_exits with a tape-backed price_fn. All ladder logic,
    locking and ledger mutation stay inside those modules — this layer
    only supplies fresh prices and timing. The 120s daemons remain as
    backstop and keep full ownership of chains the tape doesn't cover.

Free-only, by the codebase's standing rule: PumpPortal's trade streams
(subscribeTokenTrade) are a PAID tier and pumpportal.py refuses them by
design — they are not used here. DexScreener batch is the free realtime
source; prices are the same USD quotes every other consumer reads.

Safety contract of the tape price_fn: it returns a price ONLY when the
tape row is fresh (<= FRESH_S old). Stale or unknown -> 0.0, which the
exit engines already treat as "skip this cycle". A dead tape can never
fabricate a price. HL positions always get 0.0 here (no DexScreener
tape) — their own daemons price them from HL marks as before.

Never raises out of its public functions.
"""

import json
import logging
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

logger = logging.getLogger("forex_agent.daemon.launch_scanner.rt_sync")

try:
    from daemon.launch_scanner import sources as _sources
except Exception:  # pragma: no cover - script-style import path
    try:
        import sources as _sources  # type: ignore
    except Exception:
        _sources = None  # type: ignore

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
TAPE_PATH = os.path.join(HOME, "live_tape.json")
BUS_PATH = os.path.join(HOME, "launch_scanner", "signal_bus.jsonl")

SWEEP_S = 5.0            # tape refresh cadence
FRESH_S = 20.0           # a tape price older than this is not a price
BUS_WATCH_S = 2 * 3600   # bus mints stay watched for 2h
WATCH_REBUILD_S = 60.0   # watched-set rebuild cadence
MIN_EVAL_GAP_S = 5.0     # min seconds between exit evaluations per lane
CHUNK = 30               # DexScreener batch size
DS_CHAINS = ("solana", "base", "ethereum")

_LEDGERS = (
    ("ceo_ledger.json", "ceo"),
    ("degen_mirror_ledger.json", "mirror"),
    ("grad_ledger.json", "grad"),
    ("steady_ledger.json", "tracked"),
    ("stacker_ledger.json", "tracked"),
)


# ---- watched set -------------------------------------------------------

def _read_json(path: str) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _positions_of(ledger: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(ledger, dict):
        return {}
    pos = ledger.get("positions")
    if isinstance(pos, dict):
        return {m: p for m, p in pos.items() if isinstance(p, dict)}
    if isinstance(pos, list):
        return {str(p.get("mint")): p for p in pos
                if isinstance(p, dict) and p.get("mint")}
    return {}


def build_watched_set(home: str = HOME, now: Optional[float] = None,
                      bus_path: str = BUS_PATH) -> Dict[str, Dict[str, Any]]:
    """{mint: {"chain": str, "kinds": set}} for everything the system
    currently cares about. Never raises."""
    now = time.time() if now is None else now
    watched: Dict[str, Dict[str, Any]] = {}

    def add(mint: str, chain: str, kind: str) -> None:
        if not mint:
            return
        row = watched.setdefault(mint, {"chain": chain or "solana",
                                        "kinds": set()})
        row["kinds"].add(kind)

    for fname, kind in _LEDGERS:
        for mint, pos in _positions_of(
                _read_json(os.path.join(home, fname))).items():
            add(mint, str(pos.get("chain") or "solana"), kind)
    try:
        with open(bus_path) as f:
            for line in f:
                try:
                    sig = json.loads(line)
                except Exception:
                    continue
                if float(sig.get("ts") or 0) < now - BUS_WATCH_S:
                    continue
                chain = str(sig.get("chain") or "solana")
                if chain not in DS_CHAINS:
                    continue  # no DexScreener tape (hyperliquid)
                add(str(sig.get("mint") or ""), chain, "bus")
    except Exception:
        pass
    return watched


def open_mints(watched: Dict[str, Dict[str, Any]], kind: str) -> Set[str]:
    return {m for m, r in watched.items() if kind in r["kinds"]}


# ---- the tape ------------------------------------------------------------

class Tape:
    """In-memory live tape: mint -> last known USD market state."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    def update(self, mint: str, stats: Optional[Dict[str, Any]],
               now: float) -> bool:
        """Apply one DexScreener stats row. True when a price landed."""
        if not stats:
            return False
        try:
            price = float(stats.get("price_usd") or 0)
        except (TypeError, ValueError):
            return False
        if price <= 0:
            return False
        row = self.rows.setdefault(mint, {"peak": 0.0, "updates": 0})
        row.update({
            "price": price,
            "liquidity": float(stats.get("usd_liquidity") or 0),
            "mcap": float(stats.get("usd_market_cap") or 0),
            "ts": float(now),
            # tape_source (2026-10-21): names the venue this price was
            # read from — dexscreener or the geckoterminal fallback.
            "tape_source": str(stats.get("tape_source") or "unknown"),
            "updates": int(row.get("updates") or 0) + 1,
        })
        if price > float(row.get("peak") or 0):
            row["peak"] = price
            row["peak_ts"] = float(now)
        return True

    def price(self, mint: str, now: Optional[float] = None) -> float:
        """Fresh tape price, else 0.0 (the lanes' 'skip' signal)."""
        now = time.time() if now is None else now
        row = self.rows.get(mint)
        if not row:
            return 0.0
        if now - float(row.get("ts") or 0) > FRESH_S:
            return 0.0
        return float(row.get("price") or 0)

    def prune(self, keep: Iterable[str]) -> None:
        keep = set(keep)
        for mint in list(self.rows):
            if mint not in keep:
                self.rows.pop(mint, None)

    def to_dict(self, now: float) -> Dict[str, Any]:
        return {"heartbeat_ts": float(now),
                "fresh_s": FRESH_S,
                "mints": {m: dict(r) for m, r in self.rows.items()}}


def make_price_fn(tape: Tape, clock: Callable[[], float] = time.time):
    """price_fn(chain, mint, spot_index) for mirror/ceo poll_exits.

    Tape prices only; hyperliquid always 0.0 (priced by its own
    daemons from HL marks). Never raises."""
    def price_fn(chain, mint, spot_index=None):
        try:
            if (chain or "") == "hyperliquid":
                return 0.0
            return tape.price(str(mint), clock())
        except Exception:
            return 0.0
    return price_fn


# ---- refresh + exit driving ----------------------------------------------

def refresh_tape(tape: Tape, watched: Dict[str, Dict[str, Any]],
                 now: float, stats_fn: Optional[Callable] = None) -> int:
    """One batch sweep over the watched set. Returns prices updated.

    The default fetcher is tape_batch_stats: DexScreener first, the
    GeckoTerminal fallback filling mints DS missed — so a dark
    DexScreener no longer blinds the realtime exit driving."""
    if stats_fn is None:
        if _sources is None:
            return 0
        stats_fn = _sources.tape_batch_stats
    by_chain: Dict[str, List[str]] = {}
    for mint, row in watched.items():
        chain = row["chain"]
        if chain in DS_CHAINS:
            by_chain.setdefault(chain, []).append(mint)
    updated = 0
    for chain, mints in by_chain.items():
        for i in range(0, len(mints), CHUNK):
            chunk = mints[i:i + CHUNK]
            try:
                stats = stats_fn(chunk, chain=chain) or {}
            except Exception:
                logger.warning("rt_sync batch failed (%s)", chain,
                               exc_info=True)
                continue
            for mint in chunk:
                if tape.update(mint, stats.get(mint), now):
                    updated += 1
    return updated


def exit_eval_needed(tape: Tape, mints: Set[str],
                     last_prices: Dict[str, float],
                     now: Optional[float] = None) -> List[str]:
    """Open-position mints whose FRESH tape price differs from the
    price at the last exit evaluation — the event that should trigger
    a poll_exits call. Pure; never raises."""
    now = time.time() if now is None else now
    changed = []
    for mint in mints:
        px = tape.price(mint, now)
        if px > 0 and px != float(last_prices.get(mint) or 0):
            changed.append(mint)
    return changed


# ---- P7: freshness state + reconnect reconciliation ---------------------

RECONCILE_LOG = os.path.join(HOME, "reconciliation_log.jsonl")
GAP_S = 3 * SWEEP_S          # a sweep gap beyond this means we were dark
RECONCILE_MOVE_PCT = 20.0    # position moves worth journaling
_POSITION_KINDS = ("ceo", "mirror", "grad")


def journal_reconciliation(record: Dict[str, Any],
                             path: str = RECONCILE_LOG) -> bool:
    """Append one reconciliation record (JSONL). Never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
        return True
    except Exception:
        return False


class TapeHealth:
    """Freshness state machine for the tape loop (P7).

    LIVE — sweeps landing on cadence. STALE — no successful sweep
    inside STALE_AFTER_S. RECONCILING — the first sweep after a
    gap: open-position prices from before the gap are compared
    against the re-pulled tape and any move beyond
    RECONCILE_MOVE_PCT is journaled, because a position that moved
    >20% while we were dark was, for that interval, unprotected.
    """

    STALE_AFTER_S = 60.0

    def __init__(self) -> None:
        self.state = "STALE"
        self.last_ok_ts = 0.0
        self._pre_gap: Dict[str, float] = {}

    def begin_sweep(self, tape: Tape,
                    watched: Dict[str, Dict[str, Any]],
                    now: float) -> Dict[str, Any]:
        """Snapshot pre-refresh prices when a gap is detected."""
        gap = now - self.last_ok_ts if self.last_ok_ts else 0.0
        ctx: Dict[str, Any] = {"gap_s": gap, "reconciling": False}
        if self.last_ok_ts and gap > GAP_S and self.state == "LIVE":
            ctx["reconciling"] = True
            self._pre_gap = {}
            for mint, row in watched.items():
                if set(row.get("kinds") or ()) & set(_POSITION_KINDS):
                    px = tape.price(mint, now)
                    if px > 0:
                        self._pre_gap[mint] = px
            self.state = "RECONCILING"
        return ctx

    def end_sweep(self, tape: Tape,
                  watched: Dict[str, Dict[str, Any]], now: float,
                  ctx: Dict[str, Any], updated: int) -> List[Any]:
        """Close a sweep; returns [(state, info)] transitions."""
        out: List[Any] = []
        if updated > 0 or not watched:
            self.last_ok_ts = now
        if ctx.get("reconciling"):
            moved = []
            for mint, old in self._pre_gap.items():
                new = tape.price(mint, now)
                if old > 0 and new > 0:
                    pct = (new - old) / old * 100.0
                    if abs(pct) > RECONCILE_MOVE_PCT:
                        rec = {"ts": now, "mint": mint,
                               "gap_s": round(ctx["gap_s"], 1),
                               "from_px": old, "to_px": new,
                               "pct": round(pct, 1),
                               "source": "rt_sync-tape"}
                        moved.append(rec)
                        journal_reconciliation(rec)
            self.state = "LIVE"
            out.append(("RECONCILING",
                        {"gap_s": round(ctx["gap_s"], 1)}))
            out.append(("LIVE", {"gap_s": round(ctx["gap_s"], 1),
                                 "moved": moved}))
        elif self.state != "LIVE" and self.last_ok_ts:
            self.state = "LIVE"
            out.append(("LIVE", {"gap_s": round(ctx.get("gap_s", 0),
                                              1)}))
        if self.last_ok_ts and now - self.last_ok_ts \
                > self.STALE_AFTER_S and self.state == "LIVE":
            self.state = "STALE"
            out.append(("STALE", {"since": self.last_ok_ts}))
        return out

    def state_now(self, now: float) -> str:
        if self.state == "LIVE" and self.last_ok_ts and \
                now - self.last_ok_ts > self.STALE_AFTER_S:
            return "STALE"
        return self.state


def write_tape(tape: Tape, now: float, path: str = TAPE_PATH) -> bool:
    """Atomic tape snapshot (tmp + rename). Never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(tape.to_dict(now), f, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except Exception:
        logger.warning("rt_sync tape write failed", exc_info=True)
        return False
