"""Causal wallet decreases and exact-identity durable sell history."""
import copy
import importlib.machinery
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_wallet_intake import transaction

ROOT=Path(__file__).resolve().parents[1]
sw=importlib.machinery.SourceFileLoader('smart_sell_evidence',str(ROOT/'scripts/smart-watcher')).load_module()
import smartgrade


class TestWalletSellEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'grades.json'
        p=patch.object(smartgrade,'_GRADES_FILE',self.path);p.start();self.addCleanup(p.stop)
        self.tx=transaction()
        self.tx['meta']['preTokenBalances']=copy.deepcopy(self.tx['meta']['postTokenBalances'])
        self.tx['meta']['preTokenBalances'][0]['uiTokenAmount']['uiAmount']=10
        self.tx['meta']['postTokenBalances'][0]['uiTokenAmount']['uiAmount']=2

    def event(self):
        return sw.get_sold_mints('W','new',None,tx=self.tx,details=True)

    def test_closed_account_and_successful_decrease(self):
        self.assertEqual(self.event()[0]['sold_fraction'],.8)
        self.tx['meta']['postTokenBalances']=[]
        self.assertEqual(self.event()[0]['sold_fraction'],1)

    def test_internal_transfer_is_not_a_sell(self):
        self.tx['meta']['postTokenBalances'].append({'accountIndex':2,'owner':'W',
            'mint':'Mpump','uiTokenAmount':{'uiAmount':8}})
        self.assertEqual(self.event(),[])

    def test_invalid_unknown_failed_signature_no_sell(self):
        for modify in ('failed','owner','duplicate','signature'):
            tx=copy.deepcopy(self.tx)
            if modify=='failed':tx['meta']['err']={'InstructionError':[0,'fixture']}
            if modify=='owner':del tx['meta']['preTokenBalances'][0]['owner']
            if modify=='duplicate':tx['meta']['postTokenBalances']*=2
            if modify=='signature':tx['transaction']['signatures']=['other']
            self.assertEqual(sw.get_sold_mints('W','new',None,tx=tx,details=True),[])

    def test_exact_identity_and_replay_never_marks_another_wallet_buy(self):
        wallet='abcdefgh-W';other='abcdefgh-OTHER'
        original={other:{'buys':[{'mint':'Mpump','buy_ts':1}]},
                  wallet:{'buys':[{'mint':'Mpump','buy_ts':2}]}}
        self.path.write_text(json.dumps(original))
        events=self.event()
        self.assertTrue(smartgrade.record_wallet_sells(wallet,events))
        self.assertTrue(smartgrade.record_wallet_sells(wallet,events))
        result=json.loads(self.path.read_text())
        self.assertEqual(result[other],original[other])
        self.assertEqual(result[wallet]['buys'],original[wallet]['buys'])
        self.assertEqual(len(result[wallet]['sell_events']),1)
        event=result[wallet]['sell_events'][0]
        self.assertEqual(event['block_ts'],self.tx['blockTime'])
        self.assertNotIn('hold_sec',event)
        self.assertEqual(event['holding_period_status'],'unavailable_no_causal_buy_match')

    def test_conflicting_replay_cannot_rewrite_evidence(self):
        events=self.event();self.assertTrue(smartgrade.record_wallet_sells('W',events))
        before=self.path.read_bytes();events[0]['sold_fraction']=.9
        self.assertFalse(smartgrade.record_wallet_sells('W',events))
        self.assertEqual(self.path.read_bytes(),before)

    def test_corruption_and_disk_failure_not_acknowledged(self):
        self.path.write_text('{')
        self.assertFalse(smartgrade.record_wallet_sells('W',self.event()))
        self.assertEqual(self.path.read_text(),'{')
        self.path.unlink()
        with patch.object(smartgrade.os,'replace',side_effect=OSError('disk fixture')):
            self.assertFalse(smartgrade.record_wallet_sells('W',self.event()))
        self.assertFalse(self.path.exists())

    def test_invalid_values_refused_before_writing(self):
        for key,value in (('sold_fraction',True),('sold_fraction',float('nan')),('block_ts',float('inf'))):
            event=self.event()[0];event[key]=value
            self.assertFalse(smartgrade.record_wallet_sells('W',[event]))
        self.assertFalse(self.path.exists())

    def test_poll_sell_save_failure_retains_pending_then_replay_records_once(self):
        from daemon.launch_scanner.wallet_intake import WalletIntake
        intake=WalletIntake(self.tmp.name);intake.stage('W',None,'old',[])
        with patch.object(sw,'rpc',return_value=[{'signature':'new'},{'signature':'old'}]), \
             patch.object(sw,'_shared_rpc_transport') as transport, \
             patch.object(sw,'SEEN_FILE',str(Path(self.tmp.name)/'seen.json')):
            transport.return_value.rpc_typed.return_value=(self.tx,'ok')
            with patch.object(smartgrade,'save_grades',return_value=False):
                with self.assertRaises(OSError):sw.poll_wallet('W',None,{},set(),intake)
            self.assertEqual(intake.wallet('W')['pending'],['new'])
            sw.poll_wallet('W',None,{},set(),intake)
            self.assertEqual(intake.wallet('W')['pending'],[])
        self.assertEqual(len(json.loads(self.path.read_text())['W']['sell_events']),1)

    def test_concurrent_buy_and_sell_writers_preserve_both(self):
        code="""
import os,time
from pathlib import Path
from daemon.launch_scanner import smartgrade
while not Path(os.environ['GRADE_BARRIER']).exists():time.sleep(.005)
if os.environ['GRADE_KIND']=='buy':
    smartgrade.record_wallet_buys({'Mpump':['W']})
else:
    assert smartgrade.record_wallet_sells('W',[{'mint':'Mpump','signature':os.environ['GRADE_KIND'],
      'sold_fraction':.8,'block_ts':123,'provenance':'successful_transaction_balance_decrease'}])
"""
        barrier=Path(self.tmp.name)/'ready'
        workers=[subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,
            env=dict(os.environ,FOREX_AGENT_HOME=self.tmp.name,GRADE_BARRIER=str(barrier),GRADE_KIND=kind),
            text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for kind in ['buy']+[str(i) for i in range(8)]]
        barrier.touch()
        try:
            for worker in workers:
                out,err=worker.communicate(timeout=20)
                self.assertEqual(worker.returncode,0,out+err)
        finally:
            for worker in workers:
                if worker.poll() is None:worker.kill();worker.wait()
        rec=json.loads((Path(self.tmp.name)/'launch_scanner/smart_wallet_grades.json').read_text())['W']
        self.assertEqual(len(rec['buys']),1)
        self.assertEqual(len(rec['sell_events']),8)

    def test_buy_and_reconcile_cannot_report_count_after_failed_save(self):
        with patch.object(smartgrade,'save_grades',return_value=False):
            with self.assertRaises(OSError):smartgrade.record_wallet_buys({'Mpump':['W']})
        self.assertFalse(self.path.exists())
        self.assertEqual(smartgrade.record_wallet_buys({'Mpump':['W']}),1)
        before=self.path.read_bytes()
        with patch.object(smartgrade,'save_grades',return_value=False):
            with self.assertRaises(OSError):
                smartgrade.reconcile({'milestone_watch':{'Mpump':{'anchor':100,'hit':[2.0],'peak_mult':2.0}}})
        self.assertEqual(self.path.read_bytes(),before)
