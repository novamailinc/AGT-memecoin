"""Additive typed live-safety evidence (pure, no I/O, no clock: caller passes `now`). Composes WITH
core.live_safety_evidence.evaluate; it changes no existing contract. Written against last-saved bytes of
live_safety_evidence (md5 f5f48fba, verified) and live_safety_policy (md5 de713d95 plus the transient diff). It imports
core.live_safety_parsers, which here is a NEW implementation (md5 209e0615, authored 2026-11-24); the earlier parsers
module (tip md5 d433cdbf) was lost and is NOT what this file runs against. Its tests are not the old tip tests. The
integrator must review the new parsers against the producer's tree; no hash here is a claim about the current HEAD.

Closes three gaps found in the PYUSD fixture review (no owner threshold is defined here; every number comes from the caller):
 1. mint/freeze authority had no typed item (only free-text flags). Item: {"mint_authority": S, "freeze_authority": S},
    S = {"status": "revoked"|"set"|"unavailable"}. From parsed.info: key present and None -> revoked; non-empty str -> set;
    key absent, empty string or any other type -> unavailable (never assumed revoked).
 2. The epoch used for transfer-fee selection had no as-of time. Item: {"epoch": int>=0, "fetched_at": number}. A fee is
    resolved only from an epoch whose age is within the caller's max_epoch_age_s; otherwise epoch_selection_required.
 3. holders.covered_pct was emitted but never read. Policy must set min_holders_covered_pct (number 0..100). None or
    missing refuses: absence of a coverage rule is not approval, and a partial population is not a measured full one.
policy keys (all REQUIRED, no defaults): allow_mint_authority(bool), allow_freeze_authority(bool),
    max_epoch_age_s(number>0), min_holders_covered_pct(number 0..100).
Limit: validates what the producer states; it cannot prove the producer fetched the right account."""
import math

try:
    from core import live_safety_parsers as _P
except ImportError:
    import live_safety_parsers as _P

AUTH_STATUSES = ("revoked", "set", "unavailable")
POLICY_KEYS = ("allow_mint_authority", "allow_freeze_authority", "max_epoch_age_s", "min_holders_covered_pct")


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _one_authority(info, key):
    if not isinstance(info, dict) or key not in info:
        return {"status": "unavailable"}
    v = info[key]
    if v is None:
        return {"status": "revoked"}
    if isinstance(v, str) and v.strip():
        return {"status": "set"}
    return {"status": "unavailable"}


def parse_authorities(info):
    return {"mint_authority": _one_authority(info, "mintAuthority"),
            "freeze_authority": _one_authority(info, "freezeAuthority")}


def epoch_item(epoch, fetched_at):
    """Typed epoch evidence; malformed input becomes an unusable item, never a guessed epoch."""
    ok = (isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0 and _fin(fetched_at))
    return {"epoch": epoch if ok else None, "fetched_at": fetched_at if ok else None}


def epoch_fresh(item, now, max_epoch_age_s):
    """True only for a well-formed item fetched at or before `now`, no older than max_epoch_age_s."""
    if not isinstance(item, dict) or not _fin(now) or not _fin(max_epoch_age_s) or max_epoch_age_s <= 0:
        return False
    e, t = item.get("epoch"), item.get("fetched_at")
    if not (isinstance(e, int) and not isinstance(e, bool) and e >= 0 and _fin(t)):
        return False
    return 0 <= now - t <= max_epoch_age_s


def fee_with_epoch(program, info, item, now, max_epoch_age_s):
    """parse_transfer_fee, but a stale, future-dated or malformed epoch is passed on as None, so the parser answers
    epoch_selection_required instead of resolving from an epoch that may be a boundary behind."""
    ep = item.get("epoch") if isinstance(item, dict) and epoch_fresh(item, now, max_epoch_age_s) else None
    return _P.parse_transfer_fee(program, info, ep)


def _policy_ok(p):
    return (isinstance(p, dict) and all(k in p for k in POLICY_KEYS)
            and isinstance(p["allow_mint_authority"], bool) and isinstance(p["allow_freeze_authority"], bool)
            and _fin(p["max_epoch_age_s"]) and p["max_epoch_age_s"] > 0
            and _fin(p["min_holders_covered_pct"]) and 0 <= p["min_holders_covered_pct"] <= 100)


def evaluate_extras(evidence, policy, now, epoch_required=True):
    """evidence = {"authorities": parse_authorities(...), "epoch": epoch_item(...), "holders": parse_holders(...) result}.
    Returns (ok, reasons)."""
    if not _policy_ok(policy):
        return False, ["extras_policy_missing_or_malformed"]
    if not isinstance(evidence, dict):
        return False, ["extras_evidence_missing"]
    r = []
    au = evidence.get("authorities")
    for key, allow in (("mint_authority", "allow_mint_authority"), ("freeze_authority", "allow_freeze_authority")):
        s = au.get(key) if isinstance(au, dict) else None
        if not isinstance(s, dict) or s.get("status") not in AUTH_STATUSES:
            r.append(key + "_missing_or_malformed")
        elif s["status"] == "unavailable":
            r.append(key + "_unavailable")
        elif s["status"] == "set" and not policy[allow]:
            r.append(key + "_set")
    # epoch_required defaults True (fail closed). A caller may pass False ONLY when the report positively proves there is
    # no transfer fee (see live_safety_decide.no_fee_proven); the epoch exists only for fee-schedule selection.
    if epoch_required is not False and not epoch_fresh(evidence.get("epoch"), now, policy["max_epoch_age_s"]):
        r.append("epoch_missing_stale_or_malformed")
    h = evidence.get("holders")
    if not isinstance(h, dict) or h.get("measured") is not True or not _fin(h.get("covered_pct")) \
            or not 0 <= h["covered_pct"] <= 100 + 1e-6:
        r.append("holders_coverage_missing_or_malformed")
    elif h["covered_pct"] < policy["min_holders_covered_pct"]:
        r.append("holders_coverage_below_policy")
    return (not r), r
