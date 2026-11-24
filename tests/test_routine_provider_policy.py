"""No-egress actual caller routing contracts, not provider activation."""
from unittest.mock import patch
from daemon.intelligence.acquisition import routine


class TestRoutineProviderPolicy:
    def test_default_facade_explicit_revised_allocation(self):
        if hasattr(routine._local,'router'):del routine._local.router
        with patch.object(routine,'ProviderRouter') as ctor:
            routine.router()
            ctor.assert_called_once_with(helius_enabled=True,mode='revised_20261123')
        del routine._local.router

    def test_actual_routine_callers_use_common_router(self):
        from daemon.launch_scanner import smartmoney,instant_safety,funding_clusters,mimic_curve,reserves
        with patch.object(routine,'call',return_value={'kind':'ok','result':{'value':1},'provider':'alchemy'}) as call:
            assert smartmoney._rpc('getTransaction',['s'])=={'value':1}
            assert smartmoney.discovery_rpc('getSignaturesForAddress',['w'])=={'value':1}
            assert instant_safety._rpc('getAccountInfo',['m'],2)=={'value':1}
            assert funding_clusters._rpc('getTransaction',['s'])=={'value':1}
            assert mimic_curve.rpc('getAccountInfo',['m'])=={'value':1}
            assert reserves.rpc_typed('getAccountInfo',['m'])==({'value':1},'ok')
            assert reserves.last_rpc_source()=='alchemy'
            assert call.call_count==6

    def test_failure_never_falls_back_to_direct_helius(self):
        from daemon.launch_scanner import smartmoney,instant_safety
        with patch.object(routine,'call',return_value={'kind':'error','result':None,'provider':'public'}),patch('urllib.request.urlopen',side_effect=AssertionError('direct provider bypass')):
            assert smartmoney._rpc('getTransaction',['s']) is None
            assert instant_safety._rpc('getAccountInfo',['m'],2) is None

    def test_logstream_defaults_alchemy_ws_and_central_http(self):
        from daemon.launch_scanner.logstream import LogStream
        with patch.object(routine,'websocket_url',return_value='wss://alchemy.example.invalid/key'),patch.object(routine,'call',return_value={'kind':'ok','result':{'slot':1},'decision_usable':True}) as read:
            stream=LogStream()
            assert stream._ws_url=='wss://alchemy.example.invalid/key'
            assert stream._http_rpc('getTransaction',['sig'])=={'slot':1}
            assert read.call_args.kwargs['hints']=={'purpose':'normal_scanning'}

    def test_real_scanner_start_cannot_reinject_helius_chain(self):
        from daemon.launch_scanner import LaunchScanner,logstream
        from types import SimpleNamespace
        scanner=LaunchScanner.__new__(LaunchScanner)
        with patch.object(logstream,'LogStream') as ctor:
            scanner._ensure_logstream(SimpleNamespace(degen=SimpleNamespace(logstream_enabled=True)))
            ctor.assert_called_once_with()
            ctor.return_value.start.assert_called_once()

    def test_actual_wallet_poll_without_helius_uses_real_alchemy_router(self,tmp_path,monkeypatch):
        import json
        from daemon.intelligence.acquisition.provider_router import ProviderRouter
        from daemon.launch_scanner import smartmoney
        from tests.test_wallet_intake import transaction
        tx=transaction();tx['transaction']['message']['accountKeys']=[{'pubkey':k} for k in ['W','TA1','TA2']]
        calls=[]
        class Response:
            status=200
            def __init__(self,value):self.value=value
            def read(self):return json.dumps({'jsonrpc':'2.0','id':1,'result':self.value}).encode()
            def __enter__(self):return self
            def __exit__(self,*a):pass
        def transport(req,timeout=None):
            body=json.loads(req.data);calls.append((req.full_url,body['method']))
            return Response([{'signature':'new'}] if body['method']=='getSignaturesForAddress' else tx)
        monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
        monkeypatch.delenv('HELIUS_API_KEY',raising=False)
        monkeypatch.setattr(smartmoney,'api_key',lambda:None)
        monkeypatch.setattr(smartmoney,'_HOME',tmp_path)
        monkeypatch.setattr(smartmoney,'load_registry',lambda:{'W':{}})
        monkeypatch.setattr(routine._local,'router',ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport),raising=False)
        fresh=smartmoney.poll_wallets({},poll_seconds=0)
        assert fresh=={'Mpump':['W']}
        assert smartmoney._intake().wallet('W')['pending']==['new']
        assert {m for _,m in calls}=={'getSignaturesForAddress','getTransaction'}
        assert all('alchemy' in url for url,_ in calls)

    def test_actual_router_failover_critical_provenance_and_cache(self,tmp_path,monkeypatch):
        import json,urllib.error
        from daemon.intelligence.acquisition.provider_router import ProviderRouter
        from daemon.launch_scanner import instant_safety
        monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
        monkeypatch.setenv('CHAINSTACK_ENDPOINT','https://chainstack.example.invalid/node')
        calls=[]
        class Response:
            def __init__(self,status=200):
                self.status=status;self.headers={}
            def read(self):
                if self.status!=200:return b''
                return b'{"jsonrpc":"2.0","id":1,"result":{"value":[]}}'
            def __enter__(self):return self
            def __exit__(self,*a):pass
        def transport(req,timeout=None):
            calls.append(req.full_url)
            if 'alchemy' in req.full_url:
                # ANSWERED refusal (HTTP 429): the vendor definitively
                # processed and rejected the request, so failover to the
                # next provider is allowed. An UNKNOWN post-I/O outcome
                # (timeout/transport) is never retried or failed over
                # since the 2026-11-24 v3 contract fix - that case is
                # covered by test_no_failover_on_unknown_outcome below.
                return Response(status=429)
            return Response()
        monkeypatch.setattr(routine._local,'router',ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport),raising=False)
        routine.clear_evidence()
        assert instant_safety._rpc('getAccountInfo',['mint'],2)=={'value':[]}
        evidence=routine.evidence_since()[-1]
        assert evidence['provider']=='chainstack' and evidence['degraded'] and evidence['decision_usable']
        assert evidence['fallback_difference'] and len(evidence['attempts'])>=2
        # Real second call is a cache hit and must preserve degraded source truth.
        instant_safety._rpc('getAccountInfo',['mint'],2)
        cached=routine.evidence_since()[-1]
        assert cached['cached'] and cached['provider']=='chainstack' and cached['degraded']
        assert cached['fallback_difference']==evidence['fallback_difference']
        assert not any('helius' in url for url in calls)

    def test_no_failover_on_unknown_outcome(self,tmp_path,monkeypatch):
        import json,urllib.error
        from daemon.intelligence.acquisition.provider_router import ProviderRouter
        from daemon.launch_scanner import instant_safety
        monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
        monkeypatch.setenv('CHAINSTACK_ENDPOINT','https://chainstack.example.invalid/node')
        calls=[]
        class Response:
            status=200;headers={}
            def read(self):return b'{"jsonrpc":"2.0","id":1,"result":{"value":[]}}'
            def __enter__(self):return self
            def __exit__(self,*a):pass
        def transport(req,timeout=None):
            calls.append(req.full_url)
            if 'alchemy' in req.full_url:raise urllib.error.URLError('fixture outage')
            return Response()
        monkeypatch.setattr(routine._local,'router',ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport),raising=False)
        routine.clear_evidence()
        # Unknown post-I/O outcome at alchemy: terminal. No chainstack
        # attempt, no retry; the call surfaces as unusable (None).
        assert instant_safety._rpc('getAccountInfo',['mint'],2) is None
        assert calls and all('alchemy' in url for url in calls)

    def test_remaining_ordinary_readers_and_armed_ws_alchemy(self):
        from daemon.launch_scanner import exit_copy,lp_lock,armed_watch
        with patch.object(routine,'read',return_value={'value':{'amount':'1000000','decimals':3}}) as read:
            assert exit_copy._rpc('getTransaction',['sig'])['value']
            assert lp_lock.token_supply('solana','mint')=='1.0k'
            assert read.call_count==2
        with patch.object(routine,'websocket_url',return_value='wss://alchemy.example.invalid/key'):
            assert armed_watch.ArmedWatcher(lambda *a:None)._url()=='wss://alchemy.example.invalid/key'

    def test_safety_verdict_cache_preserves_original_evidence_without_mutation(self):
        from daemon.launch_scanner import instant_safety
        instant_safety._cache.clear()
        def inner(*args):
            routine._local.evidence=[{'provider':'chainstack','degraded':True,'cached':False}]
            return {'passed':False,'unknown':False}
        with patch.object(instant_safety,'_check_inner',side_effect=inner):
            first=instant_safety.check('fixture-cache-mint')
            second=instant_safety.check('fixture-cache-mint')
        assert first['acquisition_degraded'] and second['acquisition_degraded']
        assert first['acquisition_evidence']==second['acquisition_evidence']
        assert not first['verdict_cached'] and second['verdict_cached']
        second['acquisition_evidence'].clear()
        assert first['acquisition_evidence']
        instant_safety._cache.clear()

    def test_reserves_critical_typed_history_honors_unusable_fallback(self):
        from daemon.launch_scanner import reserves
        with patch.object(routine,'call',return_value={'kind':'ok','result':{'slot':1},'provider':'public','decision_usable':False,'degraded':True}):
            assert reserves.rpc_typed('getTransaction',['old'],exempt=True)==(None,'error')

    def test_missing_ws_endpoint_is_explicit_and_does_not_connect(self):
        from daemon.launch_scanner import logstream
        stream=logstream.LogStream(ws_url='wss://explicit.example.invalid')
        stream._ws_url=None
        with patch.object(logstream,'WSClient') as client:
            import pytest
            with pytest.raises(routine.WebsocketAdmissionError,match='no configured endpoint'):
                stream._serve()
            client.assert_not_called()

    def test_actual_alchemy_429_handshake_blocks_next_shared_admission(self,tmp_path,monkeypatch):
        from daemon.intelligence.acquisition.provider_router import ProviderRouter
        from daemon.launch_scanner import logstream
        from daemon.intelligence.acquisition.request_scheduler import SharedQuota
        import pytest
        monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
        r=ProviderRouter(home=str(tmp_path),helius_enabled=False)
        monkeypatch.setattr(routine._local,'router',r,raising=False)
        stream=logstream.LogStream()
        with patch.object(logstream,'WSClient') as client:
            client.return_value.connect.side_effect=RuntimeError('HTTP 429 fixture')
            with pytest.raises(RuntimeError,match='429'):stream._serve()
            assert client.return_value.connect.call_count==1
            with pytest.raises(routine.WebsocketAdmissionError):stream._serve()
            assert client.return_value.connect.call_count==1
        # The persisted quota, not a private loop flag, blocks another instance.
        quota=SharedQuota('alchemy',home=str(tmp_path))
        granted,_,_=quota.acquire('background')
        assert not granted

    def test_discovery_ws_exhausted_budget_refuses_before_socket_exit_remains_essential(self,tmp_path,monkeypatch):
        import pytest
        from daemon.intelligence.acquisition.provider_router import ProviderRouter
        from daemon.launch_scanner import logstream
        monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
        r=ProviderRouter(home=str(tmp_path),helius_enabled=False)
        monkeypatch.setattr(routine._local,'router',r,raising=False)
        priorities=[]
        def allowed(account,priority,workload=None):
            priorities.append(priority)
            return (priority=='critical','budget_stop_nonessential')
        with patch.object(r.usage,'nonessential_allowed',side_effect=allowed),patch.object(logstream,'WSClient') as client:
            with pytest.raises(routine.WebsocketAdmissionError,match='budget admission'):
                logstream.LogStream()._serve()
            client.assert_not_called()
            assert routine.websocket_admit(routine.websocket_url(purpose="normal_scanning"),critical=True)
        assert priorities==['background','critical']

    def test_lowlevel_ws_rejected_handshake_and_tls_failure_close_owned_socket(self):
        import pytest
        from daemon.launch_scanner import logstream
        class Socket:
            closed=False
            def close(self):self.closed=True
            def shutdown(self,*a):pass
            def send(self,data):return len(data)
        for mode in ('handshake','tls','proxy'):
            sock=Socket();client=logstream.WSClient('wss://alchemy.example.invalid/key')
            with patch.object(logstream.socket,'create_connection',return_value=sock),patch.object(client,'_proxy_for',return_value=('proxy',8080) if mode=='proxy' else None),patch.object(client,'_read_line',return_value='HTTP/1.1 429 Too Many Requests\r\n'),patch.object(logstream.ssl,'create_default_context') as ctx:
                if mode=='tls':ctx.return_value.wrap_socket.side_effect=RuntimeError('TLS fixture')
                else:ctx.return_value.wrap_socket.return_value=sock
                with pytest.raises(Exception):client.connect()
            assert sock.closed and client._sock is None

    def test_real_exhausted_budget_refuses_discovery_and_exit_ws(self,tmp_path,monkeypatch):
        import pytest
        from daemon.intelligence.acquisition.provider_router import ProviderRouter
        monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
        r=ProviderRouter(home=str(tmp_path),helius_enabled=False)
        monkeypatch.setattr(routine._local,'router',r,raising=False)
        r.usage.record('alchemy','alchemy','fixture','routine_account','card',cost=30_000_000)
        assert r.usage.budget_status('alchemy')['stop_nonessential']
        with pytest.raises(routine.WebsocketAdmissionError,match='budget admission'):
            routine.websocket_admit(routine.websocket_url(purpose="normal_scanning"))
        with pytest.raises(routine.WebsocketAdmissionError,match='budget_exhausted'):
            routine.websocket_admit(routine.websocket_url(purpose="normal_scanning"),critical=True)


import pytest

@pytest.mark.parametrize('caller',['safety','curve'])
@pytest.mark.parametrize('signal',['http429','rpc429'])
def test_actual_routine_caller_shared_throttle_and_recovery(tmp_path,monkeypatch,caller,signal):
    import json,urllib.error
    from daemon.intelligence.acquisition.provider_router import ProviderRouter
    from daemon.intelligence.acquisition.request_scheduler import SharedQuota,BACKOFF_BASE_S
    from daemon.launch_scanner import instant_safety,mimic_curve
    clock=[1000000.]
    monkeypatch.setattr(SharedQuota,'_time',lambda self:clock[0])
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
    monkeypatch.setenv('HELIUS_API_KEY','fixture-not-real')
    monkeypatch.delenv('CHAINSTACK_ENDPOINT',raising=False)
    calls=[];recovered=[False]
    class Response:
        status=200
        def __init__(self,body):self.body=body
        def read(self):return json.dumps(self.body).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    def transport(req,timeout=None):
        provider='alchemy' if 'alchemy.com' in req.full_url else 'public'
        calls.append(provider)
        if provider=='alchemy' and not recovered[0]:
            if signal=='http429':raise urllib.error.HTTPError(req.full_url,429,'fixture throttle',{'Retry-After':'45'},None)
            return Response({'error':{'code':429,'message':'fixture throttle'}})
        return Response({'result':{'value':provider}})
    def install():
        r=ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport)
        monkeypatch.setattr(routine._local,'router',r,raising=False)
        return r
    invoke=(lambda key:instant_safety._rpc('getAccountInfo',[key],2)) if caller=='safety' else (lambda key:mimic_curve.rpc('getAccountInfo',[key]))
    r=install();routine.clear_evidence()
    assert invoke('first')=={'value':'public'}
    assert calls==['alchemy','public']
    state=SharedQuota('alchemy',home=str(tmp_path))._read(clock[0])
    assert state['429s']==1
    assert state['backoff_until']-clock[0]==(45 if signal=='http429' else BACKOFF_BASE_S)
    first=routine.evidence_since()[-1]
    assert first['degraded'] and first['provider']=='public' and first['decision_usable']
    # A fresh router instance must respect persisted backoff and make no Alchemy request.
    clock[0]+=3
    install();assert invoke('while-cooled')=={'value':'public'}
    assert calls==['alchemy','public','public']
    assert any(a.get('provider')=='alchemy' and a.get('kind')=='rate_limited' and 'scheduler' in a.get('note','') for a in routine.evidence_since()[-1]['attempts'])
    clock[0]=state['backoff_until']+1;recovered[0]=True
    assert invoke('after-cooldown')=={'value':'alchemy'}
    assert calls==['alchemy','public','public','alchemy']
    after=SharedQuota('alchemy',home=str(tmp_path))._read(clock[0])
    assert after['backoff_until']==0 and after['backoff_s']==BACKOFF_BASE_S
    last=routine.evidence_since()[-1]
    assert last['provider']=='alchemy' and not last['degraded']


@pytest.mark.parametrize('hint,expected',[('600',600),('invalid',30),('-5',30),('nan',30)])
def test_actual_http_retry_after_minimum_survives_scheduler(tmp_path,monkeypatch,hint,expected):
    import urllib.error
    from daemon.intelligence.acquisition.provider_router import ProviderRouter
    from daemon.intelligence.acquisition.request_scheduler import SharedQuota
    from daemon.launch_scanner import instant_safety
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
    clock=1000000.
    monkeypatch.setattr(SharedQuota,'_time',lambda self:clock)
    def transport(req,timeout=None):
        raise urllib.error.HTTPError(req.full_url,429,'fixture throttle',{'Retry-After':hint},None)
    r=ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    assert instant_safety._rpc('getAccountInfo',['fixture'],2) is None
    state=SharedQuota('alchemy',home=str(tmp_path))._read(clock)
    assert state['backoff_until']-clock==expected


@pytest.mark.parametrize('second_hint',[None,5,45])
def test_ordered_throttle_responses_never_shorten_shared_deadline(tmp_path,second_hint):
    import subprocess,sys,json
    from daemon.intelligence.acquisition.request_scheduler import SharedQuota
    q=SharedQuota('alchemy',home=str(tmp_path),now=lambda:1000.)
    q.report_429(wait_hint_s=600)
    # Independent process receives an older in-flight response ten seconds later.
    program="""import sys,json
from daemon.intelligence.acquisition.request_scheduler import SharedQuota
q=SharedQuota('alchemy',home=sys.argv[1],now=lambda:1010.)
q.report_429(wait_hint_s=json.loads(sys.argv[2]))
print(q._read(1010.)['backoff_until'])
"""
    proc=subprocess.run([sys.executable,'-c',program,str(tmp_path),json.dumps(second_hint)],capture_output=True,text=True,check=True)
    assert float(proc.stdout.strip())>=1600.
    later=SharedQuota('alchemy',home=str(tmp_path),now=lambda:1011.)
    granted,wait,reason=later.acquire('critical')
    assert not granted and wait>=589 and reason=='backoff'


def test_actual_router_late_inflight_success_cannot_cancel_throttle(tmp_path,monkeypatch):
    import threading,urllib.error,json
    from daemon.intelligence.acquisition.provider_router import ProviderRouter
    from daemon.intelligence.acquisition.request_scheduler import SharedQuota
    clock=[1000.];entered=threading.Event();release=threading.Event();out=[]
    monkeypatch.setattr(SharedQuota,'_time',lambda self:clock[0])
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
    monkeypatch.delenv('CHAINSTACK_ENDPOINT',raising=False)
    class Response:
        status=200
        def read(self):return b'{"result":{"value":1}}'
        def __enter__(self):return self
        def __exit__(self,*a):pass
    def transport(req,timeout=None):
        if 'alchemy.com' not in req.full_url:return Response()
        key=json.loads(req.data)['params'][0]
        if key=='inflight':
            entered.set();assert release.wait(5);return Response()
        raise urllib.error.HTTPError(req.full_url,429,'fixture throttle',{'Retry-After':'600'},None)
    earlier=ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport)
    later=ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport)
    def run():out.append(earlier.call('getAccountInfo',['inflight']))
    thread=threading.Thread(target=run);thread.start()
    try:
        assert entered.wait(5)
        later.call('getAccountInfo',['throttled'])
        assert SharedQuota('alchemy',home=str(tmp_path))._read(clock[0])['backoff_until']==1600.
        clock[0]=1010.
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive() and out[0]['provider']=='alchemy' and out[0]['kind']=='ok'
    q=SharedQuota('alchemy',home=str(tmp_path))
    assert q._read(clock[0])['backoff_until']==1600.
    granted,wait,reason=q.acquire('critical')
    assert not granted and wait==590 and reason=='backoff'
    clock[0]=1601.;q.report_success()
    assert q._read(clock[0])['backoff_until']==0.


def test_subscription_throttle_does_not_increment_handshake_429(tmp_path,monkeypatch):
    from daemon.intelligence.acquisition.provider_router import ProviderRouter
    from daemon.intelligence.acquisition.request_scheduler import SharedQuota
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=False)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    routine.websocket_subscription_throttled(routine.websocket_url(purpose="normal_scanning"))
    assert not SharedQuota('alchemy',home=str(tmp_path)).acquire('critical')[0]
    health=r.health.snapshot()['alchemy']
    assert health['wss_handshake_429s']==0 and health['rpc_429s']==0 and health['wss_subscription_429s']==1

    from daemon.intelligence.acquisition.provider_health import HealthMonitor
    fresh=HealthMonitor(home=str(tmp_path));fresh.restore()
    assert fresh.snapshot()['alchemy']['wss_subscription_429s']==1
