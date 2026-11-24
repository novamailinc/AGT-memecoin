"""Offline causal regressions for parsed-stream lifecycle. No network/secrets."""
import os
import sys
import threading
import types
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.getcwd())
from daemon.intelligence.acquisition import helius_streams as hs, routine
from daemon.launch_scanner import LaunchScanner

class Lifecycle(unittest.TestCase):
    def client(self):
        c=hs.HeliusParsedStreamClient(wss_url='wss://beta.helius-rpc.com/?api-key=fixture')
        c._factory=MagicMock()
        return c
    def test_unknown_real_url_refused_before_factory(self):
        c=self.client();c._wss_url='wss://unrecognized.invalid/'
        with patch.object(routine,'websocket_admit',return_value=False):
            self.assertFalse(c.connect())
        c._factory.assert_not_called()
    def test_hold_survives_worker_and_can_stop(self):
        c=self.client(); waits=[]
        def pause(n): waits.append(n);c.stop()
        with patch.object(routine,'websocket_admit',side_effect=routine.WebsocketAdmissionError('fixture hold')):
            c.run_forever(sleep=pause)
        self.assertEqual(len(waits),1);c._factory.assert_not_called()
    def test_accounting_failure_latches_failclosed(self):
        c=self.client();c.connected=True;c._ws=MagicMock()
        with patch.object(routine,'websocket_result',side_effect=OSError('fixture failure')):
            c._on_error(None,Exception('429 fixture'))
        self.assertFalse(c.connected);self.assertTrue(c._accounting_blocked)
        self.assertTrue(c._stop.is_set());self.assertFalse(c.connect())
        self.assertEqual(c.accounting_failures,1)
    def test_success_accounting_failure_cannot_subscribe(self):
        c=self.client();c._ws=MagicMock()
        with patch.object(routine,'websocket_result',side_effect=OSError('fixture failure')):
            c._on_open(c._ws)
        self.assertFalse(c.connected);c._ws.send.assert_not_called()
    def test_subscription_hold_failure_latches(self):
        c=self.client();c._ws=MagicMock();c.connected=True
        with patch.object(routine,'websocket_subscription_throttled',side_effect=OSError('fixture failure')):
            c._report_subscription_throttled()
        self.assertFalse(c.connected);self.assertTrue(c._accounting_blocked)
    def scanner(self):
        s=LaunchScanner.__new__(LaunchScanner);s._stop=False
        return s
    def test_string_false_cannot_start(self):
        s=self.scanner()
        with patch.object(hs,'HeliusParsedStreamClient') as ctor:
            s._ensure_parsed_stream(types.SimpleNamespace(degen=types.SimpleNamespace(parsed_stream_enabled='false')))
        ctor.assert_not_called()
    def test_single_owner_connect_restart_reference_shutdown(self):
        s=self.scanner();c=MagicMock();c.subscribe.return_value=(True,None);jobs=[]
        class Thread:
            def __init__(self,target,**kwargs): self.target=target;jobs.append(self)
            def start(self): pass
        cfg=types.SimpleNamespace(degen=types.SimpleNamespace(parsed_stream_enabled=True))
        with patch.object(hs,'HeliusParsedStreamClient',return_value=c),patch.object(routine,'websocket_url',return_value='wss://beta.helius-rpc.com/?api-key=fixture'),patch.object(routine,'router',return_value=object()),patch.object(threading,'Thread',Thread):
            s._ensure_parsed_stream(cfg)
            self.assertEqual(len(jobs),1);c.connect.assert_not_called()
            jobs[0].target();c.run_forever.assert_called_once()
            self.assertIsNone(s._parsed_stream)
            s._ensure_parsed_stream(cfg);self.assertEqual(len(jobs),2)
            s._stop_parsed_stream();c.stop.assert_called_once();self.assertIsNone(s._parsed_stream)
    def test_case_and_trailing_dot_cannot_evade_host_guard(self):
        for h in ('BETA.HELIUS-RPC.COM','beta.helius-rpc.com.','MAINNET.HELIUS-RPC.COM.'):
            self.assertTrue(routine._is_helius_ws_host(h))
        self.assertFalse(routine._is_helius_ws_host('beta.helius-rpc.com.evil.invalid'))

if __name__=='__main__': unittest.main(verbosity=2)
