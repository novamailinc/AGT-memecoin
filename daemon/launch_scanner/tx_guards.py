#!/usr/bin/env python3
"""Transaction-outcome guards (2026-11-17, Batch 3).

Two phantom-entry guards from the pumpfun-bonkfun-bot reference
(docs/listeners-and-geyser.md; verify_create_log_gate.py,
verify_logs_listener_skips_failed_tx.py):

1. meta.err gating — a CreateEvent can survive in a REVERTED
   transaction's logs, and reverted transactions still carry
   token balances. Any parse that reads a transaction's logs or
   balances must refuse the transaction when meta.err is not
   None. A reverted create never becomes a candidate, a reverted
   oldest-tx never names a deployer, a reverted migration tx
   never prices an entry. Missing meta is UNKNOWN, not success:
   fail closed (refuse) — a load-balanced node may not have the
   tx yet, and treating "not seen" as "succeeded" is how
   phantom closes are born.

2. Whole-line log matching — match "Program log: Instruction:
   Create" as a WHOLE line. Substring matching also matches
   CreateTokenAccount / CreatePool / CreateFeeSharingConfig, so
   a pool-create log would be misread as a token create. Route
   on meta.logMessages, never gate dispatch on envelope decode.

Pure stdlib, no network, no state. Every function never raises.
"""

CREATE_LOG_LINE = "Program log: Instruction: Create"


def _meta_of(tx):
    """The meta dict of a getTransaction result — or the dict
    itself when a bare meta was passed. None when unreadable."""
    try:
        if not isinstance(tx, dict):
            return None
        if "meta" in tx:
            meta = tx.get("meta")
        elif "err" in tx:
            meta = tx
        else:
            return None
        return meta if isinstance(meta, dict) else None
    except Exception:
        return None


def tx_succeeded(tx):
    """True only when the transaction demonstrably succeeded.

    ``tx`` is a getTransaction result (or its meta dict). True
    exactly when meta is a dict with an explicit err key equal to None.
    Missing meta, a non-dict meta, or any err value (including a
    dict err) is NOT success — fail closed. Never raises."""
    meta = _meta_of(tx)
    if meta is None:
        return False
    return "err" in meta and meta["err"] is None


def tx_reverted_reason(tx):
    """Stable reason when a tx did not succeed, else None.

    "tx_reverted" when meta.err is present, "tx_unknown" when
    meta is missing/unreadable (fail-closed, same as refused).
    Never raises."""
    meta = _meta_of(tx)
    if meta is None or "err" not in meta:
        return "tx_unknown"
    if meta.get("err") is None:
        return None
    return "tx_reverted"


def whole_line_match(logs, target):
    """True when target appears as a WHOLE log line.

    ``logs`` is meta.logMessages (a list of strings) or None.
    Equality per line — never substring: "Program log:
    Instruction: Create" must not match "Program log:
    Instruction: CreateTokenAccount". Never raises."""
    try:
        if not isinstance(logs, list):
            return False
        for line in logs:
            if line == target:
                return True
        return False
    except Exception:
        return False


def find_log_line(logs, target):
    """The whole matching log line, or None.

    Same whole-line rule as whole_line_match; returns the line
    itself (identical to target when found) so callers can route
    on it. Never raises."""
    try:
        if not isinstance(logs, list):
            return None
        for line in logs:
            if line == target:
                return line
        return None
    except Exception:
        return None


def log_messages(tx):
    """meta.logMessages as a list, or [] when absent/unreadable.

    Never raises. Callers route on the returned lines with
    whole_line_match — never with ``in`` on a joined blob."""
    try:
        if not isinstance(tx, dict):
            return []
        meta = tx.get("meta")
        if not isinstance(meta, dict):
            return []
        logs = meta.get("logMessages")
        return list(logs) if isinstance(logs, list) else []
    except Exception:
        return []


def response_signature_matches(tx,requested):
    """Bind a fetched transaction to its request, not creator/swap semantics."""
    if not isinstance(requested,str) or not requested or not isinstance(tx,dict):return False
    transaction=tx.get('transaction')
    if not isinstance(transaction,dict):return False
    signatures=transaction.get('signatures')
    return isinstance(signatures,list) and bool(signatures) and type(signatures[0]) is str and signatures[0]==requested
