#!/usr/bin/env python3
"""Rolling-expectancy kill switch: the highest-EV self-modification.

For a negative-expectancy lane, the correct position size is zero.
This module tracks rolling expectancy per lane and halts lanes that
go negative. Human-only re-enable. Fail-closed.

Lanes: main, degen, challenge, nadir
"""
import fcntl
import glob
import json
import os
import sys
import time
from typing import Dict, List, Optional

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
STATE_DIR = os.path.join(HOME, "killswitch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
# Shadow copy (2026-11-11 fix): _save() keeps state.json.bak holding the
# last successfully written state, so a corrupt primary can be restored
# with one copy instead of a backup hunt. The shadow is restore
# material only — _load() NEVER falls back to it automatically, so the
# fail-closed rule (corrupt primary = everything halted until a human
# restores) is unchanged.
SHADOW_FILE = os.path.join(STATE_DIR, "state.json.bak")
LOCK_FILE = os.path.join(STATE_DIR, "state.lock")

# Best-effort at import: create STATE_DIR when possible so the
# long-standing contract holds (callers and tests may touch STATE_FILE/
# LOCK_FILE paths directly after import). Failure is DEFERRED, not
# fatal: unwritable-home environments still import, and the first real
# state access raises through _ensure_state_dir -> gates fail closed.
try:
    os.makedirs(STATE_DIR, exist_ok=True)
except Exception:
    pass


def _ensure_state_dir() -> None:
    """Create STATE_DIR on FIRST USE, never at import.

    Import-time makedirs made `import killswitch` fail outright in any
    environment where the home path was unwritable (e.g. a test sandbox
    without FOREX_AGENT_HOME), which is why callers needed a bare-import
    fallback dance. Deferring keeps the fail-closed contract exactly:
    a state dir that cannot be created raises here, _load/_lock
    propagate, and is_halted/gate fail closed on the error - identical
    to today's outcome, without the import-time side effect.
    """
    os.makedirs(STATE_DIR, exist_ok=True)

# Config per lane: (window_trades, min_trades_before_kill, halt_on_negative)
CFG = {
    "main":      {"window": 50, "min_trades": 20, "halt": True},
    "degen":     {"window": 30, "min_trades": 15, "halt": True},
    "degen_v2":  {"window": 30, "min_trades": 15, "halt": True},
    "spark":     {"window": 30, "min_trades": 15, "halt": True},
    "steady":    {"window": 30, "min_trades": 15, "halt": True},
    "stacker":   {"window": 30, "min_trades": 15, "halt": True},
    "eagle_eye": {"window": 30, "min_trades": 15, "halt": False},
    "wallet_monitor": {"window": 30, "min_trades": 10, "halt": False},
    "challenge": {"window": 30, "min_trades": 10, "halt": True},
    "nadir":     {"window": 30, "min_trades": 10, "halt": True},
    # Degen-card paper mirror lanes (2026-10-17): card -> paper parity.
    # A halted mirror stops opening paper positions ONLY — cards keep
    # firing; the user's lane is never touched by the mirror's halt.
    "degen_mirror": {"window": 30, "min_trades": 15, "halt": True},
    "base_mirror":  {"window": 30, "min_trades": 15, "halt": True},
    "eth_mirror":   {"window": 30, "min_trades": 15, "halt": True},
    "hl_mirror":    {"window": 30, "min_trades": 15, "halt": True},
    # CEO account (2026-10-20): $100 paper bankroll trading the fired-card
    # bus at $1/shot with real-money discipline. Same pre-registered rule
    # as the mirror lanes: negative rolling expectancy after 15 closes
    # halts entries (open positions still exit; the bus is unaffected).
    "ceo":          {"window": 30, "min_trades": 15, "halt": True},
    # Graduation test lane (2026-10-21, user-ordered measurement of the
    # "buy at graduation" strategy): PRE-REGISTERED verdict sample of
    # 100 closes — no halt evaluation before 100 closes exist, then a
    # negative expectancy over the sample halts the lane. grad.py also
    # enforces the exact <= 0 boundary via halt_lane() at the moment
    # the 100th close lands, and records the verdict in its ledger.
    "grad":         {"window": 100, "min_trades": 100, "halt": True},
    # Graduation RAW lane (2026-10-23, user-ordered): the friend's
    # exact game — buys EVERY graduation with NO safety gate and NO
    # veto, so his coins are a subset and can't be missed. Same
    # pre-registered verdict as grad: 100 closes, expectancy <= 0
    # halts permanently. grad_raw.py enforces the boundary in-ledger.
    "grad_raw":     {"window": 100, "min_trades": 100, "halt": True},
    # Momentum paper lane (2026-10-23, completion build): $1 paper
    # shots on momentum-feed runners (momentum_lane.py). Same
    # pre-registered verdict shape as grad/grad_raw: 100 closes,
    # expectancy <= 0 halts permanently; momentum_lane.py enforces
    # the exact boundary in-ledger via halt_lane() at close #100.
    "momentum":     {"window": 100, "min_trades": 100, "halt": True},
    # Watcher paper lane (2026-10-23, trading-arm build): $1 paper
    # shots on smart-watcher convergence fires (watcher_lane.py) —
    # the trading arm for the one signal source with winner receipts
    # in the outcome data. Same pre-registered verdict shape as
    # grad/grad_raw/momentum: 100 closes, expectancy <= 0 halts
    # permanently; watcher_lane.py enforces the exact boundary
    # in-ledger via halt_lane() at close #100.
    "watcher":      {"window": 100, "min_trades": 100, "halt": True},
    # Watcher v2 paper lane (2026-10-24, user-approved): same
    # smart-watcher convergence-fire arm as watcher, but the first
    # ladder rung banks half at 1.5x instead of 2x (v1 diagnosis:
    # 56 of 77 closes peaked 1-2x, then hit the -50% stop). Same
    # pre-registered verdict shape: 100 closes, expectancy <= 0
    # halts permanently; watcher_v2_lane.py enforces the boundary
    # in-ledger via halt_lane() at close #100.
    "watcher_v2":   {"window": 100, "min_trades": 100, "halt": True},
    # Graduation SELECT lane (2026-10-24, user-ordered): grad_raw's
    # spray + the only externally-evidenced filters (serial
    # deployers, fast-curve/few-trades, canonical-only, copycat
    # tickers); NO safety gate by design. Same pre-registered
    # verdict shape as grad/grad_raw: 100 closes, expectancy <= 0
    # halts permanently; grad.py's engine enforces the boundary
    # in-ledger via halt_lane() at close #100.
    "grad_select":  {"window": 100, "min_trades": 100, "halt": True},
    # Graduation SELECT LIVE lane (2026-11-06, mainnet build): the
    # paper grad_select lane with real $1 fills. SEPARATE lane from
    # grad_select so live halts never touch the paper lane's state.
    # Same pre-registered verdict: 100 closes, expectancy <= 0
    # halts permanently; live_select.py enforces the boundary
    # in-ledger via halt_lane() at close #100.
    "grad_select_live": {"window": 100, "min_trades": 100, "halt": True},
    # Moon-bag lane (2026-10-26, user-ordered): grad_raw's spray
    # with ONLY the final-leg exit changed (50% trailing stop, no
    # 24h time stop), $1 stakes. Same pre-registered verdict shape:
    # 100 closes, expectancy <= 0 halts permanently.
    "moon":         {"window": 100, "min_trades": 100, "halt": True},
    # Smart-SELECT lane (2026-10-27, measurement-only test):
    # smart-watcher bus fires are the trigger, but entry requires
    # ALL FOUR grad_select evidence filters (fail-closed). Same
    # pre-registered verdict shape: 100 closes, expectancy <= 0
    # halts permanently; head-to-head vs grad_select decides
    # whether smart money adds value on top of the filters.
    "smart_select": {"window": 100, "min_trades": 100, "halt": True},
    # Tranche lane (2026-10-30, user-ordered): grad_select's
    # entries + 5FG8SAT5-style exits (25%@1.5x / 25%@3x / 50%
    # trail 30% + one dip buyback). Same pre-registered verdict
    # shape: 100 closes OR 7 days, expectancy <= 0 halts
    # permanently; head-to-head vs grad_select decides whether
    # the tranche exits add value on the same entries.
    "tranche":      {"window": 100, "min_trades": 100, "halt": True},
    # Tranche-PROTECT test lane (2026-10-31, CEO diagnosis):
    # tranche's entries + exits, except the remaining legs' hard
    # stop moves to breakeven once leg A banks (31/31 leaked
    # tranche closes banked A then stopped at -50%, -$295.72).
    # Same verdict shape: 100 closes, expectancy <= 0 halts
    # permanently; head-to-head vs live tranche same-window.
    "tranche_protect": {"window": 100, "min_trades": 100, "halt": True},
    # Paper-LIVE lane (2026-11-02, user-ordered): a $10 PAPER
    # account simulating live trading — tranche_protect's package
    # (grad_select entries + staged/protect exits + buyback) at
    # $1 stakes, MAX_OPEN 10. Same verdict shape: 100 closes OR
    # 7 days, expectancy <= 0 halts permanently. PAPER ONLY —
    # no real money, no real SOL, no real profit.
    "paper_live":   {"window": 100, "min_trades": 100, "halt": True},
    # Live10 demo mirror (2026-11-02, user-ordered): the $10 demo
    # account split $2.50 x 4 live lanes (select, tranche,
    # tranche_protect, raw), mirrored at $1 stakes. Verdict at
    # 100 FUNDED mirrored closes, expectancy <= 0 halts the
    # mirror permanently. PAPER ONLY.
    "live10":       {"window": 100, "min_trades": 100, "halt": True},
    # Raw-TIGHT test lane (2026-10-31, CEO diagnosis): grad_raw's
    # spray with ONLY the hard stop tightened to -25% (halt window
    # was 89% stop-outs). Same verdict shape: 100 closes,
    # expectancy <= 0 halts permanently; head-to-head vs the
    # halted raw lane's drain window is NOT a fair comparison —
    # judge on its own expectancy + ex-top-1.
    "raw_tight":    {"window": 100, "min_trades": 100, "halt": True},
    # Mimic lane (2026-11-01, user-ordered): the traced outside
    # bot's launch game on paper — unfiltered launch spray from the
    # PumpPortal new-token stream, live-quote entries, 120s launch
    # window + chase guard. Same verdict shape: 100 closes,
    # expectancy <= 0 halts permanently; exits are the provisional
    # standard ladder until the whales' real sells are read.
    "mimic":        {"window": 100, "min_trades": 100, "halt": True},
    # Mimic-EDGE lane (2026-11-01, user-ordered redesign): the
    # full copied edge — TWAP sequenced-sell exit (64 slices,
    # ~5min), -25% stop on the unsold remainder, 60min max hold,
    # and launch filtering (copycat ticker / serial deployer,
    # >=3 in 24h rejected — grad_select's evidence filters
    # adapted to launches; the outside bot's own launch filter
    # is still unknown). Same verdict shape: 100 closes,
    # expectancy <= 0 halts permanently. Supersedes mimic v1.
    # HALTED on verdict 2026-11-02 (100 closes, -$4.09/close).
    "mimic_edge":   {"window": 100, "min_trades": 100, "halt": True},
    # Mimic-EDGE2 lane (2026-11-02, user-ordered thorough fix of
    # edge's three limits): edge's design with ONLY the exit rate
    # changed — TWAP 64 slices at 4/poll (~80s) + 8/poll below
    # 0.90x (edge's ~5min window decayed completes to ~0.93x and
    # stops gapped to 0.16x). Same verdict shape: 100 closes,
    # expectancy <= 0 halts permanently. Supersedes mimic_edge.
    "mimic_edge2":  {"window": 100, "min_trades": 100, "halt": True},
}

class _StateError(RuntimeError):
    """Killswitch state unreadable. Callers must fail closed, never
    silently treat unknown safety state as unhalted (2026-10-25: a
    torn state.json after a VM reset was silently re-seeded blank,
    evaporating every halt incl. the standing CEO halt and the
    degen_v2 structural kill)."""


_WARNED_CORRUPT = False


def _load() -> Dict:
    global _WARNED_CORRUPT
    _ensure_state_dir()
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        # Genuine first boot: nothing to lose.
        return {"lanes": {}, "events": []}
    except Exception as e:
        # Corrupt/torn file: NEVER invent a blank state over real
        # halt history, and never overwrite the evidence. Leave
        # the file in place, scream once per process, and refuse
        # every load until a human restores it from backup — so
        # is_halted stays True (fail-closed) on every call, not
        # just the first. A rename-on-first-load design would let
        # the very next call see "no file" and silently resume
        # trading on a blank state. (2026-11-11: the shadow copy
        # state.json.bak is the restore source — one copy restores
        # the last good state; _load still never falls back on its
        # own, so this fail-closed rule is unchanged.)
        if not _WARNED_CORRUPT:
            _WARNED_CORRUPT = True
            print("KILLSWITCH STATE CORRUPT — %s left in place, "
                  "refusing to load until a human restores it "
                  "from backup (shadow: %s): %s" % (STATE_FILE, SHADOW_FILE, e),
                  file=sys.stderr, flush=True)
        raise _StateError("killswitch state unreadable: %s" % (e,))


def _lock():
    """Exclusive inter-process lock for read-modify-write cycles.

    Several daemon processes (select/raw/tranche/protect traders plus
    the killswitch-check cron) all record trades into this one file.
    Without the lock, two processes can load the same state, each
    append its own trade, and the second save silently drops the
    first's trade. The lock fd is released automatically if the
    holder dies, so a crashed writer can never wedge the file.
    """
    import contextlib

    @contextlib.contextmanager
    def _locked():
        _ensure_state_dir()
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    return _locked()


def _atomic_write(path: str, payload: str):
    """Write payload to path atomically: per-process tmp file + fsync
    + os.replace + directory fsync. The tmp name carries the pid so
    two processes can never share (and interleave) one tmp file —
    that shared-tmp race is what corrupted state.json on 2026-11-10
    (one extra trailing '}'), even though the write was already
    'atomic' via a single shared .tmp path."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        pass


def _save_locked(s: Dict):
    """Persist state. Caller must hold _lock(). Writes the primary
    atomically, then the same payload to the shadow copy (best
    effort — a shadow failure is logged, never fails the save).
    Also sweeps tmp files orphaned by writers killed mid-save."""
    payload = json.dumps(s, indent=1)
    _atomic_write(STATE_FILE, payload)
    try:
        _atomic_write(SHADOW_FILE, payload)
    except Exception as e:
        print("KILLSWITCH shadow write failed (primary saved): %s" % (e,),
              file=sys.stderr, flush=True)
    try:
        cutoff = time.time() - 3600
        for stale in glob.glob(STATE_FILE + ".tmp.*"):
            try:
                if os.path.getmtime(stale) < cutoff:
                    os.unlink(stale)
            except OSError:
                pass
    except Exception:
        pass


def _save(s: Dict):
    """Public save: lock + persist. Mutating functions that already
    hold the lock call _save_locked directly."""
    with _lock():
        _save_locked(s)

def _lane_state(s: Dict, lane: str) -> Dict:
    return s["lanes"].setdefault(lane, {
        "halted": False,
        "halt_reason": "",
        "halt_ts": 0,
        "trades": [],  # list of {"ts":, "pnl":} (pnl in SOL or USD, consistent per lane)
        "checks": 0,
    })

def record_trade(lane: str, pnl: float, ts: float = None):
    """Record a closed trade's P&L for a lane."""
    try:
        with _lock():
            s = _load()
            ls = _lane_state(s, lane)
            ls["trades"].append({"ts": ts or time.time(), "pnl": pnl})
            # Keep only the window
            w = CFG.get(lane, {}).get("window", 50)
            ls["trades"] = ls["trades"][-w:]
            _save_locked(s)
    except Exception as e:
        # Never lose the ledger close over killswitch state trouble;
        # the ks record can be re-synced later. Loud, not silent.
        # (LOW-5, 2026-11-21: was _StateError only — a malformed
        # state file raised KeyError here and could lose the close.)
        print("KILLSWITCH record_trade skipped: %s" % (e,),
              file=sys.stderr, flush=True)
        return

def rolling_expectancy(lane: str) -> Dict:
    """Compute rolling expectancy for a lane."""
    s = _load()
    ls = _lane_state(s, lane)
    trades = ls["trades"]
    cfg = CFG.get(lane, {})
    n = len(trades)
    if n == 0:
        return {"n": 0, "expectancy": 0.0, "status": "no_data"}
    pnls = [t["pnl"] for t in trades]
    exp = sum(pnls) / n
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": n,
        "expectancy": round(exp, 6),
        "win_rate": round(wins / n, 3),
        "net": round(sum(pnls), 6),
        "min_trades": cfg.get("min_trades", 20),
        "enough_data": n >= cfg.get("min_trades", 20),
        "status": "ok",
    }


# Journaled risk reporting (2026-11-21): pre-halt warnings only.
# Nothing here halts or re-enables a lane — the halting rule in
# check_lane and the human-only reenable() are untouched. The
# evidence shapes come from the exit study (2026-11-21): fast
# losers ridden to the -50% stop (506 closes inside 15 min,
# 10.4% win rate, net -$6,901.14) and late entries (age >30s,
# 21.5% win rate) are what a deteriorating recent window and a
# growing loss streak look like before expectancy goes negative.
RECENT_WINDOW = 20
WARNING_DEDUP_SECONDS = 86400  # at most one WARNING per lane per day
TREND_THRESHOLD = 5.0  # USD per-trade expectancy gap, recent vs overall
ELEVATED_RECENT_EXPECTANCY = -10.0  # USD per-trade, recent window
ELEVATED_LOSS_STREAK = 5  # trailing consecutive losses


def _trades_to_pnls(trades) -> List[float]:
    """Extract per-trade P&L floats, skipping malformed entries."""
    pnls = []
    for t in trades or []:
        try:
            pnls.append(float(t["pnl"]))
        except Exception:
            continue
    return pnls


def _risk_from_trades(lane: str, pnls: List[float],
                      min_trades: int) -> Dict:
    """Pure risk computation over a P&L list. No I/O, never raises
    on bad input (callers pass already-extracted floats)."""
    n = len(pnls)
    if n == 0:
        return {"lane": lane, "n": 0, "expectancy": 0.0,
                "win_rate": 0.0, "recent_n": 0,
                "recent_expectancy": 0.0, "recent_win_rate": 0.0,
                "recent_net": 0.0, "loss_streak": 0, "trend": "flat",
                "risk": "low", "would_halt": False,
                "min_trades": min_trades}
    exp = sum(pnls) / n
    wins = sum(1 for p in pnls if p > 0)
    recent = pnls[-RECENT_WINDOW:]
    rn = len(recent)
    rexp = sum(recent) / rn
    rwins = sum(1 for p in recent if p > 0)
    rnet = sum(recent)
    streak = 0
    for p in reversed(pnls):
        if p < 0:
            streak += 1
        else:
            break
    if rexp < exp - TREND_THRESHOLD:
        trend = "deteriorating"
    elif rexp > exp + TREND_THRESHOLD:
        trend = "improving"
    else:
        trend = "flat"
    would_halt = exp < 0 and n >= min_trades
    if would_halt:
        risk = "high"
    elif (rexp < ELEVATED_RECENT_EXPECTANCY
          or streak >= ELEVATED_LOSS_STREAK
          or trend == "deteriorating"):
        risk = "elevated"
    else:
        risk = "low"
    return {"lane": lane, "n": n, "expectancy": round(exp, 6),
            "win_rate": round(wins / n, 3), "recent_n": rn,
            "recent_expectancy": round(rexp, 6),
            "recent_win_rate": round(rwins / rn, 3),
            "recent_net": round(rnet, 6), "loss_streak": streak,
            "trend": trend, "risk": risk, "would_halt": would_halt,
            "min_trades": min_trades}


def risk_report(lane: str) -> Dict:
    """Read-only risk report for a lane. Never raises, never
    writes state, never halts or re-enables anything. Missing,
    empty or unreadable lane data reports low risk with
    would_halt False — never halt on missing data. All money
    figures are per-trade expectancy in USD."""
    try:
        min_trades = CFG.get(lane, {}).get("min_trades", 20)
    except Exception:
        min_trades = 20
    try:
        s = _load()
        entry = (s.get("lanes", {}) or {}).get(lane, {}) or {}
        pnls = _trades_to_pnls(entry.get("trades", []))
        return _risk_from_trades(lane, pnls, min_trades)
    except Exception:
        return _risk_from_trades(lane, [], min_trades)


def _maybe_warn_locked(s: Dict, lane: str, rep: Dict,
                       now: float = None) -> bool:
    """Append a WARNING event (not a halt) when a lane that would
    NOT halt is at elevated or high risk. Deduplicated: at most
    one WARNING per lane per 24h, judged over the full event log
    (the same data recent_events() reads). Caller must hold
    _lock(). Returns True when an event was appended."""
    if rep.get("risk") not in ("elevated", "high"):
        return False
    if rep.get("would_halt"):
        return False
    now = now if now is not None else time.time()
    for e in s.get("events", []) or []:
        try:
            if (e.get("action") == "WARNING"
                    and e.get("lane") == lane
                    and float(e.get("ts", 0)) > now - WARNING_DEDUP_SECONDS):
                return False
        except Exception:
            continue
    s.setdefault("events", []).append({
        "ts": now, "lane": lane, "action": "WARNING",
        "reason": ("elevated risk: expectancy %.6f, recent "
                   "expectancy %.6f over last %d trades, loss "
                   "streak %d, trend %s"
                   % (rep["expectancy"], rep["recent_expectancy"],
                      rep["recent_n"], rep["loss_streak"],
                      rep["trend"])),
        "expectancy": rep["expectancy"],
        "recent_expectancy": rep["recent_expectancy"],
        "recent_net": rep["recent_net"],
        "loss_streak": rep["loss_streak"],
        "trend": rep["trend"],
        "risk": rep["risk"],
    })
    return True

def is_halted(lane: str) -> bool:
    """Check if a lane is halted. The single choke point.
    Fail-closed: unreadable state counts as halted — and so does
    ANY error reading it. A malformed-but-parseable state file
    (valid JSON, wrong shape: missing "lanes", non-dict lanes)
    used to escape as a KeyError/TypeError that the entry gates'
    except: pass swallowed, so entry proceeded with no halt
    enforcement (HIGH-2, 2026-11-21). Now every exception is
    halted; is_halted never raises."""
    try:
        s = _load()
        return _lane_state(s, lane).get("halted", False)
    except Exception:
        return True

def check_lane(lane: str) -> Dict:
    """Evaluate a lane. Halt it if rolling expectancy is negative
    with enough data. Returns decision dict."""
    try:
        with _lock():
            s = _load()
            ls = _lane_state(s, lane)
            ls["checks"] += 1
            m = rolling_expectancy(lane)
            result = {"lane": lane, "action": "none", **m}

            # Already halted → stay halted
            if ls.get("halted"):
                result["action"] = "halted"
                result["halt_reason"] = ls.get("halt_reason", "")
                _save_locked(s)
                return result

            # Risk evidence for this check, computed from the same
            # in-memory trade list that m was computed from. Used
            # only for journaling (WARNING / HALT payloads) — it
            # never changes the halt decision below.
            cfg = CFG.get(lane, {})
            rep = _risk_from_trades(
                lane, _trades_to_pnls(ls.get("trades", [])),
                cfg.get("min_trades", 20))

            # Not enough data → no decision (but an elevated-risk
            # lane still gets its one-per-day WARNING, journaled
            # below; a warning never halts).
            if not m.get("enough_data"):
                result["action"] = "warming_up"
                _maybe_warn_locked(s, lane, rep)
                _save_locked(s)
                return result

            # Negative expectancy → HALT (unchanged rule: halt on
            # negative rolling expectancy with enough data).
            if cfg.get("halt") and m["expectancy"] < 0:
                ls["halted"] = True
                ls["halt_reason"] = (
                    f"rolling expectancy {m['expectancy']:.6f} < 0 "
                    f"over last {m['n']} trades (win rate {m['win_rate']:.1%})"
                )
                ls["halt_ts"] = time.time()
                s["events"].append({
                    "ts": ls["halt_ts"], "lane": lane, "action": "HALT",
                    "reason": ls["halt_reason"],
                    "recent_expectancy": rep["recent_expectancy"],
                    "recent_net": rep["recent_net"],
                    "loss_streak": rep["loss_streak"],
                    "trend": rep["trend"],
                })
                result["action"] = "HALTED"
                result["halt_reason"] = ls["halt_reason"]
                _save_locked(s)
                return result

            result["action"] = "ok"
            _maybe_warn_locked(s, lane, rep)
            _save_locked(s)
            return result
    except Exception:
        # Refuse to decide or persist anything on unreadable OR
        # malformed state (LOW-5, 2026-11-21: a KeyError from a
        # wrong-shape state file used to crash the checker here).
        # Traders still fail closed via is_halted(); this only
        # reports that no decision could be made.
        return {"lane": lane, "action": "state_error",
                "n": 0, "expectancy": 0.0, "status": "state_error"}

def halt_lane(lane: str, reason: str = "") -> Dict:
    """Programmatic halt for PRE-REGISTERED verdict rules (e.g. the
    grad lane's 100-close sample judgment). Same state shape and event
    log as check_lane's HALT. Idempotent. Re-enable stays human-only
    (reenable()); nothing here ever un-halts. On unreadable or
    malformed state (LOW-5, 2026-11-21) it reports state_error
    instead of raising — the caller cannot halt a lane whose
    state cannot be read, and the traders fail closed via
    is_halted() in the meantime."""
    try:
        with _lock():
            s = _load()
            ls = _lane_state(s, lane)
            if ls.get("halted"):
                return {"lane": lane, "action": "halted",
                        "halt_reason": ls.get("halt_reason", "")}
            ls["halted"] = True
            ls["halt_reason"] = reason or "halted by pre-registered rule"
            ls["halt_ts"] = time.time()
            s["events"].append({
                "ts": ls["halt_ts"], "lane": lane, "action": "HALT",
                "reason": ls["halt_reason"],
            })
            _save_locked(s)
        return {"lane": lane, "action": "HALTED",
                "halt_reason": ls["halt_reason"]}
    except Exception:
        return {"lane": lane, "action": "state_error",
                "halt_reason": "", "status": "state_error"}


def reenable(lane: str, reason: str = "") -> Dict:
    """Human-only re-enable. Clears halt, keeps trade history.
    On unreadable or malformed state (LOW-5, 2026-11-21) it
    reports state_error instead of raising — a corrupt state
    must never be silently treated as re-enabled."""
    try:
        with _lock():
            s = _load()
            ls = _lane_state(s, lane)
            was = ls.get("halted", False)
            ls["halted"] = False
            ls["halt_reason"] = ""
            ls["halt_ts"] = 0
            # Reset trades so it re-warms (don't let old losses instantly re-halt)
            ls["trades"] = []
            s["events"].append({
                "ts": time.time(), "lane": lane, "action": "REENABLE",
                "reason": reason or "human decision",
            })
            _save_locked(s)
        return {"lane": lane, "was_halted": was, "action": "reenabled"}
    except Exception:
        return {"lane": lane, "was_halted": False,
                "action": "state_error", "status": "state_error"}

def status() -> Dict:
    """Full status of all lanes. On unreadable or malformed state
    (LOW-5, 2026-11-21) returns {"state_error": True, ...}
    instead of raising — the daily check reports "state
    unreadable — all lanes halted" rather than dying."""
    try:
        s = _load()
        out = {}
        for lane in CFG:
            m = rolling_expectancy(lane)
            out[lane] = {
                **m,
                "halted": _lane_state(s, lane).get("halted", False),
                "halt_reason": _lane_state(s, lane).get("halt_reason", ""),
            }
        return out
    except Exception as e:
        return {"state_error": True, "error": type(e).__name__,
                "lanes": {}}

def gate(lane: str) -> bool:
    """Entry gate: True if lane may trade, False if halted.
    Call this before any new position/card/trade. Fail-closed:
    on any error, returns False (halt)."""
    try:
        return not is_halted(lane)
    except Exception:
        return False

def recent_events(n: int = 10) -> List[Dict]:
    # LOW-5 (2026-11-21): unreadable or malformed state reports
    # no events instead of raising — the daily check reports the
    # state error via status(), it must not die here.
    try:
        s = _load()
        return s.get("events", [])[-n:]
    except Exception:
        return []
