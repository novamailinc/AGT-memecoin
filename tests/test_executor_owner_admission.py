"""Runtime wiring of core.qualification_admission.admit() into MainnetExecutor arming and sending.
Offline/inert (stub signer/RPC/Jupiter, temp registry home). The registry file here is written ONLY to model
an admitted-LABEL file; nothing authorizes anything. admit() itself is covered by test_qualification_admission;
the one mock below replaces admit() to prove the executor honours ITS answer and passes ITS inputs through."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from broker import BrokerError
from broker.memecoin.execution import mainnet_executor as M
from tests._admission_base import Base, Quote

SOL = 'So11111111111111111111111111111111111111112'


class T(Base):
    def _ex(self, provider):
        ex = M.MainnetExecutor(key_provider=self.ex._signer and object(), rpc=self.ex._rpc, jupiter=self.ex._jupiter,
                               admission_provider=provider) if provider is not None else self.ex
        ex.connect()
        return ex

    def test_label_alone_with_no_provider_refuses(self):
        self.write_state('MAINNET_CANARY')
        with self.assertRaises(BrokerError):
            self.ex.arm_live()
        self.assertTrue(self.ex.dry_run)

    def test_provider_raising_or_returning_garbage_refuses(self):
        self.write_state('MAINNET_CANARY')
        for prov in (lambda: (_ for _ in ()).throw(RuntimeError('x')), lambda: None, lambda: 'allow', lambda: {}):
            ex = self._ex(prov)
            with self.assertRaises(BrokerError):
                ex.arm_live()
            self.assertTrue(ex.dry_run)

    def test_verifier_refusal_is_honoured_and_inputs_passed_through(self):
        self.write_state('MAINNET_CANARY')
        inputs = {'report_text': 'R', 'running': {'a': 1}, 'policy': {'p': 1}, 'authorization': {'z': 1},
                  'authenticate': None, 'emergency_stop_ok': True}
        seen = {}
        def fake_admit(**kw):
            seen.update(kw); return {'allow': False, 'reasons': ['nope']}
        with mock.patch.object(M, '_admit', fake_admit):
            ex = self._ex(lambda: dict(inputs))
            with self.assertRaises(BrokerError):
                ex.arm_live()
        self.assertTrue(ex.dry_run)
        for k, v in inputs.items():
            self.assertEqual(seen[k], v)
        self.assertIn('registry_status', seen); self.assertIsInstance(seen['now'], float)

    def test_only_verifier_allow_true_arms_and_each_send_rechecks(self):
        self.write_state('MAINNET_CANARY')
        answers = [True, True, False]
        def fake_admit(**kw): return {'allow': answers.pop(0), 'reasons': []}
        with mock.patch.object(M, '_admit', fake_admit):
            ex = self._ex(lambda: {'report_text': '', 'running': {}, 'policy': {}, 'authorization': {},
                                   'authenticate': None, 'emergency_stop_ok': True})
            ex.arm_live()
            self.assertFalse(ex.dry_run)
            with self.assertRaises(Quote):                 # 2nd answer True: passes admission, reaches quote stub
                ex.execute_swap(SOL, 'MintX', 1000)
            self.calls.clear()
            with self.assertRaises(BrokerError):           # 3rd answer False: blocked BEFORE any quote
                ex.execute_swap(SOL, 'MintX', 1000)
        self.assertNotIn('quote', self.calls)

    def test_truthy_but_not_true_allow_refuses(self):
        self.write_state('MAINNET_CANARY')
        for bad in (1, 'yes', [1], {'x': 1}):
            with mock.patch.object(M, '_admit', lambda **kw: {'allow': bad, 'reasons': []}):
                ex = self._ex(lambda: {'report_text': '', 'running': {}, 'policy': {}, 'authorization': {},
                                       'authenticate': None, 'emergency_stop_ok': True})
                with self.assertRaises(BrokerError):
                    ex.arm_live()
            self.assertTrue(ex.dry_run)

    def test_disarming_marker_after_arming_blocks_next_send(self):
        self.write_state('MAINNET_CANARY')
        with mock.patch.object(M, '_admit', lambda **kw: {'allow': True, 'reasons': []}):
            ex = self._ex(lambda: {'report_text': '', 'running': {}, 'policy': {}, 'authorization': {},
                                   'authenticate': None, 'emergency_stop_ok': True})
            ex.arm_live()
            self.marker['armed'] = False
            self.calls.clear()
            with self.assertRaises(BrokerError):
                ex.execute_swap(SOL, 'MintX', 1000)
        self.assertNotIn('quote', self.calls)


if __name__ == '__main__':
    r = unittest.main(exit=False)
    n = r.result.testsRun
    sys.exit(1 if (n == 0 or not r.result.wasSuccessful()) else 0)
