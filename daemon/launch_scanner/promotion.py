#!/usr/bin/env python3
"""Controlled-learning promotion RESEARCH plumbing (section 4.6).

This module prepares offline evaluation evidence only. It is NOT a
promotion controller and it CANNOT approve anything:

- Every durable record is explicitly research-only and NOT
  ELIGIBLE for promotion: promotion_eligibility is always
  "not_eligible_research_only" while the unresolved requirements
  stand. A caller-supplied "approved" flag grants NOTHING - no
  caller can confer eligibility on itself. Preparation (computing
  labeled evidence) may proceed; a validated recommendation may
  not.
- Verdicts are descriptive only: NEED-CRITERIA (criteria missing
  or incomplete), NEED-DATA (criteria supplied but the settled
  sample is too small under it), or EVALUATED (labeled evidence
  computed). No verdict implies promotion-worthiness.
- No activation path exists: degen-learner save_weights refuses
  automatic promotion, and nothing here writes runtime weights.

Discipline that IS enforced (mirrors walk_forward.py and the
lessons hygiene rules):

- Chronological splits ONLY, on SETTLED time (the outcome event
  time). Records with no valid settled_ts are excluded - archive
  bookkeeping time is never substituted for event meaning.
- Scorers receive an ENTRY-TIME-ONLY view (entry_ts, features).
  Outcome fields are stripped, so a scorer that peeks at outcomes
  fails loudly instead of silently "winning". Note: an arbitrary
  callable cannot be proven leak-free - the harness enforces input
  schema and chronology, not feature hygiene.
- Economics are computed on ALL SETTLED recorded outcomes a scorer
  takes. Non-current-price provenance and non-finite/missing core
  outcomes are EXCLUDED, counted, and their economics reported
  SEPARATELY - never mixed, never presented as qualified
  performance (selective missingness can bias the included
  subset).

Unresolved requirements (owner-owned, not waived by this module):
capital protection, selection noise control, fresh holdout, outcome
overlap, missingness bias, counterfactual coverage.
"""

import contextlib
import fcntl
import json
import math
import os
import time

CURRENT_PRICE = "current_price_simulated_fill"

RESEARCH_LIMITS = [
    "capital_protection_not_modeled",
    "selection_noise_uncontrolled",
    "fresh_holdout_not_enforced",
    "outcome_overlap_unhandled",
    "missingness_bias_possible",
    "counterfactual_coverage_unknown",
    "callable_leakage_unprovable",
]

NOT_ELIGIBLE = "not_eligible_research_only"


def _sanitize_criteria(criteria):
    """Echo the caller's criteria with any approval flag renamed:
    a caller assertion is recorded as such, never as approval."""
    if not isinstance(criteria, dict):
        return criteria
    out = dict(criteria)
    if "approved" in out:
        out["caller_assertion_not_approval"] = bool(
            out.pop("approved"))
    return out


def _num(v):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _settled(records):
    """Split recorded outcomes into usable settled records and
    labeled exclusions. Settled records keep an entry-time view
    separate from the outcome, so scorers never see outcomes.
    Never raises."""
    settled, excluded = [], []
    for rec in records or []:
        try:
            rec = rec or {}
            mult = _num(rec.get("realized_mult"))
            if mult is None:
                excluded.append({
                    "reason": "nonfinite_or_missing_outcome"})
                continue
            ts = _num(rec.get("settled_ts"))
            if ts is None:
                # Archive bookkeeping time is NOT event time: no
                # archived_ts fallback.
                excluded.append({
                    "reason": "missing_or_invalid_settled_ts",
                    "realized_mult": mult})
                continue
            kind = str(rec.get("exit_price_kind") or "unknown")
            if kind != CURRENT_PRICE:
                excluded.append({"reason": "not_current_price",
                                 "exit_price_kind": kind,
                                 "realized_mult": mult})
                continue
            # Entry schema hygiene: a scorer is only honest when
            # its entry-time view is well formed - finite entry_ts
            # not after the settle, and a dict of features.
            entry_ts = _num(rec.get("entry_ts"))
            if entry_ts is None:
                excluded.append({
                    "reason": "missing_or_invalid_entry_ts",
                    "realized_mult": mult})
                continue
            if entry_ts > ts:
                excluded.append({"reason": "entry_after_settled",
                                 "realized_mult": mult})
                continue
            if not isinstance(rec.get("features"), dict):
                excluded.append({
                    "reason": "missing_or_invalid_features",
                    "realized_mult": mult})
                continue
            settled.append({
                "entry": {"entry_ts": entry_ts,
                          "features": rec.get("features")},
                "settled_ts": ts,
                "realized_mult": mult,
            })
        except Exception:
            excluded.append({"reason": "unreadable"})
    return settled, excluded


def _chronological_split(settled, val_frac):
    """Expanding-window split on SETTLED time: the newest fold is
    the out-of-sample validation. Chronological ONLY."""
    order = sorted(settled, key=lambda r: r["settled_ts"])
    cut = int(len(order) * (1.0 - val_frac))
    return order[:cut], order[cut:]


def _economics(settled, score_fn):
    """All-settled economics over the records a scorer takes.
    Scorer exceptions propagate: a broken scorer fails the
    evaluation loudly, never silently promotes."""
    taken = [r for r in settled if score_fn(r["entry"])]
    n = len(taken)
    if not n:
        return {"taken": 0, "win_rate": None, "mean_mult": None}
    wins = sum(1 for r in taken if r["realized_mult"] > 1.0)
    return {"taken": n,
            "win_rate": round(wins / n, 4),
            "mean_mult": round(
                sum(r["realized_mult"] for r in taken) / n, 6)}


def _excluded_economics(excluded):
    """Labeled economics of the excluded records, reported
    separately - excluded outcomes are evidence of missingness,
    never mixed into the included subset."""
    mults = [e["realized_mult"] for e in excluded or []
             if _num((e or {}).get("realized_mult")) is not None]
    out = {"n": len(excluded or []),
           "provenance": "excluded_mixed_or_unknown"}
    if mults:
        out["mean_mult"] = round(sum(mults) / len(mults), 6)
    return out


def evaluate_promotion(candidate_id, records, score_incumbent,
                       score_candidate, criteria=None, now=None):
    """One durable research-only evaluation record. No verdict in
    this record implies promotion-worthiness; without
    source-approved criteria the candidate is NOT ELIGIBLE, and
    preparation (labeled evidence) is all this produces."""
    now = time.time() if now is None else now
    settled, excluded = _settled(records)
    dec = {
        "v": 1,
        "record_type": "promotion_decision",
        "candidate_id": str(candidate_id or "unknown"),
        "created_ts": now,
        "criteria": _sanitize_criteria(criteria),
        # A caller flag never confers eligibility: eligibility is
        # fixed not-eligible while the research limits stand.
        "promotion_eligibility": NOT_ELIGIBLE,
        "research_only": True,
        "research_limits": list(RESEARCH_LIMITS),
        "activation": "not_authorized",
        "n_records": len(records or []),
        "n_settled": len(settled),
        "n_excluded": len(excluded),
        "excluded_reasons": sorted(
            {e["reason"] for e in excluded}),
        "excluded_economics": _excluded_economics(excluded),
        "provenance": "simulated_current_price_only",
        "qualified_performance": False,
    }
    val_frac = _num((criteria or {}).get("val_frac"))
    min_settled = _num((criteria or {}).get("min_settled"))
    min_val = _num((criteria or {}).get("min_val"))
    if val_frac is None or min_settled is None or min_val is None:
        dec["verdict"] = "NEED-CRITERIA"
        dec["descriptive_only"] = True
        dec["all_settled_incumbent"] = _economics(
            settled, score_incumbent)
        dec["all_settled_candidate"] = _economics(
            settled, score_candidate)
        return dec
    train, val = _chronological_split(settled, val_frac)
    dec["train_incumbent"] = _economics(train, score_incumbent)
    dec["val_incumbent"] = _economics(val, score_incumbent)
    dec["val_candidate"] = _economics(val, score_candidate)
    if len(settled) < min_settled or len(val) < min_val:
        dec["verdict"] = "NEED-DATA"
        return dec
    dec["verdict"] = "EVALUATED"
    return dec


def _home(home=None):
    if home:
        return str(home)
    return os.environ.get(
        "FOREX_AGENT_HOME",
        os.path.join(os.path.expanduser("~"), ".forex-agent"))


def _path(home=None):
    return os.path.join(_home(home), "promotion", "decisions.jsonl")


def _lock_path(home=None):
    return _path(home) + ".lock"


class _LockUnavailable(Exception):
    pass


@contextlib.contextmanager
def _write_lock(home=None):
    """Exclusive cross-process lock (the verdict_ledger lock
    pattern). FAIL CLOSED: a lock failure raises _LockUnavailable
    and the append reports failure - durability is never claimed
    from an unlocked write."""
    fd = None
    try:
        fd = os.open(_lock_path(home), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        raise _LockUnavailable("lock unavailable")
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except Exception:
            pass


def append_decision(decision, home=None):
    """One locked, fsync'd append to the durable decision log.
    Returns True only when the record is durably written. A file
    whose tail is a torn fragment is repaired under the lock (the
    fragment stays its own unreadable line, counted by
    decisions_status) so a new record never glues onto it. The
    file is fsync'd; the directory fsync is best-effort (some
    filesystems refuse it) and is the stated durability limit.
    Never raises."""
    try:
        p = _path(home)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _write_lock(home):
            with open(p, "a+") as f:
                # Torn-tail repair: a partial last line must not
                # glue onto the new record.
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size:
                    f.seek(size - 1)
                    if f.read(1) != "\n":
                        f.seek(0, os.SEEK_END)
                        f.write("\n")
                f.seek(0, os.SEEK_END)
                f.write(json.dumps(decision, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                dfd = os.open(os.path.dirname(p), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except Exception:
                pass  # directory fsync is best-effort
        return True
    except Exception:
        return False


def decisions_status(home=None):
    """Reader-side status: availability plus the unreadable-line
    count, so a torn fragment is reported, never silently dropped.
    Never raises."""
    try:
        p = _path(home)
        if not os.path.exists(p):
            return {"available": False, "error": "missing",
                    "unreadable_lines": 0, "records": 0}
        unreadable = 0
        records = 0
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict) and rec.get(
                            "record_type") == "promotion_decision":
                        records += 1
                    else:
                        unreadable += 1
                except Exception:
                    unreadable += 1
        return {"available": True, "unreadable_lines": unreadable,
                "records": records}
    except Exception as e:
        return {"available": False, "error": str(e),
                "unreadable_lines": 0, "records": 0}


def load_decisions(home=None, limit=1000):
    """Decision records with the paired read contract: records
    newest first PLUS the read status (unreadable-line count,
    partial flag), so a consumer always sees whether the log read
    clean. Never raises."""
    try:
        st = decisions_status(home)
        out = []
        if st.get("available"):
            with open(_path(home)) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict) and rec.get(
                            "record_type") == "promotion_decision":
                        out.append(rec)
            out.reverse()
        return {"records": out[:limit],
                "available": bool(st.get("available")),
                "unreadable_lines": st.get("unreadable_lines", 0),
                "partial": st.get("unreadable_lines", 0) > 0}
    except Exception:
        return {"records": [], "available": False,
                "unreadable_lines": 0, "partial": False}
