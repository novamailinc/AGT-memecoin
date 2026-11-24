"""Chainstack historical + recovery workload (Phase 6, spec §5).

Chainstack's PERMANENT workload in the three-provider program — it
does not sit idle as an emergency backup:

- Historical retrieval WITHIN the retention window: transaction
  history (``getTransaction``), historical blocks (``getBlock``),
  wallet/token activity (signature rows + transactions),
  market reconstruction (signatures -> transactions -> normalized
  event rows), replay datasets, and backfilling missed events (via
  the ``event_backfill`` driver — the router carries the workload
  as missed-event recovery).
- Risk-based INDEPENDENT VERIFICATION: selected transactions and
  account states are re-read via Chainstack and compared field by
  field against the primary provider's answer. Only a SAMPLE is
  verified — prioritised by risk (new token creation, important
  buys/sells, liquidity changes, pool creation, bonding-curve
  graduation, token migration, high-priority trading decisions,
  conflicting decoder results). Routine traffic is NEVER verified:
  verifying every transaction from every provider would burn the
  3M RU/month Developer allowance for no signal.
- Yellowstone gRPC: INVESTIGATED ONLY — see
  ``yellowstone_grpc_status()``. It is PAID on all three providers
  and this plan does not support it. No gRPC client is built, no
  dependency is added, nothing is enabled.

EVIDENCE-BASED CORRECTION (capability audit 2026-11-22, honoured
here): Chainstack Developer Free has NO archive data, so
DEEP-HISTORY retrieval goes to Alchemy (full archive back to
genesis), NOT Chainstack. Chainstack's free role is recent-read
verification + event recovery within its retention window +
independent verification. Two methods Chainstack can never serve
on Developer Free — ``getSignaturesForAddress`` (always
archive-billed, 2 RU, returns -32002) and ``getProgramAccounts``
(paid-only) — are never routed to it: signature history is
Alchemy's leg (see ``fetch_signatures``), enforced by the
capability config in ``provider_capabilities.py``, not by
assumption here.

RETENTION WINDOW: Chainstack's published retention (400M-shred
ledger) has NO published slot/time depth, so coverage is NEVER
assumed: a call is sent to Chainstack as "recent" only when the
caller asserts ``recent=True`` or supplies a measured window
(``window_slots``) plus slot hints the router can check. Without
either, the router defaults to Alchemy (archive). The window is
UNMEASURED until a live probe on the AGT target measures it.

ROUTING: this module never picks a provider. Every call goes
through the central router (``provider_router``) with a workload
declaration — ``historical_range`` for history reads,
``missed_event_recovery`` for backfill, ``independent_verification``
for verification. The router is the ONLY place routing decisions
live.

ROUTER BUG — FIXED 2026-11-22 by the coordinator (snapshot
20261122T102120Z-20261122T102700Z-router-tx-verification-fix):
``classify_workload`` used to REFUSE getTransaction + declared
``independent_verification`` ("contradicts method") because the
ROUTINE_TX override tuple omitted INDEPENDENT_VERIFICATION.
The tuple now includes it, so transaction verification declares
``independent_verification`` and routes Chainstack-first as
verifier (Helius excluded — it is the subject, not the verifier),
and the usage journal records it as independent_verification.

VERIFICATION DISCIPLINE: a comparison that cannot run (provider
error, timeout, unconfigured) is reported ``unverified`` — it is
NOT a match and NOT a conflict. A field-level difference is a
CONFLICT: it is flagged with the differing fields and never
silently resolved in either direction.

LIVE STATUS: live Alchemy/Chainstack verification from this VM is
OUTSTANDING, not failed — the url_path_segment surrogate swap is
unproven here and the keys cannot be proven from this VM (strongest
evidence 2026-11-22 ~02:11 PST: an Alchemy key copied straight
from the dashboard with Solana enabled still 401s). This module is
verified here with mocked transport + recorded fixtures only;
live verification is DEFERRED to the AGT target. Timing figures
in this module are MOCKED-TRANSPORT timings and are labelled as
such — never quote them as provider latency.

Paper-only throughout; mainnet stays disabled. No secrets in Git
or logs. Backfill is detection-only, never enters (Rule 1).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import time

from . import event_backfill as EB
from . import provider_capabilities as caps
from .provider_router import (
    HISTORICAL_RANGE,
    INDEPENDENT_VERIFICATION,
    MISSED_EVENT_RECOVERY,
)
from .request_scheduler import PRIORITY_BACKGROUND, PRIORITY_CARD

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.chainstack_workload")

# Standard read options for historical transaction retrieval.
TX_FETCH_OPTIONS = {"encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1}

# Methods Chainstack can never serve on Developer Free — derived
# from provider_capabilities.FREE_TIER_REFUSALS (the single
# source), never a second copy.
CHAINSTACK_UNSERVED_METHODS = frozenset(
    caps.FREE_TIER_REFUSALS.get("chainstack", set()))

# --- risk-based verification sampling ----------------------------------
# Priority per event kind. Only events at or above
# MIN_VERIFY_PRIORITY are eligible for independent verification;
# routine traffic (priority 10) is never verified.
VERIFY_KIND_PRIORITY = {
    "conflicting_decoder": 100,   # two decoders disagree — verify first
    "token_migration": 90,        # migration changes where the market is
    "bonding_curve_graduation": 85,
    "pool_creation": 80,
    "liquidity_change": 75,
    "token_creation": 70,         # new mint — the riskiest single event
    "important_trade": 60,        # buy/sell at/above IMPORTANT_TRADE_SOL
    "high_priority_decision": 55,  # influenced a high-priority decision
    "routine": 10,
}
MIN_VERIFY_PRIORITY = 50
DEFAULT_VERIFY_BUDGET = 10
# "Important" buy/sell threshold (SOL). A sampling heuristic for
# the verification budget — operator-tunable, not a market claim.
IMPORTANT_TRADE_SOL = 1.0

# --- Yellowstone gRPC investigation (recorded finding) -------------------
YELLOWSTONE_FINDING = (
    "Yellowstone gRPC is PAID on all three providers and this plan "
    "does not support it: Helius LaserStream gRPC mainnet is Business "
    "($499/mo) / Professional ($999/mo) — Devnet only on Developer; "
    "Alchemy Yellowstone gRPC is usage-priced ($75/TB), NOT part of "
    "the free CU allowance; Chainstack Yellowstone Geyser gRPC is a "
    "$49/mo add-on on Growth+ (2 streams), not on Developer. No "
    "gRPC client is built, no dependency is added, nothing is "
    "enabled. The free plan's streaming path is WebSocket "
    "subscriptions (standard JSON-RPC / LaserStream WSS standard on "
    "Helius Free); gap recovery stays RPC backfill.")


def yellowstone_grpc_status():
    """The Yellowstone gRPC investigation (Phase 6: investigate
    only, do not build).

    Reads the support value from ``provider_capabilities`` (the
    single source) for each provider and reports the plan finding.
    ``supported_by_plan`` is False and ``client_built`` is False by
    construction — this function is the record that the investigation
    happened and the client was deliberately NOT built.
    """
    per_provider = {}
    for account in ("helius_main", "alchemy", "chainstack"):
        per_provider[account] = caps.supports(account,
                                              "yellowstone_grpc")
    return {"supported_by_plan": False,
            "client_built": False,
            "enabled": False,
            "finding": YELLOWSTONE_FINDING,
            "per_provider": per_provider,
            "citations": ["provider_capabilities.py (ACCOUNTS[*]"
                          "['yellowstone_grpc'])",
                          "helius.dev/docs/laserstream",
                          "alchemy.com/pricing",
                          "chainstack.com/pricing"]}


# --- retention window / completeness ------------------------------------
def retention_window():
    """Chainstack retention-window status.

    Returns {"window_slots": None|int, "measured": bool, "note"}.
    The window is UNMEASURED (None) until a live probe on the AGT
    target measures it — no published slot depth exists, so no
    number is invented here. Callers that HAVE measured it pass
    ``window_slots`` explicitly to the fetch functions.
    """
    return {"window_slots": None,
            "measured": False,
            "note": ("Chainstack Developer retains a 400M-shred ledger "
                     "with NO published slot/time depth; coverage is "
                     "never assumed — the router defaults to Alchemy "
                     "(archive) unless the caller asserts recent or "
                     "supplies a measured window + slot hints.")}


def historical_completeness():
    """What historical data each provider actually makes available
    (stated only where the provider makes that data available).

    Chainstack: recent reads only, NO deep history (Developer has
    no archive). Alchemy: full archive back to genesis per docs
    (UNVERIFIED by live probe). Historical account-state reads are
    paid-only on Alchemy and are not available via standard RPC on
    any free tier — this workload does not pretend otherwise.
    """
    return {
        "chainstack": {
            "deep_history": False,
            "recent_reads": True,
            "retention_window": retention_window(),
            "methods_unavailable": sorted(CHAINSTACK_UNSERVED_METHODS),
            "note": ("Developer Free: NO archive data. "
                     "getSignaturesForAddress always archive-billed "
                     "(2 RU) and NOT served (-32002); "
                     "getProgramAccounts paid-only."),
        },
        "alchemy": {
            "deep_history": True,
            "recent_reads": True,
            "archive_depth": caps.get_account("alchemy").get(
                "archive_depth"),
            "note": ("Full archive back to genesis per docs; "
                     "UNVERIFIED by live probe."),
        },
        "signature_history_provider": "alchemy",
        "account_state_history": (
            "Not available via standard RPC on any free tier "
            "(historical account-state reads are paid-only on "
            "Alchemy). Account verification compares CURRENT state "
            "only."),
    }


# --- historical retrieval (router-routed; Chainstack in-window) ---------
def _recent_hints(recent, window_slots=None, target_slot=None,
                  latest_slot=None):
    hints = {}
    if recent:
        hints["recent"] = True
    if window_slots is not None:
        hints["window_slots"] = window_slots
    if target_slot is not None:
        hints["target_slot"] = target_slot
    if latest_slot is not None:
        hints["latest_slot"] = latest_slot
    return hints


def fetch_transaction(router, signature, recent=False, window_slots=None,
                      target_slot=None, latest_slot=None,
                      priority=PRIORITY_CARD):
    """Historical transaction retrieval for one signature.

    Routed as ``historical_range``: Chainstack first when the
    caller asserts recent (or the measured window covers the
    target), else Alchemy (archive). Returns the router's RpcResult
    as-is — callers fail closed on non-ok. ``recent`` must only be
    asserted for targets actually inside the retention window.
    """
    hints = {"workload": HISTORICAL_RANGE}
    hints.update(_recent_hints(recent, window_slots, target_slot,
                               latest_slot))
    return router.call("getTransaction", [signature, TX_FETCH_OPTIONS],
                       priority=priority, hints=hints)


def fetch_block(router, slot, recent=False, window_slots=None,
                latest_slot=None, transaction_details="signatures",
                priority=PRIORITY_CARD):
    """Historical block retrieval for one slot (default:
    transactionDetails "signatures" — the block's signature list,
    which is what market reconstruction consumes)."""
    hints = {"workload": HISTORICAL_RANGE}
    hints.update(_recent_hints(recent, window_slots, slot, latest_slot))
    params = [slot, {"encoding": "jsonParsed",
                     "maxSupportedTransactionVersion": 1,
                     "transactionDetails": transaction_details}]
    return router.call("getBlock", params, priority=priority,
                       hints=hints)


def fetch_signatures(router, address, limit=100, before=None,
                     until=None, priority=PRIORITY_CARD):
    """Signature history for an address.

    Routed as ``historical_range`` WITHOUT a recent assertion:
    ``getSignaturesForAddress`` is always archive-billed and is a
    free-tier refusal on Chainstack, so the capability gate drops
    Chainstack and Alchemy (archive) serves it. This is deliberate
    — signature history is Alchemy's leg on Free, never
    Chainstack's. Returns the router's RpcResult as-is.
    """
    options = {"limit": limit}
    if before:
        options["before"] = before
    if until:
        options["until"] = until
    return router.call("getSignaturesForAddress", [address, options],
                       priority=priority,
                       hints={"workload": HISTORICAL_RANGE})


def fetch_account_state(router, address, priority=PRIORITY_CARD):
    """CURRENT account state for an address (routine account read).
    There is no historical account-state read on any free tier —
    callers must not treat this as a point-in-time state."""
    return router.call("getAccountInfo", [address, {"encoding": "jsonParsed"}],
                       priority=priority, hints={})


def reconstruct_market_activity(router, address, limit=100, recent=False,
                                window_slots=None, latest_slot=None,
                                priority=PRIORITY_CARD):
    """Market reconstruction for one address (wallet / mint / pool).

    Signatures (Alchemy archive leg — Chainstack cannot serve them)
    -> one transaction read per signature (Chainstack first when
    recent, else Alchemy) -> normalized event rows. Returns
    {"events", "stats", "completeness"}; a failed transaction
    fetch produces NO event row — the signature is listed in
    stats["failed"] so the gap is visible, never silently dropped.
    """
    sig_result = fetch_signatures(router, address, limit=limit,
                                  priority=priority)
    stats = {"signatures_requested": limit, "signatures_found": 0,
             "events": 0, "failed": [], "signature_error": None}
    if sig_result is None or sig_result.get("kind") != "ok":
        stats["signature_error"] = (sig_result or {}).get("kind") \
            or "no_result"
        return {"events": [], "stats": stats,
                "completeness": historical_completeness()}
    rows = sig_result.get("result") or []
    stats["signatures_found"] = len(rows)
    events = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("signature"):
            continue
        tx = fetch_transaction(router, row["signature"], recent=recent,
                               window_slots=window_slots,
                               target_slot=row.get("slot"),
                               latest_slot=latest_slot,
                               priority=priority)
        if tx is None or tx.get("kind") != "ok":
            stats["failed"].append({
                "signature": row["signature"], "slot": row.get("slot"),
                "kind": (tx or {}).get("kind") or "no_result"})
            continue
        body = tx.get("result") or {}
        meta = body.get("meta") or {}
        events.append({
            "signature": row["signature"],
            "slot": row.get("slot"),
            "blockTime": body.get("blockTime"),
            "transactionIndex": row.get("transactionIndex"),
            "fee": meta.get("fee"),
            "err": meta.get("err"),
            "provider": tx.get("provider"),
        })
        stats["events"] += 1
    return {"events": EB.reconcile_event_order(events), "stats": stats,
            "completeness": historical_completeness()}


def build_replay_dataset(events, path, metadata=None):
    """Write a replay dataset (normalized event rows) to ``path``
    atomically. The metadata records the sources and the
    completeness statement — a dataset never claims coverage the
    providers do not provide."""
    payload = {
        "metadata": {
            "created_ts": time.time(),
            "events": len(events),
            "completeness": historical_completeness(),
            **(metadata or {}),
        },
        "events": list(events),
    }
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return {"path": path, "events": len(events)}


# --- risk-based independent verification --------------------------------
def classify_verification_event(event):
    """Risk classification of one event dict -> {"kind", "priority",
    "reason"}. ``event`` carries at least a ``kind`` (token_creation
    / pool_creation / liquidity_change / token_migration /
    bonding_curve_graduation / buy / sell / ...) and may carry
    ``conflicting`` (decoders disagree), ``decision_priority``
    (influenced a high-priority trading decision), and
    ``amount_sol`` (trade size)."""
    if not isinstance(event, dict):
        return {"kind": "routine", "priority": 10,
                "reason": "not an event dict"}
    if event.get("conflicting"):
        return {"kind": "conflicting_decoder", "priority": 100,
                "reason": "decoders disagree on this transaction"}
    kind = event.get("kind")
    if kind == "token_migration":
        return {"kind": kind, "priority": 90,
                "reason": "migration changes where the market is"}
    if kind == "bonding_curve_graduation":
        return {"kind": kind, "priority": 85,
                "reason": "graduation is the entry trigger"}
    if kind == "pool_creation":
        return {"kind": kind, "priority": 80,
                "reason": "new pool — liquidity facts matter"}
    if kind == "liquidity_change":
        return {"kind": kind, "priority": 75,
                "reason": "liquidity change moves the safety verdict"}
    if kind == "token_creation":
        return {"kind": kind, "priority": 70,
                "reason": "new mint — the riskiest single event"}
    if kind in ("buy", "sell"):
        amount = event.get("amount_sol")
        if isinstance(amount, (int, float)) \
                and amount >= IMPORTANT_TRADE_SOL:
            return {"kind": "important_trade", "priority": 60,
                    "reason": "trade >= %.1f SOL" % IMPORTANT_TRADE_SOL}
        if event.get("decision_priority"):
            return {"kind": "high_priority_decision", "priority": 55,
                    "reason": "influenced a high-priority trading decision"}
        return {"kind": "routine", "priority": 10,
                "reason": "trade below the important-trade threshold"}
    if event.get("decision_priority"):
        return {"kind": "high_priority_decision", "priority": 55,
                "reason": "influenced a high-priority trading decision"}
    return {"kind": "routine", "priority": 10,
            "reason": "routine traffic is never independently verified"}


def select_for_verification(events, budget=DEFAULT_VERIFY_BUDGET):
    """Risk-based sampling: rank events by verification priority,
    keep only those at/above MIN_VERIFY_PRIORITY, take at most
    ``budget``. Returns {"selected": [(event, classification)],
    "considered", "skipped_below_threshold", "budget"}.

    This is the guarantee that verification stays sampled: even a
    huge batch verifies at most ``budget`` transactions, and routine
    traffic is never selected at all.
    """
    ranked = []
    skipped = 0
    for event in events or []:
        cls = classify_verification_event(event)
        if cls["priority"] < MIN_VERIFY_PRIORITY:
            skipped += 1
            continue
        ranked.append((cls["priority"], event, cls))
    ranked.sort(key=lambda item: (-item[0],
                                  str(EB.event_id(item[1]) or "")))
    selected = [(event, cls) for _, event, cls in ranked[:budget]]
    return {"selected": selected,
            "considered": len(events or []),
            "skipped_below_threshold": skipped,
            "budget": budget}


def _complete_transaction(tx, signature=None):
    if not isinstance(tx, dict) or type(tx.get("slot")) is not int:
        return False
    body = tx.get("transaction")
    meta = tx.get("meta")
    if not isinstance(body, dict) or not isinstance(meta, dict):
        return False
    signatures = body.get("signatures")
    message = body.get("message")
    if (not isinstance(signatures, list) or not signatures or
            not all(isinstance(x, str) and x for x in signatures) or
            (signature is not None and signatures[0] != signature) or
            not isinstance(message, dict)):
        return False
    accounts = message.get("accountKeys")
    instructions = message.get("instructions")
    if (not isinstance(accounts, list) or not accounts or
            not isinstance(instructions, list) or
            "err" not in meta or type(meta.get("fee")) is not int):
        return False
    for name in ("preBalances", "postBalances"):
        balances = meta.get(name)
        if (not isinstance(balances, list) or len(balances) != len(accounts) or
                any(type(x) is not int or x < 0 for x in balances)):
            return False
    for account in accounts:
        address = account.get("pubkey") if isinstance(account, dict) else account
        if not isinstance(address, str) or not address:
            return False
    inner = meta.get("innerInstructions", [])
    if inner is not None and not isinstance(inner, list):
        return False
    all_instructions = list(instructions)
    for group in inner or []:
        if (not isinstance(group, dict) or type(group.get("index")) is not int or
                not 0 <= group["index"] < len(instructions) or
                not isinstance(group.get("instructions"), list)):
            return False
        all_instructions.extend(group["instructions"])
    return all(isinstance(ix, dict) and isinstance(ix.get("programId"), str)
               and ix["programId"] for ix in all_instructions)


def canonical_tx_digest(tx_result):
    """Comparable digest of a getTransaction result (jsonParsed).

    Fields: blockTime, fee, err (normalized), the sorted account
    pubkeys, the sorted program ids (top-level + inner), and the
    pre/post balances. None when there is no result to digest.
    Two providers returning the same transaction produce the same
    digest; any field difference is a conflict, never a rounding
    artifact (no floats are compared — fee/balances are ints).
    """
    if not _complete_transaction(tx_result):
        return None
    meta = tx_result.get("meta") or {}
    message = (tx_result.get("transaction") or {}).get("message") or {}
    accounts = set()
    programs = set()
    for key in message.get("accountKeys") or []:
        if isinstance(key, dict) and key.get("pubkey"):
            accounts.add(key["pubkey"])
        elif isinstance(key, str):
            accounts.add(key)
    for ins in message.get("instructions") or []:
        if isinstance(ins, dict) and ins.get("programId"):
            programs.add(ins["programId"])
    for inner in meta.get("innerInstructions") or []:
        for ins in (inner or {}).get("instructions") or []:
            if isinstance(ins, dict) and ins.get("programId"):
                programs.add(ins["programId"])
    err = meta.get("err")
    return {
        "signature": tx_result["transaction"]["signatures"][0],
        "slot": tx_result["slot"],
        "blockTime": tx_result.get("blockTime"),
        "fee": meta.get("fee"),
        "err": json.dumps(err, sort_keys=True) if err is not None
               else None,
        "accounts": sorted(accounts),
        "programs": sorted(programs),
        "preBalances": list(meta.get("preBalances") or []),
        "postBalances": list(meta.get("postBalances") or []),
        # Preserve ordered identities, instructions and all returned execution
        # economics. Set projections above are labels only, not equality proof.
        "transaction": tx_result["transaction"],
        "meta": meta,
        "version_evidence": ["version" in tx_result, tx_result.get("version")],
    }


def _digest_differences(primary_digest, verify_digest):
    """Field names where two digests differ (empty = agreement)."""
    if primary_digest is None or verify_digest is None:
        return ["digest_missing"]
    return sorted(k for k in primary_digest
                  if primary_digest.get(k) != verify_digest.get(k))


def verify_transaction(router, signature, primary_tx,
                       target_slot=None, recent=True,
                       priority=PRIORITY_CARD):
    """Independently verify one transaction via Chainstack.

    Re-reads the transaction and compares the canonical digest
    against ``primary_tx`` (the primary provider's answer).

    Declares ``independent_verification`` (Chainstack first as
    verifier when in-window; Helius excluded — it is the subject,
    not the verifier). ``recent`` must only be True for targets
    actually inside Chainstack's retention window; with
    recent=False the verifier is Alchemy (archive) — Chainstack
    cannot serve old transactions on Developer Free (archive
    -32002).

    Returns {"signature", "verified", "match", "status",
    "differences", "provider", "workload", "error"}:
    - status "match": digests agree.
    - status "conflict": digests differ; ``differences`` names the
      fields. Flagged, never silently resolved.
    - status "unverified": the re-read could not run (error /
      timeout / unconfigured) — NOT a match and NOT a conflict.
    """
    hints = {"workload": INDEPENDENT_VERIFICATION}
    hints.update(_recent_hints(recent, target_slot=target_slot))
    result = router.call(
        "getTransaction", [signature, TX_FETCH_OPTIONS],
        priority=priority, hints=hints)
    verdict = {"signature": signature, "verified": False,
               "match": None, "status": "unverified",
               "differences": [], "provider": (result or {}).get("provider"),
               "workload": INDEPENDENT_VERIFICATION, "error": None}
    if result is None or result.get("kind") != "ok":
        verdict["error"] = "%s: %s" % (
            (result or {}).get("kind") or "no_result",
            (result or {}).get("note") or "re-read failed")
        return verdict
    if result.get("provider") != "chainstack":
        verdict["error"] = "independent Chainstack source unavailable"
        return verdict
    if not _complete_transaction(primary_tx, signature) or not _complete_transaction(result.get("result"), signature):
        verdict["error"] = "transaction comparison evidence incomplete or identity mismatch"
        return verdict
    primary_digest = canonical_tx_digest(primary_tx)
    verify_digest = canonical_tx_digest(result.get("result"))
    diffs = _digest_differences(primary_digest, verify_digest)
    verdict["verified"] = True
    verdict["comparison_scope"] = "returned transaction/signature/slot/blockTime/meta/version equality, not completeness of chain history"
    verdict["differences"] = diffs
    if diffs:
        verdict["status"] = "conflict"
        verdict["match"] = False
    else:
        verdict["status"] = "match"
        verdict["match"] = True
    return verdict


def verify_account_state(router, address, primary_account,
                         priority=PRIORITY_CARD):
    """Independently verify one account's CURRENT state via
    Chainstack (independent_verification workload).

    Compares lamports, owner, executable, rentEpoch, and the full
    data payload (base64 string equality when both sides return
    [data, encoding]). Same verdict shape as verify_transaction;
    "unverified" when the re-read could not run. This is current
    state only — there is no historical account-state read on any
    free tier.
    """
    result = router.call(
        "getAccountInfo", [address, {"encoding": "jsonParsed"}],
        priority=priority,
        hints={"workload": INDEPENDENT_VERIFICATION})
    verdict = {"address": address, "verified": False, "match": None,
               "status": "unverified", "differences": [],
               "provider": (result or {}).get("provider"), "error": None}
    if result is None or result.get("kind") != "ok":
        verdict["error"] = "%s: %s" % (
            (result or {}).get("kind") or "no_result",
            (result or {}).get("note") or "re-read failed")
        return verdict
    if result.get("provider") != "chainstack":
        verdict["error"] = "independent Chainstack source unavailable"
        return verdict
    def complete_account(payload):
        value = payload.get("value") if isinstance(payload, dict) else None
        if not (isinstance(value, dict) and
                all(k in value for k in ("lamports", "owner", "executable", "rentEpoch", "data")) and
                type(value["lamports"]) is int and value["lamports"] >= 0 and
                isinstance(value["owner"], str) and bool(value["owner"]) and
                type(value["executable"]) is bool and type(value["rentEpoch"]) is int and value["rentEpoch"] >= 0):
            return False
        data = value["data"]
        # jsonParsed request can return parsed known-account data, or base64
        # for an account the server cannot parse. Arbitrary dicts are invalid.
        if isinstance(data, dict):
            return (isinstance(data.get("program"), str) and bool(data["program"]) and
                    isinstance(data.get("parsed"), dict) and
                    isinstance(data["parsed"].get("type"), str) and bool(data["parsed"]["type"]) and
                    isinstance(data["parsed"].get("info"), dict) and
                    type(data.get("space")) is int and data["space"] >= 0)
        if isinstance(data, list) and len(data) == 2 and data[1] == "base64" and isinstance(data[0], str):
            try:base64.b64decode(data[0], validate=True)
            except (binascii.Error, ValueError):return False
            return True
        return False
    if not complete_account(primary_account) or not complete_account(result.get("result")):
        verdict["error"] = "account comparison evidence incomplete"
        return verdict
    primary = (primary_account or {}).get("value") or {}
    verify = (result.get("result") or {}).get("value") or {}
    diffs = []
    for field in ("lamports", "owner", "executable", "rentEpoch"):
        if primary.get(field) != verify.get(field):
            diffs.append(field)
    if primary.get("data") != verify.get("data"):
        diffs.append("data")
    verdict["verified"] = True
    verdict["differences"] = sorted(diffs)
    verdict["comparison_scope"] = "returned owner/lamports/executable/rentEpoch/data fields only, separate read slots not atomic state proof"
    verdict["status"] = "conflict" if diffs else "match"
    verdict["match"] = not diffs
    return verdict


def verify_sample(router, events_with_primary,
                  budget=DEFAULT_VERIFY_BUDGET, recent=True,
                  priority=PRIORITY_CARD):
    """Verify a risk-selected sample end to end.

    ``events_with_primary`` is a list of {"event", "primary_tx"}
    (and optionally {"primary_account", "address"} for account
    verification). Selects by risk (see select_for_verification),
    verifies each selected transaction, and returns the summary:
    {"selected", "matched", "conflicts": [verdicts],
    "unverified": [verdicts], "verdicts": [...]}. Conflicts are
    returned, never resolved. ``recent`` is passed to transaction
    verification (Chainstack verifies recent reads only; see
    verify_transaction).
    """
    events = [item.get("event") for item in (events_with_primary or [])]
    selection = select_for_verification(events, budget=budget)
    selected_ids = {EB.event_id(event) for event, _ in
                    selection["selected"]}
    verdicts = []
    for item in events_with_primary or []:
        event = item.get("event") or {}
        if EB.event_id(event) not in selected_ids:
            continue
        if item.get("primary_account") is not None \
                and item.get("address"):
            verdicts.append(verify_account_state(
                router, item["address"], item["primary_account"],
                priority=priority))
        else:
            verdicts.append(verify_transaction(
                router, EB.event_id(event), item.get("primary_tx"),
                target_slot=event.get("slot"), recent=recent,
                priority=priority))
    matched = sum(1 for v in verdicts if v["status"] == "match")
    return {"selected": len(verdicts),
            "matched": matched,
            "conflicts": [v for v in verdicts
                          if v["status"] == "conflict"],
            "unverified": [v for v in verdicts
                           if v["status"] == "unverified"],
            "verdicts": verdicts}


# --- measured backfill performance (mocked transport, labelled) --------
def measure_backfill_performance(recover_fn):
    """Time one backfill run and report mocked-transport timing.

    ``recover_fn`` is a zero-arg callable returning a
    BackfillDriver report dict (e.g. ``lambda: driver.recover_gap(...)``).
    Returns the report's counts plus elapsed_s, events_per_s, and
    an explicit timing label: this measures the router + transport
    overhead in the test harness, NOT provider latency — never quote
    it as provider performance.
    """
    t0 = time.monotonic()
    report = recover_fn()
    elapsed = time.monotonic() - t0
    events = report.get("events_new", 0)
    return {"elapsed_s": round(elapsed, 4),
            "events_new": events,
            "signatures_found": report.get("signatures_found", 0),
            "pages": report.get("pages", 0),
            "events_per_s": (round(events / elapsed, 2)
                             if elapsed > 0 else None),
            "timing": ("MOCKED TRANSPORT — router + fake-opener "
                       "overhead only; NOT provider latency; do not "
                       "quote as provider performance"),
            "report": report}


# --- workload entry point ------------------------------------------------
class ChainstackWorkload:
    """Chainstack's permanent workload, bound to one router.

    Thin entry point over the module functions: historical retrieval
    in-window, market reconstruction, replay datasets, the backfill
    driver, and risk-based independent verification. Routing
    decisions stay in the router; this class only declares the
    workload and the recency evidence for each call.
    """

    def __init__(self, router, home=None, verify_budget=None):
        self.router = router
        self.home = home
        self.verify_budget = (DEFAULT_VERIFY_BUDGET
                              if verify_budget is None else verify_budget)
        self.driver = EB.BackfillDriver(router, home=home)

    def fetch_transaction(self, signature, recent=False, **kw):
        return fetch_transaction(self.router, signature, recent=recent,
                                 **kw)

    def fetch_block(self, slot, recent=False, **kw):
        return fetch_block(self.router, slot, recent=recent, **kw)

    def fetch_signatures(self, address, recent=False, **kw):
        # ``recent`` is accepted for interface symmetry with
        # fetch_block/fetch_transaction: the signatures API is
        # inherently most-recent-first, so there is no recency window
        # to declare and the flag intentionally goes no further.
        return fetch_signatures(self.router, address, **kw)

    def fetch_account_state(self, address, priority=PRIORITY_CARD):
        # Mirrors the module function's exact signature: no **kw, so
        # unsupported kwargs fail loudly at the wrapper instead of
        # being silently forwarded (reviewer contract note 2026-11-23).
        return fetch_account_state(self.router, address,
                                   priority=priority)

    def reconstruct(self, address, **kw):
        return reconstruct_market_activity(self.router, address, **kw)

    def backfill_gap(self, key, address, gap, recent=False, **kw):
        return self.driver.recover_gap(key, address, gap,
                                       recent=recent, **kw)

    def backfill_from_checkpoint(self, key, address, recent=False,
                                 **kw):
        return self.driver.recover_from_checkpoint(
            key, address, recent=recent, **kw)

    def retry_unrecovered(self, addresses, recent=False, **kw):
        return self.driver.retry_unrecovered_gaps(
            addresses, recent=recent, **kw)

    def verify_sample(self, events_with_primary, budget=None,
                      **kw):
        return verify_sample(
            self.router, events_with_primary,
            budget=self.verify_budget if budget is None else budget,
            **kw)

    def completeness(self):
        return historical_completeness()

    def yellowstone(self):
        return yellowstone_grpc_status()
