"""Owner-revised allocation, actual adapter transport, no real provider traffic."""
import json
from unittest.mock import patch
import pytest
from daemon.intelligence.acquisition import routine
from daemon.intelligence.acquisition.provider_router import ProviderRouter,ROUTING_MODE_REVISED
from daemon.intelligence.acquisition.providers.helius import HeliusAdapter
from daemon.launch_scanner import helius_ratelimit


def test_actual_revised_route_separates_normal_scan_routine_and_verification(tmp_path):
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED)
    assert r.route('getTransaction',['sig'],priority='background')['primary']=='helius'
    assert r.route('getAccountInfo',['mint'])['primary']=='helius'
    assert r.route('getTransaction',['sig'],hints={'purpose':'normal_scanning'})['primary']=='alchemy'
    assert r.route('getTransaction',['sig'],hints={'workload':'independent_verification','recent':True})['primary']=='chainstack'
    assert 'helius' not in r.route('getTransaction',['sig'],hints={'workload':'independent_verification','recent':True})['candidates']


@pytest.mark.parametrize('slot',[1,2,3])
def test_fixed_numbered_sources_and_no_denial_rotation(tmp_path,monkeypatch,slot):
    import hashlib
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-source-%d'%i)
    calls=[]
    class Response:
        status=200
        def read(self):return b'{"jsonrpc":"2.0","id":1,"result":123}'
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def transport(req,timeout=None):calls.append(req.full_url);return Response()
    a=HeliusAdapter(home=str(tmp_path),transport=transport);a.revised=True
    params=next([str(i)] for i in range(100) if int.from_bytes(hashlib.sha256(json.dumps(['getSlot',[str(i)]],sort_keys=True).encode()).digest()[:8],'big')%3==slot-1)
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        out=a.call('getSlot',params)
        assert out['kind']=='ok' and out['credential_source']=='key%d'%slot
        assert calls==['https://mainnet.helius-rpc.com/?api-key=fixture-source-%d'%slot]
        p.return_value.acquire.return_value=False
        assert a.call('getSlot',params)['kind']=='rate_limited'
        assert len(calls)==1
        buckets=[c.args[0] for c in p.return_value.acquire.call_args_list]
        assert len(set(buckets))==1


def test_socket_roles_and_shared_helius_cooldown(tmp_path,monkeypatch):
    monkeypatch.setenv('HELIUS_API_KEY_1','fixture-source-1')
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-alchemy')
    assert 'helius-rpc.com' in routine.websocket_url()
    assert 'alchemy.com' in routine.websocket_url(purpose='normal_scanning')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=False
        with pytest.raises(routine.WebsocketAdmissionError,match='cooldown'):routine.websocket_admit(routine.websocket_url())
        routine.websocket_result(routine.websocket_url(),RuntimeError('429 fixture'))
        p.return_value.report.assert_called_once()


def test_actual_routine_caller_helius_transport_and_provenance(tmp_path,monkeypatch):
    from daemon.launch_scanner import smartmoney
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-source-%d'%i)
    calls=[]
    class Response:
        status=200
        def read(self):return b'{"jsonrpc":"2.0","id":1,"result":{"value":77}}'
        def __enter__(self):return self
        def __exit__(self,*a):pass
    def transport(req,timeout=None):calls.append(req.full_url);return Response()
    r=ProviderRouter(home=str(tmp_path),transport=transport,helius_enabled=True,mode=ROUTING_MODE_REVISED)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    routine.clear_evidence()
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        assert smartmoney._rpc('getTransaction',['s'])=={'value':77}
    assert len(calls)==1 and 'helius-rpc.com' in calls[0]
    evidence=routine.evidence_since()[-1]
    assert evidence['provider']=='helius' and evidence['account']=='helius_main'
    assert evidence['credential_source'] in ('key1','key2','key3')
    assert not evidence['degraded']


def test_denied_numbered_source_never_tries_other_keys(tmp_path,monkeypatch):
    import urllib.error
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-source-%d'%i)
    calls=[]
    def denied(req,timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url,429,'rate',{},None)
    a=HeliusAdapter(home=str(tmp_path),transport=denied);a.revised=True
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        assert a.call('getSlot',[])['kind']=='rate_limited'
        assert len(calls)==1
        p.return_value.report.assert_called_once()
        assert p.return_value.report.call_args.args[1] is False


def test_normal_scan_actual_transport_stays_alchemy(tmp_path,monkeypatch):
    from daemon.launch_scanner.logstream import LogStream
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-alchemy')
    calls=[]
    class Response:
        status=200
        def read(self):return b'{"jsonrpc":"2.0","id":1,"result":{"slot":77}}'
        def __enter__(self):return self
        def __exit__(self,*a):pass
    def transport(req,timeout=None):calls.append(req.full_url);return Response()
    r=ProviderRouter(home=str(tmp_path),transport=transport,helius_enabled=True,mode=ROUTING_MODE_REVISED)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    assert LogStream()._http_rpc('getTransaction',['s'])=={'slot':77}
    assert len(calls)==1 and 'alchemy.com' in calls[0]


def test_server_minimum_hold_survives_late_success_and_new_instance(tmp_path,monkeypatch):
    from daemon.intelligence.acquisition import helius_account
    home=str(tmp_path)
    helius_account.throttled(home,900,now=1000)
    assert not helius_account.allowed(home,now=1899)
    helius_account.throttled(home,30,now=1001)
    assert not helius_account.allowed(home,now=1899)
    assert helius_account.allowed(home,now=1900)
    # Adapter success callback cannot erase durable account minimum.
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-source-%d'%i)
    with patch('time.time',return_value=1001),patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.report('ignored',True)
        a=HeliusAdapter(home=home,transport=lambda *a,**k:pytest.fail('held network'));a.revised=True
        assert a.call('getSlot',[])['kind']=='rate_limited'
        r=ProviderRouter(home=home,helius_enabled=True,mode=ROUTING_MODE_REVISED)
        monkeypatch.setattr(routine._local,'router',r,raising=False)
        with pytest.raises(routine.WebsocketAdmissionError,match='hold'):routine.websocket_admit(routine.websocket_url())


@pytest.mark.parametrize('status',[401,403,429])
def test_denial_attempt_source_attribution_and_no_key_switch(tmp_path,monkeypatch,status):
    import urllib.error
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-source-%d'%i)
    calls=[]
    def transport(req,timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url,status,'fixture',{'Retry-After':'900'},None)
    r=ProviderRouter(home=str(tmp_path),transport=transport,helius_enabled=True,mode=ROUTING_MODE_REVISED)
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        out=r.call('getSlot',[],max_attempts=1)
    assert len(calls)==1
    attempt=out['attempts'][0]
    assert attempt['credential_source'] in ('key1','key2','key3') and attempt['account']=='helius_main'
    if status==429:
        from daemon.intelligence.acquisition import helius_account
        assert not helius_account.allowed(str(tmp_path))


def test_monthly_limit_disables_same_account_http_and_wss(tmp_path,monkeypatch):
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-source-%d'%i)
    class Response:
        status=200
        def read(self):return b'{"error":{"code":429,"message":"monthly quota exceeded"}}'
        def __enter__(self):return self
        def __exit__(self,*a):pass
    r=ProviderRouter(home=str(tmp_path),transport=lambda *a,**k:Response(),helius_enabled=True,mode=ROUTING_MODE_REVISED)
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        r.call('getSlot',[],max_attempts=1)
    assert r.usage.account_disabled('helius_main')
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    with pytest.raises(routine.WebsocketAdmissionError):routine.websocket_admit(routine.websocket_url())


def test_chainstack_explicit_recent_and_measured_window_only(tmp_path):
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,chainstack_recent_window_slots=100)
    base={'workload':'independent_verification'}
    assert 'chainstack' not in r.route('getTransaction',['s'],hints=base)['candidates']
    assert 'chainstack' not in r.route('getTransaction',['s'],hints={**base,'recent':False,'target_slot':990,'latest_slot':1000})['candidates']
    assert r.route('getTransaction',['s'],hints={**base,'target_slot':990,'latest_slot':1000})['primary']=='chainstack'
    assert 'chainstack' not in r.route('getTransaction',['s'],hints={**base,'target_slot':1001,'latest_slot':1000})['candidates']


@pytest.mark.parametrize('agent_home',[False,True])
def test_default_account_root_http_and_socket_identical(tmp_path,monkeypatch,agent_home):
    from daemon.intelligence.acquisition import helius_account
    monkeypatch.setenv('HOME',str(tmp_path/'user'));monkeypatch.delenv('FOREX_AGENT_HOME',raising=False)
    expected=tmp_path/'user'/'.forex-agent'
    if agent_home:expected=tmp_path/'runtime';monkeypatch.setenv('FOREX_AGENT_HOME',str(expected))
    r=ProviderRouter(helius_enabled=True,mode=ROUTING_MODE_REVISED)
    assert r.home==str(expected) and r._adapter('helius').home==str(expected)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    monkeypatch.setenv('HELIUS_API_KEY_1','fixture-source-1')
    helius_account.throttled(r._adapter('helius').home,900)
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        routine.websocket_result(routine.websocket_url())
        routine.websocket_result(routine.websocket_url(),RuntimeError('ordinary transport failure'))
        with pytest.raises(routine.WebsocketAdmissionError,match='hold'):routine.websocket_admit(routine.websocket_url())
        p.return_value.report.assert_not_called()
    assert not (tmp_path/'user'/'helius_account_hold.sqlite3').exists()


@pytest.mark.parametrize('callback_fault',[False,True])
def test_actual_armed_helius_success_lifecycle_cleanup(tmp_path,monkeypatch,callback_fault):
    from daemon.launch_scanner import armed_watch
    monkeypatch.setenv('HELIUS_API_KEY_1','fixture-source-1')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    w=armed_watch.ArmedWatcher(lambda *args:None)
    with patch.object(helius_ratelimit,'pacer') as p,patch.object(armed_watch,'WSClient') as client:
        p.return_value.acquire.return_value=True
        client.return_value.recv_text.side_effect=lambda:(w._stop.set() or "")
        if callback_fault:
            with patch.object(routine,'websocket_result',side_effect=OSError('callback storage fault')):
                with pytest.raises(OSError):w._serve()
        else:w._serve()
        client.return_value.connect.assert_called_once()
        client.return_value.close.assert_called_once()
        assert w._active_ws is None and not w._subs and not w._pending
