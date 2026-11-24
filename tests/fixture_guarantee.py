"""Test-only helper for the grad_select completeness-guarantee allowlist.

The production allowlist (grad_select._ACCEPTED_COMPLETENESS_GUARANTEES)
is empty: no source guarantee is held. Tests that simulate a working
authoritative-history source need a proof the validator admits. They use
ONE reserved fixture id, never added to the production set, and never
imported by daemon code (tests/test_guarantee_allowlist.py enforces it).

Isolation contract (reviewed):
  * pytest-collected modules / shared worker: use scoped_admit() (a
    try/finally context) or pytest monkeypatch per test. Never call
    admit_in_script_process() at import time in such a module.
  * plain script tests (own subprocess, see scripts/run_script_tests.py):
    admit_in_script_process() at import is process-contained.
"""
from contextlib import contextmanager

FIXTURE_GUARANTEE = "fixture-proof"


@contextmanager
def scoped_admit(gs):
    """Admit FIXTURE_GUARANTEE for the with-block, then restore exactly."""
    prev = gs._ACCEPTED_COMPLETENESS_GUARANTEES
    gs._ACCEPTED_COMPLETENESS_GUARANTEES = frozenset({FIXTURE_GUARANTEE})
    try:
        yield
    finally:
        gs._ACCEPTED_COMPLETENESS_GUARANTEES = prev


def admit_in_script_process(gs):
    """Script-style tests only: the process ends with the script."""
    gs._ACCEPTED_COMPLETENESS_GUARANTEES = frozenset({FIXTURE_GUARANTEE})


def fixture_proof(gs, creator, count, now, **kw):
    kw.setdefault("completeness_guarantee", FIXTURE_GUARANTEE)
    return gs.authoritative_proof(creator, count, now, **kw)
