"""Pure fail-closed admission verifier for arming / sending live capital (directive 22.1, 22.2).

No I/O, no clock (caller passes `now`), no network, no state change. It answers one question:
may this exact strategy/config/wallet/network/amount/risk-limit combination be armed right now?
Every missing, malformed, stale, mismatched, unauthenticated or contradictory input refuses.
It defines no threshold and no authorization mechanism: numeric policy comes from the caller and
authentication is an injected callable. With no callable the authorization is refused.

Honest limits, stated in every result:
 * The registry's evidence is caller-recorded booleans, not independently verified.
 * report.context is caller-supplied to the evaluator. The owner authorization is what binds the
   report: it must carry the sha256 of the exact report text presented here.
 * emergency_stop_ok is a caller assertion about the running system, not verified here.
A positive result only means every listed check passed. It is not itself an arming action.
"""
import hashlib
import json
import math

try:
    from core import qualification_evaluator as _q
except ImportError:
    import qualification_evaluator as _q

BINDING = ("strategy_version", "config_hash", "data_period")
NEVER_ADMIT_STATES = ("PAUSED", "EMERGENCY_STOP", "DEVELOPMENT", "HISTORICAL_VALIDATION",
                      "DEMO_QUALIFICATION")
STATES = ("DEVELOPMENT", "HISTORICAL_VALIDATION", "DEMO_QUALIFICATION", "DEMO_QUALIFIED",
          "MAINNET_READY_PENDING_AUTHORIZATION", "MAINNET_CANARY", "MAINNET_RESTRICTED",
          "MAINNET_EXPANDED", "PAUSED", "EMERGENCY_STOP")
AUTH_FIELDS = ("report_sha256", "strategy_version", "config_hash", "data_period", "wallet", "network",
               "trade_amount_usd", "risk_limits", "mode", "issued_at", "expires_at")
RUNNING_FIELDS = BINDING + ("wallet", "network", "trade_amount_usd", "risk_limits", "mode")
LIMITATIONS = ("registry evidence is caller-recorded", "report.context is caller-supplied; bound only "
               "by the authorization's report_sha256", "emergency_stop_ok is a caller assertion")


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _s(v):
    return isinstance(v, str) and bool(v.strip())


def report_sha256(report_text):
    return hashlib.sha256(report_text.encode("utf-8")).hexdigest()


def _parse_report(text):
    if not isinstance(text, str) or not text.strip():
        return None, "report_missing"

    def bad(c):
        raise ValueError(c)

    def no_dups(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError("duplicate key")
            d[k] = v
        return d
    try:
        return _q.from_json_safe(json.loads(text, parse_constant=bad, object_pairs_hook=no_dups)), None
    except (ValueError, TypeError):
        return None, "report_not_strict_json"


def admit(registry_status, report_text, running, policy, authorization, authenticate, now,
          emergency_stop_ok):
    r = []

    # ---- inputs shape -------------------------------------------------------------------
    if not _fin(now):
        r.append("now_invalid")
    if not isinstance(running, dict) or any(k not in running or running[k] is None for k in RUNNING_FIELDS):
        r.append("running_binding_incomplete")
        running = {}
    else:
        for k in BINDING + ("wallet", "network", "mode"):
            if not _s(running[k]):
                r.append("running_invalid:" + k)
        if not _fin(running["trade_amount_usd"]) or running["trade_amount_usd"] <= 0:
            r.append("running_invalid:trade_amount_usd")
        if not isinstance(running["risk_limits"], dict) or not running["risk_limits"] or any(
                not _fin(v) for v in running["risk_limits"].values()):
            r.append("running_invalid:risk_limits")
    if not isinstance(policy, dict):
        r.append("policy_missing")
        policy = {}
    states = policy.get("allowed_states")
    if not (isinstance(states, list) and states and all(s in STATES for s in states)):
        r.append("policy_missing_or_invalid:allowed_states")
        states = []
    elif any(s in NEVER_ADMIT_STATES for s in states):
        r.append("policy_invalid:allowed_states_includes_non_admitting_state")
    if not _fin(policy.get("max_report_age_s")) or not policy.get("max_report_age_s", 0) > 0:
        r.append("policy_missing_or_invalid:max_report_age_s")

    # ---- registry -----------------------------------------------------------------------
    rs = registry_status
    if not isinstance(rs, dict) or rs.get("availability") != "ok":
        r.append("registry_unavailable")
    else:
        if rs.get("state") in NEVER_ADMIT_STATES or rs.get("state") not in states:
            r.append("registry_state_not_admitting:%s" % rs.get("state"))
        b = rs.get("binding")
        if not isinstance(b, dict) or any(b.get(k) != running.get(k) for k in BINDING) or not running:
            r.append("registry_binding_mismatch")
        for k in ("missing_historical", "missing_demo"):
            if rs.get(k) != []:
                r.append("registry_evidence_incomplete:" + k)

    # ---- evaluator report ---------------------------------------------------------------
    rep, err = _parse_report(report_text)
    if err:
        r.append(err)
    else:
        if rep.get("verdict") != _q.PASSED:
            r.append("report_verdict_not_passed:%s" % rep.get("verdict"))
        if rep.get("unsupported_metrics") != []:
            r.append("report_has_unsupported_metrics")
        gr = rep.get("gate_results")
        if not isinstance(gr, dict) or not gr or any(v is not True for v in gr.values()):
            r.append("report_gate_not_true")
        ctx = rep.get("context")
        if not isinstance(ctx, dict) or any(ctx.get(k) != running.get(k) for k in BINDING) or not running:
            r.append("report_context_binding_mismatch")
        ev = ctx.get("evaluated_at") if isinstance(ctx, dict) else None
        if not _fin(ev) or not _fin(now) or not _fin(policy.get("max_report_age_s")):
            r.append("report_age_unknown")
        elif now < ev:
            r.append("report_dated_in_future")
        elif now - ev > policy["max_report_age_s"]:
            r.append("report_stale")

    # ---- owner authorization ------------------------------------------------------------
    a = authorization
    if not isinstance(a, dict) or any(k not in a or a[k] is None for k in AUTH_FIELDS):
        r.append("authorization_missing_or_incomplete")
    else:
        if not callable(authenticate):
            r.append("authorization_unauthenticated:no_verifier")
        else:
            try:
                ok = authenticate(a) is True
            except Exception:
                ok = False
            if not ok:
                r.append("authorization_not_authenticated")
        if isinstance(report_text, str) and a["report_sha256"] != report_sha256(report_text):
            r.append("authorization_report_digest_mismatch")
        for k in BINDING + ("wallet", "network", "mode", "trade_amount_usd", "risk_limits"):
            if running and a[k] != running.get(k):
                r.append("authorization_mismatch:" + k)
        if not (_fin(a["issued_at"]) and _fin(a["expires_at"]) and _fin(now)):
            r.append("authorization_time_invalid")
        elif not (a["issued_at"] <= now < a["expires_at"]):
            r.append("authorization_not_in_force")

    # ---- runtime facts ------------------------------------------------------------------
    if emergency_stop_ok is not True:
        r.append("emergency_stop_not_confirmed")

    return {"allow": not r, "reasons": r, "limitations": list(LIMITATIONS),
            "note": "check result only; performs no arming and grants no authority"}
