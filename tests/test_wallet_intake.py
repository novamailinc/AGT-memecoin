"""Offline production poll seam: stage, failed detail, durable retry, ack."""
import copy
import importlib.machinery
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sw=importlib.machinery.SourceFileLoader('smart_durable_intake',str(ROOT/'scripts/smart-watcher')).load_module()
from daemon.launch_scanner.wallet_intake import WalletIntake
import smartwatch_filters as filters


def transaction(signature='new'):
    return {'blockTime':int(time.time()),'transaction':{'signatures':[signature],
            'message':{'accountKeys':['W','TA1','TA2']}},
            'meta':{'err':None,'preBalances':[2_000_000_000],
                    'postBalances':[1_000_000_000],'preTokenBalances':[],
                    'postTokenBalances':[{'accountIndex':1,'owner':'W','mint':'Mpump',
                                          'uiTokenAmount':{'uiAmount':10}}]}}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.intake=WalletIntake(self.tmp.name)
        self.intake.stage('W',None,'old',[])
        self.seen=set()
        self.page=[{'signature':'new'},{'signature':'old'}]
        self.patches=[patch.object(sw,'SEEN_FILE',str(Path(self.tmp.name)/'seen.json')),
                      patch.object(filters,'_LEDGER_FILE',Path(self.tmp.name)/'convergence.json'),
                      patch.object(sw,'enrich',return_value=None)]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def poll(self,tx=None,kind='ok',page=None):
        with patch.object(sw,'rpc',return_value=self.page if page is None else page), \
             patch.object(sw,'_shared_rpc_transport') as transport:
            transport.return_value.rpc_typed.return_value=(tx,kind)
            sw.poll_wallet('W',None,{},self.seen,self.intake)
            return transport.return_value.rpc_typed.call_count

    def test_error_then_restart_success_records_one_real_sighting(self):
        self.poll(kind='error')
        self.assertEqual(self.intake.wallet('W')['cursor'],'new')
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])
        self.intake=WalletIntake(self.tmp.name)
        self.poll(transaction())
        self.assertEqual(self.intake.wallet('W')['pending'],[])
        ledger=json.loads((Path(self.tmp.name)/'convergence.json').read_text())
        self.assertEqual(len(ledger['Mpump']['sightings']),1)
        self.assertEqual(self.poll(transaction()),0)

    def test_null_and_malformed_stay_pending(self):
        for tx,kind in ((None,'null'),({},'ok'),({'meta':[]},'ok'),({'meta':{'err':None},'transaction':[]},'ok')):
            self.poll(tx,kind)
            self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_invalid_amount_or_owner_stays_pending_not_successful_empty(self):
        for field,value in (('owner',None),('uiTokenAmount',{'uiAmount':float('nan')})):
            tx=transaction();tx['meta']['postTokenBalances'][0][field]=value
            self.poll(tx)
            self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_wrong_response_signature_stays_pending(self):
        self.poll(transaction('other'))
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_valid_no_buy_acknowledged(self):
        tx=transaction();tx['meta']['postTokenBalances']=[]
        self.assertEqual(self.poll(tx),1)
        self.assertEqual(self.intake.wallet('W')['pending'],[])

    def test_failed_chain_transaction_acknowledged_no_sighting(self):
        tx=transaction();tx['meta']['err']={'InstructionError':[0,'fixture']}
        self.poll(tx)
        self.assertEqual(self.intake.wallet('W')['pending'],[])
        self.assertFalse((Path(self.tmp.name)/'convergence.json').exists())

    def test_disk_failure_does_not_advance_cursor_or_fetch_details(self):
        with patch.object(self.intake,'_save',side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):self.poll(transaction())
        self.assertEqual(self.intake.wallet('W')['cursor'],'old')
        self.assertEqual(self.intake.wallet('W')['pending'],[])

    def test_convergence_save_failure_retains_pending(self):
        with patch.object(sw,'save_convergence',return_value=False):
            with self.assertRaises(OSError):self.poll(transaction())
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_crash_after_sighting_before_ack_replay_deduplicates_wallet(self):
        with patch.object(self.intake,'acknowledge',side_effect=RuntimeError('crash fixture')):
            with self.assertRaises(RuntimeError):self.poll(transaction())
        self.intake=WalletIntake(self.tmp.name)
        self.poll(transaction())
        ledger=json.loads((Path(self.tmp.name)/'convergence.json').read_text())
        self.assertEqual(len(ledger['Mpump']['sightings']),1)

    def test_corrupt_journal_is_unavailable_not_rebaseline(self):
        self.intake.path.write_text('{')
        with self.assertRaises(ValueError):self.poll(transaction())
        self.assertEqual(self.intake.path.read_text(),'{')

    def test_compare_and_stage_refuses_stale_writer(self):
        self.intake.stage('W','old','new',['new'])
        with self.assertRaises(ValueError):self.intake.stage('W','old','older',['older'])
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])

    def test_page_failure_does_not_advance_cursor(self):
        with patch.object(sw,'SIG_LIMIT',1),patch.object(sw,'rpc',side_effect=[[{'signature':'new'}],None]):
            sw.poll_wallet('W',None,{},self.seen,self.intake)
        self.assertEqual(self.intake.wallet('W')['cursor'],'old')

    def test_overflow_records_gap_and_queues_observed_history_oldest_first(self):
        with patch.object(sw,'SIG_LIMIT',1),patch.object(sw,'PAGEBACK_MAX',0):
            self.poll(kind='error',page=[{'signature':'new'}])
        state=self.intake.wallet('W')
        self.assertEqual(state['pending'],['new'])
        self.assertEqual(state['coverage_gap']['reason'],'pageback_limit_incomplete_history')

    def test_pending_retried_even_signature_endpoint_unavailable(self):
        self.intake.stage('W','old','new',['new'])
        self.poll(transaction(),page=[])
        self.assertEqual(self.intake.wallet('W')['pending'],[])

    def test_fresh_interpreter_retries_saved_pending_at_production_seam(self):
        self.poll(kind='error')
        code = """
import importlib.machinery,json,os
from pathlib import Path
from unittest.mock import patch
from daemon.launch_scanner.wallet_intake import WalletIntake
from tests.test_wallet_intake import transaction
sw=importlib.machinery.SourceFileLoader('restart_wallet_intake','scripts/smart-watcher').load_module()
import smartwatch_filters as filters
home=Path(os.environ['INTAKE_TEST_HOME'])
with patch.object(sw,'rpc',return_value=[]),patch.object(sw,'_shared_rpc_transport') as transport,patch.object(sw,'SEEN_FILE',str(home/'seen.json')),patch.object(sw,'enrich',return_value=None),patch.object(filters,'_LEDGER_FILE',home/'convergence.json'):
    transport.return_value.rpc_typed.return_value=(transaction(),'ok')
    intake=WalletIntake(home)
    sw.poll_wallet('W',None,{},set(),intake)
    assert intake.wallet('W')['pending']==[]
    assert len(json.loads((home/'convergence.json').read_text())['Mpump']['sightings'])==1
"""
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,
                              env=dict(os.environ,INTAKE_TEST_HOME=self.tmp.name),
                              text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.intake.wallet('W')['pending'],[])

    def test_poison_detail_rotation_cannot_starve_later_signature(self):
        self.intake.stage('W','old','new',['poison','new'])
        with patch.object(sw,'SIG_LIMIT',1),patch.object(sw,'PAGEBACK_MAX',0):
            self.poll(kind='null',page=[])
            self.assertEqual(self.intake.wallet('W')['pending'],['new','poison'])
            self.poll(transaction(),page=[])
        self.assertEqual(self.intake.wallet('W')['pending'],['poison'])

    def test_malformed_failed_status_is_not_terminal(self):
        tx=transaction();tx['meta']['err']=False
        self.poll(tx)
        self.assertEqual(self.intake.wallet('W')['pending'],['new'])


    def test_malformed_signature_page_cannot_starve_existing_pending(self):
        for page in ([None],[{'signature':'later'},None],{'unexpected':'shape'}):
            home=WalletIntake(self.tmp.name)
            old=home.wallet('W')['cursor']
            home.stage('W',old,'new',['new'])
            self.poll(transaction(),page=page)
            self.assertEqual(home.wallet('W')['pending'],[])
            self.assertEqual(home.wallet('W')['cursor'],'new')

    def test_real_fire_seen_commits_only_after_convergence_persistence(self):
        # A successful production _attempt_fire mutates only proposed seen.
        # Failed save must force the NEXT poll through the handler again.
        with patch.object(sw,'eval_convergence',return_value=(2,True)), \
             patch.object(sw,'try_fire_converged_card',return_value='fired') as fire, \
             patch.object(sw,'save_convergence',return_value=False):
            with self.assertRaises(OSError):self.poll(transaction())
            self.assertNotIn('Mpump',self.seen)
            self.assertEqual(self.intake.wallet('W')['pending'],['new'])
            with self.assertRaises(OSError):self.poll(transaction())
            self.assertEqual(fire.call_count,2)
            self.assertNotIn('Mpump',self.seen)
        with patch.object(sw,'eval_convergence',return_value=(2,True)), \
             patch.object(sw,'try_fire_converged_card',return_value='fired'):
            self.poll(transaction())
        self.assertIn('Mpump',self.seen)
        self.assertEqual(self.intake.wallet('W')['pending'],[])


    def test_bus_false_or_exception_cannot_claim_fire_success(self):
        import signal_bus
        info={'symbol':'FIX','price':1,'mcap':100000,'liq':50000,'vol':10000}
        with patch.object(sw,'_HAS_CARDS',False),patch.object(sw,'_CARDS_ENABLED',False):
            for behavior in (False, OSError('bus disk fixture')):
                with patch.object(signal_bus,'publish') as publish:
                    if isinstance(behavior,Exception):publish.side_effect=behavior
                    else:publish.return_value=behavior
                    self.assertIs(sw.fire_card('Mpump',info,'fixture',2,'W'),False)



if __name__=='__main__':unittest.main()
