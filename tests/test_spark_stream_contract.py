"""Spark's real top-level import and durable non-destructive drain contract."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('spark_contract_fixture',str(ROOT/'scripts/spark-feed'))
spec=importlib.util.spec_from_loader(loader.name,loader)
spark=importlib.util.module_from_spec(spec);loader.exec_module(spark)

class TestSparkStreamContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        for key,val in (('HOME',self.tmp.name),('WATCHLIST_FILE',str(self.home/'spark_watchlist.json'))):
            p=patch.object(spark,key,val);p.start();self.addCleanup(p.stop)
        self.ls=spark.LogStream(intake_home=self.tmp.name,intake_filename='spark_logstream_intake.json')
        self.ls._intake.receive('sig');self.ls._intake.resolve('sig','M')

    def test_actual_top_level_module_import_and_construction(self):
        code="import sys,os;sys.path.insert(0,os.path.join(os.getcwd(),'daemon','launch_scanner'));from logstream import LogStream;s=LogStream(intake_home=os.environ['STREAM_HOME']);assert s.drain()==[]"
        p=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=dict(os.environ,STREAM_HOME=str(self.home/'top')),capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)

    def test_actual_collect_persists_watchlist_before_ack_and_repeat_empty(self):
        with patch.object(spark,'LogStream',return_value=self.ls),patch.object(self.ls,'start'),patch.object(spark,'_helius_rpc_urls',return_value=[]),patch.object(spark.time,'sleep'):
            self.assertEqual(spark.collect_push_mints()[0]['mint'],'M')
            self.assertEqual(spark.load_watchlist()['M']['sig'],'sig')
            self.assertEqual(spark.collect_push_mints(),[])

    def test_save_failure_and_corrupt_state_retain_pending(self):
        with patch.object(spark,'save_watchlist',side_effect=OSError('save fixture')):
            with self.assertRaises(OSError):spark.accept_push_watchlist(self.ls,self.ls.drain())
        self.assertEqual(len(self.ls.drain()),1)
        Path(spark.WATCHLIST_FILE).write_text('{')
        with self.assertRaises(ValueError):spark.accept_push_watchlist(self.ls,self.ls.drain())
        self.assertEqual(len(self.ls.drain()),1)
        self.assertEqual(Path(spark.WATCHLIST_FILE).read_text(),'{')

    def test_ack_failure_after_save_replay_keeps_first_timestamp(self):
        original=self.ls.drain()[0]['ts']
        with patch.object(self.ls,'acknowledge',side_effect=OSError('ack fixture')):
            with self.assertRaises(OSError):spark.accept_push_watchlist(self.ls,self.ls.drain())
        items=self.ls.drain();items[0]['ts']+=100
        spark.accept_push_watchlist(self.ls,items)
        self.assertEqual(spark.load_watchlist()['M']['ts'],original)
        self.assertEqual(self.ls.drain(),[])

    def test_scanner_and_spark_consumer_ack_are_separate(self):
        scanner=spark.LogStream(intake_home=self.tmp.name)
        scanner._intake.receive('sig');scanner._intake.resolve('sig','M')
        spark.accept_push_watchlist(self.ls,self.ls.drain())
        self.assertEqual(len(scanner.drain()),1)

    def test_actual_main_holds_lifecycle_lock_through_process_prune_save(self):
        import threading
        entered=threading.Event();release=threading.Event();accepted=threading.Event()
        self.ls._intake.resolve('sig','M')
        def blocked_pools():
            entered.set();self.assertTrue(release.wait(10));return []
        def run_cycle():spark.main()
        def accept_other():
            self.ls._intake.receive('other');self.ls._intake.resolve('other','NEW')
            spark.accept_push_watchlist(self.ls,[i for i in self.ls.drain() if i['signature']=='other'])
            accepted.set()
        with patch.object(spark,'_HAS_SPARK',True),patch.object(spark.killswitch,'gate',return_value=True),patch.object(spark,'collect_push_mints',return_value=[]),patch.object(spark,'get_new_pools',side_effect=blocked_pools),patch.object(spark,'load_seen',return_value=set()),patch.object(spark,'save_seen'):
            cycle=threading.Thread(target=run_cycle);cycle.start()
            self.assertTrue(entered.wait(10))
            writer=threading.Thread(target=accept_other);writer.start()
            self.assertFalse(accepted.wait(.1))
            release.set();cycle.join(10);writer.join(10)
            self.assertFalse(cycle.is_alive());self.assertFalse(writer.is_alive())
        self.assertTrue(accepted.is_set())
        self.assertIn('NEW',spark.load_watchlist())

    def test_process_cycle_lock_blocks_accept_until_stale_save_finishes(self):
        from threading import Timer
        marker=self.home/'holding';release=self.home/'release'
        code="""
import importlib.machinery,importlib.util,os,time
from pathlib import Path
l=importlib.machinery.SourceFileLoader('spark_process',str(Path.cwd()/'scripts/spark-feed'));sp=importlib.util.spec_from_loader(l.name,l);s=importlib.util.module_from_spec(sp);l.exec_module(s)
s.WATCHLIST_FILE=str(Path(os.environ['STREAM_HOME'])/'spark_watchlist.json')
with s.watchlist_cycle_lock():
    old=s.load_watchlist();Path(os.environ['STREAM_MARKER']).touch()
    deadline=time.time()+10
    while not Path(os.environ['STREAM_RELEASE']).exists():
        assert time.time()<deadline
        time.sleep(.01)
    s.save_watchlist(old)
"""
        child=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,env=dict(os.environ,STREAM_HOME=self.tmp.name,STREAM_MARKER=str(marker),STREAM_RELEASE=str(release)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        import time
        deadline=time.time()+10
        while not marker.exists():
            self.assertLess(time.time(),deadline);time.sleep(.01)
        timer=Timer(.2,release.touch);timer.start()
        try:spark.accept_push_watchlist(self.ls,self.ls.drain())
        finally:timer.join();out,err=child.communicate(timeout=15)
        self.assertEqual(child.returncode,0,out+err)
        self.assertIn('M',spark.load_watchlist())
