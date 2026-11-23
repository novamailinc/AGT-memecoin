#!/usr/bin/env python3
"""Controlled-learning promotion plumbing (request section 4.6).

Offline evaluation -> durable promotion-decision record. There is
NO activation path: degen-learner save_weights refuses automatic
promotion, and this module only writes an advisory decision
artifact for an owner-approved demo-validation step, which itself
stays unauthorized. Criteria are source-grounded and code-defined:

- Chronological splits ONLY (expanding window; the newest fold is
  the out-of-sample validation the scorer never saw) - the same
  discipline as walk_forward.py.
- Economics are computed on ALL SETTLED recorded outcomes a scorer
  takes. Records whose exit provenance is not a current-price
  simulated fill, or whose core outcome (realized multiple or
  settle time) is non-finite/missing, are EXCLUDED and counted
  separately - never mixed into the included subset and never
  presented as qualified performance, because selective
  missingness can bias the included simulated subset.
- A candidate PROMOTES-TO-DEMO only with enough settled samples
  and a strictly better out-of-sample result than the incumbent;
  otherwise REJECT or NEED-DATA. Promotion-to-demo is still not
  runtime activation.

Scorers are callables over settled entry records; callers must
define them over entry-time features only. This harness enforces
chronological and economic discipline, not feature hygiene.
"""

import json
import math
import os
import time

CRITERIA_VERSION = 1
DEFAULT_CRITERIA = {
    "criteria_version": CRITERIA_VERSION,
    "min_settled": 30,          # minimum usable settled outcomes
    "min_val": 10,              # minimum out-of-sample fold size
    "val_frac": 0.25,           # newest fold fraction
    "min_oos_improvement": 0.0,  # strictly greater mean mult OOS
}

CURRENT_PRICE = "current_price_simulated_fill"


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
    labeled exclusions. Never raises."""
    settled, excluded = [], []
    for rec in records or []:
        try:
            mult = _num((rec or {}).get("realized_mult"))
            ts = _num((rec or {}).get("settled_ts")
                      or (rec or {}).get("ts")
                      or (rec or {}).get("archived_ts"))
            if mult is None or ts is None:
                excluded.append({"reason": "nonfinite_or_missing_core"})
                continue
            if str((rec or {}).get("exit_price_kind") or "unknown") \
                    != CURRENT_PRICE:
                excluded.append({
                    "reason": "not_current_price",
                    "exit_price_kind": str(
                        (rec or {}).get("exit_price_kind")
                        or "unknown")})
                continue
            settled.append({"ts": ts, "realized_mult": mult})
        except Exception:
            excluded.append({"reason": "unreadable"})
    return settled, excluded


def _chronological_split(records, val_frac):
    """Expanding-window split: train on the oldest, validate on the
    newest fold. Chronological ONLY, never shuffled."""
    order = sorted(records, key=lambda r: r["ts"])
    cut = int(len(order) * (1.0 - val_frac))
    return order[:cut], order[cut:]


def _economics(records, score_fn):
    """All-settled economics over the records a scorer takes."""
    taken = [r for r in records if score_fn(r)]
    n = len(taken)
    if not n:
        return {"taken": 0, "win_rate": None, "mean_mult": None}
    wins = sum(1 for r in taken if r["realized_mult"] > 1.0)
    return {"taken": n,
            "win_rate": round(wins / n, 4),
            "mean_mult": round(
                sum(r["realized_mult"] for r in taken) / n, 6)}


def evaluate_promotion(candidate_id, records, score_incumbent,
                       score_candidate, criteria=None, now=None):
    """One promotion-decision record. Advisory evidence only;
    activation is never part of the decision."""
    criteria = dict(DEFAULT_CRITERIA, **(criteria or {}))
    now = time.time() if now is None else now
    settled, excluded = _settled(records)
    train, val = _chronological_split(settled, criteria["val_frac"])
    dec = {
        "v": 1,
        "record_type": "promotion_decision",
        "candidate_id": str(candidate_id or "unknown"),
        "criteria": criteria,
        "created_ts": now,
        "n_records": len(records or []),
        "n_settled": len(settled),
        "n_excluded": len(excluded),
        "excluded_reasons": sorted(
            {e["reason"] for e in excluded}),
        "provenance": "simulated_current_price_only",
        "qualified_performance": False,
        "activation": "not_authorized",
        "train_incumbent": _economics(train, score_incumbent),
        "val_incumbent": _economics(val, score_incumbent),
        "val_candidate": _economics(val, score_candidate),
    }
    if len(settled) < criteria["min_settled"] \
            or len(val) < criteria["min_val"]:
        dec["verdict"] = "NEED-DATA"
        return dec
    inc = dec["val_incumbent"]
    cand = dec["val_candidate"]
    if not cand["taken"] or cand["mean_mult"] is None:
        dec["verdict"] = "REJECT"
        dec["reason"] = "candidate_takes_nothing_oos"
    elif inc["mean_mult"] is None or cand["mean_mult"] > \
            inc["mean_mult"] + criteria["min_oos_improvement"]:
        dec["verdict"] = "PROMOTE-TO-DEMO"
    else:
        dec["verdict"] = "REJECT"
        dec["reason"] = "no_oos_improvement"
    return dec


def _path(home=None):
    home = home or os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, "memory", "promotion_decisions.jsonl")


def append_decision(decision, home=None):
    """Durable append of one decision record. Returns True when the
    record landed; never raises."""
    try:
        p = _path(home)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(decision, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def load_decisions(home=None, limit=1000):
    """Decision records, newest first; unreadable lines skipped,
    never misread. Never raises."""
    try:
        out = []
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
        return out[:limit]
    except Exception:
        return []
