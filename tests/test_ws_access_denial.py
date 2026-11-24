"""No network: typed access denial stops retries, not HTTP or exit polling."""
from daemon.launch_scanner.logstream import LogStream, WSHandshakeError
from daemon.launch_scanner.armed_watch import ArmedWatcher
import pytest

@pytest.mark.parametrize('code',[401,403])
def test_discovery_access_denial_no_reconnect(tmp_path,code):
    stream=LogStream(ws_url='wss://fixture.invalid',intake_home=tmp_path)
    calls=[]
    def serve():calls.append(1);raise WSHandshakeError(code)
    stream._serve=serve
    stream._run()
    assert calls==[1] and not stream.healthy
    assert not stream._stop.is_set() # explicit start may retry after access repair

@pytest.mark.parametrize('code',[401,403])
def test_armed_access_denial_no_reconnect(code):
    watcher=ArmedWatcher(lambda *a:None,ws_url='wss://fixture.invalid')
    calls=[]
    def serve():calls.append(1);raise WSHandshakeError(code)
    watcher._serve=serve
    watcher._run()
    assert calls==[1]
    assert not watcher._stop.is_set()

def test_actual_handshake_propagates_status_and_closes_socket(monkeypatch):
    from daemon.launch_scanner import logstream
    class Socket:
        closed=False
        def send(self,data):return len(data)
        def close(self):self.closed=True
    sock=Socket();client=logstream.WSClient('ws://fixture.invalid')
    monkeypatch.setattr(client,'_proxy_for',lambda secure:None)
    monkeypatch.setattr(logstream.socket,'create_connection',lambda *a,**k:sock)
    monkeypatch.setattr(client,'_read_line',lambda *a:'HTTP/1.1 403 Forbidden')
    with pytest.raises(WSHandshakeError) as caught:client.connect()
    assert caught.value.status_code==403
    assert sock.closed and client._sock is None

@pytest.mark.parametrize('status',['HTTP/1.1 1010 Invalid','HTTP/1.1 200 OK','broken'])
def test_non101_status_is_not_accepted(monkeypatch,status):
    from daemon.launch_scanner import logstream
    class Socket:
        def send(self,data):return len(data)
        def close(self):pass
    client=logstream.WSClient('ws://fixture.invalid')
    monkeypatch.setattr(client,'_proxy_for',lambda secure:None)
    monkeypatch.setattr(logstream.socket,'create_connection',lambda *a,**k:Socket())
    monkeypatch.setattr(client,'_read_line',lambda *a:status)
    with pytest.raises(WSHandshakeError):client.connect()

def test_armed_connection_bound_pending_and_late_old_ack():
    import json
    watcher=ArmedWatcher(lambda *a:None)
    watcher._want={'mint':{'pool':'pool','base_vault':'base','quote_vault':'quote'}}
    class Socket:
        def send_text(self,data):pass
    old=Socket();watcher._active_ws=old
    watcher._subscribe_all(old);old_ids=set(watcher._pending)
    current=Socket();watcher._active_ws=current
    watcher._subscribe_all(current);current_ids=set(watcher._pending)
    assert old_ids.isdisjoint(current_ids)
    watcher._handle(current,json.dumps({'id':next(iter(old_ids)),'result':88}))
    watcher._handle(old,json.dumps({'id':next(iter(current_ids)),'result':99}))
    assert watcher._subs=={}
    watcher._handle(current,json.dumps({'id':next(iter(current_ids)),'result':100}))
    assert 100 in watcher._subs
    watcher._serve=lambda:(_ for _ in ()).throw(WSHandshakeError(403))
    watcher._run()
    assert watcher._pending=={} and watcher._subs=={} and watcher._active_ws is None
