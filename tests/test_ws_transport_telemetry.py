import sqlite3
from unittest.mock import patch
from daemon.intelligence.acquisition.ws_telemetry import TransportLedger
from daemon.launch_scanner.logstream import WSClient


class TestTransportTelemetry:
    def test_actual_socket_reads_writes_and_close_are_measured(self,tmp_path):
        ledger=TransportLedger(tmp_path);sid=ledger.begin('alchemy')
        client=WSClient('ws://fixture.invalid');client._meter=ledger;client._meter_id=sid
        class Socket:
            def send(self,value):return min(2,len(value))
            def recv(self,n):return b'\x81\x02hi'
            def close(self):pass
        client._sock=Socket()
        client._send_all(client._sock,b'abcdef')
        assert client.recv_text()=='hi'
        before=ledger.summary()['providers']['alchemy']
        assert before['observed_rx_bytes']==4 and before['observed_tx_bytes']==6
        assert before['unresolved_sessions']==1
        client.close();client.close()
        after=TransportLedger(tmp_path).summary()['providers']['alchemy']
        assert after['observed_tx_bytes']==12 and after['unresolved_sessions']==0

    def test_unclosed_session_survives_restart_as_unresolved_not_active(self,tmp_path):
        q=TransportLedger(tmp_path);sid=q.begin('alchemy');q.observe(sid,stage='transport_open',rx=100)
        summary=TransportLedger(tmp_path).summary()
        assert summary['unclosed_semantics']=='unresolved_not_verified_active'
        assert summary['providers']['alchemy']['unresolved_sessions']==1
        assert summary['providers']['alchemy']['observed_rx_bytes']==100

    def test_failed_handshake_owns_meter_and_closes_session(self,tmp_path,monkeypatch):
        from daemon.launch_scanner import logstream
        from daemon.intelligence.acquisition import routine
        from types import SimpleNamespace
        import pytest
        monkeypatch.setattr(routine,'router',lambda:SimpleNamespace(home=str(tmp_path)))
        class Socket:
            def send(self,v):return len(v)
            def close(self):pass
        client=WSClient('wss://solana-mainnet.g.alchemy.com/v2/fixture')
        with patch.object(client,'_proxy_for',return_value=None),patch.object(logstream.socket,'create_connection',return_value=Socket()),patch.object(logstream.ssl,'create_default_context') as ctx,patch.object(client,'_read_line',return_value='HTTP/1.1 429 Too Many Requests'):
            ctx.return_value.wrap_socket.side_effect=RuntimeError('fixture TLS')
            with pytest.raises(RuntimeError):client.connect()
        import time
        deadline=time.monotonic()+2
        while True:
            providers=TransportLedger(tmp_path).summary()['providers']
            if providers and providers['alchemy']['unresolved_sessions']==0:break
            assert time.monotonic()<deadline
            time.sleep(.01)
        row=providers['alchemy']
        assert row['attempts']==1 and row['accepted_handshakes']==0 and row['unresolved_sessions']==0


    def test_default_home_is_absolute_and_close_failure_remains_unresolved(self,tmp_path,monkeypatch):
        monkeypatch.setenv('FOREX_AGENT_HOME',str(tmp_path/'configured'))
        q=TransportLedger(None);monkeypatch.chdir(tmp_path)
        assert q.path==str(tmp_path/'configured'/'acquisition_ws_transport.sqlite')
        sid=q.begin('alchemy');client=WSClient('ws://fixture.invalid');client._meter=q;client._meter_id=sid
        class Socket:
            failed=True
            def send(self,v):return len(v)
            def close(self):
                if self.failed:raise OSError('fixture close failure')
        sock=Socket();client._sock=sock;client.close()
        assert client._sock is sock and q.summary()['providers']['alchemy']['unresolved_sessions']==1
        sock.failed=False;client.close()
        assert client._sock is None and q.summary()['providers']['alchemy']['unresolved_sessions']==0

    def test_critical_observer_never_waits_for_blocked_disk_worker(self,tmp_path):
        import threading,time
        from daemon.intelligence.acquisition.ws_telemetry import AsyncTransportMeter
        entered=threading.Event();release=threading.Event()
        def blocked(*a,**k):entered.set();release.wait(5);raise OSError('fixture blocked storage')
        with patch.object(TransportLedger,'__init__',side_effect=blocked):
            meter=AsyncTransportMeter(tmp_path);sid=meter.begin('alchemy')
            assert entered.wait(1)
            start=time.monotonic()
            for _ in range(1000):meter.observe(sid,rx=1)
            elapsed=time.monotonic()-start
            release.set()
        assert elapsed<.1 and meter.dropped>0


    def test_overflow_close_always_stops_worker_without_fake_close(self,tmp_path):
        import threading,time
        from daemon.intelligence.acquisition.ws_telemetry import AsyncTransportMeter
        entered=threading.Event();release=threading.Event()
        real=TransportLedger.observe
        def blocked(self,*a,**k):entered.set();assert release.wait(3);return real(self,*a,**k)
        with patch.object(TransportLedger,'observe',blocked):
            meter=AsyncTransportMeter(tmp_path);sid=meter.begin('alchemy')
            meter.observe(sid,rx=1);assert entered.wait(2)
            for _ in range(300):meter.observe(sid,rx=1)
            meter.observe(sid,closed=True)
            release.set();meter.worker.join(5)
        assert not meter.worker.is_alive()
        summary=TransportLedger(tmp_path).summary()
        assert summary['known_dropped_observations']>0
        assert summary['providers']['alchemy']['unresolved_sessions']==1

    def test_post_stop_observations_are_rejected_not_queued(self,tmp_path):
        from daemon.intelligence.acquisition.ws_telemetry import AsyncTransportMeter
        meter=AsyncTransportMeter(tmp_path);sid=meter.begin('alchemy');meter.stop();meter.worker.join(3)
        assert not meter.worker.is_alive()
        before=meter.dropped
        meter.observe(sid,rx=6);meter.observe(sid,closed=True)
        assert meter.queue.empty() and meter.dropped==before+2
