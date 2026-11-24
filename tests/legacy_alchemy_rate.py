"""Test-only: restore the pre-parity Alchemy bucket (5/s, burst 10).

Router/workload tests exercise routing, fallback and accounting logic with
many back-to-back Alchemy calls and are not about pacing. Production now
paces Alchemy at one start per 3.02s (see request_scheduler); the value
production uses is pinned by tests/test_alchemy_account_pacing_parity.py.
Scripts call relax_in_script_process() (own process); pytest modules use
the monkeypatch-based fixture so nothing leaks across a shared worker.
"""
LEGACY = (5.0, 10.0)
LEGACY_CHAINSTACK = (2.0, 5.0)


def relax_in_script_process():
    from daemon.intelligence.acquisition import request_scheduler as S
    S.RATES["alchemy"] = LEGACY
    S.RATES["chainstack"] = LEGACY_CHAINSTACK


def relax_with(monkeypatch):
    from daemon.intelligence.acquisition import request_scheduler as S
    monkeypatch.setitem(S.RATES, "alchemy", LEGACY)
    monkeypatch.setitem(S.RATES, "chainstack", LEGACY_CHAINSTACK)
