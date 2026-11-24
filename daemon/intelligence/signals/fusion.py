"""Signal-driven fusion engine (Phase 11, spec section 14, 2026-11-22).

Fuses the existing signals into ONE decision per candidate token.
This module is SIGNAL-DRIVEN market intelligence, NOT prediction
driven trading: no ML/pump-prediction model is the central
decision-maker, no score overrides a veto, and no trade is ever
authorised on one positive signal. The engine emits a decision
record only; it never calls any trading path, never changes a lane
setting, stake, or killswitch state. SHADOW MODE ONLY.

Decisions: BLOCK / SKIP / REVIEW / WATCH / QUALIFIED.

Fusion rules (all enforced here, all covered by
tests/test_intelligence_fusion.py):

1. HARD BLOCK wins over everything. A hard veto, a security
   hard-fail, or a verified contradictory condition forces BLOCK
   regardless of any other signal.
2. NO SINGLE-SIGNAL QUALIFICATION. QUALIFIED requires at least two
   INDEPENDENT verified positive signals (distinct signal names),
   a weighted score at or above QUALIFY_SCORE_MIN, no unresolved
   conflict, no restriction breach, and no cost-check refusal
   standing in the way of fresh evidence.
3. CONFLICTS ARE FLAGGED, NEVER RESOLVED. Cross-provider
   disagreement is recorded verbatim in the decision record. A
   conflict on a hard/security field forces BLOCK; any other
   conflict caps the decision at REVIEW.
4. PRELIMINARY IS NEVER VERIFIED EVIDENCE. An observation counts
   as verified evidence only when its lifecycle stage is
   confirmed/finalized AND its verification status is verified
   AND it is fresh (within MAX_FRESHNESS_S). Processed-stage /
   preliminary / unverified observations are listed under
   ``preliminary_excluded`` and contribute nothing to the score
   or the independent-positive count.
5. RESTRICTIONS are enforced, not scored away: liquidity below
   LIQ_FLOOR_USD or mcap below MCAP_FLOOR_USD caps the decision
   at SKIP; an execution cost above the caller's cap caps it at
   REVIEW.
6. COST GATE. The decision path consults
   CostManager.pre_call_check (Phase 9) before any provider-backed
   read; the outcome (allowed / refused, and why) is recorded on
   every decision record. Suppression applies to background /
   nonessential reads only -- essential (critical/card) reads are
   never suppressed.

Input schema (explicit dicts; the launch_scanner monolith is NEVER
imported -- dual-import hazard. Callers translate the existing
signal outputs into this schema; the mapping is documented in
``SIGNAL_MAPPING`` below):

signal observation::
    {
      "name": str,            # e.g. "veto", "instant_safety",
                              # "grad_select_filters", "conviction",
                              # "regime", "deployer_band",
                              # "event_graduation", "event_momentum"
      "outcome": "pass"|"fail"|"flag"|"unknown",
                              # pass = positive evidence,
                              # fail = negative evidence,
                              # flag = advisory only, unknown = no evidence
      "hard_veto": bool,      # veto.py veto_check passed=False
      "security_hard_fail": bool,  # instant-safety hard fail
      "contradictory": bool,  # verified condition contradicting
                              # the positive case (e.g. pool is not
                              # canonical while another signal claims
                              # it is)
      "weight": float,        # default from DEFAULT_WEIGHTS
      "reliability": float,   # 0..1, clamped; default 1.0
      "freshness_s": float|None,  # age of the observation
      "provider": str|None,   # which provider supplied it
      "stage": str|None,      # confirmed|finalized|processed|
                              # preliminary
      "verification": str|None,  # verified|unverified|conflicting
      "evidence": dict,       # free-form supporting facts
      "detail": str,          # short human-readable note
    }

restrictions::
    {"liquidity_usd": float|None, "mcap_usd": float|None,
     "execution_cost_usd": float|None,
     "max_execution_cost_usd": float|None,
     "regime_sit_out": bool}

This package must NOT import the launch_scanner __init__ monolith.
"""

from __future__ import annotations

import hashlib
import json
import time

# ---------------------------------------------------------------------------
# Mapping: existing signal outputs -> fusion input schema.
# The fusion engine re-implements NOTHING below; it reads verdict
# structures (or their dict translations) produced elsewhere.
# ---------------------------------------------------------------------------
SIGNAL_MAPPING = {
    # veto.py veto_check(mint, ...) -> {"passed", "vetoes", "flags",
    # "details"}. passed=False -> ONE observation:
    #   {"name": "veto", "outcome": "fail", "hard_veto": True,
    #    "evidence": {"vetoes": [...], "flags": [...],
    #                 "details": {...}}}
    # passed=True -> {"name": "veto", "outcome": "pass",
    #    "evidence": {"vetoes": [], "flags": [...]}}.
    # Bundle/insider-cluster EXISTENCE is a flag, never a veto:
    # only cluster share >= 50%, or 20-50% with sampled wallets
    # net-selling, arrives here as hard_veto=True.
    "veto": "daemon/launch_scanner/veto.py veto_check()",
    # Instant safety (Token-2022 traps etc., incl. permanentDelegate
    # / transferHook / nonTransferable / default-frozen, transfer
    # fee > 10%): a hard fail arrives as
    #   {"name": "instant_safety", "outcome": "fail",
    #    "security_hard_fail": True, ...};
    # safety-unknown arrives as outcome "unknown" (never a pass).
    "instant_safety": "instant safety report (RugCheck / Token-2022 "
                      "extension read)",
    # grad_select four evidence filters (conviction.score_candidate /
    # gate): serial deployer >= 3, fast curve (<= 3600s AND <= 250
    # curve signatures), canonical pools only, copycat tickers
    # >= 3/24h. All four passing -> outcome "pass"; any failing
    # -> outcome "fail" with evidence naming the failed filter.
    # conviction.gate() BLOCK tier -> outcome "fail" AND
    # "contradictory" True only when the gate failure is a verified
    # fact (e.g. serial deployer), not a score judgement.
    "grad_select_filters": "daemon/launch_scanner/conviction.py "
                           "score_candidate()/gate()",
    # Conviction gate + REVIEW queue: gate tier STRONG/STANDARD ->
    # outcome "pass"; WEAK -> outcome "flag" (advisory, never a
    # qualifying positive); REVIEW queue membership -> outcome
    # "flag" with evidence {"review_queue": True} and caps the
    # decision at REVIEW via the "review" flag below.
    "conviction": "daemon/launch_scanner/conviction.py gate() + "
                  "REVIEW queue",
    # Regime stake scaling + sit-out: sit-out active ->
    # restrictions["regime_sit_out"] = True (caps at REVIEW);
    # otherwise outcome "pass" with evidence {"scale": ...}.
    "regime": "regime stake scaling + sit-out flag",
    # Deployer band (0 / 1-2 / 3+ / unknown): band 1-2 -> outcome
    # "pass"; band 0 -> outcome "flag"; band 3+ -> outcome "fail";
    # unknown -> outcome "unknown". The band is supporting evidence
    # only; it can never qualify a token on its own.
    "deployer_band": "deployer band (entry_features)",
    # Event-pipeline observations (Phase 8 LogicalEvent): built by
    # observations_from_events() in this module from verified +
    # confirmed/finalized events only. Preliminary events are
    # excluded by rule 4, never translated into a pass.
    "event_graduation": "Phase 8 LogicalEvent token_graduated",
    "event_momentum": "Phase 8 LogicalEvent token_trade (verified)",
}

DECISION_BLOCK = "BLOCK"
DECISION_SKIP = "SKIP"
DECISION_REVIEW = "REVIEW"
DECISION_WATCH = "WATCH"
DECISION_QUALIFIED = "QUALIFIED"
DECISIONS = (DECISION_BLOCK, DECISION_SKIP, DECISION_REVIEW,
             DECISION_WATCH, DECISION_QUALIFIED)

# Minimum independent verified positive signals for QUALIFIED.
MIN_INDEPENDENT_POSITIVES = 2
# Minimum weighted score (sum of weight * reliability over verified
# positive signals, minus verified negatives) for QUALIFIED.
QUALIFY_SCORE_MIN = 2.0
# An observation older than this is stale: recorded under
# stale_excluded, never verified evidence.
MAX_FRESHNESS_S = 300.0
# Runner-only bar (standing rule): liquidity >= $25k, mcap >= $50k.
LIQ_FLOOR_USD = 25000.0
MCAP_FLOOR_USD = 50000.0

# Verified lifecycle stages (mirrors Phase 8 CONFIRMED_LIFECYCLES).
CONFIRMED_STAGES = frozenset(["confirmed", "finalized"])
# Stages that are preliminary by definition.
PRELIMINARY_STAGES = frozenset(["processed", "preliminary",
                                "unconfirmed"])

DEFAULT_WEIGHTS = {
    "veto": 0.0,              # hard gate, never a score contributor
    "instant_safety": 0.0,    # hard gate, never a score contributor
    "grad_select_filters": 2.0,
    "conviction": 1.5,
    "regime": 0.5,
    "deployer_band": 1.0,
    "event_graduation": 1.5,
    "event_momentum": 1.0,
}

# Decision precedence, strongest first. A cap is applied by taking
# the weaker (later in this order) of the current decision and the
# cap: BLOCK > SKIP > REVIEW > WATCH > QUALIFIED is NOT the order
# for capping; capping means "no stronger than X", so the order
# from strongest to weakest is BLOCK, SKIP, REVIEW, WATCH,
# QUALIFIED -- wait, WATCH is weaker than REVIEW. Strength order:
_STRENGTH = {DECISION_BLOCK: 4, DECISION_SKIP: 3,
             DECISION_REVIEW: 2, DECISION_WATCH: 1,
             DECISION_QUALIFIED: 0}


def _cap(decision, cap):
    """Return the weaker of ``decision`` and ``cap``.

    QUALIFIED (strength 0) is the strongest positive outcome, so
    capping QUALIFIED at REVIEW yields REVIEW; capping BLOCK at
    REVIEW leaves BLOCK (a block is never softened by a cap).
    """
    if _STRENGTH[decision] >= _STRENGTH[cap]:
        return decision
    return cap


def _clamp_reliability(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return max(0.0, min(1.0, value))


def _is_verified_evidence(obs, now=None):
    """True only for verified + confirmed/finalized + fresh evidence.

    Returns (is_verified, exclusion_reason_or_None).
    """
    now = now if now is not None else time.time()
    stage = (obs.get("stage") or "").lower() \
        if isinstance(obs.get("stage"), str) else None
    verification = (obs.get("verification") or "").lower() \
        if isinstance(obs.get("verification"), str) else None
    if verification == "conflicting":
        return False, "conflicting_verification"
    if verification == "unverified":
        return False, "unverified"
    if stage in PRELIMINARY_STAGES:
        return False, "preliminary_stage"
    if stage is not None and stage not in CONFIRMED_STAGES:
        return False, "unconfirmed_stage"
    if verification is not None and verification != "verified":
        return False, "unverified"
    if stage is None and verification is None:
        # No provenance at all: not verified evidence. A caller
        # that cannot state stage/verification must not be able to
        # qualify a token. (Existing in-process signals that carry
        # no chain stage pass verification="verified" explicitly.)
        return False, "no_verification_provenance"
    freshness = obs.get("freshness_s")
    if freshness is not None:
        try:
            if float(freshness) > MAX_FRESHNESS_S:
                return False, "stale"
        except (TypeError, ValueError):
            return False, "unverified"
    return True, None


def observations_from_events(events):
    """Translate Phase 8 LogicalEvents into fusion observations.

    Only verified + confirmed/finalized events become positive
    observations. Preliminary / unverified / conflicting events
    become ``preliminary_excluded`` entries via the returned
    ``excluded`` list -- they are NEVER translated into a pass.
    Event disagreements become conflict entries.

    ``events`` is a list of LogicalEvent objects or their
    ``to_dict()`` dicts. Returns (observations, excluded, conflicts).
    """
    observations = []
    excluded = []
    conflicts = []
    for event in events:
        data = event.to_dict() if hasattr(event, "to_dict") \
            else dict(event)
        event_type = data.get("event_type")
        if event_type == "token_graduated":
            name = "event_graduation"
        elif event_type == "token_trade":
            name = "event_momentum"
        elif event_type in ("token_created", "token_migrated",
                            "pool_created"):
            name = "event_%s" % event_type
        else:
            continue
        for disagreement in data.get("disagreements") or []:
            conflicts.append({
                "field": disagreement.get("field"),
                "values_by_provider": dict(
                    disagreement.get("values") or {}),
                "source": "event_pipeline",
                "event_id": data.get("event_id"),
            })
        verified, reason = _is_verified_evidence({
            "stage": data.get("lifecycle_status"),
            "verification": data.get("verification_status"),
            "freshness_s": None,
        })
        obs = {
            "name": name,
            "outcome": "pass" if verified else "unknown",
            "hard_veto": False,
            "security_hard_fail": False,
            "contradictory": False,
            "weight": DEFAULT_WEIGHTS.get(name, 1.0),
            "reliability": 1.0,
            "freshness_s": None,
            "provider": ",".join(data.get("providers") or [])
                       or None,
            "stage": data.get("lifecycle_status"),
            "verification": data.get("verification_status"),
            "evidence": {
                "event_id": data.get("event_id"),
                "event_type": event_type,
                "signature": data.get("signature"),
            },
            "detail": "phase-8 event %s" % event_type,
        }
        if verified:
            observations.append(obs)
        else:
            excluded.append({
                "name": name,
                "reason": reason or "not_verified",
                "event_id": data.get("event_id"),
                "stage": data.get("lifecycle_status"),
                "verification": data.get("verification_status"),
            })
    return observations, excluded, conflicts


def decision_id_for(mint, lane, input_snapshot):
    """Deterministic decision id: sha256 of mint + lane + canonical
    input snapshot. Replaying the same input reproduces the id."""
    canonical = json.dumps({"mint": mint, "lane": lane,
                            "input": input_snapshot},
                           sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FusionEngine(object):
    """Pure signal-fusion engine. No I/O, no trading calls.

    ``fuse()`` takes one candidate's fused inputs and returns a
    complete decision record (dict). ``mode`` is always "shadow".
    """

    def __init__(self, min_independent_positives=None,
                 qualify_score_min=None,
                 max_freshness_s=None,
                 liq_floor_usd=None, mcap_floor_usd=None):
        self.min_independent_positives = (
            MIN_INDEPENDENT_POSITIVES
            if min_independent_positives is None
            else min_independent_positives)
        self.qualify_score_min = (
            QUALIFY_SCORE_MIN if qualify_score_min is None
            else qualify_score_min)
        self.max_freshness_s = (
            MAX_FRESHNESS_S if max_freshness_s is None
            else max_freshness_s)
        self.liq_floor_usd = (
            LIQ_FLOOR_USD if liq_floor_usd is None else liq_floor_usd)
        self.mcap_floor_usd = (
            MCAP_FLOOR_USD if mcap_floor_usd is None
            else mcap_floor_usd)

    # -- public ------------------------------------------------------
    def fuse(self, mint, lane="unknown", signals=None, events=None,
             conflicts=None, restrictions=None, cost_check=None):
        """Fuse one candidate's inputs into a decision record.

        ``signals``: list of observation dicts (see module docstring).
        ``events``: Phase 8 LogicalEvents (or dicts); translated via
        observations_from_events(). ``conflicts``: explicit
        cross-provider conflict entries. ``restrictions``: dict
        (liquidity_usd, mcap_usd, execution_cost_usd,
        max_execution_cost_usd, regime_sit_out). ``cost_check``:
        dict {allowed, reason, account, priority} recorded verbatim.
        """
        signals = list(signals or [])
        caller_signals = list(signals)  # snapshot BEFORE the
        # event-derived observations are merged in: replay()
        # re-derives them from the stored events, so storing the
        # merged list would double-count them on replay.
        restrictions = dict(restrictions or {})
        conflicts = list(conflicts or [])
        caller_conflicts = list(conflicts)  # same reason as above
        now = time.time()

        # Event pipeline observations (verified only; preliminary
        # events land in preliminary_excluded).
        event_obs, event_excluded, event_conflicts = \
            observations_from_events(events or [])
        conflicts = conflicts + event_conflicts
        signals = signals + event_obs

        per_signal = []
        preliminary_excluded = list(event_excluded)
        stale_excluded = []
        verified_positives = []   # (name, weighted contribution)
        verified_negatives = []
        flags = []
        hard_block_reasons = []
        review_reasons = []

        for obs in signals:
            name = obs.get("name") or "unknown"
            outcome = obs.get("outcome") or "unknown"
            weight = obs.get("weight")
            if weight is None:
                weight = DEFAULT_WEIGHTS.get(name, 1.0)
            try:
                weight = float(weight)
            except (TypeError, ValueError):
                weight = 1.0
            reliability = _clamp_reliability(
                obs.get("reliability", 1.0))
            verified, exclusion = _is_verified_evidence(obs, now=now)
            entry = {
                "name": name,
                "outcome": outcome,
                "verified_evidence": bool(verified),
                "exclusion_reason": exclusion,
                "weight": weight,
                "reliability": reliability,
                "provider": obs.get("provider"),
                "stage": obs.get("stage"),
                "verification": obs.get("verification"),
                "freshness_s": obs.get("freshness_s"),
                "evidence": dict(obs.get("evidence") or {}),
                "detail": obs.get("detail") or "",
            }
            per_signal.append(entry)

            # Hard gates fire on the observation itself, whether or
            # not it counts as verified evidence: a veto is a veto.
            if obs.get("hard_veto"):
                hard_block_reasons.append(
                    "hard_veto:%s" % name)
            if obs.get("security_hard_fail"):
                hard_block_reasons.append(
                    "security_hard_fail:%s" % name)
            if obs.get("contradictory") and verified:
                hard_block_reasons.append(
                    "verified_contradiction:%s" % name)

            if outcome == "flag":
                flags.append(name)
                if (obs.get("evidence") or {}).get("review_queue"):
                    review_reasons.append("review_queue:%s" % name)
                continue
            if not verified:
                if exclusion == "stale":
                    stale_excluded.append({
                        "name": name, "reason": "stale",
                        "freshness_s": obs.get("freshness_s")})
                elif exclusion in ("preliminary_stage",
                                   "unconfirmed_stage",
                                   "unverified",
                                   "no_verification_provenance"):
                    preliminary_excluded.append({
                        "name": name, "reason": exclusion,
                        "stage": obs.get("stage"),
                        "verification": obs.get("verification")})
                # Unverified observations contribute nothing.
                continue
            contribution = weight * reliability
            if outcome == "pass":
                verified_positives.append((name, contribution))
            elif outcome == "fail":
                verified_negatives.append((name, contribution))

        score = (sum(c for _, c in verified_positives)
                 - sum(c for _, c in verified_negatives))
        independent_positives = sorted(
            {name for name, _ in verified_positives})

        # -- decision ------------------------------------------------
        reasons = []
        decision = DECISION_QUALIFIED

        if hard_block_reasons:
            decision = DECISION_BLOCK
            reasons.extend(hard_block_reasons)
        else:
            # Conflicts: flagged, never resolved. A conflict on a
            # hard/security field blocks; any other conflict caps
            # at REVIEW.
            hard_conflict = [c for c in conflicts
                             if str(c.get("field") or "").lower()
                             in ("security", "safety", "veto",
                                 "concentration", "mint_authority",
                                 "freeze_authority")]
            if hard_conflict:
                decision = DECISION_BLOCK
                reasons.append("conflicting_evidence:hard_field")
            elif conflicts:
                decision = _cap(decision, DECISION_REVIEW)
                reasons.append("conflicting_evidence:flagged")
                review_reasons.append(
                    "conflict_on_%s"
                    % ",".join(sorted({str(c.get("field"))
                                       for c in conflicts})))

            # Restrictions.
            liq = restrictions.get("liquidity_usd")
            if liq is not None:
                try:
                    if float(liq) < self.liq_floor_usd:
                        decision = _cap(decision, DECISION_SKIP)
                        reasons.append("liquidity_below_floor")
                except (TypeError, ValueError):
                    pass
            mcap = restrictions.get("mcap_usd")
            if mcap is not None:
                try:
                    if float(mcap) < self.mcap_floor_usd:
                        decision = _cap(decision, DECISION_SKIP)
                        reasons.append("mcap_below_floor")
                except (TypeError, ValueError):
                    pass
            if restrictions.get("regime_sit_out"):
                decision = _cap(decision, DECISION_REVIEW)
                reasons.append("regime_sit_out")
            exec_cost = restrictions.get("execution_cost_usd")
            exec_cap = restrictions.get("max_execution_cost_usd")
            if exec_cost is not None and exec_cap is not None:
                try:
                    if float(exec_cost) > float(exec_cap):
                        decision = _cap(decision, DECISION_REVIEW)
                        reasons.append("execution_cost_exceeds_cap")
                except (TypeError, ValueError):
                    pass

            # Cost gate: a refused background refresh means the
            # evidence on hand may be stale -- it can never support
            # a fresh QUALIFIED. (An allowed check, or an essential
            # priority that is never suppressed, leaves the
            # decision untouched.)
            cost_check = cost_check or {}
            if cost_check.get("allowed") is False:
                decision = _cap(decision, DECISION_REVIEW)
                reasons.append("cost_check_refused")

            # Evidence strength.
            if decision != DECISION_BLOCK:
                if verified_negatives:
                    decision = _cap(decision, DECISION_SKIP)
                    reasons.append("verified_negative:%s" % ",".join(
                        sorted({n for n, _ in verified_negatives})))
                if len(independent_positives) >= \
                        self.min_independent_positives \
                        and score >= self.qualify_score_min:
                    # QUALIFIED only if nothing above capped it.
                    if decision == DECISION_QUALIFIED:
                        reasons.append(
                            "qualified:%d_independent_positives"
                            % len(independent_positives))
                elif len(independent_positives) == 1:
                    decision = _cap(decision, DECISION_REVIEW)
                    reasons.append("single_positive_signal")
                    review_reasons.append("single_positive_signal")
                elif len(independent_positives) >= \
                        self.min_independent_positives:
                    # Enough independent positives, but the
                    # weighted score is below the threshold: never
                    # QUALIFIED on headcount alone.
                    decision = _cap(decision, DECISION_REVIEW)
                    reasons.append("score_below_threshold")
                elif not verified_positives and not verified_negatives:
                    decision = _cap(decision, DECISION_WATCH)
                    reasons.append("no_verified_evidence")
                elif not verified_positives:
                    decision = _cap(decision, DECISION_SKIP)
                    reasons.append("no_verified_positive")

            if review_reasons and decision == DECISION_QUALIFIED:
                decision = DECISION_REVIEW
                reasons.extend(review_reasons)

        if not reasons:
            reasons.append("no_decision_reason_recorded")

        input_snapshot = {
            "signals": caller_signals,
            "events": [e.to_dict() if hasattr(e, "to_dict")
                       else dict(e) for e in (events or [])],
            "conflicts": caller_conflicts,
            "restrictions": restrictions,
            "cost_check": cost_check or {},
        }
        record = {
            "decision_id": decision_id_for(mint, lane,
                                           input_snapshot),
            "mint": mint,
            "lane": lane,
            "decision": decision,
            "reason": "; ".join(reasons),
            "reasons": list(reasons),
            "score": round(score, 4),
            "qualified_signals": list(independent_positives),
            "positive_signals": [
                {"name": n, "contribution": round(c, 4)}
                for n, c in verified_positives],
            "negative_signals": [
                {"name": n, "contribution": round(c, 4)}
                for n, c in verified_negatives],
            "per_signal": per_signal,
            "conflicts": [dict(c) for c in conflicts],
            "conflicts_flagged": bool(conflicts),
            "preliminary_excluded": preliminary_excluded,
            "stale_excluded": stale_excluded,
            "flags": flags,
            "restrictions": restrictions,
            "cost_check": cost_check or {},
            "mode": "shadow",
            "engine": {
                "min_independent_positives":
                    self.min_independent_positives,
                "qualify_score_min": self.qualify_score_min,
                "max_freshness_s": self.max_freshness_s,
                "liq_floor_usd": self.liq_floor_usd,
                "mcap_floor_usd": self.mcap_floor_usd,
            },
            "input": input_snapshot,
            "created_at": now,
        }
        return record


class ShadowFusionRunner(object):
    """Shadow-mode wiring: event pipeline + signal outputs -> journal.

    Reads verified events from a Phase 8 EventStore (or an
    in-memory list), asks an injected ``signal_provider`` callable
    for the existing signals' verdicts in the fusion input schema,
    consults CostManager.pre_call_check before any provider-backed
    read, fuses, and writes the decision record to a
    DecisionJournal. It NEVER writes to a live ledger, NEVER
    changes a lane setting, stake, or killswitch state, and NEVER
    restarts a daemon. If a live signal output cannot be read
    without touching a live file, the caller passes a signal
    provider that reads it read-only (or returns []); the proper
    live wiring is recorded as a follow-up, not improvised here.
    """

    def __init__(self, engine=None, event_store=None,
                 journal=None, cost_manager=None):
        self.engine = engine or FusionEngine()
        self.event_store = event_store
        self.journal = journal
        self.cost_manager = cost_manager

    def run(self, mint, lane="unknown", signal_provider=None,
            events=None, restrictions=None, cost_account=None,
            cost_priority="background", cost_workload="fusion_shadow"):
        """Produce + journal one decision record for ``mint``.

        ``signal_provider``: callable(mint, lane) -> list of signal
        observation dicts (read-only shadow reader). It is called
        ONLY when the pre-call check allows the read (or when no
        cost manager is configured). ``events`` overrides the
        event-store read (tests / fixtures). Returns the record.
        """
        cost_check = {"allowed": True, "reason": None,
                      "account": cost_account,
                      "priority": cost_priority,
                      "checked": False}
        provider_signals = []
        if self.cost_manager is not None and cost_account is not None:
            allowed, reason = self.cost_manager.pre_call_check(
                cost_account, cost_priority, workload=cost_workload)
            cost_check = {"allowed": bool(allowed),
                          "reason": reason,
                          "account": cost_account,
                          "priority": cost_priority,
                          "checked": True}
        if cost_check["allowed"] and signal_provider is not None:
            try:
                provider_signals = list(
                    signal_provider(mint, lane) or [])
            except Exception as exc:  # never raise in shadow mode
                provider_signals = []
                cost_check["provider_error"] = repr(exc)
        if events is None and self.event_store is not None:
            try:
                stored = self.event_store.list_events(limit=200)
                events = [e for e in stored
                          if (e.normalized_accounts or {}).get("mint")
                          == mint
                          or any((m or {}).get("mint") == mint
                                 for m in (e.token_movement or []))]
            except Exception:
                events = []
        record = self.engine.fuse(
            mint, lane=lane, signals=provider_signals,
            events=events or [], restrictions=restrictions,
            cost_check=cost_check)
        if self.journal is not None:
            self.journal.record(record)
        return record
