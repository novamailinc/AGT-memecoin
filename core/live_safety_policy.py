"""Fail-closed live-lane safety-gate policy (pure, no I/O). Directive basis (the user's original AGT master
directive message, Nov 22 14:25 PT, wamid ...NjA4MzdENEMxRDFBQzYyQTZGAA==, read from the observation record):
critical information unavailable => degraded/no-trade; unknown or incompletely decoded token extensions never
get an unconditional safety pass; failed or unavailable routes are a refusal case. (Repo comments that cite
a "directive section" are not used as authority.)

Same call shape as daemon.launch_scanner.grad._safety_ok: returns (ok, why, report, transient).

Real report contract (daemon/launch_scanner/safety.py, instant_safety.py at 5e5ab448):
  dict(passed, score, flags, fails, unknown[, source, topHolders, instant]); the instant on-chain path adds
  instant={token2022_extensions:[names], transfer_fee:{status,...}|None, mint_authority, freeze_authority,...}.
  Every extension problem the instant parser cannot verify is a flag containing "(unverified - review)";
  a transferFeeConfig extension yields transfer_fee.status == "epoch_selection_required" (the effective fee
  is NOT resolved by the parser); the instant path ALWAYS adds "LP lock unverified (instant path)".

Policy (strict allowlist, no defaults for what is acceptable):
  * report is a dict; unknown is exactly False; passed is exactly True; fails == []; score finite number.
  * EVERY flag must be accepted by the caller via accepted_flag_prefixes (explicit, no default; empty means
    no flag is accepted). Flags containing ALWAYS_REFUSE terms are refused even if a prefix would accept them.
  * Extension coverage: the report must carry instant.token2022_extensions (a list of strings); every name
    must be in the caller's verified_extensions; transfer_fee, when present, must have status "resolved"
    (the current parser never emits that, so any transfer-fee extension refuses). A report with no
    `instant` block carries no extension evidence and refuses unless require_extension_evidence=False,
    which the caller must set explicitly and own.
Limits: this validates what the report states; it is not a decoder and cannot prove the parser saw every
extension. Which flags and extensions are acceptable is an owner/producer decision."""
import math

ALWAYS_REFUSE = ("undecod", "unparseable", "unrecognized", "unreadable", "undecoded", "partial", "decoder",
                 "unavailable", "not checked", "incomplete", "failed to", "unknown")


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _decide(rep, accepted_flag_prefixes, verified_extensions, require_extension_evidence):
    if rep.get("unknown") is not False:
        return False, "safety unknown", True
    if rep.get("passed") is not True:
        return False, "; ".join(map(str, rep.get("fails") or ["not passed"]))[:160], False
    if rep.get("fails") != []:
        return False, "safety report has fails or malformed fails", False
    if not _fin(rep.get("score")):
        return False, "safety score missing or non-numeric", False
    flags = rep.get("flags")
    if not isinstance(flags, list) or any(not isinstance(f, str) for f in flags):
        return False, "safety flags malformed", False
    for f in flags:
        low = f.lower()
        if any(t in low for t in ALWAYS_REFUSE):
            return False, "safety evidence incomplete: " + f[:120], True
        if not any(f.startswith(p) for p in accepted_flag_prefixes):
            return False, "flag not accepted by live policy: " + f[:120], False
    inst = rep.get("instant")
    if inst is None:
        if require_extension_evidence is not False:
            return False, "no extension evidence in report", True
        return True, "", False
    if not isinstance(inst, dict) or not isinstance(inst.get("token2022_extensions"), list) \
            or any(not isinstance(n, str) for n in inst["token2022_extensions"]):
        return False, "extension evidence malformed", True
    for n in inst["token2022_extensions"]:
        if n not in verified_extensions:
            return False, "extension not in verified set: %s" % n[:60], True
    tf = inst.get("transfer_fee")
    if tf:
        if not isinstance(tf, dict) or tf.get("status") != "resolved":
            return False, "transfer fee not resolved", True
    return True, "", False


def live_safety_decision(run_safety, accepted_flag_prefixes=(), verified_extensions=(),
                         require_extension_evidence=True):
    if (not isinstance(accepted_flag_prefixes, (list, tuple, frozenset, set))
            or not isinstance(verified_extensions, (list, tuple, frozenset, set))
            or any(not isinstance(p, str) or not p for p in accepted_flag_prefixes)):
        return False, "policy arguments malformed", None, True
    try:
        res = run_safety()
    except Exception as exc:
        return False, "safety error (%s): live refuses" % type(exc).__name__, None, True
    if not (isinstance(res, tuple) and len(res) == 4):
        return False, "safety result malformed", None, True
    ok, why, rep, _tr = res
    if not isinstance(rep, dict):
        return False, "no safety report: unconditional pass refused", None, True
    if ok is not True:
        # Retry only when the report itself says it is unknown (or carries a non-False unknown value). A definite
        # failure, including a report with no "unknown" key, is NOT transient.
        transient_ = ("unknown" in rep) and (rep.get("unknown") is not False)
        return False, str(why or "safety not ok")[:160], rep, transient_
    good, reason, transient = _decide(rep, tuple(accepted_flag_prefixes), frozenset(verified_extensions),
                                      require_extension_evidence)
    return (True, "", rep, False) if good else (False, reason, rep, transient)
