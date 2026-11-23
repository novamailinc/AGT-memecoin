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
