"""Actual second wallet source, shared parser and scanner handoff failures."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from daemon.launch_scanner import smartmoney,smartgrade,LaunchScanner
from daemon.launch_scanner.wallet_intake import WalletIntake
from tests.test_wallet_intake import transaction

ROOT=Path(__file__).resolve().parents[1]


class TestScannerWalletIntake(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.state={}
        self.tx=transaction()
        self.tx['transaction']['message']['accountKeys']=[{'pubkey':k} for k in ['W','TA1','TA2']]
        self.fail=False
        self.patches=[patch.object(smartmoney,'_HOME',self.home),
            patch.object(smartgrade,'_GRADES_FILE',self.home/'grades.json'),
            patch.object(smartmoney,'api_key',return_value='fixture'),
            patch.object(smartmoney,'load_registry',return_value={'W':{}}),
            patch.object(smartmoney,'_rpc',side_effect=self.rpc),patch.object(smartmoney.time,'sleep')]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.intake=smartmoney._intake()

    def rpc(self,method,params):
        if method=='getSignaturesForAddress':return [{'signature':'new'}]
        return None if self.fail else self.tx

    def poll(self):return smartmoney.poll_wallets(self.state,poll_seconds=0)

    def test_failed_detail_restart_handoff_ack_requires_scanner_commit(self):
        self.fail=True;self.assertEqual(self.poll(),{})
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])
        self.state={};self.fail=False
        self.assertEqual(self.poll(),{'Mpump':['W']})
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])
        # Failed downstream state persistence must not accept the outbox.
        store=Mock();store.journal_add.side_effect=OSError('state fixture')
        with self.assertRaises(OSError):LaunchScanner._commit_smart_wallet_activity(SimpleNamespace(name='launch_scanner'),store,self.state)
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])
        self.state={}
        self.assertEqual(self.poll(),{'Mpump':['W']})
        from storage import Store
        store=Store(str(self.home/'state.db'))
        try:LaunchScanner._commit_smart_wallet_activity(SimpleNamespace(name='launch_scanner'),store,self.state)
        finally:store.close()
        self.assertEqual(self.intake.wallet('W')['pending'],[])
        self.assertEqual(self.poll(),{})

    def test_failed_unknown_duplicate_owner_signature_remain_pending(self):
        for kind in ('failed','unknown','duplicate','owner','signature'):
            tx=transaction()
            if kind=='failed':tx['meta']['err']={'InstructionError':[0,'fixture']}
            if kind=='unknown':del tx['meta']['err']
            if kind=='duplicate':tx['meta']['postTokenBalances']*=2
            if kind=='owner':del tx['meta']['postTokenBalances'][0]['owner']
            if kind=='signature':tx['transaction']['signatures']=['other']
            self.tx=tx;self.assertEqual(self.poll(),{})
            self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_valid_no_buy_handoff_can_commit_without_false_activity(self):
        self.tx['meta']['postTokenBalances']=[]
        self.assertEqual(self.poll(),{})
        self.assertEqual(self.state['smart_delivery_refs'],{'W':['new']})
        smartmoney.acknowledge_wallet_activity(self.state)
        self.assertEqual(self.intake.wallet('W')['pending'],[])

    def test_grade_write_failure_propagates_from_actual_fasttrack(self):
        self.poll()
        config=SimpleNamespace(degen=SimpleNamespace(smart_fasttrack_enabled=False))
        with patch.object(smartgrade,'save_grades',return_value=False):
            with self.assertRaises(OSError):LaunchScanner._smart_fasttrack(SimpleNamespace(),config,self.state,{'Mpump':['W']})
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_partial_fasttrack_state_cannot_leak_through_finally_save(self):
        fresh=self.poll();self.state['seen']={}
        scanner=LaunchScanner.__new__(LaunchScanner)
        def partial(*args):
            self.state['smart_fasttracked']={'Mpump':True}
            raise OSError('partial fasttrack')
        with patch.object(scanner,'_smart_fasttrack',side_effect=partial):
            with self.assertRaises(OSError):scanner._handoff_smart_wallet_activity(None,Mock(),self.state,fresh)
        self.assertNotIn('smart_fasttracked',self.state)
        self.assertEqual(self.state['smart_delivery_refs'],{})
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_throttle_clears_stale_ack_refs_and_malformed_discovery_retries(self):
        self.fail=True;self.poll();self.fail=False
        with patch.object(smartmoney,'_rpc',side_effect=lambda m,p: [{'bad':'page'}] if m=='getSignaturesForAddress' else self.tx):
            self.assertEqual(self.poll(),{'Mpump':['W']})
        self.assertEqual(smartmoney.poll_wallets(self.state,poll_seconds=180),{})
        self.assertEqual(self.state['smart_delivery_refs'],{})
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_unverified_state_readback_does_not_ack(self):
        self.poll();store=Mock();store.journal_query.return_value=[]
        with self.assertRaises(OSError):LaunchScanner._commit_smart_wallet_activity(SimpleNamespace(name='launch_scanner'),store,self.state)
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_internal_transfer_not_buy_and_no_suffix_filter_added(self):
        tx=transaction()
        tx['meta']['preTokenBalances']=copy.deepcopy(tx['meta']['postTokenBalances'])
        tx['meta']['postTokenBalances'][0]['accountIndex']=2
        with patch.object(smartmoney,'_rpc',return_value=tx):
            self.assertEqual(smartmoney._bought_mints('W','new'),set())
        tx['meta']['preTokenBalances']=[]
        tx['meta']['postTokenBalances'][0]['mint']='not-suffix-filtered'
        with patch.object(smartmoney,'_rpc',return_value=tx):
            self.assertEqual(smartmoney._bought_mints('W','new'),{'not-suffix-filtered'})

    def test_crash_after_state_commit_reconciles_without_replaying_grade_or_pack(self):
        self.poll()
        from storage import Store
        from daemon.common import get_daemon_state
        store=Store(str(self.home/'state.db'))
        try:
            with patch.object(smartmoney,'acknowledge_wallet_activity',side_effect=OSError('ack crash')):
                with self.assertRaises(OSError):LaunchScanner._commit_smart_wallet_activity(SimpleNamespace(name='launch_scanner'),store,self.state)
            self.state=get_daemon_state(store,'launch_scanner')
            self.assertEqual(self.intake.wallet('W')['pending'],['new'])
            self.assertEqual(self.poll(),{})
            self.assertEqual(self.intake.wallet('W')['pending'],[])
        finally:store.close()

    def test_actual_scanner_cycle_poll_to_grade_commit_and_no_duplicate_pack(self):
        from contextlib import ExitStack
        from storage import Store
        from daemon.common import get_daemon_state
        import daemon.launch_scanner as scanner_module
        from daemon.launch_scanner import packdetect
        scanner=LaunchScanner.__new__(LaunchScanner);scanner._stop=True
        config=SimpleNamespace(degen=SimpleNamespace(enabled=True,smart_fasttrack_enabled=False))
        store=Store(str(self.home/'cycle.db'))
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(scanner_module,'load_config',return_value=config))
                stack.enter_context(patch.object(scanner_module,'get_store',return_value=store))
                stack.enter_context(patch.object(scanner,'_fresh_adapter',return_value=object()))
                for method in ('_ensure_logstream','_ensure_pumpportal','_manage_exits','_retry_pending','_drain_logstream','_drain_pumpportal'):
                    stack.enter_context(patch.object(scanner,method))
                for module_name in ('measure_store','watch_store'):
                    module=__import__('daemon.launch_scanner.'+module_name,fromlist=['restore'])
                    stack.enter_context(patch.object(module,'restore'))
                scanner.run_once()
                committed=get_daemon_state(store,'launch_scanner')
                self.assertEqual(committed['smart_activity'],{'Mpump':['W']})
                self.assertEqual(len(committed[packdetect.EVENTS_KEY]),1)
                self.assertEqual(self.intake.wallet('W')['pending'],[])
                scanner.run_once()
                after=get_daemon_state(store,'launch_scanner')
                self.assertEqual(len(after[packdetect.EVENTS_KEY]),1)
                self.assertTrue(smartgrade.load_grades())
        finally:store.close()

    def test_state_load_failure_stops_before_any_scanner_activation_or_ack(self):
        import daemon.launch_scanner as scanner_module
        from daemon.common import _StateReadFailed
        self.poll();scanner=LaunchScanner.__new__(LaunchScanner)
        with patch.object(scanner_module,'load_config',return_value=SimpleNamespace(degen=SimpleNamespace(enabled=True))),patch.object(scanner_module,'get_store',return_value=Mock()),patch.object(scanner_module,'get_daemon_state',return_value=_StateReadFailed()),patch.object(scanner,'_ensure_logstream') as launch:
            scanner.run_once()
            launch.assert_not_called()
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_corrupt_journal_not_rebaseline(self):
        self.poll();before=self.intake.path
        before.write_text('{')
        with self.assertRaises(ValueError):self.poll()
        self.assertEqual(before.read_text(),'{')

    def test_fresh_interpreter_returns_unaccepted_delivery(self):
        self.poll()
        code="""
from pathlib import Path
from unittest.mock import patch
import os
from daemon.launch_scanner import smartmoney
with patch.object(smartmoney,'_HOME',Path(os.environ['SCANNER_TEST_HOME'])),patch.object(smartmoney,'api_key',return_value='fixture'),patch.object(smartmoney,'load_registry',return_value={'W':{}}),patch.object(smartmoney,'_rpc',return_value=[]):
    state={}
    assert smartmoney.poll_wallets(state,poll_seconds=0)=={'Mpump':['W']}
    assert state['smart_delivery_refs']=={'W':['new']}
"""
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=dict(os.environ,SCANNER_TEST_HOME=self.tmp.name),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
