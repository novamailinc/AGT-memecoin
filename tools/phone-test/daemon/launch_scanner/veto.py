"""Exclusion-first veto engine: hard vetoes applied BEFORE any paper entry.

From the 2026-10-16 deep research (MELT, Georgia Tech, 41,470 launches):
- The only approach with measured loss reduction (60% -> 30% simulated loss)
  was exclusion-first: veto the worst candidates instead of picking winners.
- Raw holder concentration is nearly useless (ablation drop 0.0036).
- Entity-adjusted (bundle) concentration is the #2 signal family (drop 0.0278).
- Market-activity / wash-trade features are #1 (drop 0.0360).
- 36.5% of supply sits in coordinated/bundled wallets invisible to raw data.
- 21.4% of pre-migration transactions are wash trades.

This module unifies the existing fragmented checks into ONE hard-veto layer:
  1. bundle.analyze()          -> entity-adjusted concentration; vetoes
     only on extreme share (>= 50%) or on a mid-size cluster (>= 20%)
     that is actively DISTRIBUTING (2026-10-20: existence alone is not
     a veto — bundled launches were ~2x more likely to become large
     tokens in the largest launch dataset)
  2. holder_tracker.wash_detected() -> bump-bot wash pattern (HARD veto)
  3. deployer_tracker          -> repeat-rug / factory deployers (HARD veto)
  4. safety.check_token()      -> RugCheck hard fails (already hard, kept)

veto_check(mint, chain, context) -> dict(passed, vetoes, flags, details)
  passed=False -> DO NOT ENTER, drop silently. No score can override a veto.
  Never raises; unknown -> fail-open with flag (veto only on positive evidence).

Vetoes are logged to ~/.forex-agent/veto_log.jsonl for measurement:
which vetoes fire, on what, and whether vetoed coins later ran (counterfactual).

Every EVALUATION (pass and fail) is also journaled to
~/.forex-agent/veto_events.jsonl (see _log_veto_event below), so the
veto path's reachability is observable even when nothing is vetoed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.veto")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_VETO_LOG = _HOME / "veto_log.jsonl"

# Thresholds (from research + existing code)
# Bundle doctrine (2026-10-20): cluster EXISTENCE never vetoes. Only
# an extreme entity-adjusted share (>= BUNDLE_HARD_PCT) vetoes on size
# alone; a mid-size cluster (>= BUNDLE_BEHAVIOR_PCT) vetoes only with
# positive distribution evidence from bundle.cluster_distribution().
BUNDLE_BEHAVIOR_PCT = 20.0      # cluster share that triggers behavior check
BUNDLE_HARD_PCT = 50.0          # cluster share that vetoes on size alone
WASH_TX_FLOOR = 50               # txns/5min with flat holders = wash
DEPLOYER_MIN_LAUNCHES_VETO = 3  # >=3 launches, 0 survivors = near-veto -> veto
TOP10_MAX_PCT = 40.0            # RugCheck top-10 concentration veto


def _veto_log_path() -> Path:
    """Resolve the veto log path at WRITE time, not import time.

    Root cause of the missing veto_log.jsonl (found 2026-10-20): the
    write itself was fine, but _log_veto was effectively unreachable —
    it only runs when veto_check returns passed=False, and every live
    caller either sat behind a halted killswitch gate (main lane,
    halted 2026-10-15, returns before the veto block), was structurally
    killed (degen_v2), or called veto_check in a degraded mode
    (mirror/CEO run_bundle=False with no safety report / tx count, so
    3 of 4 checks can only answer "unknown"). On top of that, the old
    bare `except: pass` would have swallowed any write failure with
    zero trace anywhere. Resolving the path per-write and surfacing
    failures in the daemon log removes the silent-failure class.
    """
    home = os.environ.get("FOREX_AGENT_HOME")
    if home:
        return Path(home) / "veto_log.jsonl"
    return _VETO_LOG


def _log_veto(mint: str, symbol: str, chain: str, vetoes: List[str],
              details: Dict[str, Any], lane: str = "unknown") -> None:
    """Append a veto record for counterfactual measurement.

    Fail-soft: a logging failure never blocks an entry path, but it is
    reported to the daemon log instead of being swallowed silently.
    """
    try:
        rec = {
            "ts": time.time(),
            "mint": mint,
            "symbol": symbol,
            "chain": chain,
            "lane": lane,
            "vetoes": vetoes,
            "details": details,
        }
        # default=str: detail dicts come from deployer/bundle internals
        # and may hold non-JSON-native values; a serialization error
        # must not lose the record.
        line = json.dumps(rec, default=str)
        with open(_veto_log_path(), "a") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning("veto log write failed for %s (%s): %r",
                       mint, lane, e)


# ---- veto-event journal (observability only, 2026-10-23) ----------------
# veto_log.jsonl above records only FAILED checks, and in production
# it was never written — so "vetoes never fire" and "veto path
# unreachable" were indistinguishable. This journal records EVERY
# evaluation (pass and fail) to ~/.forex-agent/veto_events.jsonl so
# reachability is provable from the file's existence and pass rate.
# It NEVER influences a decision: fail-soft, single buffered append.
# Volume: veto_check is called only at entry consideration (grad
# _veto_ok, ceo _veto_ok, mirror _veto_ok, and the main/steady entry
# paths — once per candidate/card, never in exit-polling or any hot
# loop), so full per-evaluation journaling is bounded by candidate
# volume (hundreds/day worst case) and needs no sampling.
# Rotation follows recorder.py's discipline: daily roll of the
# active file to veto_events-YYYYMMDD.jsonl, oldest archives pruned
# so all veto_events files together stay <= ~50MB.

_VETO_EVENTS_MAX_BYTES = 50 * 1024 * 1024
_veto_events_lock = threading.Lock()


def _veto_events_path() -> Path:
    """Resolve at WRITE time (same lesson as _veto_log_path)."""
    home = os.environ.get("FOREX_AGENT_HOME")
    if home:
        return Path(home) / "veto_events.jsonl"
    return _HOME / "veto_events.jsonl"


def _prune_veto_events(active: Path) -> None:
    """Delete oldest rotated archives until veto_events* files fit
    the cap. The active file is never deleted. Never raises."""
    try:
        files = []
        total = 0
        for p in active.parent.glob("veto_events*.jsonl"):
            try:
                st = p.stat()
                files.append((st.st_mtime, str(p), st.st_size))
                total += st.st_size
            except Exception:
                continue
        files.sort()
        for _mt, pstr, size in files:
            if total <= _VETO_EVENTS_MAX_BYTES:
                break
            if Path(pstr) == active:
                continue
            try:
                os.unlink(pstr)
                total -= size
            except Exception:
                continue
    except Exception:
        pass


def _rotate_veto_events_if_needed(active: Path) -> None:
    """Roll the active file when its UTC day has passed or it alone
    has reached the cap. Never raises."""
    try:
        if not active.exists():
            return
        st = active.stat()
        file_day = time.strftime("%Y%m%d", time.gmtime(st.st_mtime))
        today = time.strftime("%Y%m%d", time.gmtime())
        if file_day == today and st.st_size < _VETO_EVENTS_MAX_BYTES:
            return
        base = active.parent / ("veto_events-%s.jsonl" % file_day)
        dest = base
        n = 1
        while dest.exists():
            dest = active.parent / ("veto_events-%s.%d.jsonl"
                                    % (file_day, n))
            n += 1
        os.replace(str(active), str(dest))
        _prune_veto_events(active)
    except Exception:
        pass


def _log_veto_event(mint: str, chain: str, passed: bool,
                    vetoes: List[str], lane: str = "unknown") -> None:
    """Append one compact evaluation record. Fail-soft: a journal
    failure is reported to the daemon log, never raised, and never
    changes the veto result."""
    try:
        rec: Dict[str, Any] = {
            "ts": time.time(),
            "mint": mint,
            "chain": chain,
            "passed": bool(passed),
            "vetoes": list(vetoes or []),
        }
        if lane and lane != "unknown":
            rec["source_lane"] = lane
        line = json.dumps(rec, default=str)
        with _veto_events_lock:
            path = _veto_events_path()
            _rotate_veto_events_if_needed(path)
            with open(path, "a") as f:
                f.write(line + "\n")
            _prune_veto_events(path)
    except Exception as e:
        logger.warning("veto event journal write failed for %s: %r",
                       mint, e)


def _check_bundle(mint: str) -> tuple[bool, str, Dict]:
    """Entity-adjusted bundle veto: size alone only at the extreme,
    otherwise behavior-gated (2026-10-20).

    - cluster share >= 50%            -> VETO (extreme share)
    - cluster share 20-50% AND the sampled cluster wallets are net
      selling this token (bundle.cluster_distribution) -> VETO
    - cluster share 20-50%, holding or behavior unknown -> NO veto;
      a "bundle:cluster X% holding" flag is recorded in details
      (fail-open: vetoes only on positive evidence)
    - cluster share < 20%             -> pass, unchanged
    """
    try:
        from . import bundle
        deep = bundle.analyze(mint, max_cluster_pct=BUNDLE_HARD_PCT)
        try:
            pct = float(deep.get("cluster_pct") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct >= BUNDLE_HARD_PCT:
            return False, (
                "bundle:cluster %.1f%% >= %.0f%% (extreme share)"
                % (pct, BUNDLE_HARD_PCT)), deep
        if pct >= BUNDLE_BEHAVIOR_PCT:
            wallets = [str(w.get("wallet"))
                       for w in (deep.get("cluster_wallets") or [])
                       if isinstance(w, dict) and w.get("wallet")]
            beh = bundle.cluster_distribution(mint, wallets) \
                if wallets else {"verdict": "unknown"}
            deep["behavior"] = beh
            verdict = beh.get("verdict")
            if verdict == "distributing":
                return False, (
                    "bundle:cluster %.1f%% distributing "
                    "(cluster wallets net selling)" % pct), deep
            # Holding, or behavior data unavailable -> flag, never veto.
            deep["flag"] = "bundle:cluster %.1f%% %s" % (
                pct, "holding" if verdict == "holding"
                else "behavior unknown")
            return True, "", deep
        return True, "", deep
    except Exception as e:
        # Fail-open on infrastructure error, but flag it
        return True, "", {"unknown": True, "error": str(e)[:100]}


def _check_wash(mint: str, tx_count_5m: Optional[int]) -> tuple[bool, str, Dict]:
    """Bump-bot wash pattern: high txns + flat holder velocity."""
    if tx_count_5m is None:
        return True, "", {"unknown": True, "reason": "no txn data"}
    try:
        from . import holder_tracker
        if holder_tracker.wash_detected(mint, tx_count_5m):
            return False, "wash:bump-bot pattern (txns up, holders flat)", {
                "tx_count_5m": tx_count_5m}
        return True, "", {"tx_count_5m": tx_count_5m}
    except Exception as e:
        return True, "", {"unknown": True, "error": str(e)[:100]}


def _check_deployer(mint: str, chain: str) -> tuple[bool, str, Dict]:
    """Repeat-rug / factory deployer hard veto (was score penalty, now veto)."""
    if chain.lower() != "solana":
        return True, "", {"skipped": "non-solana"}
    try:
        from . import deployers as deployer_mod
        from . import deployer_tracker
        dep = deployer_mod.resolve_deployer(mint, "solana")
        if not dep:
            return True, "", {"unknown": True, "reason": "deployer unresolvable"}
        ds = deployer_tracker.deployer_score(dep)
        if ds.get("is_factory"):
            return False, "deployer:fee-farming factory", ds
        # Repeat launcher with zero survivors -> veto (research: near-veto)
        detail = ds.get("detail", {}) if isinstance(ds, dict) else {}
        launches = detail.get("launches", ds.get("launches", 0))
        runners = detail.get("runners", ds.get("runners", 0))
        if isinstance(launches, int) and launches >= DEPLOYER_MIN_LAUNCHES_VETO \
                and runners == 0:
            return False, (
                f"deployer:repeat-rug ({launches} launches, 0 runners)"), ds
        return True, "", ds
    except Exception as e:
        return True, "", {"unknown": True, "error": str(e)[:100]}


def _check_concentration(report: Optional[Dict]) -> tuple[bool, str, Dict]:
    """RugCheck top-holder concentration veto (fast, no extra RPC)."""
    if not report:
        return True, "", {"unknown": True, "reason": "no safety report"}
    try:
        top10 = None
        for h in (report.get("topHolders") or [])[:10]:
            pass
        holders = report.get("topHolders") or []
        if holders:
            top10 = sum(float(h.get("pct", 0) or 0) for h in holders[:10])
        if top10 is not None and top10 > TOP10_MAX_PCT:
            return False, f"concentration:top10 {top10:.1f}% > {TOP10_MAX_PCT}%", {
                "top10_pct": round(top10, 1)}
        return True, "", {"top10_pct": round(top10, 1) if top10 else None}
    except Exception as e:
        return True, "", {"unknown": True, "error": str(e)[:100]}


def veto_check(mint: str, chain: str = "solana", symbol: str = "",
               safety_report: Optional[Dict] = None,
               tx_count_5m: Optional[int] = None,
               run_bundle: bool = True,
               lane: str = "unknown") -> Dict[str, Any]:
    """Run all hard vetoes. Returns dict(passed, vetoes, flags, details).

    passed=False means DO NOT ENTER under any circumstances — no score,
    no bonus, no lane can override a veto. `lane` names the calling
    entry path (main/steady/degen_v2/mirror/ceo) and is recorded in
    the veto log so counterfactuals can be attributed per lane.
    """
    vetoes: List[str] = []
    flags: List[str] = []
    details: Dict[str, Any] = {}

    # 1. Concentration (cheap, from existing RugCheck report)
    ok, reason, det = _check_concentration(safety_report)
    details["concentration"] = det
    if not ok:
        vetoes.append(reason)
    elif det.get("unknown"):
        flags.append("concentration:unknown")

    # 2. Wash-trade pattern (cheap, in-memory holder velocity)
    ok, reason, det = _check_wash(mint, tx_count_5m)
    details["wash"] = det
    if not ok:
        vetoes.append(reason)
    elif det.get("unknown"):
        flags.append("wash:unknown")

    # 3. Deployer (local registry lookup, cheap)
    ok, reason, det = _check_deployer(mint, chain)
    details["deployer"] = det
    if not ok:
        vetoes.append(reason)
    elif det.get("unknown"):
        flags.append("deployer:unknown")

    # 4. Bundle / entity-adjusted concentration (expensive RPC walk — last)
    if run_bundle and chain.lower() == "solana":
        ok, reason, det = _check_bundle(mint)
        details["bundle"] = {
            k: v for k, v in det.items()
            if k in ("passed", "cluster_pct", "fails", "flags", "unknown",
                     "error", "behavior", "flag")}
        if not ok:
            vetoes.append(reason)
        elif det.get("unknown"):
            flags.append("bundle:unknown")
        if det.get("flag"):
            flags.append(str(det["flag"]))
    else:
        details["bundle"] = {"skipped": True}

    passed = not vetoes
    if not passed:
        _log_veto(mint, symbol, chain, vetoes, details, lane=lane)
        logger.warning("VETO [%s] %s %s: %s", lane, symbol, mint[:8],
                       "; ".join(vetoes))
    _log_veto_event(mint, chain, passed, vetoes, lane=lane)

    return {
        "passed": passed,
        "vetoes": vetoes,
        "flags": flags,
        "details": details,
    }
