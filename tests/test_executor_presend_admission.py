"""Pre-send admission re-check (proposed; pairs with executor-tip-presend-recheck.diff). NOT executed (source-only).
Offline stubs only. Static check binds the ORDER (a re-check precedes each network send call); direct checks bind the
helper's fail-closed behaviour. Reaching the pre-send point through execute_swap would need full quote/simulate/sign
fakes, so order is asserted from source instead of by driving a live-shaped swap."""
import ast, os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from broker import BrokerError
from broker.memecoin.execution import mainnet_executor as M
from tests._admission_base import Base

PROV = lambda: {'report_text': '', 'running': {}, 'policy': {}, 'authorization': {}, 'authenticate': None,
                'emergency_stop_ok': True}


class TestHelper(Base):
    def _armed(self, answers):
        self.write_state('MAINNET_CANARY')
        it = iter(answers)
        p = mock.patch.object(M, '_admit', lambda **kw: {'allow': next(it), 'reasons': []}); p.start(); self.addCleanup(p.stop)
        ex = M.MainnetExecutor(key_provider=object(), rpc=self.ex._rpc, jupiter=self.ex._jupiter, admission_provider=PROV)
        ex.connect(); ex.arm_live(); return ex

    def test_helper_passes_only_on_allow_and_armed_marker(self):
        ex = self._armed([True, True]); ex._require_live_send_admission()

    def test_helper_refuses_when_admission_flips(self):
        ex = self._armed([True, False])
        with self.assertRaises(BrokerError):
            ex._require_live_send_admission()

    def test_helper_refuses_when_marker_disarmed(self):
        ex = self._armed([True, True]); self.marker['armed'] = False
        with self.assertRaises(BrokerError):
            ex._require_live_send_admission()

    def test_helper_refuses_on_provider_exception(self):
        ex = self._armed([True])           # arm consumed one answer; the next next() raises StopIteration -> refuse
        with self.assertRaises(BrokerError):
            ex._require_live_send_admission()


class TestOrder(unittest.TestCase):
    def test_every_network_send_is_preceded_by_a_recheck(self):
        path = M.__file__
        tree = ast.parse(open(path).read())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'execute_swap')
        checks, sends = [], []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr == '_require_live_send_admission': checks.append(n.lineno)
                if n.func.attr in ('send_bundle', 'send_transaction'): sends.append(n.lineno)
        self.assertGreaterEqual(len(checks), 2, 'need top-of-call AND pre-send check')
        self.assertTrue(sends, 'no send call found: test is vacuous')
        for s in sends:
            self.assertTrue(any(c < s for c in checks[1:]) or max(checks) < s, 'send at %d has no preceding re-check' % s)
        self.assertLess(max(checks), min(sends), 'last re-check must precede the first network send')


if __name__ == '__main__':
    r = unittest.main(exit=False)
    sys.exit(1 if (r.result.testsRun == 0 or not r.result.wasSuccessful()) else 0)
