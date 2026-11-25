#!/usr/bin/env python3
"""The LIVE (real-money) lane keeps mandatory, fail-closed token-safety and veto checks.

Original owner directive sections 11 and 25: every proposed trade passes enforceable token-safety / evidence checks,
and the bot never opens positions when critical intelligence is unreliable. The raw no-gate stubs belong to the
experimental research lanes only. Plain script, offline: the safety and veto backends are replaced by local stubs.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="live_gates_")
os.environ["FOREX_AGENT_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests._guard_fixture as _GF
_GF.block_network()

from broker import BrokerError
from broker.memecoin.execution.mainnet_executor import MainnetExecutor
from daemon.launch_scanner import grad_raw as _raw
from daemon.launch_scanner import live_select

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


class _Exec(MainnetExecutor):
    def __init__(self):
        self._dry_run = True
        self._wallet_pubkey = "testwallet11111111111111111111111111"


def _boom(*a, **k):
    raise RuntimeError("backend down")


class _Ns:
    pass


def main():
    eng = live_select.make_live_engine(_TMP, _Exec())
    M = "GateMint1111111111111111111111111111pump"

    # -- the raw stubs are not installed ------------------------------
    check("live engine does not use the no-gate safety stub", eng._safety_ok is not _raw._no_safety_gate)
    check("live engine does not use the no-op veto stub", eng._veto_ok is not _raw._no_veto)
    eng._dossiers = None  # no birth dossier: the gate rides on the live check

    # -- safety is fail closed ------------------------------------------
    s = _Ns()
    eng._safety = s
    s.check_token = _boom
    ok, why, rep, transient = eng._safety_ok(M)
    check("safety backend error refuses the entry", ok is False and transient is True)
    s.check_token = lambda chain, mint, timeout=10: {"unknown": True}
    ok, why, rep, transient = eng._safety_ok(M)
    check("unknown safety data is not a pass", ok is False and "unknown" in why)
    s.check_token = lambda chain, mint, timeout=10: {"passed": False, "fails": ["mint authority live"]}
    ok, why, rep, transient = eng._safety_ok(M)
    check("a failed safety report refuses the entry", ok is False and "mint authority" in why)
    # EMITTER-SHAPED synthetic reports (test-only, NOT recorded data). Shape follows daemon/launch_scanner/instant_safety._check_impl and check():
    # top-level passed/score/flags/fails/unknown/source/topHolders, an "instant" block, then check() adds acquisition_evidence, acquisition_degraded
    # and verdict_cached. score is a finite int exactly as the emitter computes it (100 - 25*fails - 5*flags). The live gate reads: unknown, passed,
    # fails, score, flags, instant.token2022_extensions, instant.transfer_fee, instant.{mint,freeze}_authority_status and acquisition_evidence.
    import copy
    _EVD = [{"method": m, "kind": "ok", "decision_usable": True, "attempts": []}
            for m in ("getAccountInfo", "getTokenLargestAccounts", "getMultipleAccounts")]

    def _emit(**over):
        r = {"passed": True, "score": 100, "flags": [], "fails": [], "unknown": False, "source": "instant-onchain", "topHolders": [],
             "instant": {"token2022_extensions": [], "token_program_id": "Tokenkeg", "transfer_fee": None, "mint_authority": None,
                         "freeze_authority": None, "mint_authority_status": "revoked", "freeze_authority_status": "revoked",
                         "as_of": 0.0, "top1_user_pct": 1.0, "top10_user_pct": 5.0, "program_held_pct": 0.0, "holder_rows": [],
                         "supply_raw": 1000, "epoch_info": None},
             "acquisition_evidence": copy.deepcopy(_EVD), "acquisition_degraded": False, "verdict_cached": False}
        r.update(over)
        return r

    def _run(rep):
        s.check_token = lambda chain, mint, timeout=10: copy.deepcopy(rep)
        return eng._safety_ok(M)

    ok, why, rep, transient = _run(_emit())
    check("an emitter-shaped passing report with typed revoked authorities and evidence is the only pass", ok is True and why == "" and transient is False)
    r = _emit(); del r["score"]
    ok, why, rep, transient = _run(r)
    check("REGRESSION: a scoreless report refuses on the score, not incidentally (not transient)", ok is False and "score" in why and transient is False)
    for label, bad in (("NaN", float("nan")), ("inf", float("inf")), ("bool", True), ("string", "100"), ("None", None)):
        ok, why, rep, transient = _run(_emit(score=bad))
        check("score %s refuses on the score" % label, ok is False and "score" in why)
    r = _emit(); del r["instant"]
    ok, why, rep, transient = _run(r)
    check("a report with a finite score but no instant block refuses for missing extension evidence", ok is False and "extension evidence" in why)
    ok, why, rep, transient = _run(_emit(instant={"token2022_extensions": []}))
    check("an instant block without typed authority statuses refuses on the authority status", ok is False and "mint_authority_status" in why)
    r = _emit(); r["instant"]["mint_authority_status"] = "set"
    ok, why, rep, transient = _run(r)
    check("a set mint authority is refused on mint_authority_status", ok is False and "mint_authority_status" in why)
    r = _emit(); r["instant"]["freeze_authority_status"] = "unavailable"
    ok, why, rep, transient = _run(r)
    check("an unavailable freeze authority is refused on freeze_authority_status", ok is False and "freeze_authority_status" in why)
    ok, why, rep, transient = _run(_emit(flags=["LP lock unverified (instant path)"], score=95))
    check("an unaccepted flag is refused on the flag with the empty default policy", ok is False and ("flag" in why))
    r = _emit(); r["instant"]["token2022_extensions"] = ["transferFeeConfig"]
    ok, why, rep, transient = _run(r)
    check("a Token-2022 extension outside the verified set is refused on the extension", ok is False and "extension" in why)
    r = _emit(); r.pop("acquisition_evidence")
    ok, why, rep, transient = _run(r)
    check("missing acquisition evidence refuses on the evidence", ok is False and "acquisition evidence" in why)
    r = _emit(); r["acquisition_evidence"][0]["decision_usable"] = False
    ok, why, rep, transient = _run(r)
    check("an unusable evidence entry refuses on the evidence", ok is False and "evidence" in why)
    r = _emit(); r["acquisition_evidence"] = r["acquisition_evidence"][:2]
    ok, why, rep, transient = _run(r)
    check("a missing required read refuses on the evidence", ok is False and "required read" in why)
    ok, why, rep, transient = _run({"passed": True, "fails": []})
    check("a bare passed/fails report is refused (unknown not False)", ok is False and "unknown" in why)

    # -- veto is fail closed (grad._veto_ok alone fails OPEN on an engine exception) ----
    v = _Ns()
    eng._veto = v
    v.veto_check = _boom
    ok, why = eng._veto_ok(M, "SYM", {"passed": True})
    check("veto engine exception refuses the entry (fail closed)", ok is False and "fail closed" in why)
    v.veto_check = lambda mint, chain, **k: {"passed": False, "vetoes": ["wash pattern"]}
    ok, why = eng._veto_ok(M, "SYM", {"passed": True})
    check("a veto refuses the entry", ok is False and "wash" in why)
    v.veto_check = lambda mint, chain, **k: {"passed": True, "vetoes": []}
    ok, why = eng._veto_ok(M, "SYM", {"passed": True})
    check("no veto passes", ok is True)

    # -- construction refuses stub gates ---------------------------------
    class _Bad:
        _safety_ok = staticmethod(_raw._no_safety_gate)
        _veto_ok = staticmethod(_raw._no_veto)
    refused = False
    try:
        live_select._install_mandatory_gates(_Bad)
    except BrokerError:
        refused = True
    check("installing the no-op stubs on the live engine is refused", refused)

    # -- the lane says it is not paper equivalent ---------------------------
    terms = live_select._preregistration()["terms"]
    check("preregistration: paper_equivalent is False", terms.get("paper_equivalent") is False)
    check("preregistration: safety gate is not NONE", not str(terms.get("safety_gate")).startswith("NONE"))
    check("preregistration: veto is not NONE", not str(terms.get("veto")).startswith("NONE"))
    print("ALL LIVE GATE TESTS PASSED (%d checks)" % _PASSED)


if __name__ == "__main__":
    main()
