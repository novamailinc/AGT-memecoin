"""core.live_safety_decide: one PURE decision over the typed evidence (adapter + evidence + extras). NEW, nothing calls it.
decide(report, evidence_policy, extras_policy, now, program_owners=()) -> (ok, reasons, gaps)
 - evidence_policy: core.live_safety_evidence policy (all keys required). extras_policy: core.live_safety_extras policy
   (all keys required). No value is defaulted or invented here; a missing or malformed policy refuses.
 - now: caller's clock (finite number); this module reads no clock.
 - A transfer fee reported as epoch_selection_required is resolved only when the report's own schedules are both
   "available" AND the supplied epoch is fresh under extras_policy["max_epoch_age_s"]; otherwise it stays unresolved and
   refuses. Selection rule: newer schedule if epoch >= newer.epoch else older (same rule instant_safety records).
 - Refusal is the union of the evidence refusals and the extras refusals. ok is True only when both pass.
Limit: validates what the producer stated; it cannot prove the report is for the right mint or is current beyond the epoch
age check."""
import math
from core import live_safety_adapter as _A
from core import live_safety_evidence as _E
from core import live_safety_extras as _X


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _resolve_fee(rep, ev, now, extras_policy):
    inst = rep.get("instant") if isinstance(rep, dict) else None
    tf = inst.get("transfer_fee") if isinstance(inst, dict) else None
    cur = ev.get("transfer_fee")
    if not (isinstance(tf, dict) and isinstance(cur, dict) and cur.get("status") == "epoch_selection_required"):
        return
    if not isinstance(extras_policy, dict) or not _X._fin(extras_policy.get("max_epoch_age_s")):
        return
    item = ev.get("epoch")
    if not _X.epoch_fresh(item, now, extras_policy["max_epoch_age_s"]):
        return
    sch = tf.get("schedules")
    if not isinstance(sch, dict):
        return
    o, n = sch.get("olderTransferFee"), sch.get("newerTransferFee")
    for x in (o, n):
        if not (isinstance(x, dict) and x.get("status") == "available" and _int(x.get("epoch"))
                and isinstance(x.get("transferFeeBasisPoints"), int) and not isinstance(x["transferFeeBasisPoints"], bool)
                and 0 <= x["transferFeeBasisPoints"] <= 10000):
            return
    cur_bps = n["transferFeeBasisPoints"] if item["epoch"] >= n["epoch"] else o["transferFeeBasisPoints"]
    ev["transfer_fee"] = {"status": "resolved", "current_bps": cur_bps, "newer_bps": n["transferFeeBasisPoints"],
                          "authority_set": cur.get("authority_set", False)}


def no_fee_proven(rep):
    """True only when the report POSITIVELY proves there is no transfer fee. Every condition must hold, and it is derived
    here independently of decide()'s own evidence:
      - instant.extensions_parse (typed, from the emitter) has complete True, extensions_key matching the program
        kind, entry_count == len(entries), every entry status "parsed", no repeated extension name;
      - classic SPL program: extensions_key "absent". Token-2022 program: extensions_key "list" (a Token-2022 mint whose
        key is MISSING is not proven: that cannot be told from a parse that lost the list). Other or missing program kind
        is not proven;
      - token2022_extensions is a well-formed list of names, equal to the entry names, without transferFeeConfig;
        transfer_fee is None;
      - no report flag mentions an extension or a transfer fee, and a separate adapter pass reports extensions complete
        with no review items, fee status "none", and no extensions:/transfer_fee: gap.
    Any other state, including a missing field from an older emitter, keeps the epoch required."""
    inst = rep.get("instant") if isinstance(rep, dict) else None
    if not isinstance(inst, dict) or inst.get("transfer_fee") is not None:
        return False
    xp = inst.get("extensions_parse")
    if not isinstance(xp, dict) or xp.get("complete") is not True:
        return False
    entries, key, kind = xp.get("entries"), xp.get("extensions_key"), xp.get("token_program_kind")
    if not isinstance(entries, list) or xp.get("entry_count") != len(entries):
        return False
    if not ((kind == "spl" and key == "absent") or (kind == "token2022" and key == "list")):
        return False
    if key == "absent" and entries:
        return False
    enames = []
    for e in entries:
        if not (isinstance(e, dict) and e.get("status") == "parsed" and isinstance(e.get("name"), str) and e["name"]):
            return False
        enames.append(e["name"])
    if len(set(enames)) != len(enames) or "transferFeeConfig" in enames:
        return False
    names = inst.get("token2022_extensions")
    if not (isinstance(names, list) and names == enames):
        return False
    flags = rep.get("flags")
    if flags is not None:
        if not isinstance(flags, list):
            return False
        for f in flags:
            if not isinstance(f, str) or "extension" in f.lower() or "transfer fee" in f.lower():
                return False
    try:
        ev, gaps = _A.report_to_evidence(rep)
    except Exception:
        return False
    ex, tf = ev.get("extensions"), ev.get("transfer_fee")
    if not (isinstance(ex, dict) and ex.get("complete") is True and ex.get("review") == []
            and isinstance(tf, dict) and tf.get("status") == "none"):
        return False
    return not any(isinstance(g, str) and (g.startswith("extensions:") or g.startswith("transfer_fee:")) for g in gaps)


def decide(report, evidence_policy, extras_policy, now, program_owners=()):
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        return False, ["now_missing_or_malformed"], []
    ev, gaps = _A.report_to_evidence(report, program_owners)
    _resolve_fee(report, ev, now, extras_policy)
    ok1, r1 = _E.evaluate(ev, evidence_policy)
    ok2, r2 = _X.evaluate_extras(ev, extras_policy, now, epoch_required=not no_fee_proven(report))
    return (ok1 and ok2), list(r1) + list(r2), gaps
