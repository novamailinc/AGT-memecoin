"""SEAM tests: MainnetExecutor -> REAL core.qualification_admission.admit() (unpublished tip cbb38ec wiring).
Proposed new file; NOT executed (source-only review) - first execution is the producer's wrapper run.
test_executor_owner_admission.py mocks M._admit; these tests use the real verifier with one failing condition each.
Synthetic fixture values only: the report is a hand-built dict with verdict q.PASSED (unreachable from the real
evaluator by design), the authenticate callable is a test lambda, registry status is patched. NOTHING here is
owner authorization, a threshold, or evidence of qualification. Offline: stub signer/RPC/Jupiter, no network."""
import ast, copy, math, os, sys, unittest
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from broker import BrokerError
from broker.memecoin.execution import mainnet_executor as M
from core import qualification_admission as A
from core import qualification_evaluator as q
from core.qualification import Registry
from tests._admission_base import Base, Quote

SOL = 'So11111111111111111111111111111111111111112'
B = {'strategy_version': 'v1', 'config_hash': 'h1', 'data_period': 'p1'}
RUN = dict(B, wallet='W', network='mainnet-beta', trade_amount_usd=5.0, mode='canary',
           risk_limits={'max_dd': 0.1, 'max_day': 0.03})            # TEST-FIXTURE values, not owner limits
NOW = 1_000_000.0
POL = {'allowed_states': ['MAINNET_CANARY'], 'max_report_age_s': 3600}  # TEST-FIXTURE, not an owner policy
REG = dict(availability='ok', state='MAINNET_CANARY', binding=dict(B), missing_historical=[], missing_demo=[])


class Clock:
    def __init__(self, t): self.t = t
    def __getattr__(self, n): return getattr(__import__('time'), n)
    def time(self): return self.t


class Seam(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock(NOW)
        p = mock.patch.object(M, 'time', self.clock); p.start(); self.addCleanup(p.stop)
        self.reg = dict(REG)
        p = mock.patch.object(Registry, 'status', lambda s: dict(self.reg)); p.start(); self.addCleanup(p.stop)
        self.report = q.dumps_report({'verdict': q.PASSED, 'unsupported_metrics': [], 'gate_results': {'a': True},
                                      'context': dict(B, evaluated_at=NOW - 60), 'reasons': [], 'metrics': {}})
        self.auth = dict(B, report_sha256=A.report_sha256(self.report), wallet='W', network='mainnet-beta',
                         trade_amount_usd=5.0, risk_limits=dict(RUN['risk_limits']), mode='canary',
                         issued_at=NOW - 10, expires_at=NOW + 100)
        self.authn = lambda a: True
        self.estop = True
        self.running = copy.deepcopy(RUN)
        self.policy = dict(POL)

    def provider(self):
        return {'report_text': self.report, 'running': self.running, 'policy': self.policy,
                'authorization': self.auth, 'authenticate': self.authn, 'emergency_stop_ok': self.estop}

    def new_ex(self):
        ex = M.MainnetExecutor(key_provider=object(), rpc=self.ex._rpc, jupiter=self.ex._jupiter,
                               admission_provider=lambda: self.provider())
        ex.connect()
        return ex

    def refuses_arm(self):
        self.write_state('MAINNET_CANARY')
        ex = self.new_ex()
        with self.assertRaises(BrokerError):
            ex.arm_live()
        self.assertTrue(ex.dry_run)
        self.assertNotIn('quote', self.calls)


class TestRealVerifierThroughExecutor(Seam):
    def test_baseline_synthetic_inputs_arm_then_send_reaches_quote(self):
        # control: without this passing, the refusal tests below would prove nothing
        self.write_state('MAINNET_CANARY')
        ex = self.new_ex(); ex.arm_live()
        self.assertFalse(ex.dry_run)
        with self.assertRaises(Quote):
            ex.execute_swap(SOL, 'MintX', 1000)

    def test_digest_mismatch_refuses(self):
        self.auth['report_sha256'] = '0' * 64; self.refuses_arm()

    def test_expired_authorization_refuses(self):
        self.auth['expires_at'] = NOW - 1; self.refuses_arm()

    def test_authenticate_not_exactly_true_refuses(self):
        self.authn = lambda a: 1; self.refuses_arm()

    def test_emergency_stop_not_true_refuses(self):
        self.estop = 'yes'; self.refuses_arm()

    def test_running_binding_mismatch_refuses(self):
        self.running['config_hash'] = 'other'; self.refuses_arm()

    def test_registry_binding_mismatch_refuses(self):
        self.reg['binding'] = dict(B, data_period='other'); self.refuses_arm()

    def test_registry_missing_evidence_refuses(self):
        self.reg['missing_demo'] = ['x']; self.refuses_arm()


class TestRevocationAfterArming(Seam):
    def _armed(self):
        self.write_state('MAINNET_CANARY')
        ex = self.new_ex(); ex.arm_live(); self.calls.clear(); return ex

    def _blocked(self, ex):
        with self.assertRaises(BrokerError):
            ex.execute_swap(SOL, 'MintX', 1000)
        self.assertNotIn('quote', self.calls)

    def test_authorization_expiry_after_arming_blocks_next_send(self):
        ex = self._armed(); self.clock.t = NOW + 101; self._blocked(ex)

    def test_authenticator_turning_false_after_arming_blocks_next_send(self):
        ex = self._armed(); self.authn = lambda a: False; self._blocked(ex)

    def test_emergency_stop_after_arming_blocks_next_send(self):
        ex = self._armed(); self.estop = False; self._blocked(ex)

    def test_report_going_stale_after_arming_blocks_next_send(self):
        ex = self._armed(); self.clock.t = NOW + 3601 - 60 + 1
        self.auth['expires_at'] = NOW + 10 ** 6       # isolate staleness: authorization still in force
        self._blocked(ex)


class TestFixtureScopeGap(unittest.TestCase):
    def test_gap_guard_fixture_has_no_permanent_class_assignment(self):
        # GAP on tip cbb38ec: admit_registry_for_fixture assigns MainnetExecutor._require_owner_admission with no
        # restore, so one call leaks a no-op owner gate into every later test in the process. Expected RED until the
        # stub is monkeypatch/mock.patch scoped.
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_guard_fixture.py')
        bad = []
        for n in ast.walk(ast.parse(open(p).read())):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Attribute) and t.value.attr == 'MainnetExecutor':
                        bad.append(ast.unparse(n))
        self.assertEqual(bad, [], 'permanent executor class patch: %r' % bad)


if __name__ == '__main__':
    r = unittest.main(exit=False)
    sys.exit(1 if (r.result.testsRun == 0 or not r.result.wasSuccessful()) else 0)
