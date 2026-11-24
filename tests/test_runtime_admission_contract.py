"""Runtime-binding CONTRACT tests for live admission (directive 22: no unqualified lane can be armed).
New file, unittest style (runs under pytest too). Offline/inert: stub signer/RPC/Jupiter, temp registry home,
no network. Each test states the property the integrated system must have. Tests marked GAP are expected
RED on a tree that only checks the registry state label; they are the acceptance bar for wiring
core.qualification_admission.admit() into arm_live and the send path. Fixtures write registry JSON directly
ONLY to model a forged/edited local file; nothing here authorizes anything."""
import ast, os, tempfile, unittest
from types import SimpleNamespace
from unittest import mock

from broker import BrokerError
from broker.memecoin.execution import mainnet_executor as M
from core.qualification import Registry

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
BIND = {'strategy_version': 'v', 'config_hash': 'h', 'data_period': 'p'}
from tests._admission_base import Base, Quote  # split out so other admission tests keep their shared base

class TestAdmission(Base):
    def test_gap_forged_registry_label_alone_must_not_arm(self):
        # GAP: the registry file is editable by whoever can write the host; a bare mainnet label with no
        # evidence, no evaluator report and no owner authorization must still refuse.
        self.write_state('MAINNET_CANARY', evidence={})
        with self.assertRaises(BrokerError):
            self.ex.arm_live()
        self.assertTrue(self.ex.dry_run)

    def test_gap_label_with_all_evidence_still_needs_owner_authorization(self):
        from core import qualification as Q
        ev = {k: {'passed': True, 'source': 's', 'justification': 'j', 'binding': BIND}
              for k in Q.HISTORY_CRITERIA + Q.DEMO_CRITERIA}
        self.write_state('MAINNET_CANARY', evidence=ev)
        with self.assertRaises(BrokerError):
            self.ex.arm_live()
        self.assertTrue(self.ex.dry_run)

    def test_missing_or_corrupt_registry_refuses(self):
        with self.assertRaises(BrokerError):
            self.ex.arm_live()
        open(Registry(self.home).path, 'w').write('{bad')
        with self.assertRaises(BrokerError):
            self.ex.arm_live()
        self.assertTrue(self.ex.dry_run)

    def test_gap_disarming_the_marker_after_arming_blocks_the_next_send(self):
        # GAP (Fix B): send path must re-check "armed", not only marker existence.
        self.write_state('MAINNET_CANARY')
        with mock.patch.object(M.MainnetExecutor, '_require_admission', lambda s: None):
            self.ex.arm_live()
            self.assertFalse(self.ex.dry_run)
            self.marker['armed'] = False
            with self.assertRaises(BrokerError):
                self.ex.execute_swap('So11111111111111111111111111111111111111112', 'MintX', 1000)
        self.assertNotIn('quote', self.calls)

    def test_revocation_after_arming_blocks_send_before_quote(self):
        self.write_state('MAINNET_CANARY')
        with mock.patch.object(M.MainnetExecutor, '_require_admission', lambda s: None):
            self.ex.arm_live()
        self.write_state('PAUSED')
        with self.assertRaises(BrokerError):
            self.ex.execute_swap('So11111111111111111111111111111111111111112', 'MintX', 1000)
        self.assertNotIn('quote', self.calls)

    def test_admission_exception_fails_closed(self):
        with mock.patch.object(Registry, 'status', side_effect=RuntimeError('x')):
            with self.assertRaises(BrokerError):
                self.ex.arm_live()
        self.assertTrue(self.ex.dry_run)


def _assigns(path, attrs):
    tree = ast.parse(open(path).read())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and t.attr in attrs:
                    out.append((t.attr, ast.unparse(n.value)))
    return out


class TestStaticBypass(unittest.TestCase):
    def test_gap_no_duplicate_executor_copy_outside_broker(self):
        dups = []
        for d, _, fs in os.walk(ROOT):
            if '.git' in d or os.sep + 'tests' in d:
                continue
            if 'mainnet_executor.py' in fs and 'broker' + os.sep + 'memecoin' not in d:
                dups.append(os.path.relpath(d, ROOT))
        self.assertEqual(dups, [], 'duplicate mainnet_executor trees: %r' % dups)

    def test_gap_veto_unknown_evidence_must_not_fail_open(self):
        p = os.path.join(ROOT, 'daemon', 'launch_scanner', 'veto.py')
        if not os.path.exists(p):
            self.skipTest('veto.py not at expected path')
        src = open(p).read().lower()
        self.assertNotIn('fail open', src, 'veto.py documents/implements fail-open')

    def test_only_executor_sends_on_mainnet(self):
        # any non-test module calling send_bundle/send_transaction other than the executor + its rpc clients
        allowed = {'mainnet_executor.py', 'devnet_executor.py', 'rpc.py', 'jito.py'}
        hits = []
        for d, _, fs in os.walk(ROOT):
            if '.git' in d or os.sep + 'tests' in d or os.sep + 'tools' in d:
                continue
            for f in fs:
                if f.endswith('.py') and f not in allowed:
                    s = open(os.path.join(d, f), errors='ignore').read()
                    if '.send_bundle(' in s or '.send_transaction(' in s:
                        hits.append(os.path.relpath(os.path.join(d, f), ROOT))
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
