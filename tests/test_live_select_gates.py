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
    # SYNTHETIC report shapes (test-only, not recorded data). The live lane now also requires typed authority status.
    _SYN = {"token2022_extensions": [], "mint_authority_status": "revoked", "freeze_authority_status": "revoked"}
    _EVD = [{"method": m, "kind": "ok", "decision_usable": True, "attempts": []}
            for m in ("getAccountInfo", "getTokenLargestAccounts", "getMultipleAccounts")]
    s.check_token = lambda chain, mint, timeout=10: {"passed": True, "fails": [], "flags": [], "unknown": False,
                                                    "acquisition_evidence": _EVD, "instant": dict(_SYN)}
    ok, why, rep, transient = eng._safety_ok(M)
    check("a passing safety report with typed revoked authorities is the only pass", ok is True)
    s.check_token = lambda chain, mint, timeout=10: {"passed": True, "fails": []}
    ok, why, rep, transient = eng._safety_ok(M)
    check("a passing report without typed authority evidence is refused", ok is False)
    s.check_token = lambda chain, mint, timeout=10: {"passed": True, "fails": [], "flags": [],
                                                    "unknown": False, "acquisition_evidence": _EVD, "instant": dict(_SYN, mint_authority_status="set")}
    ok, why, rep, transient = eng._safety_ok(M)
    check("a set mint authority is refused", ok is False and "mint_authority_status" in why)
    s.check_token = lambda chain, mint, timeout=10: {"passed": True, "fails": [], "flags": [],
                                                    "unknown": False, "acquisition_evidence": _EVD, "instant": dict(_SYN, freeze_authority_status="unavailable")}
    ok, why, rep, transient = eng._safety_ok(M)
    check("an unavailable freeze authority is refused", ok is False and "freeze_authority_status" in why)
    s.check_token = lambda chain, mint, timeout=10: {"passed": True, "fails": [], "flags": ["LP lock unverified (instant path)"],
                                                    "unknown": False, "acquisition_evidence": _EVD, "instant": dict(_SYN)}
    ok, why, rep, transient = eng._safety_ok(M)
    check("an unaccepted flag is refused with the empty default policy", ok is False)

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
