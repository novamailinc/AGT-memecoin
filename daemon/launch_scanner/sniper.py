"""Sniper-tier runner-probability ranking ("monitor wide, alert narrow").

Scores every quality-passed candidate 0-100 from signals that already
exist and are live — fingerprint composite, tracked smart-wallet buys,
early velocity (rising polls), buy/sell pressure, and hot-deployer
history as a small boost. Pure logic: no I/O, no trading, no RPC calls
(the deployer check reads only the already-cached deployer, never
resolving — history must never slow the hot path).

Fail-safe throughout: a missing signal contributes 0, never a penalty
and never a boost. Unknown/unscored handling is unchanged — this module
only ever sees coins that ALREADY passed ``_instant_verdict``.

Weights and the tier threshold are ``degen.*`` config knobs so the
Sunday review can tune them on evidence without a code change. Safety
gates never self-tune and are untouched by this module.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import deployers
from . import smartmoney


def _num(value: Any, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ----------------------------------------------------------------------
# Market-tape fallback fingerprint (2026-10-21, KIMCHI post-mortem)
#
# The fp tracker only accumulates polls for mints the judge cycles
# while they sit in `candidates`. Migration/logstream candidates are
# ranked (birth + rescore) with the tracker at None — in the live log
# the max sniper score ever recorded was 6.0 of 100 (bar 50): fp (30)
# and pressure (10) were dead for the entire graduated-runner class.
# This fallback computes the SAME shape of composite from one live
# DexScreener snapshot: buy-rate acceleration (m5 rate vs h1 rate
# instead of last-poll vs earlier-poll mean), volume acceleration,
# and buy pressure. Holder growth is unobservable from tape, so the
# composite is CAPPED at 70 and can never verdict HOT — the missing
# 20-pt holder component is an honest discount, not a free pass.
# ----------------------------------------------------------------------
TAPE_COMPOSITE_CAP = 70.0


def tape_composite(stats: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Fingerprint-shaped composite from one market snapshot.

    ``stats`` is a sources._pair_to_stats dict (buys_m5/sells_m5,
    buys_h1/sells_h1, vol_m5/vol_h1). Returns None when the snapshot
    carries no usable transaction data at all. Never raises.
    """
    try:
        if not stats:
            return None
        b5 = float(stats.get("buys_m5") or 0)
        s5 = float(stats.get("sells_m5") or 0)
        b1 = float(stats.get("buys_h1") or 0)
        s1 = float(stats.get("sells_h1") or 0)
        v5 = float(stats.get("vol_m5") or 0)
        v1 = float(stats.get("vol_h1") or 0)
        if (b5 + s5) <= 0 and (b1 + s1) <= 0:
            return None
        # Buy pressure: widest window with data (h1 is less noisy).
        if (b1 + s1) > 0:
            pressure = b1 / (b1 + s1)
        else:
            pressure = b5 / (b5 + s5)
        # Rate acceleration: per-minute m5 rate vs per-minute h1 rate.
        # Mirrors fingerprints.py (last vs earlier mean, cap 4x). When
        # the h1 window is empty but m5 is active, the mint is newer
        # than the window — treat as max acceleration only if the m5
        # count is meaningful (>=10 buys), else neutral 1.0.
        def _accel(m5_val: float, h1_val: float, min_m5: float) -> float:
            r5 = m5_val / 5.0
            r1 = h1_val / 60.0
            if r1 > 0:
                return min(9.99, r5 / r1)
            return 4.0 if m5_val >= min_m5 else 1.0
        buy_accel = _accel(b5, b1, 10.0)
        vol_accel = _accel(v5, v1, 1000.0)
        parts = [
            (30.0, min(buy_accel, 4.0) / 4.0),
            (25.0, min(vol_accel, 4.0) / 4.0),
            (15.0, pressure),
        ]
        total_w = sum(w for w, _ in parts)
        composite = round(sum(w * v for w, v in parts) / total_w * 100.0, 1)
        composite = min(composite, TAPE_COMPOSITE_CAP)
        verdict = "WARMING" if composite >= 45.0 else "COLD"
        return {
            "composite": composite,
            "verdict": verdict,
            "buy_accel": round(buy_accel, 2),
            "vol_accel": round(vol_accel, 2),
            "holder_growth_pct": None,
            "buy_pressure": round(pressure, 3),
            "source": "tape",
        }
    except Exception:
        return None


def rank_candidate(degen: Any, signals: Dict[str, Any]) -> Dict[str, Any]:
    """Score a quality-passed candidate 0-100 for runner probability.

    ``signals`` keys (all optional; missing == 0 contribution):
      fp_composite  0-100 fingerprint composite (None when too early)
      fp_verdict    "HOT" / "WARMING" / "COLD" / None
      smart_buys    int — tracked wallets that bought this mint
      pack_strength 0-1 — known wallet-pack coordinated buying
                      (on-mode only: up to sniper_pack_bonus_max pts)
      rising_polls  int — consecutive rising observations
      buy_pressure  0-1 buys/(buys+sells) (None when unknown)
      hot_deployer  bool — deployer already cached AND hot in registry
      narrative_heat  0.5-2.0 narrative heat multiplier (#5). Heat above
                      1.0 earns up to ``narrative_boost_max`` bonus points;
                      heat <= 1.0 is NEVER a penalty (soft signal only).

    Returns {"score", "tier" ("sniper"/"standard"), "reasons",
             "threshold", "enabled"}. Tier is "sniper" when enabled and
    score >= sniper_tier_threshold. Reasons are short human phrases for
    the card's one-line "why".
    """
    signals = signals or {}
    enabled = bool(getattr(degen, "sniper_enabled", True))
    threshold = _num(getattr(degen, "sniper_tier_threshold", 50.0), 50.0)
    w_fp = _num(getattr(degen, "sniper_weight_fp", 30.0), 30.0)
    # Smart-money is WATCHLIST-ONLY (user decision 2026-10-20):
    # delayed/cluster copy-trading measured -41% avg in the research
    # pass and our own follower grades show 0 active wallets, so the
    # smart-buy component contributes 0.0 points by default. The
    # formula and reason strings below stay intact (reasons carry a
    # "(watchlist)" suffix — the signal is displayed, never scored);
    # re-enabling requires follower-grade evidence, not a default edit.
    w_smart = _num(getattr(degen, "sniper_weight_smart", 0.0), 0.0)
    w_vel = _num(getattr(degen, "sniper_weight_velocity", 20.0), 20.0)
    w_press = _num(getattr(degen, "sniper_weight_pressure", 10.0), 10.0)
    boost_dep = _num(getattr(degen, "sniper_boost_deployer", 5.0), 5.0)

    parts: List[float] = []
    reasons: List[str] = []

    # Fingerprint composite 0-100 -> weighted share. HOT/WARMING earn a
    # reason line; COLD still contributes its (small) score, no praise.
    fp = signals.get("fp_composite")
    if isinstance(fp, (int, float)) and fp > 0:
        parts.append(w_fp * _clamp01(float(fp) / 100.0))
        verdict = (signals.get("fp_verdict") or "").upper()
        _tape = signals.get("fp_source") == "tape"
        if verdict == "HOT":
            reasons.append("HOT fingerprint")
        elif verdict == "WARMING":
            reasons.append("warming tape print" if _tape
                           else "warming fingerprint")

    # Tracked smart-wallet buys: effective (grade-weighted) buys.
    # Continuous share of the weight: 2.0 effective buys earn it all —
    # exactly the old "2+ wallets -> full, 1 -> half" when every weight
    # is 1.0, but a proven picker now counts for more (and an unproven
    # one for less) than a raw headcount.
    try:
        eff_smart = float(signals.get("smart_buys") or 0)
    except (TypeError, ValueError):
        eff_smart = 0.0
    if eff_smart > 0:
        parts.append(w_smart * min(1.0, eff_smart / 2.0))
        try:
            n_raw = int(signals.get("smart_buys_raw")
                        if signals.get("smart_buys_raw") is not None
                        else round(eff_smart))
        except (TypeError, ValueError):
            n_raw = 0
        if abs(eff_smart - n_raw) > 0.05:
            reasons.append("%d smart buys (%.1f eff, grade-weighted) "
                           "(watchlist)" % (n_raw, eff_smart))
        else:
            reasons.append("%d smart buy%s (watchlist)" % (
                n_raw, "" if n_raw == 1 else "s"))

    # Wallet-pack bonus (ON mode only): coordinated buying by a known
    # pack earns up to sniper_pack_bonus_max points scaled by pack
    # strength. In measure/off mode pack_bonus_points() returns 0.0,
    # so scoring is untouched until the user flips the switch.
    try:
        from . import packdetect as _packdetect  # noqa: PLC0415 (lazy)
        _pstr = float(signals.get("pack_strength") or 0.0)
        _pbonus = _packdetect.pack_bonus_points(degen, _pstr)
    except Exception:
        _pbonus = 0.0
    if _pbonus > 0:
        parts.append(_pbonus)
        try:
            _nm = int(signals.get("pack_members") or 0)
        except (TypeError, ValueError):
            _nm = 0
        reasons.append("pack buy \U0001F43A (%d members)" % _nm)

    # Early velocity: consecutive rising polls. Needs at least 2 to
    # count as momentum; a single uptick is noise.
    try:
        r = int(signals.get("rising_polls") or 0)
    except (TypeError, ValueError):
        r = 0
    if r >= 3:
        parts.append(w_vel)
        reasons.append("rising momentum")
    elif r == 2:
        parts.append(w_vel * 0.65)
        reasons.append("rising momentum")
    elif r == 1:
        parts.append(w_vel * 0.3)

    # Buy/sell pressure 0-1 -> weighted share. Missing stays 0.
    p = signals.get("buy_pressure")
    if isinstance(p, (int, float)):
        parts.append(w_press * _clamp01(float(p)))

    # Hot deployer: small flat boost, never a penalty. Unknown/cold
    # deployers are neutral by construction (deployers.is_hot).
    if signals.get("hot_deployer"):
        parts.append(boost_dep)
        reasons.append("hot deployer")

    # Narrative heat boost (#5): a coin riding a hot narrative earns up
    # to ``narrative_boost_max`` extra points, scaled by how far above
    # neutral (1.0) its heat is. Heat <= 1.0 (cold/neutral/unknown)
    # contributes 0 — a cold narrative never blocks or penalizes an
    # otherwise qualified coin. Toggle: ``narrative_boost_enabled``.
    if bool(getattr(degen, "narrative_boost_enabled", True)):
        heat = signals.get("narrative_heat")
        if isinstance(heat, (int, float)) and float(heat) > 1.0:
            max_boost = _num(getattr(degen, "narrative_boost_max", 8.0),
                             8.0)
            if max_boost > 0:
                boost = min(max_boost,
                            max_boost * (float(heat) - 1.0))
                parts.append(boost)
                reasons.append("hot narrative x%.2f" % float(heat))

    score = round(min(100.0, sum(parts)), 1)
    tier = "sniper" if (enabled and score >= threshold) else "standard"
    # Compact signal snapshot — the #6 entry learner's raw material.
    # Recorded on paper positions at entry so closed trades can be
    # attributed back to the signals that picked them.
    snap = signals if isinstance(signals, dict) else {}
    snapshot = {
        "smart_buys": _num(snap.get("smart_buys"), 0.0),
        "smart_buys_raw": _num(snap.get("smart_buys_raw"),
                               _num(snap.get("smart_buys"), 0.0)),
        "fp": _num(snap.get("fp_composite"), 0.0),
        "fp_verdict": str(snap.get("fp_verdict") or ""),
        "fp_source": str(snap.get("fp_source") or ""),
        "rising_polls": _num(snap.get("rising_polls"), 0.0),
        "buy_pressure": _num(snap.get("buy_pressure"), 0.0),
        "hot_deployer": bool(snap.get("hot_deployer")),
        "heat": _num(snap.get("narrative_heat"), 1.0),
    }
    return {
        "score": score,
        "tier": tier,
        "reasons": reasons,
        "threshold": threshold,
        "enabled": enabled,
        "signals_snapshot": snapshot,
    }


def sniper_line(result: Optional[Dict[str, Any]]) -> str:
    """One-line "why this made the tier" for the instant card."""
    if not result:
        return ""
    why = " + ".join(result.get("reasons") or []) or "quality-passed"
    return "🎯 sniper %.1f — %s" % (float(result.get("score") or 0), why)


def collect_signals(owner: Any, state: Dict[str, Any], degen: Any,
                    mint: Optional[str],
                    entry: Optional[Dict[str, Any]],
                    tape_stats: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Gather the live signals for one candidate. Never raises, never
    does I/O beyond cheap local reads: fingerprint tracker (in-memory),
    smart-money activity (in-memory), mcap history (in-memory), and the
    deployer cache (in-memory; ``deployers.is_hot`` reads the local
    registry file — no RPC, never a fresh resolution here).

    Every lookup is guarded: on any trouble the signal stays at its
    zero default, which can only lower the score, never inflate it.
    """
    signals: Dict[str, Any] = {
        "fp_composite": None,
        "fp_verdict": None,
        "smart_buys": 0,
        "rising_polls": 0,
        "buy_pressure": None,
        "hot_deployer": False,
    }
    # Fingerprint composite / verdict / pressure / rising streak.
    try:
        tracker = getattr(owner, "_fp_tracker", None)
        sc = tracker.score(mint) if (tracker and mint) else None
        if sc:
            signals["fp_composite"] = sc.get("composite")
            signals["fp_verdict"] = sc.get("verdict")
            signals["buy_pressure"] = sc.get("buy_pressure")
            signals["fp_source"] = "tracker"
            try:
                signals["rising_polls"] = int(sc.get("rising_buys") or 0)
            except (TypeError, ValueError):
                pass
        elif tape_stats and bool(getattr(
                degen, "sniper_tape_fallback_enabled", True)):
            # Tape fallback (2026-10-21): the tracker never accumulated
            # polls for this mint (migration/logstream class) — judge
            # the live market tape instead of scoring 0 by default.
            # The tracker stays authoritative whenever it has a score.
            tc = tape_composite(tape_stats)
            if tc:
                signals["fp_composite"] = tc.get("composite")
                signals["fp_verdict"] = tc.get("verdict")
                signals["buy_pressure"] = tc.get("buy_pressure")
                signals["fp_source"] = "tape"
    except Exception:
        pass
    # Tracked smart-wallet buys (distinct wallets, recent activity),
    # weighted by grade: a proven picker's buy counts more than an
    # unproven wallet's. Weights default to 1.0 (neutral) when grading
    # is off or a wallet has thin history — the raw count is the
    # fallback, never zeroed.
    try:
        if mint:
            addrs = list((state.get("smart_activity") or {}).get(mint)
                         or [])
            signals["smart_buys_raw"] = len(addrs)
            if addrs and bool(getattr(degen, "sniper_smart_grade_enabled",
                                      True)):
                # 2026-10-17 rebuild: weights now come from simulated
                # FOLLOWER P&L (follower_grader), not the wallet's own
                # peak multiples (smartgrade.wallet_weight is superseded).
                from . import follower_grader  # noqa: PLC0415 (lazy)
                weights = follower_grader.follower_weights(degen)
                eff = sum(float(weights.get(a, 1.0)) for a in addrs)
            else:
                eff = float(len(addrs))
            signals["smart_buys"] = round(eff, 2)
    except Exception:
        pass
    # Velocity from the seen-entry mcap history: trailing consecutive
    # rises. Takes the max with the fingerprint rising streak.
    try:
        hist = (entry or {}).get("mcap") or []
        rises = 0
        for i in range(len(hist) - 1, 0, -1):
            try:
                prev = float(hist[i - 1][1] or 0)
                cur = float(hist[i][1] or 0)
            except (TypeError, ValueError, IndexError):
                break
            if cur > prev > 0:
                rises += 1
            else:
                break
        signals["rising_polls"] = max(int(signals["rising_polls"] or 0),
                                      rises)
    except Exception:
        pass
    # Hot deployer: cache-only. A mint whose deployer was never resolved
    # (or resolved cold/unknown) earns no boost — neutral, never penalty.
    try:
        cache = state.get("deployer_cache") or {}
        cent = cache.get(mint) if isinstance(cache, dict) else None
        dep = (cent or {}).get("deployer") if isinstance(cent, dict) else None
        if dep and deployers.is_hot(dep, degen):
            signals["hot_deployer"] = True
    except Exception:
        pass
    # Wallet-pack signal: coordinated buying by wallets that habitually
    # co-buy. Mode-gated inside packdetect: off = 0.0; measure = logged
    # only; on = also scored in rank_candidate. Never raises.
    signals["pack_strength"] = 0.0
    signals["pack_members"] = 0
    try:
        from . import packdetect  # noqa: PLC0415 (lazy: init order)
        if mint and packdetect.mode(degen) != "off":
            sig = packdetect.pack_signal_for_mint(state, degen, mint)
            signals["pack_strength"] = float(sig.get("strength") or 0.0)
            signals["pack_members"] = len(sig.get("members") or [])
            if signals["pack_strength"] > 0:
                signals["pack_packs"] = [p["pack_id"]
                                         for p in (sig.get("packs") or [])]
                packdetect.measure(state, degen, mint, sig)
    except Exception:
        pass
    return signals
