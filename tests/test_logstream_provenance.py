import json
from daemon.launch_scanner.logstream import LogStream
from daemon.intelligence.acquisition import routine

def test_actual_caller_persists_fallback_provenance_across_restart(tmp_path,monkeypatch):
    from tests.test_logstream import _parsed_tx
    sig='sig';payload=_parsed_tx('mint')
    evidence={'kind':'ok','provider':'chainstack','result':payload,'attempts':[{'provider':'alchemy','kind':'error'},{'provider':'chainstack','kind':'ok'}],'decision_usable':True,'degraded':True,'completeness':'retention-limited','fallback':True}
    monkeypatch.setattr(routine,'call',lambda *a,**k:evidence)
    stream=LogStream(intake_home=tmp_path)
    stream._handle_message(json.dumps({'method':'logsNotification','params':{'result':{'value':{'err':None,'signature':sig}}}}))
    restored=LogStream(intake_home=tmp_path)
    row=restored.drain()[0];assert row['mint']=='mint'
    assert row['acquisition_evidence']['provider']=='chainstack'
    assert row['acquisition_evidence']['degraded'] is True
    assert len(row['acquisition_evidence']['attempts'])==2
    evidence['attempts'].clear()
    assert len(restored.drain()[0]['acquisition_evidence']['attempts'])==2

def test_unusable_read_keeps_intake_unresolved(tmp_path,monkeypatch):
    from tests.test_logstream import _parsed_tx
    monkeypatch.setattr(routine,'call',lambda *a,**k:{'kind':'ok','provider':'public','result':_parsed_tx('mint'),'decision_usable':False,'degraded':True})
    stream=LogStream(intake_home=tmp_path)
    stream._handle_message(json.dumps({'method':'logsNotification','params':{'result':{'value':{'err':None,'signature':'sig'}}}}))
    assert stream.drain()==[]
    assert 'sig' in stream._intake.pending()

def test_real_router_transport_fallback_reaches_scanner_consider(tmp_path,monkeypatch):
    """No external network, actual routing/decoding/intake/scanner handoff.

    The WS notification is injected at the callback, not transport-validated.
    _consider captures its argument; buy safety/trade decisions are not run.
    """
    import urllib.error
    from daemon.intelligence.acquisition.provider_router import ProviderRouter
    from daemon.launch_scanner import LaunchScanner
    import daemon.launch_scanner as scanner_module
    from tests.test_logstream import _parsed_tx
    from types import SimpleNamespace
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-not-real')
    monkeypatch.setenv('CHAINSTACK_ENDPOINT','https://chainstack.example.invalid/node')
    calls=[]
    class Response:
        status=200
        def read(self):return json.dumps({'jsonrpc':'2.0','id':1,'result':_parsed_tx('mint')}).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    def transport(req,timeout=None):
        calls.append(req.full_url)
        if 'alchemy' in req.full_url:raise urllib.error.HTTPError(req.full_url,429,'Too Many Requests',{},__import__('io').BytesIO(b'{"error":{"code":429,"message":"rate limit"}}'))
        return Response()
    monkeypatch.setattr(routine._local,'router',ProviderRouter(home=str(tmp_path),helius_enabled=False,transport=transport),raising=False)
    stream=LogStream(intake_home=tmp_path)
    stream._handle_message(json.dumps({'method':'logsNotification','params':{'result':{'value':{'err':None,'signature':'sig'}}}}))
    assert len(calls)>=2 and 'alchemy' in calls[0] and 'api.mainnet-beta.solana.com' in calls[-1]
    scanner=LaunchScanner.__new__(LaunchScanner)
    scanner._logstream=LogStream(intake_home=tmp_path)
    scanner.name='provenance-test';seen=[]
    scanner._consider=lambda config,adapter,state,candidate:seen.append(candidate)
    class Store:
        rows=[]
        def journal_add(self,row):self.rows=[row]
        def journal_query(self,**kwargs):return self.rows
        def prune_journal(self,**kwargs):pass
    monkeypatch.setattr(scanner_module,'get_store',lambda:Store())
    scanner._drain_logstream(SimpleNamespace(),None,{})
    assert len(seen)==1 and seen[0]['mint']=='mint'
    evidence=seen[0]['extra']['acquisition_evidence']
    assert evidence['provider']=='public' and evidence['fallback'] is True
    assert evidence['completeness']=='retention-limited' and evidence['decision_usable'] is True
    assert len(evidence['attempts'])>=2
    assert scanner._logstream.drain()==[]
