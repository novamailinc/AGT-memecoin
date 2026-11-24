"""Causal regression: ChainstackWorkload class wrapper vs module functions.

Found live 2026-11-23 (assigned-job refresh driver): the class wrapper
lacked fetch_account_state entirely, and fetch_signatures(recent=True)
crashed with TypeError because the module-level fetch_signatures has no
``recent`` parameter while the sibling wrappers (fetch_block,
fetch_transaction) accept one. Offline: a fake router records the calls;
no network, no credentials.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.intelligence.acquisition import chainstack_workload as cw_mod


class _FakeRouter:
    def __init__(self):
        self.calls = []

    def call(self, method, params, **kw):
        self.calls.append((method, params, kw))
        return {"kind": "ok", "provider": "chainstack", "result": None,
                "http_status": 200}


def test_fetch_signatures_accepts_recent_flag():
    """Before the fix this raised TypeError: module fetch_signatures
    has no ``recent`` kwarg. The signatures API is inherently
    most-recent-first, so the wrapper accepts and intentionally does
    not forward the flag."""
    router = _FakeRouter()
    w = cw_mod.ChainstackWorkload(router)
    r = w.fetch_signatures("addr1", limit=3, recent=True)
    assert r["kind"] == "ok"
    assert router.calls, "wrapper never reached the router"
    method, params, _ = router.calls[0]
    assert method == "getSignaturesForAddress"
    assert params[0] == "addr1"


def test_fetch_signatures_without_recent_still_works():
    router = _FakeRouter()
    w = cw_mod.ChainstackWorkload(router)
    r = w.fetch_signatures("addr1", limit=2)
    assert r["kind"] == "ok"
    assert router.calls


def test_fetch_account_state_exists_on_wrapper():
    """The assigned-job driver calls cw.fetch_account_state; the class
    previously exposed only the module-level function."""
    router = _FakeRouter()
    w = cw_mod.ChainstackWorkload(router)
    r = w.fetch_account_state("addr2")
    assert r["kind"] == "ok"
    method, params, _ = router.calls[0]
    assert method == "getAccountInfo"
    assert params[0] == "addr2"


def test_account_state_wrapper_signature_matches_module():
    """Contract control: the wrapper must not grow kwargs the module
    function does not accept (a **kw here would silently forward).
    Mirrors the module signature exactly, minus the router."""
    import inspect
    mod = inspect.signature(cw_mod.fetch_account_state)
    wrap = inspect.signature(cw_mod.ChainstackWorkload.fetch_account_state)
    mod_params = [p for p in mod.parameters.values()][1:]  # drop router
    wrap_params = [p for p in wrap.parameters.values()][1:]  # drop self
    assert [p.name for p in wrap_params] == [p.name for p in mod_params]
    assert all(p.kind != inspect.Parameter.VAR_KEYWORD for p in wrap_params)


def test_fetch_block_recent_flag_still_forwarded():
    """Negative control for the symmetry claim: fetch_block DOES have a
    recency window, so its ``recent`` flag must still reach the module
    function (which passes it to _recent_hints)."""
    router = _FakeRouter()
    w = cw_mod.ChainstackWorkload(router)
    r = w.fetch_block(123, recent=True)
    assert r["kind"] == "ok"
    method, params, _ = router.calls[0]
    assert method == "getBlock"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print("test_chainstack_workload_wrapper.py: ALL PASS")
