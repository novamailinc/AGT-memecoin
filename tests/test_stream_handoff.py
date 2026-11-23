"""Actual log receipt/detail/drain/commit seams, no network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from types import SimpleNamespace
from daemon.launch_scanner import LaunchScanner,logstream
from daemon.launch_scanner.stream_intake import StreamIntake
from storage import Store
from daemon.common import get_daemon_state


def notification(sig='sig',**kw):
    value=dict(signature=sig,err=None,logs=[]);value.update(kw)
    return json.dumps({'method':'logsNotification','params':{'result':{'value':value}}})


class TestStreamHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ls=logstream.LogStream(intake_home=self.tmp.name)
        self.scanner=LaunchScanner.__new__(LaunchScanner);self.scanner._logstream=self.ls;self.scanner._stop=False
        self.store=Store(str(Path(self.tmp.name)/'state.db'));self.addCleanup(self.store.close)
        self.state={'seen':{}}

    def test_failed_detail_restart_and_non_destructive_drain(self):
        with patch.object(self.ls,'_mint_for_signature',return_value=None):self.ls._handle_message(notification())
        self.assertIn('sig',self.ls._intake.pending())
        other=logstream.LogStream(intake_home=self.tmp.name)
        with patch.object(other,'_mint_for_signature',return_value='M'):other._retry_pending()
        self.assertEqual(other.drain(),other.drain())
        self.assertEqual(other.drain()[0]['mint'],'M')

    def test_actual_drain_failed_commit_rollback_retry_and_ack(self):
        self.ls._intake.receive('sig');self.ls._intake.resolve('sig','M')
        def consider(c,a,state,candidate):state['seen']['M']={'queued':True}
        with patch.object(self.scanner,'_consider',side_effect=consider),patch('daemon.launch_scanner.get_store',return_value=self.store):
            with patch.object(self.store,'journal_add',side_effect=OSError('write fixture')):
                self.scanner._drain_logstream(None,None,self.state)
            self.assertEqual(self.state['seen'],{})
            self.assertIn('sig',self.ls._intake.pending())
            self.scanner._drain_logstream(None,None,self.state)
        self.assertEqual(self.ls.drain(),[])
        self.assertEqual(get_daemon_state(self.store,'launch_scanner')['seen'],{'M':{'queued':True}})

    def test_stop_before_drain_retains_entire_batch(self):
        for sig in ('a','b'):
            self.ls._intake.receive(sig);self.ls._intake.resolve(sig,'M'+sig)
        self.scanner._stop=True
        self.scanner._drain_logstream(None,None,self.state)
        self.assertEqual(len(self.ls.drain()),2)

    def test_postcommit_ack_failure_reload_only_ack(self):
        self.ls._intake.receive('sig');self.ls._intake.resolve('sig','M')
        with patch.object(self.scanner,'_consider'),patch('daemon.launch_scanner.get_store',return_value=self.store),patch.object(self.ls,'acknowledge',side_effect=OSError('ack fixture')):
            self.scanner._drain_logstream(None,None,self.state)
        loaded=get_daemon_state(self.store,'launch_scanner')
        self.assertEqual(loaded['logstream_committed_refs'],['sig'])
        with patch.object(self.scanner,'_consider') as consider:
            self.scanner._drain_logstream(None,None,loaded)
            consider.assert_not_called()
        self.assertEqual(self.ls.drain(),[])

    def test_response_signature_mismatch_stays_pending(self):
        tx={'meta':{'err':None},'transaction':{'signatures':['other'],'message':{'instructions':[{'parsed':{'type':'initializeMint','info':{'mint':'M'}}}]}}}
        with patch.object(self.ls,'_http_rpc',return_value=tx):
            self.ls._handle_message(notification())
        self.assertEqual(self.ls.drain(),[])
        self.assertIn('sig',self.ls._intake.pending())

    def test_unknown_envelope_not_received(self):
        value={'signature':'sig','logs':[]}
        self.ls._handle_message(json.dumps({'method':'logsNotification','params':{'result':{'value':value}}}))
        self.assertEqual(self.ls._intake.pending(),{})

    def test_pending_poison_rotation_and_resolved_backlog_cannot_starve_details(self):
        for i in range(15):
            sig='resolved'+str(i);self.ls._intake.receive(sig);self.ls._intake.resolve(sig,'M'+str(i))
        self.ls._intake.receive('poison');self.ls._intake.receive('good')
        with patch.object(self.ls,'_mint_for_signature',side_effect=lambda sig: 'OK' if sig=='good' else None):
            self.ls._retry_pending()
        self.assertEqual(self.ls._intake.pending()['good']['mint'],'OK')
        self.assertIn('poison',self.ls._intake.pending())

    def test_actual_readback_failure_leaves_unaccepted(self):
        self.ls._intake.receive('sig');self.ls._intake.resolve('sig','M')
        with patch.object(self.scanner,'_consider'),patch('daemon.launch_scanner.get_store',return_value=self.store),patch.object(self.store,'journal_query',return_value=[]):
            self.scanner._drain_logstream(None,None,self.state)
        self.assertIn('sig',self.ls._intake.pending())
        self.assertNotIn('logstream_commit',self.state)

    def test_fresh_process_restores_unaccepted_resolved_delivery(self):
        import os,subprocess,sys
        self.ls._intake.receive('sig');self.ls._intake.resolve('sig','M')
        code="from daemon.launch_scanner.logstream import LogStream; import os; s=LogStream(intake_home=os.environ['STREAM_HOME']); assert s.drain()[0]['mint']=='M'; s.acknowledge(['sig']); assert s.drain()==[]"
        result=subprocess.run([sys.executable,'-c',code],env=dict(os.environ,STREAM_HOME=self.tmp.name),capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.ls.drain(),[])

    def test_concurrent_process_receipts_preserve_all_signatures(self):
        import os,subprocess,sys
        code="from daemon.launch_scanner.stream_intake import StreamIntake; import os; s=StreamIntake(os.environ['STREAM_HOME']); s.receive(os.environ['STREAM_SIG'])"
        jobs=[subprocess.Popen([sys.executable,'-c',code],env=dict(os.environ,STREAM_HOME=self.tmp.name,STREAM_SIG='sig'+str(i)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(6)]
        for job in jobs:
            out,err=job.communicate(timeout=20)
            self.assertEqual(job.returncode,0,out+err)
        self.assertEqual(len(self.ls._intake.pending()),6)

    def test_postreplace_fsync_failure_retains_visible_receipt_on_retry(self):
        import os
        real=os.fsync;calls=[]
        def fail_directory(fd):
            calls.append(fd)
            if len(calls)==2:raise OSError('directory fsync fixture')
            return real(fd)
        with patch('daemon.launch_scanner.stream_intake.os.fsync',side_effect=fail_directory):
            with self.assertRaises(OSError):self.ls._intake.receive('sig')
        self.assertIn('sig',self.ls._intake.pending())
        self.assertFalse(self.ls._intake.receive('sig'))
        self.assertEqual(len(self.ls._intake.pending()),1)

    def test_corruption_preserved_and_unresolved_not_acked(self):
        intake=self.ls._intake;intake.receive('sig');intake.accept(['sig'])
        self.assertIn('sig',intake.pending())
        intake.path.write_text('{')
        with self.assertRaises(ValueError):intake.receive('other')
        self.assertEqual(intake.path.read_text(),'{')
