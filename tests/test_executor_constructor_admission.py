"""Constructor selection cannot replace explicit arming. All dependencies inert."""
from types import SimpleNamespace
import pytest
from broker import BrokerError
from broker.memecoin.execution import mainnet_executor as M


class QuoteBoundary(Exception):
    pass


@pytest.fixture
def inert(monkeypatch):
    marker = {'machine': 'in-memory-test-only', 'armed': False}
    calls = []

    class Signer:
        def __init__(self, *args, **kwargs):
            calls.append('signer-created')

        def pubkey_str(self):
            return 'INERT-NOT-A-WALLET'

        def keypair(self):
            raise AssertionError('key/sign boundary must never be reached')

    class Provider:
        def get_key(self, *args):
            raise AssertionError('no key may be read')

    class Jupiter:
        def get_quote(self, *args, **kwargs):
            calls.append('quote')
            raise QuoteBoundary()

    monkeypatch.setattr(M.signer_mod, 'DevnetSigner', Signer)
    monkeypatch.setattr(M, 'read_deployment_marker', lambda *args: dict(marker))
    rpc = SimpleNamespace(get_health=lambda: 'ok', get_slot=lambda: 1)

    def make(**kwargs):
        return M.MainnetExecutor(key_provider=Provider(), rpc=rpc,
                                 jupiter=Jupiter(), **kwargs)

    return marker, calls, make


@pytest.mark.parametrize('value', [False, None, 0, 1, '', 'false', 'true', [], {}])
def test_constructor_refuses_non_true_dry_run_before_dependency_use(inert, value):
    marker, calls, make = inert
    with pytest.raises(BrokerError):
        make(dry_run=value)
    assert calls == []


def test_default_and_explicit_true_require_arm_live(inert):
    marker, calls, make = inert
    for kwargs in ({}, {'dry_run': True}):
        ex = make(**kwargs)
        ex.connect()
        assert ex.dry_run is True
        with pytest.raises(BrokerError):
            ex.arm_live()
        assert ex.dry_run is True
        ex.disarm()
        assert ex.dry_run is True
    assert 'quote' not in calls


@pytest.mark.parametrize('value', [False, None, 0, 1, 'true', 'yes'])
def test_marker_truthiness_does_not_arm(inert, value):
    marker, calls, make = inert
    marker['armed'] = value
    ex = make()
    ex.connect()
    with pytest.raises(BrokerError):
        ex.arm_live()
    assert ex.dry_run is True
    assert 'quote' not in calls


def test_arm_live_refuses_without_registry_admission_even_with_armed_marker(inert):
    # the registry admission gate is real (not stubbed) here: the marker alone never arms
    marker, calls, make = inert
    ex = make()
    ex.connect()
    marker['armed'] = True
    with pytest.raises(BrokerError):
        ex.arm_live()
    assert ex.dry_run is True
    assert 'quote' not in calls


def test_explicit_arm_and_disarm_remain_unchanged_at_inert_boundary(inert, monkeypatch):
    # DISCLOSED FIXTURE EDIT: this test checks the arm/disarm MECHANICS at the inert boundary. The registry admission
    # step (which now gates arming, directive 11/25) is stubbed for this test only; its refusal is tested above.
    monkeypatch.setattr(M.MainnetExecutor, '_require_admission', lambda self: None)
    marker, calls, make = inert
    ex = make()
    ex.connect()
    marker['armed'] = True
    ex.arm_live()
    assert ex.dry_run is False
    with pytest.raises(QuoteBoundary):
        ex.execute_swap(M.SOL_MINT, 'INERT-OUTPUT', 1)
    assert calls.count('quote') == 1
    ex.disarm()
    assert ex.dry_run is True
