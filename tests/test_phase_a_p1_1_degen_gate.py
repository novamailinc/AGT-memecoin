#!/usr/bin/env python3
"""Phase A regression (P1-1, 2026-11-22): LaunchScanner._paper_enter
must consult the killswitch gate when the daemon runs as
`python3 -m daemon.launch_scanner` — i.e. with repo-root-only
sys.path, where the bare `import killswitch` raised
ModuleNotFoundError and fail-closed EVERY degen paper entry (live
log: KILLSWITCH GATE ERROR, 2026-11-21 21:02 + 21:56). The gate now
uses the package-qualified import with bare fallback (the grad.py
two-step pattern). No network, no ledger writes past the gate:
both cases return before the entry path.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


def main():
    # Reproduce the daemon's import conditions: repo root on
    # sys.path, daemon/launch_scanner NOT on sys.path, no bare
    # 'killswitch' module loaded.
    ls_dir = os.path.abspath(os.path.join(REPO, "daemon",
                                          "launch_scanner"))
    sys.path = [p for p in sys.path
                if os.path.abspath(p or ".") != ls_dir]
    sys.modules.pop("killswitch", None)
    try:
        import killswitch  # noqa: F401
        raise AssertionError(
            "bare 'import killswitch' works here — setup does not "
            "reproduce the daemon (P1-1 would pass vacuously)")
    except ModuleNotFoundError:
        pass
    check("setup reproduces daemon: bare import killswitch fails",
          True)

    from daemon.launch_scanner import LaunchScanner
    import daemon.launch_scanner.killswitch as ks_mod
    import daemon.launch_scanner.veto as veto_mod

    calls = []
    real_gate = ks_mod.gate
    real_veto = veto_mod.veto_check

    def _enter(gate_value, veto_value=None):
        def _gate(lane):
            calls.append(lane)
            return gate_value
        ks_mod.gate = _gate
        if veto_value is not None:
            veto_mod.veto_check = lambda *a, **k: veto_value
        card = {}
        try:
            result = LaunchScanner._paper_enter(
                object(), None, None, {}, card, None,
                "TestMint1111111111111111111111111111111111pump",
                "TEST")
        finally:
            ks_mod.gate = real_gate
            veto_mod.veto_check = real_veto
        return result, card

    # Case A: gate halts -> HELD note, no error note.
    calls.clear()
    result, card = _enter(False)
    check("gate halted: entry refused", result is False)
    check("gate halted: gate consulted for 'main'",
          calls == ["main"])
    check("gate halted: note says held, not gate error",
          "main lane halted" in card.get("note", ""))
    check("gate halted: no KILLSWITCH GATE ERROR path",
          "gate error" not in card.get("note", ""))

    # Case B: gate passes, veto rejects -> veto note, gate consulted.
    calls.clear()
    result, card = _enter(True, {"passed": False,
                                 "vetoes": ["test-veto"]})
    check("gate passed: entry refused by veto", result is False)
    check("gate passed: gate consulted for 'main'",
          calls == ["main"])
    check("gate passed: note is a veto, not a gate error",
          "veto: test-veto" in card.get("note", ""))

    print("ALL %d CHECKS PASSED" % _PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
