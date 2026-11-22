"""Read-only shadow reader: live signal outputs -> fusion inputs.

Phase 13 finalize — the Phase 11 live-wiring follow-up, built the
only way it can be built without touching a live file and without
importing the launch_scanner monolith (dual-import hazard).

What this module does
---------------------
Translates EXISTING live signal outputs into the fusion input
schema (see ``SIGNAL_MAPPING`` in fusion.py). It produces
observations; it never calls a trading path, never changes a lane
setting, stake, or killswitch state, never writes a live file,
and never imports ``daemon.launch_scanner`` (the package
``__init__`` monolith). Two mechanisms make that possible:

1. Pure translators. ``veto_observation`` / ``safety_observation``
   translate a veto_check / safety.check_token RESULT dict handed
   in by the caller. ``conviction_observation``,
   ``grad_select_filters_observation`` and
   ``deployer_band_observation`` translate measured values /
   entry_features dicts. ``regime_observation`` translates a
   lane's stored closes. ``signals_for_close`` assembles all of
   the file-based translators for one stored close record, and
   ``close_signal_provider(home)`` turns that into a
   ``signal_provider(mint, lane)`` callable for ShadowFusionRunner
   — it reads lane ledgers read-only and returns [] for a mint
   with no stored close.

2. Standalone file-loading. conviction.py, regime.py and
   instant_safety.py are stdlib-only modules with NO relative
   imports, so they can be loaded straight from their file paths
   under a synthetic module name (``_load_standalone``) without
   executing the launch_scanner package ``__init__``. The loader
   REFUSES any module whose source contains a relative import —
   that is the guard that keeps the monolith out.

What is deliberately NOT wired (and why)
-----------------------------------------
* ``veto_check()`` itself: it lazy-imports ``bundle``,
  ``holder_tracker``, ``deployers`` and ``deployer_tracker`` via
  relative imports (``from . import ...``), which only resolve
  inside the monolith package; it WRITES veto journals
  (veto_log.jsonl, veto_events.jsonl) under FOREX_AGENT_HOME on
  every evaluation; and its bundle step walks live RPC. There is
  no read-only way to call it from here. The wiring plan, if the
  coordinator ever wants a live veto feed: extract the pure
  veto predicates into a stdlib-only module (or run veto_check in
  a subprocess with FOREX_AGENT_HOME pointed at a scratch dir and
  the journal paths redirected), and only then translate its
  result dict with ``veto_observation``.
* Live instant-safety reads: ``live_safety_observation`` loads
  instant_safety.py standalone and calls ``check(mint)`` — a live
  RPC read that consumes provider quota. It is opt-in only, never
  called by any default path in this module, and is meant for an
  explicit on-phone shadow run, not for this VM (Helius is
  flapping in auto-quiet here).
* Regime scale: ``regime_observation`` reports sit-out; the
  stake-scale itself is a trading decision and is never emitted.

Every observation this module emits carries
``verification="verified"`` with a ``stage`` of "confirmed" ONLY
when the underlying output is a measured fact (a stored close's
entry_features, a gate verdict on stated values). A translator
that cannot establish provenance returns outcome "unknown".
Freshness is the caller's stated age; the engine re-clocks it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_LAUNCH_SCANNER = os.path.join(_REPO, "daemon", "launch_scanner")

_RELATIVE_IMPORT = re.compile(r"^\s*(from\s+\.|import\s+\.)",
                              re.MULTILINE)

# Modules proven stdlib-only with no relative imports (verified
# 2026-11-22 by reading their sources; the loader re-checks on
# every load, so a future relative import fails loudly instead of
# silently dragging the monolith in).
_LOADABLE = ("conviction", "regime", "instant_safety")

_LOADED = {}


class ShadowReaderError(Exception):
    """The reader cannot load or translate something. Never raised
    for a missing signal — translators return outcome "unknown"
    instead; this error is only for the loader guard and bad
    caller input."""


def _load_standalone(name):
    """Load daemon/launch_scanner/<name>.py without importing the
    launch_scanner package. Raises ShadowReaderError when the
    module is not on the allow-list or its source contains a
    relative import (the monolith guard)."""
    if name in _LOADED:
        return _LOADED[name]
    if name not in _LOADABLE:
        raise ShadowReaderError(
            "%s is not standalone-loadable (allow-list: %s); "
            "loading it would import the launch_scanner monolith"
            % (name, ", ".join(_LOADABLE)))
    path = os.path.join(_LAUNCH_SCANNER, "%s.py" % name)
    try:
        with open(path) as fh:
            source = fh.read()
    except OSError as exc:
        raise ShadowReaderError("cannot read %s: %s" % (path, exc))
    if _RELATIVE_IMPORT.search(source):
        raise ShadowReaderError(
            "%s contains a relative import; refusing to load it "
            "standalone (it would resolve inside the monolith)"
            % name)
    module_name = "intelligence_shadow_%s" % name
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _LOADED[name] = module
    return module


def _obs(name, outcome, evidence=None, detail="", freshness_s=None,
         hard_veto=False, security_hard_fail=False,
         contradictory=False, weight=None, reliability=1.0):
    return {"name": name, "outcome": outcome,
            "hard_veto": hard_veto,
            "security_hard_fail": security_hard_fail,
            "contradictory": contradictory,
            "weight": weight, "reliability": reliability,
            "freshness_s": freshness_s,
            "provider": "live-shadow-reader",
            "stage": "confirmed", "verification": "verified",
            "evidence": dict(evidence or {}), "detail": detail}


# ---------------------------------------------------------------------------
# Translators (pure: no I/O, no network, no monolith import)
# ---------------------------------------------------------------------------

def veto_observation(veto_result):
    """Translate a veto_check() RESULT dict (produced elsewhere)
    into a fusion observation. This function never CALLS
    veto_check — see the module docstring for why that call is
    unwired. veto_result=None -> outcome "unknown" (no evidence,
    never a pass)."""
    if not isinstance(veto_result, dict):
        return _obs("veto", "unknown",
                    {"note": "no veto result supplied; full "
                             "veto_check is not callable from the "
                             "shadow reader"},
                    detail="veto: no result (unwired)")
    vetoes = veto_result.get("vetoes") or []
    flags = veto_result.get("flags") or []
    if veto_result.get("passed") is False or vetoes:
        return _obs("veto", "fail", hard_veto=True,
                    evidence={"vetoes": list(vetoes),
                              "flags": list(flags),
                              "details": veto_result.get("details")
                              or {}},
                    detail="veto: %s" % "; ".join(vetoes[:2]))
    return _obs("veto", "pass",
                evidence={"vetoes": [], "flags": list(flags)},
                detail="veto: passed")


def safety_observation(report):
    """Translate a safety.check_token() / instant_safety.check()
    RESULT dict into a fusion observation. report=None or
    unknown=True -> outcome "unknown" (never a pass). passed=False
    -> outcome "fail" with security_hard_fail=True."""
    if not isinstance(report, dict):
        return _obs("instant_safety", "unknown",
                    {"note": "no safety report supplied"},
                    detail="instant_safety: no report")
    if report.get("unknown"):
        return _obs("instant_safety", "unknown",
                    {"flags": list(report.get("flags") or [])},
                    detail="instant_safety: report unknown")
    if report.get("passed") is False:
        fails = list(report.get("fails") or [])
        return _obs("instant_safety", "fail",
                    security_hard_fail=True,
                    evidence={"fails": fails,
                              "flags": list(report.get("flags") or []),
                              "score": report.get("score")},
                    detail="instant_safety: %s"
                           % "; ".join(fails[:2]))
    return _obs("instant_safety", "pass",
                evidence={"score": report.get("score"),
                          "flags": list(report.get("flags") or [])},
                detail="instant_safety: passed")


def grad_select_filters_observation(values):
    """Translate the four grad_select evidence filters from a
    measured-values dict (the shape stamped into a close's
    entry_features: deployer_launches_24h, curve_trades, ttg_s,
    ticker_others_24h, plus canonical-pool evidence when the
    caller supplies it).

    All four passing -> "pass". Any failing or unknown filter ->
    "fail" with evidence naming it (fail-closed, as the live lane
    is: an unknown filter never enters)."""
    values = values if isinstance(values, dict) else {}
    failed = []
    unknown = []

    def _num(key):
        try:
            v = values.get(key)
            return None if v is None or isinstance(v, bool) \
                else float(v)
        except (TypeError, ValueError):
            return None

    dep = _num("deployer_launches_24h")
    if dep is None:
        unknown.append("deployer_launches_24h")
    elif dep >= 3:
        failed.append("serial_deployer>=3")
    trades = _num("curve_trades")
    if trades is None:
        unknown.append("curve_trades")
    elif trades > 250:
        failed.append("curve_trades>250")
    ttg = _num("ttg_s")
    if ttg is None:
        unknown.append("ttg_s")
    elif ttg > 3600:
        failed.append("slow_curve>3600s")
    copy = _num("ticker_others_24h")
    if copy is None:
        unknown.append("ticker_others_24h")
    elif copy >= 3:
        failed.append("copycat_tickers>=3")
    if values.get("canonical_pool") is False:
        failed.append("non_canonical_pool")
    if failed or unknown:
        return _obs("grad_select_filters", "fail",
                    evidence={"failed": failed, "unknown": unknown},
                    detail="grad_select_filters: %s"
                           % "; ".join(failed + unknown))
    return _obs("grad_select_filters", "pass",
                evidence={"deployer_launches_24h": dep,
                          "curve_trades": trades, "ttg_s": ttg,
                          "ticker_others_24h": copy},
                detail="grad_select_filters: all four pass")


def conviction_observation(values, freshness_s=None):
    """Translate conviction.score_candidate()/gate() (loaded
    standalone — conviction.py is pure stdlib) into a fusion
    observation. BLOCK tier -> "fail" AND contradictory=True only
    when the blocking reason is a verified fact (serial deployer
    or copycat ticker), per SIGNAL_MAPPING; a score judgement
    (slow entry, high mcap) blocks without the contradiction flag.
    WEAK -> "flag" (advisory, never a qualifying positive).
    STRONG/STANDARD -> "pass". SKIP -> "fail"."""
    conviction = _load_standalone("conviction")
    result = conviction.gate(values)
    conv = result.get("conviction") or {}
    tier = conv.get("tier")
    reasons = list(conv.get("reasons") or [])
    verified_fact_block = bool(
        set(reasons) & {"serial_deployer", "copycat_ticker"})
    evidence = {"tier": tier, "score": conv.get("score"),
                "components": conv.get("components") or {},
                "reasons": reasons,
                "gate_reason": result.get("reason")}
    verdict = result.get("verdict")
    if verdict == "reject" and tier == "BLOCK":
        return _obs("conviction", "fail",
                    contradictory=verified_fact_block,
                    evidence=evidence, freshness_s=freshness_s,
                    detail="conviction: BLOCK (%s)"
                           % "; ".join(reasons[:2]))
    if verdict == "reject":
        return _obs("conviction", "fail", evidence=evidence,
                    freshness_s=freshness_s,
                    detail="conviction: SKIP (%s)"
                           % "; ".join(reasons[:2]))
    if verdict == "review":
        return _obs("conviction", "flag", evidence=evidence,
                    freshness_s=freshness_s,
                    detail="conviction: WEAK -> review queue")
    return _obs("conviction", "pass", evidence=evidence,
                freshness_s=freshness_s,
                detail="conviction: %s (score %s)"
                       % (tier, conv.get("score")))


def deployer_band_observation(entry_features):
    """Translate the deployer band from a close's entry_features:
    band 1-2 -> "pass"; band 0 -> "flag"; band 3+ -> "fail";
    unknown -> "unknown". The band is supporting evidence only;
    it can never qualify a token on its own."""
    features = entry_features if isinstance(entry_features, dict) \
        else {}
    band = features.get("deployer_band")
    launches = features.get("deployer_launches_24h")
    if band is None and isinstance(
            features.get("select"), dict):
        launches = features["select"].get("deployer_launches_24h",
                                          launches)
    if band is None and launches is not None:
        try:
            n = float(launches)
            band = "1-2" if 1 <= n <= 2 else ("0" if n == 0 else "3+")
        except (TypeError, ValueError):
            band = None
    if band in ("1-2", "1–2"):
        return _obs("deployer_band", "pass",
                    evidence={"band": band,
                              "deployer_launches_24h": launches},
                    detail="deployer_band: 1-2 launches")
    if band == "0":
        return _obs("deployer_band", "flag",
                    evidence={"band": band},
                    detail="deployer_band: 0 launches (new deployer)")
    if band == "3+":
        return _obs("deployer_band", "fail",
                    evidence={"band": band,
                              "deployer_launches_24h": launches},
                    detail="deployer_band: 3+ launches (serial)")
    return _obs("deployer_band", "unknown",
                evidence={"band": band},
                detail="deployer_band: unknown")


def regime_observation(closes):
    """Translate regime.sit_out() (loaded standalone — regime.py
    is pure stdlib) over a lane's stored closes. Returns
    {"observation", "restrictions"}: sit-out active ->
    restrictions {"regime_sit_out": True} (caps the decision at
    REVIEW) and the observation is a flag; otherwise the
    observation is a "pass" with the regime evidence. The stake
    scale itself is never emitted."""
    regime = _load_standalone("regime")
    info = regime.sit_out(closes or [])
    evidence = {"regime": info.get("regime"),
                "sit_out": info.get("sit_out"),
                "reason": info.get("reason"),
                "evidence": info.get("evidence") or {}}
    if info.get("sit_out"):
        return {"observation": _obs(
                    "regime", "flag", evidence=evidence,
                    detail="regime: sit-out (%s)"
                           % (info.get("reason") or "cold_bleed")),
                "restrictions": {"regime_sit_out": True}}
    return {"observation": _obs(
                "regime", "pass", evidence=evidence,
                detail="regime: %s, no sit-out"
                       % (info.get("regime") or "?")),
            "restrictions": {"regime_sit_out": False}}


# ---------------------------------------------------------------------------
# File-based assembly (read-only)
# ---------------------------------------------------------------------------

def _values_from_close(close):
    """Merge a stored close's entry_features + entry_snapshot into
    one measured-values dict for the conviction / filter
    translators. Read-only; returns {} when nothing is stamped."""
    values = {}
    features = (close or {}).get("entry_features") or {}
    for key in ("grad_age_s", "mcap", "liquidity",
                "deployer_launches_24h", "curve_trades", "ttg_s",
                "ticker_others_24h", "bear_score"):
        if features.get(key) is not None:
            values[key] = features[key]
    select = features.get("select") or features.get("tranche") or {}
    if isinstance(select, dict):
        for key in ("deployer_launches_24h", "curve_trades",
                    "ttg_s", "ticker_others_24h"):
            if values.get(key) is None and select.get(key) is not None:
                values[key] = select[key]
    snapshot = (close or {}).get("entry_snapshot") or {}
    for key in ("mcap", "liquidity"):
        if values.get(key) is None and snapshot.get(key) is not None:
            values[key] = snapshot[key]
    return values


def signals_for_close(close, freshness_s=None):
    """All file-based fusion observations for one stored close
    record (read-only translation of what the live lane stamped
    at entry time). Veto / instant-safety come back "unknown"
    here: no veto result or safety report is stamped on a close,
    and the reader never invents one — a caller that HAS the
    report translates it with veto_observation /
    safety_observation and appends it."""
    close = close or {}
    features = close.get("entry_features") or {}
    values = _values_from_close(close)
    freshness = freshness_s
    if freshness is None:
        try:
            freshness = max(
                0.0, time.time() - float(close.get("close_ts") or 0))
        except (TypeError, ValueError):
            freshness = None
    observations = [
        veto_observation(None),
        safety_observation(_close_safety_report(close)),
        grad_select_filters_observation(values),
        conviction_observation(values, freshness_s=freshness),
        deployer_band_observation(features),
    ]
    return observations


def _close_safety_report(close):
    """The safety report stamped on a close, if it carries a real
    verdict shape (a "passed"/"fails" verdict). The usual stamped
    safety blob ({"score", "flags", "source"}) is NOT a verdict —
    it becomes None here, i.e. outcome "unknown", never a pass."""
    safety = (close.get("entry_snapshot") or {}).get("safety")
    if isinstance(safety, dict) and ("passed" in safety
                                     or "fails" in safety):
        return safety
    return None


def _read_lane_closes(home, lane):
    path = os.path.join(home, "%s_ledger.json" % lane)
    try:
        with open(path) as fh:
            return (json.load(fh) or {}).get("closes") or []
    except (OSError, ValueError):
        return []


def close_signal_provider(home):
    """Build a ShadowFusionRunner signal_provider(mint, lane) that
    reads lane ledgers READ-ONLY and returns the stored close's
    translated signals, or [] when the mint has no stored close
    in that lane (no signal is never reported as a pass)."""
    def provider(mint, lane):
        for close in _read_lane_closes(home, lane):
            if close.get("mint") == mint:
                return signals_for_close(close)
        return []
    return provider


def regime_restrictions(home, lane):
    """Read-only regime restrictions for a lane from its stored
    closes (see regime_observation)."""
    return regime_observation(_read_lane_closes(home, lane))[
        "restrictions"]


# ---------------------------------------------------------------------------
# Opt-in live read (NOT a default path)
# ---------------------------------------------------------------------------

def live_safety_observation(mint, timeout=8.0):
    """Opt-in live Token-2022 / transfer-hook safety read via the
    standalone-loaded instant_safety.check(mint). Makes LIVE RPC
    calls and consumes provider quota — call it only from an
    explicit on-phone shadow run, never from this VM while Helius
    is flapping, and never in a loop over many mints."""
    safety = _load_standalone("instant_safety")
    report = safety.check(mint, timeout=timeout)
    return safety_observation(report)
