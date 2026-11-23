import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from core.qualification import Registry, HISTORY_CRITERIA, DEMO_CRITERIA, STATES


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.r = Registry(self.temp.name)
        self.b = dict(strategy_version='v1', config_hash='sha256:fixture',
                      data_period='fixture:start/end')

    def init(self):
        return self.r.initialize(self.b)

    def record_all(self, criteria):
        for criterion in criteria:
            self.r.record(criterion, True, 'fixture:'+criterion,
                          'fixture evidence only, not measured qualification', self.b)

    def test_missing_not_development_or_qualified(self):
        s = self.r.status()
        self.assertEqual(s['availability'], 'missing')
        self.assertFalse(s['qualified'])
        self.assertNotIn('state', s)

    def test_initialize_no_reset_and_no_implicit_qualification(self):
        s = self.init()
        self.assertEqual(s['state'], 'DEVELOPMENT')
        self.assertFalse(s['qualified'])
        self.assertEqual(s['missing_demo'], list(DEMO_CRITERIA))
        with self.assertRaises(ValueError): self.init()
        with self.assertRaises(ValueError): self.r.initialize({})

    def test_corrupt_state_never_overwritten(self):
        self.r.path.parent.mkdir(parents=True, exist_ok=True)
        self.r.path.write_text('{torn')
        self.assertEqual(self.r.status()['availability'], 'corrupt_or_unreadable')
        with self.assertRaises(ValueError): self.init()
        with self.assertRaises(ValueError): self.r.transition('PAUSED', 'test')
        self.assertEqual(self.r.path.read_text(), '{torn')

    def test_malformed_binding_is_unavailable(self):
        self.init()
        d = json.loads(self.r.path.read_text()); d['binding'] = []
        self.r.path.write_text(json.dumps(d))
        self.assertFalse(self.r.status()['qualified'])
        with self.assertRaises(ValueError): self.r.record('trade_sample', True, 'x', 'y', self.b)

    def test_no_jump_or_mainnet_transition(self):
        self.init()
        for state in ('DEMO_QUALIFIED', *STATES[4:8]):
            with self.assertRaises(ValueError): self.r.transition(state, 'test')
        self.assertEqual(self.r.status()['state'], 'DEVELOPMENT')

    def test_historical_prerequisites_fail_closed(self):
        self.init()
        self.r.transition('HISTORICAL_VALIDATION', 'offline acceptance')
        with self.assertRaises(ValueError): self.r.transition('DEMO_QUALIFICATION', 'test')
        self.record_all(HISTORY_CRITERIA)
        s = self.r.transition('DEMO_QUALIFICATION', 'recorded fixture checks')
        self.assertFalse(s['qualified'])
        self.assertFalse(s['execution_authorized'])

    def test_wrong_identity_false_unknown_empty_source_refused(self):
        self.init()
        with self.assertRaises(ValueError):
            self.r.record('trade_sample', True, 'source', 'reason', dict(self.b, strategy_version='v2'))
        for val in (None, 'true', 1):
            with self.assertRaises(ValueError): self.r.record('trade_sample', val, 's', 'r', self.b)
        with self.assertRaises(ValueError): self.r.record('trade_sample', True, '', 'r', self.b)
        self.r.record('trade_sample', False, 's', 'failed sample', self.b)
        self.assertIn('trade_sample', self.r.status()['missing_demo'])

    def test_fixture_qualification_explicit_only_and_revocable(self):
        self.init()
        self.record_all(HISTORY_CRITERIA + DEMO_CRITERIA)
        self.assertEqual(self.r.status()['state'], 'DEVELOPMENT')
        for target in ('HISTORICAL_VALIDATION', 'DEMO_QUALIFICATION', 'DEMO_QUALIFIED'):
            self.r.transition(target, 'fixture only')
        s = self.r.status()
        self.assertTrue(s['qualified'])
        self.assertFalse(s['execution_authorized'])
        self.assertEqual(s['evidence_provenance'], 'caller_recorded_not_independently_verified')
        self.r.record('data_integrity', False, 'fixture:corruption', 'unresolved gap', self.b)
        self.assertEqual(self.r.status()['state'], 'PAUSED')
        self.assertFalse(self.r.status()['qualified'])

    def test_corrupt_evidence_cannot_qualify(self):
        self.init(); self.record_all(HISTORY_CRITERIA + DEMO_CRITERIA)
        d = json.loads(self.r.path.read_text())
        d['state'] = 'DEMO_QUALIFIED'; d['evidence']['drawdown'] = 'pass'
        self.r.path.write_text(json.dumps(d))
        self.assertFalse(self.r.status()['qualified'])
        self.assertIn('drawdown', self.r.status()['missing_demo'])

    def test_concurrent_records_preserve_all_and_audit(self):
        self.init()
        errors=[]
        def write(c):
            try: self.r.record(c, True, 'fixture:'+c, 'fixture', self.b)
            except Exception as exc: errors.append(str(exc))
        threads=[threading.Thread(target=write,args=(c,)) for c in DEMO_CRITERIA]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(set(self.r.status()['evidence']), set(DEMO_CRITERIA))
        self.assertEqual(len(self.r.status()['audit']), 1 + len(DEMO_CRITERIA))

    def test_fresh_process_cli_and_resume_no_automatic_graduation(self):
        self.init(); self.r.transition('EMERGENCY_STOP', 'fixture stop')
        result=subprocess.run([sys.executable, '-m', 'core.qualification',
                               '--home', self.temp.name, 'status'],
                              text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)['state'], 'EMERGENCY_STOP')
        self.r.transition('HISTORICAL_VALIDATION', 'explicit restart review')
        self.assertFalse(self.r.status()['qualified'])


if __name__=='__main__': unittest.main()
