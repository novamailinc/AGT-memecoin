"""Strict live safety wiring in live_select._install_mandatory_gates (proposed; pairs with live_select-strict-safety.diff).
Pure: a fake engine object, no executor, no network, no signing. NOT executed (source-only); hand-traced.
SYNTHETIC reports. The policies below are TEST-ONLY, not owner choices."""
import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from broker import BrokerError
from daemon.launch_scanner import live_select as L
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)

def EV(**k):
    out = []
    for m in ("getAccountInfo", "getTokenLargestAccounts", "getMultipleAccounts"):
        out.append(dict({"method": m, "kind": "ok", "decision_usable": True, "degraded": False, "fallback": False, "attempts": []}, **k))
    return out

def rep(**o):
    r = {"passed": True, "unknown": False, "fails": [], "score": 1.0, "flags": [],
         "acquisition_evidence": EV(), "instant": {"token2022_extensions": [], "mint_authority": None, "freeze_authority": None, "mint_authority_status": "revoked", "freeze_authority_status": "revoked", "top1_user_pct": 1.0, "top10_user_pct": 5.0, "program_held_pct": 0.0}}
    r.update(o); return r

def eng(report, veto=(True, "ok")):
    return SimpleNamespace(_safety_ok=lambda m: (True, "", report, False), _veto_ok=lambda m, s, r: veto)

def run(report, policy=None):
    e = eng(report); L._install_mandatory_gates(e, policy); return e._safety_ok("MintX")

ok, why, r, tr = run(rep())
check("clean report with no flags and no extensions passes under the empty default", ok is True and why == "")
check("any flag refused when no prefix is accepted", run(rep(flags=["LP lock unverified (instant path)"]))[0] is False)
check("flag accepted only by an explicit prefix", run(rep(flags=["LP lock unverified (instant path)"]), {"accepted_flag_prefixes": ["LP lock unverified"]})[0] is True)
check("incomplete-evidence flag refused even with a matching prefix", run(rep(flags=["Token-2022 extension undecoded"]), {"accepted_flag_prefixes": ["Token-2022"]})[0] is False)
check("extension refused unless in verified_extensions", run(rep(instant={"token2022_extensions": ["transferHook"], "mint_authority": None, "freeze_authority": None}))[0] is False)
check("extension accepted when owner lists it", run(rep(instant={"token2022_extensions": ["metadataPointer"], "mint_authority_status": "revoked", "freeze_authority_status": "revoked"}), {"verified_extensions": ["metadataPointer"]})[0] is True)
check("transfer fee epoch_selection_required refused", run(rep(instant={"token2022_extensions": [], "transfer_fee": {"status": "epoch_selection_required"}}))[0] is False)
check("report without extension evidence refused by default", run(rep(instant=None))[0] is False)
check("require_extension_evidence=False cannot waive the typed authority check", run(rep(instant=None), {"require_extension_evidence": False})[0] is False)
check("dossier-source report without instant block refused by default", run(rep(instant=None, source="dossier:instant-onchain"))[0] is False)
check("unknown report refused", run(rep(unknown=True))[0] is False)
check("not passed refused", run(rep(passed=False, fails=["x"]))[0] is False)
check("safety exception refuses and is transient", (lambda t: t[0] is False and t[3] is True)(
    (lambda e: (L._install_mandatory_gates(e), e._safety_ok("m"))[1])(SimpleNamespace(_safety_ok=lambda m: (_ for _ in ()).throw(RuntimeError("x")), _veto_ok=lambda m, s, r: (True, "ok")))))
check("no-op shaped safety result refused (no report)", (lambda e: (L._install_mandatory_gates(e), e._safety_ok("m"))[1][0] is False)(
    SimpleNamespace(_safety_ok=lambda m: (True, "", None, False), _veto_ok=lambda m, s, r: (True, "ok"))))
e = eng(rep(), veto=(True, "veto unavailable: x")); L._install_mandatory_gates(e)
check("veto fail-closed is kept", e._veto_ok("m", "s", rep())[0] is False)
def _raises():
    try:
        L._install_mandatory_gates(SimpleNamespace(_safety_ok=None, _veto_ok=None)); return False
    except BrokerError:
        return True
check("missing gates still refused", _raises())
check("malformed policy arg is treated as empty (strictest), not as accept-all", run(rep(flags=["a"]), "accept all")[0] is False)

def _inst(m="revoked", f="revoked"):
    return {"token2022_extensions": [], "mint_authority_status": m, "freeze_authority_status": f}
check("typed status revoked on both passes", run(rep(instant=_inst()))[0] is True)
check("mint status set refused", run(rep(instant=_inst(m="set")))[0] is False)
check("freeze status unavailable refused", run(rep(instant=_inst(f="unavailable")))[0] is False)
check("missing typed status refused", run(rep(instant={"token2022_extensions": []}))[0] is False)
check("malformed typed status refused", run(rep(instant=_inst(m=True)))[0] is False)
check("opt-out of extension evidence does not waive typed status", run(rep(instant={"token2022_extensions": []}), {"require_extension_evidence": False})[0] is False)

def _r(ev):
    r = rep(instant=_inst()); r["acquisition_evidence"] = ev; return r
check("usable evidence passes", run(_r(EV()))[0] is True)
_fb = EV(); _fb[0]["fallback"] = True; _fb[0]["degraded"] = True; _fb[0]["attempts"] = [{"provider": "helius", "kind": "timeout"}, {"provider": "alchemy", "kind": "ok"}]
check("failed attempt then usable fallback is NOT refused", run(_r(_fb))[0] is True)
check("degraded True alone is not a refusal", run(_r([dict(e, degraded=True) for e in EV()]))[0] is True)
check("missing evidence refused", run(_r(None))[0] is False)
check("empty evidence refused", run(_r([]))[0] is False)
check("non-list evidence refused", run(_r("ok"))[0] is False)
check("non-dict entry refused", run(_r(["x"]))[0] is False)
_bad = EV(); _bad[1]["decision_usable"] = False
check("entry decision_usable False refused", run(_r(_bad))[0] is False)
_bad = EV(); _bad[1]["decision_usable"] = "True"
check("entry decision_usable non-bool refused", run(_r(_bad))[0] is False)
_bad = EV(); _bad[2]["kind"] = "timeout"
check("entry kind not ok refused", run(_r(_bad))[0] is False)
check("missing a required read refused", run(_r(EV()[:2]))[0] is False)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
