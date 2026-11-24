import copy, json, math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import qualification_admission as m
import qualification_evaluator as q

B = {"strategy_version": "v1", "config_hash": "h1", "data_period": "p1"}
RUN = dict(B, wallet="W", network="mainnet-beta", trade_amount_usd=5.0, risk_limits={"max_dd": 0.1, "max_day": 0.03},
           mode="canary")
NOW = 10_000.0
POL = {"allowed_states": ["MAINNET_READY_PENDING_AUTHORIZATION"], "max_report_age_s": 3600}
REG = dict(availability="ok", state="MAINNET_READY_PENDING_AUTHORIZATION", binding=dict(B),
           missing_historical=[], missing_demo=[])

def report(**over):
    r = {"verdict": q.PASSED, "unsupported_metrics": [], "gate_results": {"a": True, "b": True},
         "context": dict(B, evaluated_at=NOW - 60), "reasons": [], "metrics": {"profit_factor": math.inf}}
    r.update(over); return r

def text(r=None):
    return q.dumps_report(r or report())

def auth(t=None, **over):
    a = dict(B, report_sha256=m.report_sha256(t or text()), wallet="W", network="mainnet-beta",
             trade_amount_usd=5.0, risk_limits={"max_dd": 0.1, "max_day": 0.03}, mode="canary",
             issued_at=NOW - 10, expires_at=NOW + 100)
    a.update(over); return a

OK = lambda a: True

def run(reg=REG, t=None, running=RUN, pol=POL, a="d", authn=OK, now=NOW, es=True):
    t = text() if t is None else t
    return m.admit(reg, t, running, pol, auth(t) if a == "d" else a, authn, now, es)

def test_all_good_synthetic_allows():
    r = run()
    assert r["allow"] is True and r["reasons"] == [] and "no arming" in r["note"]
    assert r["limitations"]

def test_no_authenticator_refuses_even_when_everything_else_passes():
    r = run(authn=None)
    assert not r["allow"] and "authorization_unauthenticated:no_verifier" in r["reasons"]
    assert not run(authn=lambda a: False)["allow"]
    assert not run(authn=lambda a: 1)["allow"]                 # truthy but not exactly True
    def boom(a): raise RuntimeError("x")
    assert "authorization_not_authenticated" in run(authn=boom)["reasons"]

def test_real_today_shape_always_refuses():
    # registry cannot hold a mainnet state; evaluator PASSED unreachable; no authorization artifact
    reg = dict(availability="ok", state="DEMO_QUALIFIED", binding=dict(B), missing_historical=[], missing_demo=[],
               qualified=True, execution_authorized=False, runtime_gate_connected=False)
    rep = report(verdict=q.INSUFFICIENT_EVIDENCE, unsupported_metrics=["signal_family_ablation:x"],
                 gate_results={"a": True, "b": False})
    r = m.admit(reg, text(rep), RUN, POL, None, None, NOW, True)
    assert not r["allow"]
    for need in ("registry_state_not_admitting:DEMO_QUALIFIED", "report_verdict_not_passed:INSUFFICIENT_EVIDENCE",
                 "report_has_unsupported_metrics", "report_gate_not_true", "authorization_missing_or_incomplete"):
        assert need in r["reasons"], need

def test_registry_refusals():
    for bad in (None, {}, dict(REG, availability="missing"), dict(REG, availability="corrupt_or_unreadable")):
        assert "registry_unavailable" in run(reg=bad)["reasons"]
    for st in m.NEVER_ADMIT_STATES + ("DEMO_QUALIFIED", "MAINNET_EXPANDED"):
        assert not run(reg=dict(REG, state=st))["allow"]
    assert "registry_binding_mismatch" in run(reg=dict(REG, binding=dict(B, config_hash="other")))["reasons"]
    assert "registry_evidence_incomplete:missing_demo" in run(reg=dict(REG, missing_demo=["x"]))["reasons"]
    reg = dict(REG); del reg["missing_historical"]
    assert "registry_evidence_incomplete:missing_historical" in run(reg=reg)["reasons"]

def test_policy_refusals_no_defaults():
    assert not run(pol={})["allow"] and not run(pol=None)["allow"]
    assert not run(pol=dict(POL, max_report_age_s=0))["allow"]
    assert not run(pol=dict(POL, max_report_age_s=float("inf")))["allow"]
    assert not run(pol=dict(POL, allowed_states=[]))["allow"]
    assert not run(pol=dict(POL, allowed_states=["NOPE"]))["allow"]
    r = run(pol=dict(POL, allowed_states=["PAUSED", "MAINNET_CANARY"]))
    assert "policy_invalid:allowed_states_includes_non_admitting_state" in r["reasons"]

def test_report_refusals():
    for rep, need in ((report(verdict=q.NOT_QUALIFIED), "report_verdict_not_passed"),
                      (report(unsupported_metrics=["x"]), "report_has_unsupported_metrics"),
                      (report(unsupported_metrics=None), "report_has_unsupported_metrics"),
                      (report(gate_results={"a": True, "b": False}), "report_gate_not_true"),
                      (report(gate_results={}), "report_gate_not_true"),
                      (report(gate_results={"a": 1}), "report_gate_not_true"),
                      (report(context=dict(B, config_hash="x", evaluated_at=NOW - 1)), "report_context_binding_mismatch"),
                      (report(context=dict(B)), "report_age_unknown"),
                      (report(context=dict(B, evaluated_at=NOW - 7200)), "report_stale"),
                      (report(context=dict(B, evaluated_at=NOW + 5)), "report_dated_in_future")):
        r = run(t=text(rep))
        assert not r["allow"] and any(x.startswith(need) for x in r["reasons"]), need
    assert "report_not_strict_json" in run(t='{"verdict": NaN}')["reasons"]
    assert "report_not_strict_json" in run(t="not json")["reasons"]
    assert "report_missing" in run(t="  ")["reasons"]

def test_authorization_binding_and_digest():
    t = text()
    assert "authorization_report_digest_mismatch" in run(a=auth(t, report_sha256="0" * 64))["reasons"]
    other = text(report(reasons=["tampered"]))
    assert "authorization_report_digest_mismatch" in m.admit(REG, other, RUN, POL, auth(t), OK, NOW, True)["reasons"]
    for k, v in (("wallet", "W2"), ("network", "devnet"), ("trade_amount_usd", 50.0), ("mode", "expanded"),
                 ("risk_limits", {"max_dd": 0.5, "max_day": 0.03}), ("strategy_version", "v2"),
                 ("config_hash", "h2"), ("data_period", "p2")):
        assert ("authorization_mismatch:" + k) in run(a=auth(t, **{k: v}))["reasons"], k
    for k in m.AUTH_FIELDS:
        a = auth(t); del a[k]
        assert "authorization_missing_or_incomplete" in run(a=a)["reasons"], k
        a = auth(t); a[k] = None
        assert "authorization_missing_or_incomplete" in run(a=a)["reasons"], k

def test_authorization_time_window():
    assert "authorization_not_in_force" in run(a=auth(expires_at=NOW))["reasons"]       # expiry exclusive
    assert "authorization_not_in_force" in run(a=auth(issued_at=NOW + 1))["reasons"]
    assert "authorization_time_invalid" in run(a=auth(issued_at=float("nan")))["reasons"]
    assert run(a=auth(issued_at=NOW))["allow"]                                          # issued == now ok

def test_running_binding_and_runtime_facts():
    assert "running_binding_incomplete" in run(running={})["reasons"]
    r = dict(RUN); del r["wallet"]
    assert not run(running=r)["allow"]
    assert "running_invalid:trade_amount_usd" in run(running=dict(RUN, trade_amount_usd=0))["reasons"]
    assert "running_invalid:risk_limits" in run(running=dict(RUN, risk_limits={}))["reasons"]
    assert "running_invalid:risk_limits" in run(running=dict(RUN, risk_limits={"a": float("nan")}))["reasons"]
    assert "emergency_stop_not_confirmed" in run(es=False)["reasons"]
    assert "emergency_stop_not_confirmed" in run(es=1)["reasons"]
    assert "now_invalid" in run(now=None)["reasons"] and not run(now=float("inf"))["allow"]

def test_every_refusal_individually_flips_a_good_case():
    assert run()["allow"]
    flips = [dict(reg=dict(REG, state="PAUSED")), dict(authn=None), dict(es=False),
             dict(pol=dict(POL, max_report_age_s=1)), dict(running=dict(RUN, wallet="X"))]
    for f in flips:
        assert not run(**f)["allow"], f

def test_inputs_not_mutated_and_pure():
    reg, run_, pol, a = copy.deepcopy(REG), copy.deepcopy(RUN), copy.deepcopy(POL), auth()
    ac = copy.deepcopy(a)
    m.admit(reg, text(), run_, pol, a, OK, NOW, True)
    assert reg == REG and run_ == RUN and pol == POL and a == ac
    src = open(os.path.join(os.path.dirname(__file__), "..", "core", "qualification_admission.py")).read()
    for bad in ("open(", "import os", "socket", "subprocess", "urllib", "time.time", "environ"):
        assert bad not in src, bad


def test_duplicate_keys_rejected_at_any_depth():
    t = text()
    top = t[:-1] + ', "verdict": "NOT_QUALIFIED"}'          # last-wins would flip a PASSED
    assert t.endswith("}")
    r = m.admit(REG, top, RUN, POL, auth(top), OK, NOW, True)
    assert not r["allow"] and "report_not_strict_json" in r["reasons"]
    dup_gate = text().replace('"gate_results": {', '"gate_results": {"a": false, ', 1)
    assert dup_gate != text()
    r = m.admit(REG, dup_gate, RUN, POL, auth(dup_gate), OK, NOW, True)
    assert not r["allow"] and "report_not_strict_json" in r["reasons"]
    ctx = text().replace('"context": {', '"context": {"config_hash": "h1", ', 1)
    assert ctx != text()
    r = m.admit(REG, ctx, RUN, POL, auth(ctx), OK, NOW, True)
    assert "report_not_strict_json" in r["reasons"]


if __name__ == "__main__":
    # Non-vacuous stdlib runner: exits non-zero on any failure or if no test ran.
    import traceback
    ran = failed = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            ran += 1
            try:
                _f()
            except Exception:
                failed += 1
                print("FAIL", _n)
                traceback.print_exc()
    print("ran %d, failed %d" % (ran, failed))
    sys.exit(1 if failed or not ran else 0)
