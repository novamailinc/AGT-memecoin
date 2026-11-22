#!/usr/bin/env python3
"""Tests for the Phase 11 signal-driven fusion engine (2026-11-22).

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_fusion.py

Covers:
- single positive signal can NEVER produce QUALIFIED (caps REVIEW);
- a hard veto, a security hard-fail, or a verified contradictory
  condition forces BLOCK regardless of other signals;
- conflicting evidence is flagged, never silently resolved (caps
  REVIEW, or BLOCK on a hard/security field);
- preliminary (processed-stage / unverified) observations are NEVER
  verified evidence (listed under preliminary_excluded, score 0);
- stale observations are excluded from qualification;
- CostManager.pre_call_check refusals are recorded on the decision
  record and cap it at REVIEW; essential (critical/card) reads are
  never suppressed;
- decision records are complete and replayable from the journal
  (deterministic ids, full input snapshot, replay() agreement);
- the shadow runner wires the Phase 8 event pipeline in read-only
  and never calls a signal provider whose read was refused;
- restrictions (liquidity / mcap floors, execution-cost cap,
  regime sit-out) are enforced.

Fixtures are local only: no live provider calls, no live lane
files touched, no daemon restarted.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.acquisition.cost_management import (
    CostManager,
)
from daemon.intelligence.acquisition.request_scheduler import (
    PRIORITY_BACKGROUND,
    PRIORITY_CARD,
    PRIORITY_CRITICAL,
)
from daemon.intelligence.events.event_models import LogicalEvent
from daemon.intelligence.signals import (
    DecisionJournal,
    FusionEngine,
    ShadowFusionRunner,
)
from daemon.intelligence.signals.fusion import (
    DECISION_BLOCK,
    DECISION_QUALIFIED,
    DECISION_REVIEW,
    DECISION_SKIP,
    DECISION_WATCH,
    observations_from_events,
)

FAILS = []
PASSES = [0]


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


def total():
    return PASSES[0] + len(FAILS)


MINT = "TestMint1111111111111111111111111111111111111"


def sig(name, outcome="pass", weight=None, reliability=1.0,
        provider="test", stage=None, verification="verified",
        freshness_s=10.0, **extra):
    """One fusion input observation. Verified by default: callers
    that cannot state stage/verification must pass verification
    explicitly (in-process signals carry verification='verified')."""
    obs = {"name": name, "outcome": outcome,
           "hard_veto": False, "security_hard_fail": False,
           "contradictory": False, "weight": weight,
           "reliability": reliability, "freshness_s": freshness_s,
           "provider": provider, "stage": stage,
           "verification": verification, "evidence": {},
           "detail": "fixture %s" % name}
    obs.update(extra)
    return obs


def two_positives():
    return [sig("grad_select_filters", weight=2.0),
            sig("deployer_band", weight=1.0)]


ENGINE = FusionEngine()

# ---------------- 1. single positive signal can never QUALIFY -------
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=2.0)],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("single positive signal is NOT QUALIFIED",
      rec["decision"] != DECISION_QUALIFIED)
check("single positive signal caps at REVIEW",
      rec["decision"] == DECISION_REVIEW)
check("single positive reason names the rule",
      "single_positive_signal" in rec["reason"])

# A single STRONG positive (huge weight) still cannot qualify.
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=99.0)],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("single signal cannot qualify even with huge weight",
      rec["decision"] != DECISION_QUALIFIED)

# Two independent verified positives DO qualify.
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives(),
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("two independent verified positives QUALIFY",
      rec["decision"] == DECISION_QUALIFIED)
check("QUALIFIED record names both qualifying signals",
      rec["qualified_signals"] == ["deployer_band",
                                   "grad_select_filters"])
check("QUALIFIED score meets the threshold",
      rec["score"] >= 2.0)

# Two positives from the SAME signal name are one independent signal.
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=2.0),
                           sig("grad_select_filters", weight=2.0,
                               provider="other")],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("same signal twice is still one independent positive",
      rec["decision"] != DECISION_QUALIFIED)

# ---------------- 2. hard gates force BLOCK -------------------------
veto = sig("veto", outcome="fail", hard_veto=True,
           verification="verified")
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives() + [veto],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("hard veto forces BLOCK despite two positives",
      rec["decision"] == DECISION_BLOCK)
check("BLOCK reason names the veto",
      "hard_veto:veto" in rec["reason"])

safety = sig("instant_safety", outcome="fail",
             security_hard_fail=True, verification="verified")
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives() + [safety],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("security hard-fail forces BLOCK despite two positives",
      rec["decision"] == DECISION_BLOCK)
check("BLOCK reason names the security fail",
      "security_hard_fail:instant_safety" in rec["reason"])

contra = sig("grad_select_filters", outcome="pass", weight=2.0,
             contradictory=True, verification="verified")
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[contra, sig("deployer_band", weight=1.0)],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("verified contradictory condition forces BLOCK",
      rec["decision"] == DECISION_BLOCK)

# A veto alone (no positives at all) is still BLOCK, not SKIP.
rec = ENGINE.fuse(MINT, lane="grad_select", signals=[veto])
check("veto alone is BLOCK", rec["decision"] == DECISION_BLOCK)

# ---------------- 3. conflicts flagged, never resolved --------------
rec = ENGINE.fuse(
    MINT, lane="grad_select", signals=two_positives(),
    conflicts=[{"field": "liquidity_usd",
                "values_by_provider": {"helius": 40000,
                                       "alchemy": 12000},
                "source": "test"}],
    restrictions={"liquidity_usd": 50000.0, "mcap_usd": 100000.0})
check("conflicting evidence is flagged, not resolved",
      rec["conflicts_flagged"] is True
      and len(rec["conflicts"]) == 1
      and rec["conflicts"][0]["values_by_provider"]["helius"] == 40000
      and rec["conflicts"][0]["values_by_provider"]["alchemy"] == 12000)
check("conflict caps decision at REVIEW (never QUALIFIED)",
      rec["decision"] == DECISION_REVIEW)
check("conflict reason is recorded",
      "conflicting_evidence" in rec["reason"])

rec = ENGINE.fuse(
    MINT, lane="grad_select", signals=two_positives(),
    conflicts=[{"field": "security",
                "values_by_provider": {"helius": "fail",
                                       "alchemy": "pass"},
                "source": "test"}],
    restrictions={"liquidity_usd": 50000.0, "mcap_usd": 100000.0})
check("conflict on a hard/security field forces BLOCK",
      rec["decision"] == DECISION_BLOCK)

# ---------------- 4. preliminary is never verified evidence ---------
prelim = sig("event_momentum", outcome="pass", weight=1.0,
             stage="processed", verification="unverified",
             freshness_s=5.0)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=2.0),
                           prelim],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("preliminary observation is NOT verified evidence",
      rec["per_signal"][1]["verified_evidence"] is False)
check("preliminary observation is listed as excluded",
      any(e["name"] == "event_momentum"
          for e in rec["preliminary_excluded"]))
check("preliminary + one verified positive cannot QUALIFY",
      rec["decision"] != DECISION_QUALIFIED)
check("preliminary contributes zero to the score",
      rec["score"] == 2.0)

# No provenance at all (no stage, no verification) is not evidence.
noprov = sig("event_momentum", outcome="pass", weight=1.0,
             stage=None, verification=None)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=2.0),
                           noprov],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("observation with no verification provenance is excluded",
      rec["decision"] != DECISION_QUALIFIED
      and any(e["reason"] == "no_verification_provenance"
              for e in rec["preliminary_excluded"]))

# Phase 8 event translation: processed event excluded, confirmed in.
ev_processed = LogicalEvent(
    signature="sig_processed_1", slot=100, event_type="token_trade",
    commitment="processed", lifecycle_status="processed",
    verification_status="unverified",
    normalized_accounts={"mint": MINT})
ev_confirmed = LogicalEvent(
    signature="sig_confirmed_1", slot=101, event_type="token_trade",
    commitment="confirmed", lifecycle_status="confirmed",
    verification_status="verified",
    normalized_accounts={"mint": MINT})
obs, excluded, conflicts = observations_from_events(
    [ev_processed, ev_confirmed])
check("processed event translates to zero observations",
      len(obs) == 1 and obs[0]["name"] == "event_momentum")
check("processed event is listed excluded",
      len(excluded) == 1
      and excluded[0]["stage"] == "processed")
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=2.0)],
                  events=[ev_processed],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("processed event alone cannot supply the 2nd positive",
      rec["decision"] != DECISION_QUALIFIED
      and len(rec["preliminary_excluded"]) == 1)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=2.0)],
                  events=[ev_confirmed],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("verified confirmed event supplies the 2nd positive",
      rec["decision"] == DECISION_QUALIFIED)

# Stale observations are excluded from qualification.
stale = sig("deployer_band", outcome="pass", weight=1.0,
            freshness_s=3600.0)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("grad_select_filters", weight=2.0),
                           stale],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("stale observation is listed stale_excluded",
      len(rec["stale_excluded"]) == 1)
check("stale observation cannot supply the 2nd positive",
      rec["decision"] != DECISION_QUALIFIED)

# ---------------- 5. restrictions enforced --------------------------
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives(),
                  restrictions={"liquidity_usd": 1000.0,
                                "mcap_usd": 100000.0})
check("liquidity below $25k floor caps at SKIP",
      rec["decision"] == DECISION_SKIP)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives(),
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 1000.0})
check("mcap below $50k floor caps at SKIP",
      rec["decision"] == DECISION_SKIP)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives(),
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0,
                                "execution_cost_usd": 5.0,
                                "max_execution_cost_usd": 1.0})
check("execution cost above cap caps at REVIEW",
      rec["decision"] == DECISION_REVIEW)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives(),
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0,
                                "regime_sit_out": True})
check("regime sit-out caps at REVIEW",
      rec["decision"] == DECISION_REVIEW)

# Verified negative evidence (filter fail) -> SKIP, not QUALIFIED.
neg = sig("grad_select_filters", outcome="fail", weight=2.0)
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[neg, sig("deployer_band", weight=1.0)],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("verified filter fail yields SKIP",
      rec["decision"] == DECISION_SKIP)
# No verified evidence at all -> WATCH.
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=[sig("deployer_band", outcome="unknown")],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("no verified evidence yields WATCH",
      rec["decision"] == DECISION_WATCH)

# ---------------- 6. cost gate (real CostManager, temp home) --------
home_w = tempfile.mkdtemp(prefix="fusion_cost_warn_")
cm_w = CostManager(home=home_w)
cm_w.tracker.record("alchemy", "alchemy", "getTransaction",
                    "routine_tx", PRIORITY_CARD, cost=24_000_000,
                    kind="ok")
allowed, reason = cm_w.pre_call_check("alchemy", PRIORITY_BACKGROUND)
check("fixture: background refused at 80% budget",
      allowed is False)
runner = ShadowFusionRunner(engine=ENGINE, cost_manager=cm_w)
called = []


def _provider(mint, lane):
    called.append((mint, lane))
    return two_positives()


rec = runner.run(MINT, lane="grad_select",
                 signal_provider=_provider,
                 restrictions={"liquidity_usd": 50000.0,
                               "mcap_usd": 100000.0},
                 cost_account="alchemy",
                 cost_priority=PRIORITY_BACKGROUND)
check("cost refusal is recorded on the decision record",
      rec["cost_check"]["allowed"] is False
      and rec["cost_check"]["reason"] == "budget_warn_suppress_background"
      and rec["cost_check"]["checked"] is True)
check("refused read never calls the signal provider",
      called == [])
check("cost refusal caps the decision at REVIEW",
      rec["decision"] != DECISION_QUALIFIED)
check("cost refusal reason is recorded",
      "cost_check_refused" in rec["reason"])

# Essential (critical) reads are never suppressed at the same level.
called.clear()
rec = runner.run(MINT, lane="grad_select",
                 signal_provider=_provider,
                 restrictions={"liquidity_usd": 50000.0,
                               "mcap_usd": 100000.0},
                 cost_account="alchemy",
                 cost_priority=PRIORITY_CRITICAL)
check("critical priority is allowed at 80% budget",
      rec["cost_check"]["allowed"] is True)
check("allowed essential read calls the signal provider",
      len(called) == 1)
check("essential read can QUALIFY on two positives",
      rec["decision"] == DECISION_QUALIFIED)
called.clear()
rec = runner.run(MINT, lane="grad_select",
                 signal_provider=_provider,
                 restrictions={"liquidity_usd": 50000.0,
                               "mcap_usd": 100000.0},
                 cost_account="alchemy",
                 cost_priority=PRIORITY_CARD)
check("card priority is allowed at 80% budget",
      rec["cost_check"]["allowed"] is True and len(called) == 1)

# ---------------- 7. journal: complete + replayable -----------------
home_j = tempfile.mkdtemp(prefix="fusion_journal_")
journal = DecisionJournal(
    path=os.path.join(home_j, "fusion_decisions.db")).open()
rec = ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives(),
                  conflicts=[{"field": "liquidity_usd",
                              "values_by_provider": {"helius": 1,
                                                     "alchemy": 2},
                              "source": "test"}],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0},
                  cost_check={"allowed": True, "reason": None,
                              "account": "alchemy",
                              "priority": "background",
                              "checked": True})
did = journal.record(rec)
check("decision record is complete (all required fields)",
      all(k in rec for k in (
          "decision_id", "mint", "lane", "decision", "reason",
          "score", "qualified_signals", "positive_signals",
          "negative_signals", "per_signal", "conflicts",
          "conflicts_flagged", "preliminary_excluded",
          "stale_excluded", "flags", "restrictions", "cost_check",
          "mode", "engine", "input", "created_at"))
      and rec["mode"] == "shadow")
check("journal round-trips the record",
      journal.get(did) is not None
      and journal.get(did)["decision_id"] == did
      and journal.get(did)["decision"] == rec["decision"])
check("journal lists by mint",
      len(journal.list_by_mint(MINT)) == 1)
check("journal count agrees",
      journal.count() == 1
      and journal.count(DECISION_REVIEW) == 1)
replay = journal.replay(did, ENGINE)
check("replay from the journal matches the stored decision",
      replay["matches"] is True and replay["mismatches"] == [])
check("replay reports stored vs recomputed",
      replay["stored"]["decision"] == rec["decision"]
      and replay["recomputed"]["decision"] == rec["decision"])
check("deterministic decision id: same input, same id",
      ENGINE.fuse(MINT, lane="grad_select",
                  signals=two_positives(),
                  conflicts=[{"field": "liquidity_usd",
                              "values_by_provider": {"helius": 1,
                                                     "alchemy": 2},
                              "source": "test"}],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0},
                  cost_check={"allowed": True, "reason": None,
                              "account": "alchemy",
                              "priority": "background",
                              "checked": True})["decision_id"] == did)
# Re-recording the same input is idempotent (same id, one row).
journal.record(rec)
check("re-recording the same decision is idempotent",
      journal.count() == 1)
# Replay detects an engine change (stricter threshold) -- needs a
# QUALIFIED record, since a conflict-capped REVIEW cannot change.
rec_q = ENGINE.fuse(MINT + "Q", lane="grad_select",
                    signals=two_positives(),
                    restrictions={"liquidity_usd": 50000.0,
                                  "mcap_usd": 100000.0})
check("fixture: qualified record for the replay-change check",
      rec_q["decision"] == DECISION_QUALIFIED)
did_q = journal.record(rec_q)
strict = FusionEngine(qualify_score_min=99.0)
replay2 = journal.replay(did_q, strict)
check("replay detects a changed engine (mismatch reported)",
      replay2["matches"] is False
      and "decision" in replay2["mismatches"])
journal.close()

# ---------------- 8. shadow runner + event store (read-only) --------
from daemon.intelligence.events import EventStore

home_e = tempfile.mkdtemp(prefix="fusion_events_")
store = EventStore(
    path=os.path.join(home_e, "events.db")).open()
store.upsert_event(ev_confirmed)
store.upsert_event(ev_processed)
home_j2 = tempfile.mkdtemp(prefix="fusion_journal2_")
journal2 = DecisionJournal(
    path=os.path.join(home_j2, "fusion_decisions.db")).open()
runner2 = ShadowFusionRunner(engine=ENGINE, event_store=store,
                             journal=journal2)
rec = runner2.run(MINT, lane="grad_select",
                  signal_provider=lambda m, l: [
                      sig("grad_select_filters", weight=2.0)],
                  restrictions={"liquidity_usd": 50000.0,
                                "mcap_usd": 100000.0})
check("shadow runner fuses store events + provider signals",
      rec["decision"] == DECISION_QUALIFIED)
check("shadow runner journals the decision (own DB, not a ledger)",
      journal2.count() == 1
      and journal2.get(rec["decision_id"]) is not None)
check("shadow runner records the processed event as excluded",
      len(rec["preliminary_excluded"]) == 1)
store.close()
journal2.close()

print("\n%d checks, %d failed" % (total(), len(FAILS)))
if FAILS:
    print("FAILED: %s" % FAILS)
    sys.exit(1)
print("ALL PASS")
