"""Shared producer-to-consumer bus must retain simultaneous publications."""
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
BUS=importlib.machinery.SourceFileLoader('concurrent_bus_under_test',str(ROOT/'daemon/launch_scanner/signal_bus.py')).load_module()


class TestSignalBusConcurrency(unittest.TestCase):
    def test_multiple_processes_retain_every_successful_publication(self):
        with tempfile.TemporaryDirectory() as home:
            code='''
import os,sys,time
from pathlib import Path
from daemon.launch_scanner.signal_bus import publish,make_signal
while not Path(os.environ['BUS_BARRIER']).exists():time.sleep(.005)
for i in range(12):
    assert publish(make_signal('solana','fixture',sys.argv[1]+':'+str(i))) is True
'''
            barrier=Path(home)/'ready'
            env=dict(os.environ,FOREX_AGENT_HOME=home,BUS_BARRIER=str(barrier),PYTHONPATH=str(ROOT))
            workers=[subprocess.Popen([sys.executable,'-c',code,str(i)],cwd=ROOT,env=env,
                                      text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for i in range(12)]
            barrier.touch()
            try:
                for worker in workers:
                    out,err=worker.communicate(timeout=25)
                    self.assertEqual(worker.returncode,0,out+err)
            finally:
                for worker in workers:
                    if worker.poll() is None:worker.kill();worker.wait()
            rows=[json.loads(line) for line in (Path(home)/'launch_scanner/signal_bus.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),144)
            self.assertEqual(len({row['mint'] for row in rows}),144)

    def test_replace_failure_preserves_existing_snapshot_and_returns_false(self):
        with tempfile.TemporaryDirectory() as home,patch.object(BUS,'BUS_PATH',str(Path(home)/'bus.jsonl')):
            self.assertTrue(BUS.publish(BUS.make_signal('solana','fixture','first')))
            before=Path(BUS.BUS_PATH).read_bytes()
            with patch.object(BUS.os,'replace',side_effect=OSError('replace fixture')):
                self.assertFalse(BUS.publish(BUS.make_signal('solana','fixture','second')))
            self.assertEqual(Path(BUS.BUS_PATH).read_bytes(),before)
            self.assertFalse(list(Path(home).glob('.signal-bus-*')))

    def test_same_mint_other_producer_records_are_not_silently_deduplicated(self):
        with tempfile.TemporaryDirectory() as home,patch.object(BUS,'BUS_PATH',str(Path(home)/'bus.jsonl')):
            self.assertTrue(BUS.publish(BUS.make_signal('solana','source1','M')))
            self.assertTrue(BUS.publish(BUS.make_signal('solana','source2','M')))
            rows=[json.loads(line) for line in Path(BUS.BUS_PATH).read_text().splitlines()]
            self.assertEqual([r['source'] for r in rows],['source1','source2'])

    def test_existing_retention_bound_kept(self):
        with tempfile.TemporaryDirectory() as home,patch.object(BUS,'BUS_PATH',str(Path(home)/'bus.jsonl')),patch.object(BUS,'MAX_BUS_LINES',2):
            for mint in ('one','two','three'):self.assertTrue(BUS.publish(BUS.make_signal('solana','fixture',mint)))
            self.assertEqual([json.loads(line)['mint'] for line in Path(BUS.BUS_PATH).read_text().splitlines()],['two','three'])

    def test_event_identity_replay_dedups_but_rebinding_mint_refuses(self):
        with tempfile.TemporaryDirectory() as home,patch.object(BUS,'BUS_PATH',str(Path(home)/'bus.jsonl')):
            sig=BUS.make_signal('solana','fixture','one');sig['event_id']='intent1'
            self.assertTrue(BUS.publish(sig))
            self.assertTrue(BUS.publish(dict(sig,ts=sig['ts']+100)))
            self.assertFalse(BUS.publish(dict(sig,mint='other')))
            self.assertEqual(len(Path(BUS.BUS_PATH).read_text().splitlines()),1)
