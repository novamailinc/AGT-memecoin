"""Tests for daemon/launch_scanner/logstream.py."""

import queue
import time
import unittest

from daemon.launch_scanner import logstream
from daemon.launch_scanner.logstream import LogStream, WSClient, WSError, extract_mint


def _parsed_tx(mint):
    return {
        "transaction": {
            "signatures":["sig"],
            "message": {
                "instructions": [{
                    "parsed": {"type": "initializeMint",
                               "info": {"mint": mint}},
                }],
            },
        },
        "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [],
                 "innerInstructions": []},
    }


class ExtractMintTest(unittest.TestCase):
    def test_parsed_initialize_mint(self):
        self.assertEqual(extract_mint(_parsed_tx("NEWMINT1")), "NEWMINT1")

    def test_balance_diff_fallback(self):
        tx = {
            "transaction": {"message": {"instructions": []}},
            "meta": {
                "preTokenBalances": [
                    {"mint": logstream.WSOL_MINT, "owner": "x"},
                ],
                "postTokenBalances": [
                    {"mint": logstream.WSOL_MINT, "owner": "x"},
                    {"mint": "FALLBACKMINT", "owner": "y"},
                ],
                "innerInstructions": [],
            },
        }
        self.assertEqual(extract_mint(tx), "FALLBACKMINT")

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_mint({}))
        self.assertIsNone(extract_mint({"meta": {}}))
        self.assertIsNone(extract_mint(None))


class LogStreamDegradationTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)

    def stream(self):
        return LogStream(intake_home=self.tmp.name)

    def test_http_rpc_method_not_shadowed_by_url_attr(self):
        # Regression: __init__ once stored the URL string in self._http_rpc,
        # shadowing the _http_rpc() method -> "'str' object is not callable".
        ls = self.stream()
        self.assertTrue(callable(ls._http_rpc))
        self.assertIsInstance(ls._http_rpc_url, str)

    def test_mint_for_signature_uses_rpc(self):
        ls = self.stream()
        ls._http_rpc_url = "http://x/"
        ls._last_rpc_ts = 0.0
        ls._rpc_fail_count = 0
        ls._last_rpc_warn = 0.0
        ls._http_rpc = lambda method, params: _parsed_tx("RPCMINT")
        self.assertEqual(ls._mint_for_signature("sig"), "RPCMINT")
    def test_missing_execution_status_refuses_fetched_mint(self):
        ls=LogStream.__new__(LogStream);ls._last_rpc_ts=0.0
        tx=_parsed_tx('UNKNOWN');del tx['meta']['err']
        ls._http_rpc=lambda *args:tx
        self.assertIsNone(ls._mint_for_signature('sig'))

    def test_unreachable_ws_degrades_not_crashes(self):
        # Nothing listening on this port: the thread must fail, back off,
        # and leave the scanner on polling — never raise.
        ls = LogStream(ws_url="ws://127.0.0.1:1",intake_home=self.tmp.name)
        ls.start()
        try:
            time.sleep(0.5)
            self.assertFalse(ls.healthy)
            self.assertEqual(ls.drain(), [])
        finally:
            ls.stop()

    def test_drain_returns_unaccepted_items(self):
        ls=self.stream()
        for sig,mint in (('s1','M1'),('s2','M2')):
            ls._intake.receive(sig);ls._intake.resolve(sig,mint)
        out=ls.drain()
        self.assertEqual([i['mint'] for i in out],['M1','M2'])
        self.assertEqual(ls.drain(),out)
        ls.acknowledge(['s1','s2'])
        self.assertEqual(ls.drain(),[])

    def test_failed_tx_notifications_ignored(self):
        ls = self.stream()
        ls._q = queue.Queue()
        ls._seen_sigs = set()
        ls._mint_for_signature = lambda sig: (_ for _ in ()).throw(
            AssertionError("must not fetch failed txs"))
        msg = ('{"jsonrpc":"2.0","method":"logsNotification","params":'
               '{"result":{"value":{"signature":"bad1",'
               '"err":{"InstructionError":[0,"Custom"]},"logs":[]}},'
               '"subscription":1}}')
        ls._handle_message(msg)  # must not raise, must not queue
        self.assertEqual(ls.drain(), [])

    def test_duplicate_signatures_deduped(self):
        ls = self.stream()
        ls._q = queue.Queue()
        ls._seen_sigs = set()
        ls._mint_for_signature = lambda sig: "DUPMINT"
        msg = ('{"jsonrpc":"2.0","method":"logsNotification","params":'
               '{"result":{"value":{"signature":"dup1","err":null,"logs":[]}},'
               '"subscription":1}}')
        ls._handle_message(msg)
        ls._handle_message(msg)
        self.assertEqual(len(ls.drain()), 1)


class WSClientTest(unittest.TestCase):
    def test_connect_refused_raises_wserror(self):
        ws = WSClient("ws://127.0.0.1:1", timeout=3)
        with self.assertRaises(Exception):
            ws.connect()

    def test_bad_scheme_rejected(self):
        ws = WSClient("http://example.com/", timeout=3)
        with self.assertRaises(WSError):
            ws.connect()




class SubscriptionAcceptanceTest(unittest.TestCase):
    def test_health_waits_for_every_subscription_ack_and_clears_on_disconnect(self):
        import tempfile,json
        from unittest.mock import patch
        from daemon.intelligence.acquisition import routine
        with tempfile.TemporaryDirectory() as home:
            stream=LogStream(ws_url='wss://fixture.invalid',programs=('program1','program2'),intake_home=home)
            seen=[]
            class Socket:
                messages=iter([{'id':1,'result':11},{'id':2,'result':12},None])
                def connect(self):seen.append(stream.healthy)
                def send_text(self,msg):seen.append(stream.healthy)
                def recv_text(self):
                    seen.append(stream.healthy)
                    value=next(self.messages)
                    return json.dumps(value) if value is not None else None
                def close(self):pass
            with patch.object(logstream,'WSClient',return_value=Socket()),patch.object(routine,'websocket_admit'),patch.object(routine,'websocket_result'):
                with self.assertRaises(WSError):stream._serve()
            self.assertEqual(seen,[False,False,False,False,False,True])
            self.assertFalse(stream.healthy)

    def test_subscription_error_never_reports_healthy(self):
        import tempfile,json
        from unittest.mock import patch
        from daemon.intelligence.acquisition import routine
        for response in ({'id':1,'error':{'code':429}},{'id':1,'result':True},{'id':1,'result':None}):
            with tempfile.TemporaryDirectory() as home:
                stream=LogStream(ws_url='wss://fixture.invalid',intake_home=home)
                class Socket:
                    def connect(self):pass
                    def send_text(self,msg):pass
                    def recv_text(self):return json.dumps(response)
                    def close(self):pass
                with patch.object(logstream,'WSClient',return_value=Socket()),patch.object(routine,'websocket_admit'),patch.object(routine,'websocket_result'):
                    with self.assertRaisesRegex(WSError,'subscription rejected'):stream._serve()
                self.assertFalse(stream.healthy)


    def test_post_ack_errors_and_contradictions_fail_closed(self):
        import tempfile,json
        from unittest.mock import patch
        from daemon.intelligence.acquisition import routine
        for final in ({'id':1,'result':12},{'id':1,'error':{'code':429}},{'id':999,'error':{'code':429}}):
            with tempfile.TemporaryDirectory() as home:
                stream=LogStream(ws_url='wss://fixture.invalid',intake_home=home)
                class Socket:
                    messages=iter([{'id':1,'result':11},final])
                    def connect(self):pass
                    def send_text(self,msg):pass
                    def recv_text(self):return json.dumps(next(self.messages))
                    def close(self):pass
                with patch.object(logstream,'WSClient',return_value=Socket()),patch.object(routine,'websocket_admit'),patch.object(routine,'websocket_result'),patch.object(routine,'websocket_subscription_throttled') as throttle:
                    with self.assertRaises(WSError):stream._serve()
                    self.assertEqual(throttle.call_count,int('error' in final))
                self.assertFalse(stream.healthy)

    def test_identical_ack_repeat_and_notification_connection_binding(self):
        import tempfile,json
        from unittest.mock import patch
        from daemon.intelligence.acquisition import routine
        with tempfile.TemporaryDirectory() as home:
            stream=LogStream(ws_url='wss://fixture.invalid',intake_home=home)
            delivered=[]
            def notification(sub):return {'method':'logsNotification','params':{'subscription':sub,'result':{'value':{'signature':'s','err':None}}}}
            class Socket:
                messages=iter([{'id':1,'result':11},{'id':1,'result':11},notification(99),notification(11),None])
                def connect(self):pass
                def send_text(self,msg):pass
                def recv_text(self):
                    value=next(self.messages)
                    return json.dumps(value) if value is not None else None
                def close(self):pass
            stream._handle_message=lambda msg:delivered.append(json.loads(msg)['params']['subscription'])
            with patch.object(logstream,'WSClient',return_value=Socket()),patch.object(routine,'websocket_admit'),patch.object(routine,'websocket_result'):
                with self.assertRaises(WSError):stream._serve()
            self.assertEqual(delivered,[11])
            self.assertFalse(stream.healthy)

    def test_absolute_ack_deadline_and_quiet_freshness_bound(self):
        import tempfile,json
        from unittest.mock import patch
        from daemon.intelligence.acquisition import routine
        clock=[100.]
        with tempfile.TemporaryDirectory() as home:
            stream=LogStream(ws_url='wss://fixture.invalid',intake_home=home)
            class Socket:
                def connect(self):pass
                def send_text(self,msg):pass
                def recv_text(self):
                    clock[0]+=11
                    return json.dumps({'id':999,'result':22})
                def close(self):pass
            with patch.object(logstream.time,'monotonic',side_effect=lambda:clock[0]),patch.object(logstream,'WSClient',return_value=Socket()),patch.object(routine,'websocket_admit'),patch.object(routine,'websocket_result'):
                with self.assertRaisesRegex(WSError,'acknowledgement deadline'):stream._serve()
                stream._set_healthy(True)
                self.assertTrue(stream.healthy)
                clock[0]+=61
                self.assertFalse(stream.healthy)  # quiet delivery is unverified, not asserted broken
                stream._set_healthy(True)
                self.assertTrue(stream.healthy)

    def test_lowlevel_ping_flood_cannot_extend_absolute_deadline(self):
        from unittest.mock import patch
        clock=[100.]
        client=WSClient('ws://fixture.invalid');client.read_deadline=102.
        class Socket:
            def settimeout(self,value):self.timeout=value
            def recv(self,n):clock[0]+=.6;return b'\x89\x00'
            def send(self,data):return len(data)
        sock=Socket();client._sock=sock
        with patch.object(logstream.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(WSError,'absolute websocket response deadline'):client.recv_text()
        self.assertLessEqual(sock.timeout,2.)

    def test_ping_reply_write_uses_remaining_deadline(self):
        from unittest.mock import patch
        clock=[100.];timeouts=[]
        client=WSClient('ws://fixture.invalid');client.read_deadline=102.
        class Socket:
            def settimeout(self,value):self.timeout=value;timeouts.append(value)
            def recv(self,n):clock[0]+=1.9;return b'\x89\x00'
            def send(self,data):
                clock[0]+=self.timeout
                raise TimeoutError('fixture blocking write')
        client._sock=Socket()
        with patch.object(logstream.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(TimeoutError):client.recv_text()
        self.assertAlmostEqual(timeouts[-1],.1)
        self.assertAlmostEqual(clock[0],102.)

if __name__ == '__main__':
    unittest.main()
