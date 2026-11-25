"""Typed safety-evidence contract for the live lane (pure, no I/O, proposal: no producer emits this yet).

Replaces text matching on flags with typed per-item statuses. Every item is REQUIRED; a missing,
malformed, unavailable or unverified item refuses by default. The caller supplies what it accepts; nothing
here is owner policy and no numeric threshold is defined (None means "not enforced", stated explicitly).

evidence = {
  "lp_lock":      {"status": "burned"|"locked"|"unverified"|"unavailable"},
  "holders":      {"measured": bool, "program_held_pct": num, "unclassified_pda_pct": num},
  "extensions":   {"complete": bool, "names": [str], "review": [ {name, reason} ]},
  "transfer_fee": {"status": "none"|"resolved"|"epoch_selection_required"|"unavailable",
                   "current_bps": num|None, "newer_bps": num|None, "authority_set": bool},
}
policy = {"accepted_lp_statuses": [...], "verified_extensions": [...],
          "max_program_held_pct": num|None, "max_unclassified_pda_pct": num|None,
          "max_fee_bps": num|None, "allow_fee_authority": bool}   # all keys required (no defaults)
Returns (ok, reasons). Unknown critical evidence is a refusal, not an owner-exception request.
Limit: validates what the producer states; cannot prove the parser saw every extension."""
import math

LP_STATUSES = ("burned", "locked", "unverified", "unavailable")
FEE_STATUSES = ("none", "resolved", "epoch_selection_required", "unavailable")
POLICY_KEYS = ("accepted_lp_statuses", "verified_extensions", "max_program_held_pct",
               "max_unclassified_pda_pct", "max_fee_bps", "allow_fee_authority")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _strs(v):
    return isinstance(v, (list, tuple, set, frozenset)) and all(isinstance(x, str) and x for x in v)


def _opt_num(v):
    return v is None or _num(v)


def _policy_ok(p):
    return (isinstance(p, dict) and all(k in p for k in POLICY_KEYS)
            and _strs(p["accepted_lp_statuses"]) and set(p["accepted_lp_statuses"]) <= {"burned", "locked", "unverified"}
            and _strs(p["verified_extensions"]) and _opt_num(p["max_program_held_pct"])
            and _opt_num(p["max_unclassified_pda_pct"]) and _opt_num(p["max_fee_bps"])
            and isinstance(p["allow_fee_authority"], bool))


def evaluate(evidence, policy):
    if not _policy_ok(policy):
        return False, ["policy_missing_or_malformed"]
    if not isinstance(evidence, dict):
        return False, ["evidence_missing"]
    r = []
    lp = evidence.get("lp_lock")
    if not isinstance(lp, dict) or lp.get("status") not in LP_STATUSES:
        r.append("lp_lock_missing_or_malformed")
    elif lp["status"] == "unavailable" or lp["status"] not in policy["accepted_lp_statuses"]:
        r.append("lp_lock_not_accepted:%s" % lp["status"])

    h = evidence.get("holders")
    if (not isinstance(h, dict) or h.get("measured") is not True or not _num(h.get("program_held_pct"))
            or not _num(h.get("unclassified_pda_pct"))):
        r.append("holders_unmeasured_or_malformed")
    else:
        if policy["max_program_held_pct"] is not None and h["program_held_pct"] > policy["max_program_held_pct"]:
            r.append("holders_program_held_over_limit")
        if policy["max_unclassified_pda_pct"] is not None and h["unclassified_pda_pct"] > policy["max_unclassified_pda_pct"]:
            r.append("holders_unclassified_pda_over_limit")

    e = evidence.get("extensions")
    if (not isinstance(e, dict) or e.get("complete") is not True or not _strs(e.get("names"))
            or not isinstance(e.get("review"), list)):
        r.append("extensions_incomplete_or_malformed")
    else:
        if e["review"]:
            r.append("extensions_have_review_items")
        for n in e["names"]:
            if n not in set(policy["verified_extensions"]):
                r.append("extension_not_verified:%s" % n[:40])

    f = evidence.get("transfer_fee")
    if not isinstance(f, dict) or f.get("status") not in FEE_STATUSES or not isinstance(f.get("authority_set"), bool):
        r.append("transfer_fee_missing_or_malformed")
    else:
        st = f["status"]
        if st in ("epoch_selection_required", "unavailable"):
            r.append("transfer_fee_unresolved:%s" % st)
        elif st == "resolved":
            if not _num(f.get("current_bps")) or not _num(f.get("newer_bps")):
                r.append("transfer_fee_bps_missing")
            elif policy["max_fee_bps"] is not None and max(f["current_bps"], f["newer_bps"]) > policy["max_fee_bps"]:
                r.append("transfer_fee_over_limit")
        elif st == "none" and (f.get("current_bps") not in (None, 0) or f.get("newer_bps") not in (None, 0)):
            r.append("transfer_fee_status_contradiction")
        if f["authority_set"] and not policy["allow_fee_authority"]:
            r.append("transfer_fee_authority_set")
    return (not r), r
