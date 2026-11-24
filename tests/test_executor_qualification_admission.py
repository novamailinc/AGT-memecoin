"""Live arming/sending is admitted ONLY by the qualification registry (fail closed): a deployment
marker alone never arms; a registry that is missing/unreadable/not in a mainnet-admitted state
refuses arm_live; and a registry change AFTER arming (pause/revocation) blocks the send path.
All dependencies inert. The registry JSON written below is a TEST FIXTURE for the admitted state;
the offline registry itself cannot enter mainnet states, and no real approval mechanism exists."""
import pytest
from broker import BrokerError
from tests.test_executor_constructor_admission import inert  # noqa: F401
from core.qualification import Registry

BIND = {'strategy_version': 'v', 'config_hash': 'h', 'data_period': 'p'}


def _write_state(home, state):
    r = Registry(home)
    r._write({'v': 1, 'state': state, 'binding': BIND, 'evidence': {}, 'audit': []})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('FOREX_AGENT_HOME', str(tmp_path))
    return str(tmp_path)


def _armed(inert):
    marker, calls, make = inert
    marker['armed'] = True
    ex = make()
    ex.connect()
    return ex, calls


def test_marker_armed_but_registry_missing_refuses(inert, home):
    ex, calls = _armed(inert)
    with pytest.raises(BrokerError):
        ex.arm_live()
    assert ex.dry_run is True


@pytest.mark.parametrize('state', ['DEVELOPMENT', 'HISTORICAL_VALIDATION', 'DEMO_QUALIFICATION',
                                   'DEMO_QUALIFIED', 'MAINNET_READY_PENDING_AUTHORIZATION',
                                   'PAUSED', 'EMERGENCY_STOP'])
def test_non_admitted_states_refuse_arming(inert, home, state):
    _write_state(home, state)
    ex, calls = _armed(inert)
    with pytest.raises(BrokerError):
        ex.arm_live()
    assert ex.dry_run is True


def test_corrupt_registry_refuses(inert, home):
    (Registry(home).path).write_text('{not json')
    ex, calls = _armed(inert)
    with pytest.raises(BrokerError):
        ex.arm_live()
    assert ex.dry_run is True


def test_revocation_after_arming_blocks_the_send_path_before_any_quote(inert, home, monkeypatch):
    # DISCLOSED 2026-11-24: replaces ONLY the owner-admission step (no authorization artifact exists); the
    # registry-label revocation check under test stays real. Owner step is covered in test_executor_owner_admission.
    from broker.memecoin.execution import mainnet_executor as _M
    monkeypatch.setattr(_M.MainnetExecutor, '_require_owner_admission', lambda self, st: None)
    _write_state(home, 'MAINNET_CANARY')            # fixture: admitted state
    ex, calls = _armed(inert)
    ex.arm_live()
    assert ex.dry_run is False
    _write_state(home, 'PAUSED')                     # revoked after arming
    with pytest.raises(BrokerError):
        ex.execute_swap('So11111111111111111111111111111111111111112', 'MintX', 1000)
    assert 'quote' not in calls
